# ADR-0004 — Faz 1 sinyal yolu: tek süreç, SQLite kuyruk, kimlik doğrulamadan sonra hız sınırı

Durum: kabul edildi · 2026-11-13 · Karar: Oğuz (Faz 1 plan onayı) + tasarım denetimi bulguları · İlgili: SPEC §5.2–§5.6;
değişmez kurallar 3, 5, 6, 7, 9; ADR-0000

## Bağlam

Faz 1 alıcısı TradingView alert'ini alır, doğrular, boyut önerisi ekler ve Telegram'a iletir. Kısıtlar:

- TradingView isteği 3 saniyede yanıt alamazsa iptal eder; yeniden denediği belgelenmemiştir. Yanıt, Telegram'ın o an
  ulaşılabilir olmasına bağlı olamaz.
- TradingView webhook'ları dört sabit adresten gelir, ama bu adresler **bütün TradingView kullanıcılarının ortak**
  adresleridir: kaynak IP "TradingView'dan geldi"yi kanıtlar, "benim alarmımdan geldi"yi kanıtlamaz. mTLS yoktur.
- Tek kullanıcı, günde onlarca istek, küçük bir VPS ya da kullanıcının kendi bilgisayarı. Süreç çökebilir, Telegram
  dakikalarca ulaşılmaz olabilir, bilgisayar yeniden başlayabilir.
- Bir giriş sinyalinin ömrü 60 saniyedir (`signal_validity_seconds`); geç iletilen giriş, iletilmeyenden kötüdür.

Kod: `src/propbot/signals/` (`receiver.py`, `processor.py`, `heartbeat.py`, `service.py`), `src/propbot/store/db.py`,
`src/propbot/notify/telegram.py`.

## Seçenekler

1. **Bellek içi kuyruk** (`asyncio.Queue`): en basit; süreç çökünce 200 dönülmüş sinyaller kaybolur, yeniden başlayınca
   neyin gönderildiği bilinmez.
2. **Ayrı süreçler + mesaj aracısı** (Redis/RabbitMQ): bu hacim için gereksiz bağımlılık ve işletim yükü.
3. **Tek süreç + SQLite (WAL) kuyruk ve giden kutusu:** alıcı satırı yazar ve 200 döner; işleme ve gönderme aynı süreçte,
   veritabanı üzerinden.

## Karar

Seçenek 3.

**Süreç.** Tek süreç, tek asyncio döngüsü: uvicorn (FastAPI alıcısı) + işleyici + alarm hattı izleyicisi + Telegram
(long polling ve giden kutusu göndericisi). Telegram long polling kullanır, gelen port açılmaz. Arka plan görevlerinden
biri kendiliğinden biterse süreç sıfırdan farklı kodla çıkar ve systemd yeniden başlatır: "çalışıyor görünen ama sinyal
iletmeyen bot" en kötü hata biçimidir. Kalıcı her şey veritabanındadır; yeniden başlayan süreç kaldığı yerden sürer.

**Alıcı sırası.** İlk başarısız adım yanıtı belirler:

| # | Adım | Başarısızsa |
|---|---|---|
| 1 | istemci IP'si (aşağıda) | 403 |
| 2 | gövde boyutu ≤ 4 KB (Content-Length, yoksa akarken) | 413 |
| 3 | JSON: tek nesne; yinelenen anahtar, NaN/Infinity, taşan sayı yok | 400 |
| 4 | token (`hmac.compare_digest`) | 401 |
| 5 | hız sınırı: dakikada 30, **yalnızca token'ı doğru isteklerde sayılır** | 429 |
| 6 | şema (`TvAlert`) | 422 |
| 7 | kayıt: `UNIQUE(strategy_id, symbol, bar_time, action)` | yinelenen → 200 `duplicate` |

SPEC §5.2'deki ilk sıralamada hız sınırı token'dan önceydi. Kaynak adresler ortak olduğu için bu, adresimizi öğrenen her
TradingView kullanıcısına sinyal iletimini (çıkış mesajları dahil) sessizce kapatma düğmesi verirdi. Token'dan önceki iş
sınırlıdır: en fazla 4 KB okunur ve ayrıştırılır, iş parçacığına geçilmez, veritabanına yazılmaz. Tek istisna: izinli
adresten gelen ama kimliğini kanıtlamayan istekler için **saatte en fazla bir** alarm satırı (token değişiminden sonra
eski token'la kalan bir alarm başka türlü sessizce düşerdi).

**İstemci IP'si.** Bot yalnızca loopback'i dinler. Bağlantının karşı ucu güvenilir vekilse (varsayılan: `127.0.0.1`,
`::1`) `X-Forwarded-For` zorunludur ve listenin en sağdaki adresi istemcidir; değilse başlık yok sayılır. Başlık adı
ayarlanamaz: desteklenen her ön uç (Caddy, Tailscale Funnel, Cloudflare tüneli) yalnızca bu başlığı kendisi yazar ya da
sonuna ekler; başka bir başlık bir tünelden olduğu gibi geçip taklit edilebilir. uvicorn'un kendi vekil başlığı işlemesi
kapalıdır. `trusted_proxies` yalnızca loopback/özel ve en çok 256 adreslik aralık olabilir.

**Kuyruk ve işleme.** `synchronous=FULL`: 200 döndüyse satır diskte. İşleyici en eski sinyali tek transaction'da işler:
sonuç + bütçe kaydı + giden kutusu satırları birlikte yazılır ya da hiç yazılmaz (tam bir kez işleme). İşlenemeyen bir
sinyal (yazılım hatası) kuyruğu tıkamaz: `error` sonucuyla kenara alınır, alarm gönderilir, sıradaki sinyale geçilir.

**Gönderme.** Giden kutusu FIFO'dur; gönderici ağ çağrısı sırasında transaction tutmaz. Teslimat **en az bir kez**dir.
Geçici hatalarda geri çekilme en çok 30 saniyedir ve yeni satır gelince hemen yeniden denenir (uzun bekleme, Telegram
döndükten sonra taze girişlerin süresini doldururdu). Bir giriş mesajı geçerliliği dolmadan gönderilemezse:

- İstek hiç çıkmadıysa (bağlantı hatası): "[İLETİLEMEDİ — ATLA]" bildirimi, bütçe kaydı serbest.
- Çıkmış olabiliyorsa (yanıt zaman aşımı, ya da süreç gönderirken yeniden başladı): "[İLETİM BELİRSİZ]" bildirimi,
  bütçe sayılmaya devam eder. "Gönderildi olarak işaretlenmedi", "kullanıcıya ulaşmadı" demek değildir.

**Zaman.** Bütün zaman damgaları UTC epoch milisaniyedir; bileşenler saati kendileri okumaz, bir `Clock` alır.
Bar kapanışı = `bar_time` + zaman dilimi süresi. Giriş alert'i hesaplanan bar kapanışından 2 saniyeden fazla **önce**
gelirse iki saat uyuşmuyor demektir: geçerlilik hesaplanamaz, sinyal "ATLA" olarak iletilir (değişmez kural 3).

**Sırlar.** Token gövdeden çıkarılır, karşılaştırılır ve atılır; veritabanına, günlüğe ve Telegram mesajına girmez.
Günlükler tek bir zincirden geçer ve bilinen her sır `***` olur (kütüphane kayıtları dahil). Yakalanmamış istisnalar ham
traceback olarak basılmaz, aynı zincirden geçer: python-telegram-bot'un `InvalidToken` mesajı bot token'ını içerir.

## Sonuçlar

- Tek makine, tek süreç; yatay ölçek yok (gerek de yok). Aynı bot token'ıyla ikinci bir kopya çalışırsa Telegram
  `Conflict` verir ve alarm üretilir.
  *Güncelleme (2026-11-15, uygulama denetimi PORT-1):* aynı veritabanında ikinci bir `propbot serve` artık hiç
  başlamaz (`<db>.lock` üzerinde `flock`), ve dinleme soketi arka plan görevlerinden önce bağlanır. `Conflict` alarmı
  yalnızca başka bir veritabanı/makinede aynı token'la çalışan kopya için kalır.
  *Güncelleme (2026-11-16, kullanıcı kararı 53):* aynı bot token'ıyla ikinci kopya kodla da engellenir. İki dinleyici
  botun güncellemelerini rastgele paylaşır; `/kill` sinyal iletmeyen kopyaya düşebilir. İki katman:
  1. **Makine genelinde bot kilidi.** `serve`, veritabanı kilidinden hemen sonra bot kimliğiyle (token'ın iki noktadan
     önceki, gizli olmayan kısmı) adlandırılmış bir soketi Linux'un soyut ad alanına bağlar
     (`\0propbot-telegram-bot-<id>`). Seçenekler: `/run/lock` altında `flock` (birim dosyasında `ReadWritePaths`
     değişikliği ve servis kullanıcısı / elle çalıştıran kullanıcı arasında dosya izni sorunu ister), `/tmp` altında
     dosya (`PrivateTmp=true` servisin `/tmp`'sini ayırır: elle başlatılan kopya kilidi göremez). Soyut soket dosya
     bırakmaz, süreç nasıl ölürse ölsün çekirdek bırakır, birim dosyasına dokunmaz. Sınırı ağ ad alanıdır (konteyner,
     başka makine görmez) ve Linux'a özgüdür; başka platformda `serve` kilidi alamadığı için başlamaz (dağıtım
     hedefi Ubuntu).
  2. **Başlangıçtaki Conflict.** Kilidin görmediği kopya için: polling'e başlayalı 60 saniyeyi geçmemiş bir süreç
     409 Conflict alırsa yeni gelen odur ve çekilir — polling'i hemen durdurur, "[ALARM] Bu PropBot kopyası (makine
     adı) kendini DURDURDU" mesajını kuyruğa yazıp göndermeyi dener (alarm veritabanında saatte bire kısıtlanır,
     systemd'nin yeniden deneme döngüsünde tekrar etmez) ve `TelegramFatalError` ile süreci bitirir. 60 saniyeden
     uzun çalışan süreç botu tutar ve eski alarmı verir. Seçenek "ikisi de çalışsın, yalnızca alarm" idi (önceki
     davranış): komutların hangi kopyaya gittiği belirsiz kalırdı (değişmez kural 3: belirsizlikte fail-closed).
  Kalan açıklar: (a) Telegram'ın 409'u hangi isteğe verdiği bu ortamdan gözlenemedi; kural, yeni gelenin de 409
  aldığı (bilinen) davranışa dayanır — yalnızca eski süreç 409 alıyorsa önceki davranış (alarm) sürer. (b) Başka
  makinedeki kopya, kendini durdurana kadarki birkaç saniyede bir komut almış olabilir; iki alarm da bunu söyler.
  (c) Canlı servis, başka makinede aynı token'la bir kopya çalışırken yeniden başlarsa duran canlı servis olur
  (yeni gelen odur): alarmı makine adıyla verir, systemd öbür kopya kapanana kadar yeniden dener.
- Süreç kapalıyken gelen alert kaybolur (TradingView yeniden denemez). İzleyici, bir izleme penceresine denk gelen
  kapalı kalma süresini açılışta bildirir; heartbeat sessizliği ayrıca alarm üretir. Yeniden başlatmalar izlenen
  pencerelerin dışında yapılır.
- Teslimat en az bir kez olduğu için, süreç tam gönderme anında ölürse aynı mesaj iki kez gelebilir; mesajda sinyal
  kimliği yazar.
- Veritabanı büyür, budama yoktur (yılda birkaç MB). Şema sürümü değişirse eski dosya açılmaz; göç (migration) yoktur.
  *(2026-11-16: şema sürümü 2 oldu — `signals.outcome` denetimine `skip_symbol` ve `skip_stop_too_tight` eklendi.
  Sürüm 1 dosyası açılmaz; eski dosya yeniden adlandırılır, servis yenisini oluşturur: runbook §11.)*
- Hız sınırı artık yabancı istekleri sınırlamaz; onları 4 KB gövde sınırı ve sabit maliyetli ret yolu sınırlar. Seçenek
  A'da ayrıca ufw ve Caddy yalnızca dört TradingView adresini geçirir.
- `auto`, `approve` ve `paper` modları bu sürümde yoktur; emir yolu yazılmamıştır (ADR-0000).
