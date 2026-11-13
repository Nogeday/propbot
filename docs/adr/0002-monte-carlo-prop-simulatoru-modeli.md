# ADR-0002 — Monte Carlo prop simülatörünün modelleme kararları

Durum: kabul edildi · 2026-11-13 · Karar: Oğuz (plan onayı) · İlgili: SPEC §6.5, §8.1, §9 (G4), ADR-0003

Numara notu: ADR-0000 (yürütme sinyali venue verisinde; Faz 1) ve ADR-0001 (MT5 köprüsü; Faz 3) SPEC'te ayrılmıştır.

## Bağlam

Go/No-Go kapısı G4, "seçilen risk ve venue kurallarıyla P(iki fazı geçme) ≥ %60 ve P(ihlal) ≤ %20" ister. Bu sayıyı
üreten simülatörün girdisi yalnızca **R cinsinden işlem listesidir**: giriş/çıkış zamanı ve sonuç. Liste gün içi
özsermayeyi, lot yuvarlamayı ve spread'i taşımaz; firmanın kuralları ise özsermaye üzerinden denetlenir. Aradaki boşluk
varsayımla kapanır ve her varsayım geçme / ihlal olasılığını oynatır. Bu kayıt o varsayımları tek yerde toplar.

Kod: `src/propbot/backtest/trades.py`, `montecarlo.py`, `report.py`. Kullanım: `docs/monte-carlo.md`.

## Kararlar

| # | Konu | Değerlendirilen seçenekler | Karar ve gerekçe |
|---|---|---|---|
| 1 | Örnekleme birimi | işlem bazında bağımsız · **gün bloğu** · çok günlü blok | Gün bloğu, yerine koyarak (SPEC §6.5). Aynı prop gününün işlemleri birlikte gelir; iki stratejinin aynı günkü ilişkisi korunur. Günler arası bağımlılık (rejim, seri) modellenmez. |
| 2 | Örnekleme havuzu | yalnızca işlemli günler · **işlemsiz hafta içi günler dahil** | İlk–son işlem arasındaki bütün hafta içi günler + işlem olan diğer günler. İşlem sıklığı korunur, geçme süresi "piyasa günü" olarak anlam kazanır. Tatiller boş gün sayılır (yılda ~9 gün; süreyi ≈ %3–4 uzatır, olasılıkları etkilemez). |
| 3 | Prop günü | UTC günü · NY günü · **venue `daily_reset`** | İşlem, girişinin prop gününe yazılır (`core/propday.py`). Reset'i aşan işlem sayılır ve raporda uyarı olur. |
| 4 | İşlem içi yüzen zarar | yalnızca kapanış · **stop'a kadar (−1R)** · ölçülmüş `mae_r` | Varsayılan `stop`: her işlem kapanmadan önce stop'una kadar aleyhe gitmiş sayılır; CSV'de `mae_r` varsa o kullanılır. `closed` seçeneği iyimser üst sınırı verir. Gerekçe: kural özsermayeyle denetlenir; SPEC §3.1'deki `fill_model: pessimistic` ile aynı tutum. |
| 5 | Örtüşen işlemler | sırayla · **birlikte en kötü** | Aynı anda açık işlemlerin en kötü yüzen sonuçları toplanır. Eşit zaman damgasında giriş, çıkıştan önce sayılır (örtüşme varsayılır). |
| 6 | İhlal | tabanın altına inince · **tabana değince** | `alt sınır ≤ taban` ihlaldir. Günlük taban = gün başı bakiye − `daily_loss_limit_pct`; toplam taban = −`max_loss_pct`. İkisi aynı olayda geçilirse neden, önce geçilen (yüksek) tabandır. Limit tutarı başlangıç bakiyesinin yüzdesidir. |
| 7 | `daily_loss_basis` | ayrı ayrı modelle · **tek model** | Stratejiler reset'te pozisyon taşımadığı için bakiye = özsermaye; iki baz değeri aynı sonucu verir. Reset'i aşan işlem varsa uyarı yazılır (madde 3). |
| 8 | Hedef | çıkış anında · **yalnızca düzken** | Bakiye ≥ hedef ve açık pozisyon yoksa hedefe varılmıştır; yol o gün başka işlem almaz. |
| 9 | Minimum işlem günü | normal riskle devam · **risksiz tamamlama** | Hedefe varıldığında eksik günler, sonucu etkilemeyen mikro işlemlerle tamamlanmış sayılır; geçme günü o kadar ileri kayar. Yalnızca işlem açılan günler sayılır. |
| 10 | Faz geçişi | aynı gün · **ertesi gün, taze bakiye** | Sonraki faz ertesi piyasa günü başlangıç bakiyesiyle başlar. Firma kuralı varsayımıdır (**VERIFY**). |
| 11 | Ufuk | sınırsız · **`--max-days` (1000)** | Ufukta sonuçlanmayan yollar ayrı raporlanır; geçme ya da ihlale zorla yazılmaz. FTMO'da süre sınırı görünmüyor (**VERIFY**). |
| 12 | Maks. düşüş | sınav yolu üzerinde · **kural durdurmasız, sabit pencere** | Aynı örneklenmiş günlerde, ihlal/geçme durdurması ve faz sıfırlaması olmadan, `--dd-days` (250) boyunca; kapanış bakiyesi zirvesinden alt sınıra. İhlalde kesilen yol %99 dilimini limite sabitlerdi. ES = en kötü %5 / %1 yolun ortalaması. |
| 13 | Risk tabanı ve boyut | bileşik · **sabit, kesirli** | K/Z = R × risk %, taban başlangıç bakiyesi (SPEC §8.1). Lot adımına yuvarlama yok (kontrat alanları boş); gerçek risk nominalin biraz altında kalır. `risk_pct` 0,5 kod tavanını aşamaz. |
| 14 | Maliyet | **işlem başına sabit R** · stop mesafesine göre | `--cost-r` her işlemden düşülür. Liste maliyet dahilse `--r-is-net`. İkisi de yoksa rapor "maliyetsiz koşu" uyarısı taşır (SPEC §6.3). |
| 15 | Botun kendi kuralları | simülatörde yeniden yaz · **modelleme** | Günlük bütçe, haftalık tavan, ardışık kayıp duraklaması ve düzleştirme eşiği simülatörde **yok**. Risk motoru yazıldığında simülatör aynı kodu çağıracak; kural mantığı iki yerde kopyalanmayacak. |
| 16 | Rastgelelik | tek akış · **ayrık akışlar** | `SeedSequence(seed).spawn(2)`: gün çekimleri ve atlama çekimleri ayrı akışlardır ve yolların durumundan bağımsızdır. Aynı tohum her seçenekte aynı günleri verir; seçenekler aynı günler üzerinde karşılaştırılır. numpy sürümü `uv.lock` ile sabittir (akış sürümler arasında garanti edilmez). |
| 17 | Risk küçültme kademesi | bot kuralı · **yalnız simülatörde, deneysel** | `--dd-risk-steps`: bakiye başlangıcın belirli yüzde altındayken risk çarpanla küçülür. Çarpanlar (0, 1] ve artmayan; taban riskin üstüne çıkılamaz (değişmez kural 4). Kural hâline gelmesi ayrı ADR ve SPEC §8.1 değişikliği ister. |

## Sonuçlar

- **Yön:** varsayılanlar ihlal olasılığını büyütecek tarafta seçildi. E = 0, RR 1:2, %0,5 risk örneğinde faz 1'i geçme
  olasılığı `closed` ile 0,496, `stop` ile 0,483'tür; gerçek değer ikisinin arasındadır.
- **G4:** rapor olasılık verir, PASS/FAIL demez. G4 için derlenmiş ve doğrulanmış Pine'dan gerçek işlem listesi ve
  doğrulanmış kurallar (`rules_verified_at`) gerekir. Sentetik liste kanıt değildir.
- **Simülasyon hatası ≠ girdi belirsizliği:** rapordaki %95 güven aralığı yalnızca yol sayısından gelir. Az işlemli bir
  listenin kendi belirsizliği çok daha büyüktür; rapor bunun için t-istatistiğini ve G2 eşiğini gösterir.
- **Doğrulama:** kural mekaniği elle kurulmuş gün dizilerinde kesin sonuçlarla; E = 0 / RR 1:2 durumu kafes yürüyüşünün
  kapalı formuyla (faz 1: 0,4959; iki faz: 0,3270; 20 000 yolda 4 standart hata içinde); adil para durumu 1/2 ve 1/3 ile
  sınanır. Geliştirme sırasında çekirdeğe 20 kasıtlı hata verildi; hepsi testlerce yakalandı.
- **Sonraki adımlar:** TradingView dışa aktarım dönüştürücüsü (örnek dosya gelince), risk motoruyla ortak ön kontrol,
  lot yuvarlama (kontrat alanları dolunca), gün sonu trailing toplam kayıp (profilde formül tanımlanınca).
