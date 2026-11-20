# ADR-0007 — Faz 2'ye erken geçiş; veri ve paritede "bloklanmama" ilkesi

Durum: kabul edildi · 2026-11-18 · Karar: Oğuz (2026-11-18) · İlgili: SPEC §4.2, §5.7, §6, §9; CLAUDE.md "Çalışma
şekli"; ADR-0000, ADR-0002

## Bağlam

CLAUDE.md fazları sıralar ve "Faz N'e geç" denmeden sonraki faza geçilmez. 2026-11-18 itibarıyla:

- **Faz 0** kodu bitti, ama kabulü (§4.2 madde 5) kullanıcının TradingView'da yapacağı derleme ve önce/sonra
  karşılaştırmasına bağlı; bu ortamdan TradingView'a erişilemiyor.
- **Faz 1** kodu bitti, ama kabulü (§5.7) 10 işlem günlük canlı koşu istiyor.
- **Faz 2**'nin çekirdeği (§6.1–6.4, §6.6) fiyat verisi ve derlenmiş Pine tabanı gelince yapılacaktı. Depoda gerçek
  CME/US500 OHLCV verisi de TradingView "List of Trades" dışa aktarımı da yok.

İki saha testi takvim süresi ister; kod geliştirmesi onları beklerse haftalarca durur.

## Seçenekler

- A. Sırayı koru: Faz 0 derlemesi ve Faz 1 koşusu bitene kadar Faz 2'ye başlama.
- B. **Faz 2'yi şimdi yap; iki saha testini "operasyonel saha testi" olarak sonraya bırak.** Veri ve parite kanıtı
  olmadan yapılabilecek her şeyi yap, yapılamayanı açıkça "kanıt bekliyor" olarak işaretle.
- C. Faz 2'yi yap ve sentetik veriyle üretilen sonuçları kabul kanıtı say. (Reddedildi: sentetik veri hiçbir stratejinin
  kenarını, paritesini ya da veri kalitesini kanıtlamaz.)

## Karar

**B.** Kullanıcı 2026-11-18'de Faz 2'nin kalan tüm dilimlerine ve yerel web paneline (ADR-0009) geçişi açıkça onayladı;
Faz 0 derlemesi ve Faz 1'in 10 günlük koşusu kod geliştirmesini bloklamaz. Faz sırasının geri kalanı değişmedi: Faz 3'e
(emir gönderen her şey) ancak "Faz 3'e geç" kararıyla geçilir ve Faz 4 kapısı (§9) aynen durur.

Geçişin kuralları:

1. **Pine referansı sabit:** `pine/` dosyalarının `144abab` commit'indeki hâli Python portlarının referansıdır. Faz 0
   derlemesi bir Pine dosyasını değiştirirse ilgili Python stratejisi ve testleri aynı değişiklikle güncellenir
   (CHANGELOG'da izlenir).
2. **Sentetik veriyle bloklanmama:** gerçek veri yokken motor, stratejiler ve raporlar deterministik **sentetik** OHLCV
   barlarıyla geliştirilir ve test edilir (`propbot data synth`, sabit tohum; Monte Carlo'daki `synthetic_trades.csv`
   emsali). Veri yükleyici dosya biçimini kendisi tanır: gerçek veri geldiğinde değişen tek şey dosya yoludur.
3. **Sentetik veri kanıt değildir ve kendini ele verir.** Üretilen dosyanın ilk satırı `#propbot-synthetic …`
   işaretidir; yükleyici bunu `DataSource.synthetic` olarak taşır. Bu işareti taşıyan bir seriyle üretilen her rapor
   (backtest, maliyet matrisi, walk-forward, Monte Carlo girdisi, panel kartı) görünür bir **"SENTETİK VERİ"** uyarısı
   taşır ve **Go/No-Go raporu böyle bir girdiyle hiçbir zaman PASS veremez** (G1 FAIL; kodda ve testte zorunlu).
   Değişmez kural 1 (`auto` için son Go/No-Go PASS) bu yüzden sentetik veriyle açılamaz.
4. **Parite altyapısı kurulur, parite kanıtı bekler.** TradingView "List of Trades" dışa aktarımını okuyan ayrıştırıcı
   ve eşleştirici (§6.6) yazılır; ama dışa aktarım biçimi bu ortamdan doğrulanamadı (VERIFY) ve gerçek dışa aktarım
   olmadan ≥ %98 koşulu **kanıtlanmış sayılmaz**. `tests/parity/golden/` altındaki dosyalar bugün yalnızca Python
   motorunun kendi çıktısını sabitler (regresyon); TradingView kanıtı değildir ve dosyalarda böyle yazar.
5. **Kabul dürüst raporlanır.** §6.7'nin "parite ≥ %98" ve "veri raporu temiz (gerçek veride)" maddeleri kullanıcının
   sağlayacağı dosyalara bağlıdır; Faz 2 teslim raporu bunları "kanıt bekliyor" olarak listeler, PASS demez.

## Sonuçlar

- Faz 2 kodu ve paneli, iki saha testi sürerken hazır olur; gerçek veri ve TradingView dışa aktarımı geldiğinde
  yapılacak iş bir komut dizisidir (runbook), yeni kod değil.
- Sentetik barlar gerçek piyasanın mikro yapısını (açılış boşlukları, haber barları, spread) taşımaz; sentetik veride
  görülen "kâr" ya da "zarar" hiçbir şey söylemez. Raporlardaki uyarı bu yüzden kaldırılamaz biçimde koda bağlıdır.
- Pine–Python farkları ancak gerçek parite koşusunda görünür. Portlar SPEC §3 ve Pine kaynağına göre yazıldı ve
  karşıt denetimden geçti, ama TradingView emülatörünün belgelenmemiş davranışları (ör. `ta.pivothigh` eşitlik
  kuralı, `strategy.openprofit` içinde komisyon, erken kapanış günlerinde `session.islastbar`) VERIFY olarak
  `CHANGELOG.md` açık bulgularında durur.
- Faz 0 derlemesi Pine'da değişiklik gerektirirse Python tarafı da değişir; bu, erken geçişin bilinen maliyetidir.

## Uygulama notları (2026-11-18)

Karar metni değişmedi; uygulanan sistem şu noktalarda ayrıntılanır:

- Sentetik üretim `uv run propbot data synth` (varsayılan 5 dk, 250 seans, tohum 1). Yükleyici başlıktan önceki `#`
  satırlarından herhangi biri `#propbot-synthetic` ile başlıyorsa dosyayı sentetik sayar; üstüne not eklenmiş sentetik
  dosya da gerçek sayılmaz.
- "Go/No-Go'da PASS veremez" kuralı yalnızca G1'de değil, sentetik bir artefaktı **kullanan her kapıda** uygulanır:
  o kapı KALDI ("SENTETİK VERİ"). Monte Carlo için ayrıca köken satırındaki `synthetic=true` ya da dosya adında
  `synthetic` / `sentetik` geçmesi yeterlidir. Raporun sentetik işareti, kapıların **kullandığı** artefaktlardan
  gelir; `reports/` altındaki seçilmemiş sentetik dosyalar raporu işaretlemez.
- Faz 2'de Go/No-Go hiçbir girdiyle GO olamaz: G5 (Faz 3 demo koşusu) her zaman DEĞERLENDİRİLEMEDİ'dir.
- `tests/parity/golden/` 24 dosyadır (6 strateji × 2 sentetik fikstür × 2 dolum modeli); her dosyanın başlığı "SENTETİK
  VERİ … not evidence" der. Motor, port, MES maliyetleri ya da fikstür değişirse bilerek yeniden üretilir
  (`PYTHONPATH=tests .venv/bin/python -m support.golden`) ve fark gözden geçirilir.
- `pine/` dosyaları `144abab`'dan beri değişmedi. Portların Pine'dan bilinçli tek davranış sapması MTF ısınmasındaki
  başa baş stop'tur (SPEC §3.1; 2026-11-19 kullanıcı kararıyla "beklenen güvenli sapma" olarak onaylandı);
  boyutun lot adımına yuvarlanması varsayılan olmayan
  "Fractional" modda boyutu küçültebilir.
- §6.7 kabulünün madde madde durumu ve "kanıt bekliyor" maddelerinin gerektirdiği dosyalar `CHANGELOG.md` Faz 2
  bölümündedir.
