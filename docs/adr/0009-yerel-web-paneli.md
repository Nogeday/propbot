# ADR-0009 — Yerel web paneli (`propbot dashboard`): mimari ve güvenlik

Durum: kabul edildi · 2026-11-18 · Karar: Oğuz (2026-11-18: panel Faz 2 kapsamına alındı) · İlgili: SPEC §5.5, §6.8,
§8; değişmez kurallar 1, 3, 6, 7, 10; ADR-0004, ADR-0005, ADR-0007

## Bağlam

Botun bugünkü tek arayüzü Telegram'dır. Kullanıcı kendi bilgisayarında, çalışırken hiç yapay zekâ tokenı harcamayan,
Türkçe bir web paneli istedi: hesap ve sinyal durumu, yerel kontrol (duraklat / devam / KILL / Telegram özeti) ve
backtest–Monte Carlo sonuçlarının tablo ve grafik kartları. Panel yeni bir saldırı yüzeyidir: KILL'i kaldırabilen bir
düğme, yanlış kurulursa ağdaki herkese ya da kullanıcının tarayıcısında açık başka bir siteye açılır.

## Seçenekler

- **Süreç:** (A) `propbot serve`'in içine gömülü rotalar; (B) **ayrı süreç**, aynı SQLite dosyası.
  A'da panel hatası sinyal hattını düşürebilir ve webhook portu (Caddy arkasında internete açık) ile aynı uygulamayı
  paylaşır. B'de servis kapalıyken de rapor ekranı çalışır; SQLite WAL iki süreci taşır (ADR-0004).
- **Arayüz teknolojisi:** (A) React/Vue + derleme zinciri; (B) Jinja2 şablonları; (C) **sunucuda üretilen HTML +
  küçük, bağımlılıksız JavaScript, satır içi SVG grafikler.** A ve B yeni bağımlılık ve (A için) Node gerektirir.
- **Kontrol aksiyonları:** (A) panel durum bayraklarını kendisi yazar; (B) **Telegram komutlarının kodunu
  (`notify.commands.Commands`) çağırır.** B tek bir kural kümesi demektir: PIN kilidi, BEKLEMEDE kısıtları, metinler.
- **Telegram bildirimi:** (A) panel Bot API'yi kendisi çağırır (bot token'ı ikinci bir süreçte, ikinci bir
  `getUpdates` olmasa da ikinci bir gönderici); (B) **giden kutusuna (outbox) yazar**, çalışan `serve` gönderir.
- **Erişim:** (A) parola/oturum; (B) **yalnızca loopback**, kimlik doğrulama yok, tarayıcı kaynaklı saldırılara karşı
  Host/Origin + CSRF denetimi.

## Karar

1. **Ayrı süreç, yalnızca loopback.** `uv run propbot dashboard [--port 8787]` kendi FastAPI uygulamasını
   `127.0.0.1`'de açar. Loopback olmayan bir adres **reddedilir** (seçenek yok; `serve` ile aynı `is_loopback_host`
   denetimi). Başka bir bilgisayardan bakmak için SSH tüneli kullanılır (`ssh -L 8787:127.0.0.1:8787 …`); panel
   Caddy'ye ya da ufw'ye eklenmez.
2. **Tarayıcı kaynaklı saldırılara karşı** (kimlik doğrulama olmadığı için zorunlu):
   - Her istekte `Host` başlığı `127.0.0.1:<port>`, `localhost:<port>` ya da `[::1]:<port>` olmalı (DNS rebinding).
   - Durum değiştiren her istek `POST`'tur; `Origin` (varsa) aynı kaynak olmalı ve süreç başına rastgele üretilen
     CSRF belirteci form alanında gelmeli (sabit zamanlı karşılaştırma). `GET` hiçbir şeyi değiştirmez.
   - Yanıtlar `Content-Security-Policy: default-src 'none'` tabanlı sıkı bir politika (satır içi betik yok; stil ve
     betik panelin kendi `/assets/` yolundan), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
     `Referrer-Policy: same-origin`, `Cache-Control: no-store` taşır (`no-referrer` değil: o politikada tarayıcı
     panelin kendi formlarını `Origin: null` ile gönderir ve Origin denetimi her aksiyonu reddeder; gerçek
     tarayıcı testinde görüldü). Dış kaynak (CDN, font, görsel) yoktur; panel
     internetsiz çalışır.
   - Sayfaya yazılan her değişken HTML olarak kaçışlanır (sinyal alanları dışarıdan gelir).
3. **Sıfır yapay zekâ tokenı:** panel yalnızca yerel dosyaları ve veritabanını okur; hiçbir model/API çağrısı yoktur.
4. **Veri kaynakları, salt-okunur:** `data/propbot.db` (yoksa **oluşturulmaz**; panel "veritabanı yok" der ve analiz
   ekranı yine çalışır), `config/venues/*.yaml`, `config/risk.yaml`, ve `reports/` altındaki JSON rapor artefaktları
   (Monte Carlo, backtest, maliyet matrisi, walk-forward, Go/No-Go; en yenisi gösterilir).
5. **Dürüst hesap ekranı.** Bot Faz 3'e kadar emir göndermez ve gerçek bakiyeyi, dolumları bilmez. Bu yüzden:
   - Bakiye varsayılan olarak venue profilindeki `initial_balance`'tır ve **"varsayılan"** etiketiyle gösterilir.
     Kullanıcı panelden güncel bakiye / özsermaye ve (isteğe bağlı) gün başı bakiyesini **elle** girebilir; değer
     durum tablosunda saklanır, **"elle girildi · tarih"** etiketiyle gösterilir ve prop günü değişince "güncel değil"
     olarak işaretlenir. Hedef ilerlemesi (Faz 1 / Faz 2) ve toplam kayıp tamponu bu bakiyeden, venue profilindeki
     oranlarla hesaplanır (kural değerleri koda gömülmez; kural 10).
   - Günlük tampon iki ayrı satırdır: (a) botun **risk defteri** — bugün iletilen girişlerin toplam riski, botun günlük
     bütçesine ve firmanın günlük limitine oranla (ADR-0005; en kötü durum); (b) kullanıcı gün başı bakiyesini ve
     güncel özsermayeyi girdiyse **beyan edilen** günlük kayıp. Ölçülmemiş bir değer ölçülmüş gibi gösterilmez.
   - "Açık / kapalı işlemler" **sinyal** kayıtlarından türetilir (iletilen giriş + çıkış/iptal takibi +
     `/aldim`–`/atladim` işaretleri) ve ekranda böyle yazar.
   - Bot durumu: `KILL` > `BEKLEMEDE` (kopya, ADR-0004) > `DURAKLATILDI` > `ETKİN`; ayrıca servisin canlılığı
     (`last_alive_ms`) ayrı bir rozetle: bayraklar ETKİN dese de servis çalışmıyorsa ekran bunu söyler.
6. **Kontrol aksiyonları = Telegram komutları.** Duraklat, Devam, KILL ve KILL sonrası Devam, `Commands.handle`
   üzerinden yürür; PIN ve PIN kilidi (5 yanlış / 15 dk) Telegram ile **ortaktır**. Duraklat/Devam yerel onay kutusu
   ister; KILL ve KILL'in kaldırılması PIN ister. PIN yalnızca `POST` gövdesinde, loopback üzerinde taşınır; loglanmaz,
   sayfaya geri yazılmaz. Sırlar `.env`'den `load_settings` ile okunur (kural 6); `.env` okunamıyorsa ya da veritabanı
   yoksa panel **salt-okunur** açılır ve düğmeler devre dışıdır. Panel bot token'ını kullanmaz.
7. **Telegram bildirimi giden kutusuyla.** Her aksiyon ve "Telegram Durum/Test Özeti Gönder" düğmesi giden kutusuna
   bir bildirim yazar; çalışan `propbot serve` onu whitelist'teki tek sohbete gönderir (kural 7). Servis çalışmıyorsa
   panel bunu söyler: bildirim kuyrukta bekler, aksiyonun kendisi (bayrak) yine de yazılmıştır.
8. **KILL'in anlamı ekranda yazar:** Faz 1–2'de bot emir göndermez; KILL yeni giriş sinyallerini keser, açık bir
   pozisyonu **kapatmaz** (pozisyon kullanıcının terminalindedir). Düğmenin altında bu cümle durur.
9. **Yerleşim:** `src/propbot/dashboard/` — `app` (uygulama ve rotalar), `security`, `live` (hesap/sinyal görünüm
   modeli), `actions`, `analysis` (rapor artefaktları), `charts` (SVG), `pages` (HTML). Grafikler sunucuda üretilen
   SVG'dir; JavaScript yalnızca canlı ekranı birkaç saniyede bir yeniler.

## Sonuçlar

- Panel, `serve`'den bağımsızdır: çökmesi sinyal hattını etkilemez, servis kapalıyken de raporlar görülür.
- Kimlik doğrulama yoktur: aynı bilgisayarda oturum açabilen herkes paneli görebilir ve Duraklat/Devam'ı kullanabilir
  (KILL ve kaldırılması PIN ister). Çok kullanıcılı bir makinede panel çalıştırılmamalıdır; runbook bunu yazar.
- Elle girilen bakiye kullanıcının beyanıdır; yanlış girilirse tampon çubukları yanlış olur. Gerçek değerler Faz 3'te
  MT5 köprüsünden gelecek ve elle giriş o zaman kalkacak.
- Panel aksiyonlarının Telegram'a düşmesi çalışan bir `serve`'e bağlıdır.
- Panel durum tablosuna ve giden kutusuna yazdığı için veritabanı şemasının sürümü (ADR-0004) panel için de
  bağlayıcıdır: sürüm uyuşmazlığında panel de açmayı reddeder ve runbook §11'e yönlendirir.

## Uygulama notları (2026-11-18)

Karar metni değişmedi; uygulanan panel (`src/propbot/dashboard/`) şu noktalarda ayrıntılanır ya da ayrılır.
İşletim: `docs/runbook.md` §13.

- **Okuma kilitsizdir:** canlı ekran veritabanını `Store.read()` ile okur (`BEGIN DEFERRED` + `PRAGMA query_only`, tek
  anlık görüntü; yazma kilidi almaz, `serve`'i bekletmez). Dosya `Store(create=False)` ile açılır: yoksa ya da boşsa
  oluşturulmaz, şema yazılmaz, WAL'a çevrilmez. Panel dosyanın aygıt/inode numarasını izler; dosya yeniden adlandırılır
  ya da silinirse (runbook §11'deki şema geçişi) yeni dosyayı açar ya da "veritabanı yok" der.
- **Salt-okunur koşullar** karar 6'dakilere ek olarak `config/tv_alerts.yaml` okunamadığında da geçerlidir
  (`Commands` onu ister). Boş (0 bayt) dosya "boş" olarak gösterilir ve dokunulmaz.
- **Aksiyon ile bildirimi iki ayrı işlemdir** (Duraklat, Devam, KILL, KILL'i kaldır, Özet): `Commands.handle` kendi
  işlemini açar ve `Store` iç içe işleme izin vermez. Bildirim yazılamazsa durum değişmiş kalır ve sayfa
  "YAZILAMADI" der (güvenli yön; açık bulgu 73). Bakiye ve bildirimi tek işlemdir.
- **Sonuç geri okunarak doğrulanır:** 200 yalnızca KILL/DURAKLAT bayrakları istenen değerdeyse; 409 yapılmadı (yanlış
  PIN, kilit, KILL sürerken Devam), 503 veritabanı meşgul, 500 sonucu okunamadı. "KILL'i kaldır" KILL yokken PIN
  denetiminden önce reddedilir (bilinçli bir duraklatmayı sessizce kaldırmasın). BEKLEMEDE kopyanın veritabanında özet
  ve bakiye de reddedilir.
- **Özet** `/durum` + `/saglik` metnidir; yanıtlayan sürece ait satırlar (çalışma süresi, reddedilen webhook, yetkisiz
  sohbet, saat farkı) çıkarılır, çünkü panelden bakınca panelin kendi sayaçlarını gösterirlerdi.
- **Sınırlar:** son 7 günün en çok 1000 sinyali okunur, 25'i ve türetilmiş en çok 50 işlem gösterilir; kesilirse not
  düşülür. Bir istek veritabanı kilitliyse en çok 5 sn bekler (iş parçacığında, olay döngüsünde değil).
- **Güvenlik ayrıntıları:** `Host` eksik ya da yinelenmişse 400, başka bir sunucuyu adlandırıyorsa 421. `Origin` yoksa
  `Referer` denetlenir; ikisi de yoksa yalnızca form belirteci korur. Form gövdesi en çok 2048 bayt, yalnızca
  urlencoded, yinelenen alan reddedilir. WebSocket kapsamları kapatılır; OpenAPI/dokümantasyon rotaları kapalıdır.
  uvicorn'un uygulamaya ulaşmadan verdiği kendi 400 yanıtı (yinelenen `Host`) panelin güvenlik başlıklarını taşımaz
  (gövde düz metin, istek verisi içermez; açık bulgu 76).
- **Analiz ekranı** G3/G4 eşik çizgilerini `config/backtest.yaml`'dan değil SPEC §9 kod tabanlarından çizer (ekran
  bunu yazar); hüküm yalnızca Go/No-Go artefaktından gelir. Ayrıca salt-okunur `GET /api/live` JSON'u vardır.
- Gerçek tarayıcıyla bir koşuda `Referrer-Policy: no-referrer`'ın panelin kendi formlarını kırdığı görüldü; karar 2'deki
  `same-origin` bu yüzdendir.
- **Kullanıcı onayı (2026-11-19):** karar 1 onaylandı: panelin yalnızca `127.0.0.1` üzerinde kalması uygun bulundu,
  uzaktan bakış için SSH tüneli yeterlidir. Davranış değişmedi: `--host` yalnızca loopback adresini (`127.0.0.1` ya da
  `::1`) kabul eder, varsayılan `127.0.0.1`, başka adreste dinleme seçeneği yoktur. Karar 2'deki
  `Referrer-Policy: same-origin` kullanıcının altı kararı arasında değildir; ayrı bir karar verilmedi, teslimat bütün
  olarak onaylandığı için aynen durur.
