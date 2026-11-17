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
  2. **Varışta Conflict → bekleme.** Kilidin görmediği kopya için Telegram'ın 409 yanıtı kullanılır. Kopya, bir
     `getUpdates` isteği ilk kez (ya da 60 saniyeden uzun sessizlikten sonra yeniden) yanıtlandığında "varmış" sayılır;
     varıştan sonraki 60 saniyede iki Conflict alan kopya yeni gelendir. Polling'i durdurur, depoya `standby` işareti
     yazar (işleyici girişleri `skip_standby` olarak bildirir, gönderici bekleyen giriş mesajlarını düşürür),
     "[ALARM] … BEKLEMEDE" mesajını gönderir ve **çalışmaya devam eder**: stop güncelleme / çıkış / iptal
     iletilir. Yeniden başlatılan kopya yine beklemeye düşerse bunu söyler ("… yeniden başlatıldı, ama … yine
     BEKLEMEDE"): kullanıcı ETKİN bildirimini beklemektedir. Bu ikinci alarm depoda 5 dakikada bire kısıtlanır;
     kısıtlama olmasa çöküp yeniden başlayan bir süreç onu birkaç saniyede bir yinelerdi (ilk biçimde hiç
     gönderilmiyordu: üçüncü denetim turunun bulgusu). İşaret sürecin ömründen uzun yaşar; yeniden başlayan kopya yeniden dinler ve 60 saniye boyunca
     Conflict görmeden yanıt alırsa işareti kaldırıp "[BİLGİ] … yeniden ETKİN" der (işaretin iki değeri vardır:
     "beklemede" ve "yeniden dinliyor, henüz bir dakika yalnız kalmadı"; giriş bildirimi hangisinde olduğunu söyler).
     60 saniyeden uzun süredir yanıt alan kopya botu tutar ve eski alarmı verir.
     - *Yol vermeyen dinleyici:* öbür süreç bu sürümden değilse (başka bir program, eski bir kopya, webhook) yol
       vermez; iki dinleyici de yalnızca Conflict alır, python-telegram-bot'un bekleme aralığı 30 saniyeye çıkar ve
       komutlar hangisi o anda soruyorsa ona gider. Zaten çalışan kopya **iki dakika boyunca Conflict'ten başka
       yanıt alamazsa** o da beklemeye geçer. Seçenek "botu tutmaya devam et" idi: komutların kendisine ulaşacağına
       güvenemeyen bir kopya giriş iletmeye devam ederdi (değişmez kural 3). İkinci turun bulgusu bu durumu
       rastlantıya bağlı buldu (30 sn'lik aralıkta tek bir başarısız istek 60 sn'lik sessizlik sayılıp kopyayı
       "yeni gelen" yapıyordu); kural, aynı sonucu belirli bir süreyle verir.
     - *Neden çıkmıyor (ilk uygulama çıkıyordu; denetim bulgusu, 2026-11-16):* `Restart=always` altında çıkan kopya
       5 saniyede bir geri gelir; her seferinde kutusundaki giriş mesajlarını gönderir, öbür kopyanın uzun
       sorgusunu keser, bir komut alabilir ve arada alıcı kapalı kaldığı için çıkış alert'leri kaybolur. Canlı servis
       o sırada yeniden başlarsa iki kopya birbirini sürekli "yeni gelen" görür ve hiçbiri botu tutamaz.
     - *Neden kendiliğinden yeniden denemiyor:* beklemedeki kopyanın her denemesi, o sırada yeni başlamış canlı
       servisi bekleme durumuna düşürebilir; iki kopya da deniyorsa çözüm rastlantıya kalır. Durumu düzeltmek zaten
       bir insan eylemi (kopyalardan birini kapatmak) gerektirir; yeniden başlatma o eylemin parçasıdır.
     - *Neden iki Conflict:* tek bir 409, elle gönderilmiş bir `getUpdates`'ten ya da önceki sürecin Telegram'da
       kalmış isteğinden gelebilir; gerçek bir dinleyici 1–2 saniye içinde yinelenir (python-telegram-bot hata
       sonrası 1 sn, sonra 1,5 sn bekler). Tek Conflict'le beklemeye geçmek canlı servisi yeniden başlatılana kadar
       susturabilirdi.
     - *Neden "süreç yaşı" değil "varış":* süreç yaşıyla karar verildiğinde, bir dakikadan uzun süredir çalışan iki
       kopya (ağ kesintisinden ya da uykudan dönen makine) birbirine hiç yol vermez. Varış, Telegram'ın yanıtlarından
       ölçülür ve makinenin açılış saatiyle (`CLOCK_BOOTTIME`: uyku dahil, saat düzeltmelerinden etkilenmez) tutulur.
     - *Komutlar:* varıştan sonraki ilk 3 saniyede (bir Conflict görüldüyse ondan sonraki 10 saniyede) gelen komut
       bekletilir. Kopya beklemeye geçerse `/kill` ve `/duraklat` yine uygulanır (yalnızca risk azaltır; yanıt, komutun
       öbür sürece ulaşmadığını söyler), öbür komutlar "yeniden gönder" yanıtıyla reddedilir.
  Kalan açıklar: (a) Telegram'ın 409'u hangi isteğe verdiği bu ortamdan gözlenemedi; kural, iki dinleyicinin de
  sırayla 409 aldığı (bilinen) davranışa dayanır — yalnızca eski süreç 409 alıyorsa yeni kopya beklemeye geçmez,
  eski kopya iki dakika sonra "yol vermeyen dinleyici" kuralıyla bekler ve bot yeni kopyada kalır. (b) Öbür süreç 3 saniyeden geç ortaya çıkarsa (hata sonrası uzun
  bekleme aralığındaysa) yeni gelen kopya o arada aldığı bir komutu uygulamış olur. (c) Canlı servis, başka makinede
  aynı token'la dinleyen bir kopya varken yeniden başlarsa beklemeye geçen canlı servis olur; girişleri keser, alarmı
  makine adıyla verir ve yeniden başlatılana kadar öyle kalır. (d) İki kopya aynı dakikada varırsa ikisi de beklemeye
  geçer (kimse komut almaz, girişler kesilir, iki alarm gelir). (e) Beklemedeki kopya, öbür kopya kaybolsa da
  kendiliğinden etkinleşmez.
- Süreç kapalıyken gelen alert kaybolur (TradingView yeniden denemez). İzleyici, bir izleme penceresine denk gelen
  kapalı kalma süresini açılışta bildirir; heartbeat sessizliği ayrıca alarm üretir. Yeniden başlatmalar izlenen
  pencerelerin dışında yapılır.
- Teslimat en az bir kez olduğu için, süreç tam gönderme anında ölürse aynı mesaj iki kez gelebilir; mesajda sinyal
  kimliği yazar.
- Veritabanı büyür, budama yoktur (yılda birkaç MB). Şema sürümü değişirse eski dosya açılmaz; göç (migration) yoktur.
  *(2026-11-16: şema sürümü 3 oldu — `signals.outcome` denetimine `skip_symbol`, `skip_stop_too_tight` (sürüm 2) ve
  `skip_standby` (sürüm 3) eklendi. Eski sürümle oluşturulmuş dosya açılmaz; yeniden adlandırılır, servis yenisini
  oluşturur: runbook §11.)*
- Hız sınırı artık yabancı istekleri sınırlamaz; onları 4 KB gövde sınırı ve sabit maliyetli ret yolu sınırlar. Seçenek
  A'da ayrıca ufw ve Caddy yalnızca dört TradingView adresini geçirir.
- `auto`, `approve` ve `paper` modları bu sürümde yoktur; emir yolu yazılmamıştır (ADR-0000).
