# ADR-0000 — Yürütme sinyali, işlem yapılan mekânın kendi verisinde hesaplanır

Durum: kabul edildi · 2026-11-13 · Karar: Oğuz (`CLAUDE.md` "Mekan kararları"; Faz 1 plan onayı) · İlgili: SPEC §2.1, §5,
§6, §7; değişmez kurallar 2, 3, 5; denetim D10; açık bulgular 18, 22

## Bağlam

Stratejiler TradingView'da `CME_MINI:MES1!` (vadeli) grafiğinde çalışıyor; işlem ise FTMO'da MT5 üzerinde `US500.cash`
(CFD) ile yapılacak. İki fiyat serisi aynı değil: vadeli ile nakit endeks arasında baz farkı var, spread ve seans saatleri
farklı, MES'te gerçek hacim, CFD'de tick hacmi var (denetim D10). Bir stratejinin MES grafiğinde bulduğu seviye (FVG sınırı,
stop, limit fiyatı) FTMO grafiğinde aynı fiyata denk gelmez.

TradingView'dan dışarı çıkan tek kanal webhook'tur: alarm `alert()` çağrısıyla tetiklenir, TradingView isteği 3 saniyede
yanıt alamazsa iptal eder, alarmların süresi dolar, bir alarm oluşturulduğu andaki script ve girdilerle çalışır. Zincirdeki
her halka (TradingView sunucusu, internet, TLS vekili, alıcı) sinyali geciktirebilir ya da düşürebilir.

Faz 1 bu kanalı kullanır, ama emir göndermez: sinyal Telegram'a gelir, uygulayıp uygulamamaya insan karar verir.
Soru şu: Faz 3'te emirleri neyin sinyali tetikleyecek?

## Seçenekler

1. **Webhook yürütme yolunda:** TradingView alert'i geldiğinde bot MT5'e emir gönderir. Python tarafı en ince hâlinde kalır.
   Ama stop ve TP seviyeleri başka bir enstrümanın fiyatıyla gelir; kaçan ya da geç gelen alert korumasız pozisyon ya da
   kaçmış çıkış demektir; idempotency ve tazelik bizim denetleyemediğimiz bir zincire bağlanır.
2. **Sinyal venue verisinde:** strateji mantığı Python'a taşınır (Faz 2) ve MT5'ten gelen barlar üzerinde çalışır
   (`MarketDataFeed` → `Strategy.on_bar` → risk motoru → `ExecutionAdapter`). TradingView yalnızca Faz 1 sinyali ve
   araştırma/backtest aracı olarak kalır.
3. **Hibrit:** emir TradingView alert'iyle tetiklenir, seviyeler venue verisinde yeniden hesaplanır. İki sistemin
   kusurlarını birleştirir: hem webhook zincirine bağımlılık hem de iki ayrı sinyal mantığı.

## Karar

Seçenek 2. Webhook **yürütme yolunda kullanılmaz**.

- Emir üreten sinyal, emrin gideceği enstrümanın kendi barlarından hesaplanır; stop ve TP girişle aynı istekte, o
  enstrümanın fiyatıyla native emir olarak gider (değişmez kural 2).
- Veri gecikmesi, bar boşluğu ya da saat sapması botun kendi beslemesinde görülür ve fail-closed davranışı tetikler
  (değişmez kural 3); bunu üçüncü tarafın teslimat zincirine bırakmayız.
- Faz 1 alıcısında emir yolu **yoktur**: `ExecutionAdapter` çağıran kod yazılmaz, mod `signal` dışına çıkamaz
  (`propbot.risk.modes`, ADR-0004).
- Faz 1 mesajları seviyeleri fiyat olarak değil **puan mesafesi** olarak verir ("giriş fiyatına göre stop −12.50 puan,
  TP +25.00 puan"). Limit girişlerde (Silver Bullet) seviyenin TradingView fiyatı olduğu ve baz farkı kadar kayabileceği
  mesajda yazar.

## Sonuçlar

- Strateji mantığı iki yerde yaşar (Pine ve Python). Bunun bedeli parite testidir (SPEC §6.6): aynı veri üzerinde Pine ile
  Python aynı işlemleri üretmelidir. Pine dosyaları Faz 0'dan sonra referanstır.
- TradingView sinyali ile botun kendi sinyali ileride yan yana gelebilir. Aralarındaki fark bir gözlem metriğidir, emir
  kararı değildir; Faz 1 alıcısı Faz 3'te de bilgi kanalı olarak kalabilir.
- Venue'ya göre değişen girdiler (MTF hacim filtresi, seans saatleri, spread) parite ve Go/No-Go değerlendirmesinde
  ayrıca izlenir (açık bulgu 22).
- Faz 1'de insan, MES grafiğinde üretilen sinyali CFD'de uygular: giriş fiyatı farklıdır, bu yüzden mesajdaki geçerlilik
  süresi ve fiyat sapması sınırı (`signal_validity_seconds`, `signal_max_drift_frac_of_stop`) aşıldıysa sinyal atlanır.
  Yarım günlerde `exit` alert'ine güvenilmez (açık bulgu 18); kapanıştan önce elle düzleştirme gerekir.
