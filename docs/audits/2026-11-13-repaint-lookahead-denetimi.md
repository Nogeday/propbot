# Repaint / lookahead / bar-içi dolum denetimi — 2026-11-13

**Kapsam:** `pine/` altındaki altı strateji ve `config/*.yaml`. Depoda `.pine` göstergesi yok; yalnızca stratejiler var.
**İncelenen commit:** `6f6cf14` — aşağıdaki satır numaraları bu commit'e göredir.
**Yöntem:** statik inceleme. Claude Code Pine'ı derleyemez ve çalıştıramaz; dinamik doğrulama TradingView'da
`docs/tv-verification-checklist.md` ile yapılır. HTF kalıbı için TradingView'ın resmi Pine Script dokümanına
("Other timeframes and data") bakıldı.

## 1. Sonuç

1. **Sinyal üretiminde repaint ya da lookahead kaynağı bulunmadı.** Bütün kararlar kapanmış barda veriliyor, piyasa
   emirleri bir sonraki barın açılışında doluyor, tek üst-zaman-dilimi çağrısı doğru kalıpla yazılmış (bölüm 2).
2. **Denetim isteğindeki `expression[1]` + `lookahead_off` kalıbı uygulanmadı** — uygulanırsa canlı ile backtest ayrışır (D1).
3. Kalan riskler sinyalde değil, **backtest dolum varsayımlarında** (D2, D4, D6, D7) ve **canlı yürütme farklarında**
   (D5, D9, D10). İkisi için kod değişikliği önerildi (E11, E12); diğerleri bot tarafının işi ve açık bulgu olarak kayıtlı.
4. YAML dosyalarında değer çelişkisi yok; ama onları doğrulayan kod da yok (bölüm 4). Denetim isteğindeki bazı risk
   kuralları SPEC'le çelişiyor (bölüm 5).
5. Python optimizasyon / stres testi paketi bu denetimde **yazılmadı** (bölüm 6).

## 2. Temiz çıkan kontroller

| # | Kontrol | Sonuç | Kanıt |
|---|---|---|---|
| T1 | `request.*` çağrıları | Yalnızca MTF'de, doğru kalıpla | `mtf_trend_continuation_strategy.pine:163–165`: `[ta.ema(...)[1], ta.ema(...)[1]]` + `lookahead_on`. Diğer beş dosyada `request.` yok. |
| T2 | `calc_on_every_tick` / `process_orders_on_close` = `true` | Yok | SB 68–69, MR 71–72, MTF 62 açıkça `false`; NY, Asian, Master'da bildirilmemiş (varsayılan `false`). |
| T3 | `varip`, `barstate.isrealtime` / `ishistory` / `isnew` | Yok | `barstate.islast` yalnızca tablo ve etiket çiziminde: NY 553, 570, 582 · Asian 491, 499, 511 · Master 792, 808, 820 · SB 536, 545 · MR 405, 417 · MTF 569, 587. |
| T4 | Negatif offset, `timenow`, `time_close`, `last_bar_index` | Yok | — |
| T5 | Giriş koşulları | Kapanmış barın OHLC'si; emir sonraki barın açılışında | NY 408, 444 · Asian 383, 406 · Master 567, 599, 712, 733 · MR 288–289 · MTF 238–239. SB girişleri bekleyen limit emridir (408, 411). |
| T6 | Pivotlar (SB) | Onaydan sonra kullanılıyor | 285–286: `ta.pivothigh/low(pivotLen, pivotLen)`; değer `pivotLen` bar gecikmeyle gelir, geçmişe taşınmıyor. |
| T7 | Alert'ler | Yalnızca bar kapanışında | `alert.freq_once_per_bar_close`: NY 278 · Asian 277 · Master 379 · SB 282 · MR 186 · MTF 252. |
| T8 | Stop / trailing | Bar kapanışında hesaplanır, sonraki bara uygulanır | NY 482–506 · Master 628–643 · MTF 444–518. Bar içi trailing yok. |

## 3. Bulgular

Önem: **Yüksek** = canlıda sistematik fark · **Orta** = backtest'i iyimser gösterir ya da canlıda özel kural ister ·
**Düşük** = kırılganlık / küçük sapma · **Bilgi** = işlemleri etkilemez.

### D1 — İstekteki HTF kalıbı canlı/backtest ayrışması yaratır · Yüksek · uygulanmadı
- **Nerede:** `mtf_trend_continuation_strategy.pine:163–165` (depodaki tek `request.security`).
- **Bugün:** `[ema[1], ema[1]]` + `lookahead = barmerge.lookahead_on`. Geçmiş ve gerçek zamanlı barlar aynı değeri görür:
  son **kapanmış** 1H barın EMA'sı. Gelecek sızmaz, çünkü `[1]` isteği bir önceki HTF bara kaydırır.
- **İstenen:** `[1]` + `lookahead_off`. Bu durumda geçmiş barlarda yeni HTF değeri ancak 1H bar **kapanırken** gelir: bir
  saatin ilk 11 beş-dakikalık barında *iki önceki* 1H barın değeri görülür. Gerçek zamanlı barlarda ise aynı saat boyunca
  *bir önceki* 1H barın değeri görülür.
- **Canlıda neden zarar:** gelecek sızmaz ama backtest, canlıdan bir 1H bar daha gecikmeli çalışır. Trend filtresi
  (`htfBull` / `htfBear`, 167–168) canlıda backtest'tekinden bir saate kadar erken döner; canlı girişler backtest'in hiç
  görmediği barlarda oluşur. Ölçtüğün strateji ile çalıştırdığın strateji aynı olmaz.
- **Dayanak:** TradingView dokümanı, offset ile `lookahead_on` birleşimini repaint'ten kaçınmanın önerilen yolu olarak
  verir; tehlikeli olan offset'siz `lookahead_on`'dur ve depoda yoktur. SPEC §4.1 B8 de aynı kuralı koyuyor.
- **Karar:** değişiklik yok.

### D2 — TP limitlerinde "dokunma = dolum" · Orta · öneri E12
- **Nerede:** `strategy.exit(..., limit = ...)` — NY 418, 452, 486, 506 · Asian 393, 414, 438, 448 · Master 632, 643, 757,
  759 · MR 311, 316, 345, 357 · MTF 378, 404, 466, 518. Bu beş dosyada `backtest_fill_limits_assumption` yok
  (Silver Bullet'ta var: 67).
- **Ne:** emülatör, fiyat TP seviyesine değdiği anda limit emrini dolmuş sayar.
- **Canlıda neden zarar:** bekleyen limit emir sıranın sonundadır; fiyat seviyeye değip dönerse dolmaz. Backtest o işlemi
  tam hedef kazancı olarak yazar; canlıda aynı işlem trailing stop'la, başa başta ya da zararla kapanır. En hassas dosya
  hedefi yakın olan MR'dir (hedef = BB ortası).
- **Öneri:** beş dosyaya `backtest_fill_limits_assumption = 1` (fiyat seviyeyi 1 tick geçince dolum). Sonuçlar bir miktar
  kötüleşir; bu beklenen ve istenen yöndedir.

### D3 — Yürütme modeli bayrakları örtük · Düşük · öneri E11
- **Nerede:** `strategy()` bildirimleri — NY 49–63, Asian 49–63, Master 60–74: `calc_on_every_tick`,
  `calc_on_order_fills`, `process_orders_on_close` yok. SB (55–72) ve MR'de `calc_on_order_fills`, MTF'de son ikisi yok.
- **Ne:** varsayılanlar doğru (`false`), yani bugün davranış doğru; ama Properties'teki "Recalculate → After order is
  filled / On every tick" ve "Fill orders → On bar close" kutuları kod varsayılanını ezer.
- **Canlıda neden zarar:** "On every tick" açılırsa gerçek zamanlı barda emirler ve trailing bar içinde hesaplanır,
  geçmiş barlar ise her zaman kapanışta; ileriye dönük test backtest'ten ayrışır. "On bar close" açılırsa girişler
  sinyal barının kapanış fiyatından dolar — canlıda ulaşılamayan bir fiyat. Alert'ler `freq_once_per_bar_close` olduğu
  için Faz 1 sinyalleri bundan etkilenmez.
- **Öneri:** üç bayrağı altı dosyada açıkça yaz (davranış değişmez); doğrulama listesine "kutular işaretsiz" kontrolü.

### D4 — Zorunlu kapanışlar barın kapanış fiyatından doluyor · Orta · kod değişikliği yok
- **Nerede:** `immediately = true` — NY 518 · Asian 459 · Master 651, 765, 776 · SB 482, 501, 517 · MR 327 · MTF 536.
- **Ne:** seans sonu, günlük zarar ve tarih sonu kapanışları, koşulun görüldüğü barın kapanış fiyatından (± slipaj) dolar.
  Normal günde 1m/5m barda kapanış ≈ sonraki açılış; fark ihmal edilebilir.
- **Canlıda neden zarar:** kapanış `session.islastbar` ile tetiklendiğinde (yarım gün, erken kapanış, yalnız-RTH grafik)
  script bar kapandıktan sonra çalışır. Canlıda emir seans kapandıktan sonra çıkar, o fiyattan dolamaz ve pozisyon bir
  sonraki seansa boşluk riskiyle kalır. Backtest bunu göstermez.
- **Öneri:** Pine'da değişiklik yok (B3 tasarımı; amaç backtest'te pozisyonu geceye taşımamak). Canlıda düzleştirme
  **takvim tabanlı ve kapanıştan önce** yapılmalı — SPEC §3.1 ve §7.2 bunu zaten şart koşuyor. Faz 1 alıcısı yarım
  günlerde `exit` alert'ine güvenmemeli.

### D5 — Stop, fiyatın yanlış tarafına düşebilir · Orta · kod değişikliği yok
- **Nerede:** MTF runner stop'u — `cand = math.max(be, trailLong)` 444, 460; short 496, 512; Chandelier 208–209.
- **Ne:** başa baş seviyesi ya da Chandelier, bar kapanışında güncel fiyatın üstünde (long) kalabilir: TP1'e değip sert
  dönen bar ya da hızlı düşüş. Emülatör böyle bir stop'u sonraki barın açılışında piyasa çıkışı gibi doldurur.
- **Canlıda neden zarar:** gerçek broker, fiyatın yanlış tarafındaki stop değişikliğini reddeder. Emir reddedilince
  pozisyon eski (daha uzak) stop'la kalır; backtest'in "başa başta çıktım" dediği işlem canlıda tam zarara gidebilir.
- **Öneri:** Pine'da değişiklik yok. Faz 3 yürütme kuralı olarak SPEC §3.4.4'e yazıldı: yeni stop fiyatın yanlış
  tarafındaysa stop'u değiştirme, piyasa emriyle kapat. NY ve Master'daki trailing adayları (482, 502; 628, 639) kapanışın
  her zaman zarar tarafındadır; yalnızca `slBuf = 0` ve bar tam dibinde kapanırsa stop fiyata eşit olur.

### D6 — Aynı barda stop ve hedef · Düşük
- Emülatör bar içi yolu varsayar (açılış yükseğe yakınsa O→H→L→C, değilse O→L→H→C). 1 dakikanın altı veri yok; hangi
  seviyenin önce geldiği bilinemez. SPEC §3.1 Python tarafında `pessimistic` (önce stop) varsayımını şart koşuyor.
  TradingView'da Premium ile Bar Magnifier açık bir kontrol koşusu farkı gösterir (doğrulama listesi C).

### D7 — Sabit 1 tick slipaj · Düşük–Orta
- **Nerede:** `slippage = 1` — NY 60, Asian 60, Master 71, SB 66, MR 70, MTF 61.
- NY açılışında ve haber barlarında piyasa ve stop emirlerinde 1 tick iyimserdir. Dar stop'larda maliyet/R oranı zaten
  yüksek (1,5 puanlık stop'ta ≈ 0,47R). Öneri: doğrulama listesine 2× maliyet koşusu; Python'da maliyet duyarlılık matrisi.

### D8 — Boyut sinyal barından, dolum sonraki açılıştan · Düşük
- **Nerede:** NY 412, 448 · Asian 387, 410 · Master 572, 604, 717, 738 · MR 339, 351 · MTF 364, 392.
- Boyut sinyal barının kapanışına göre hesaplanır; açılış boşluğunda gerçekleşen risk %0,5'ten sapar (yapısal stop'ta
  artar ya da azalır). Bilinen ve belgelenmiş (§3.1); bot tarafında `signal_max_drift_frac_of_stop` sınırlar.

### D9 — `session.islastbar` gerçek zamanlıda doğrulanmadı · Düşük
- **Nerede:** NY 190, 196 · Asian 195, 455 · Master 270, 283, 291, 294 · SB 231, 514 · MR 227, 324 · MTF 184, 533.
- Geçmiş barlarda güvenilir. Gerçek zamanlı barda TradingView'ın seans takvimine dayanır; erken kapanış günlerinde canlı
  `exit` alert'inin gelip gelmediği test edilmedi. D4 ile aynı sonuca çıkar: canlı düzleştirme takvimle yapılmalı.

### D10 — MTF hacim filtresi venue'ya göre değişir · Düşük
- **Nerede:** `volOK`, MTF 199. MES'te gerçek hacim, FTMO `US500.cash` CFD'sinde tick hacmi kullanılır. Yürütme sinyali
  venue verisinde hesaplanacağı için (ADR-0000) MES'te ölçülen sonuç CFD'ye birebir taşınmaz. G1 ve parite için izlenmeli.

### D11 — Geçmişe çizilen görseller · Bilgi
- **Nerede:** NY 347, 357, 429, 463 · Asian 343, 346, 358, 361 · Master 511, 520, 586, 618, 669, 672, 683, 686 · SB 381, 416.
- "Sweep" etiketi ve giriş kutuları, kurulum onaylandıktan sonra daha önceki barlara çizilir. İşlemleri etkilemez; ama
  grafiğe bakınca kurulum o anda görülebiliyormuş gibi durur. Elle değerlendirme yaparken dikkate al.

### D12 — MTF'de `htfTf` için koruma yok · Bilgi
- **Nerede:** MTF 86. HTF, grafik zaman diliminden büyük olmalı; değilse filtre anlamsızlaşır. Grafik 5m değilse uyarı
  etiketi var (569), ama `htfTf` ≤ grafik zaman dilimi durumu için ayrı kontrol yok.

### D13 — IS/OOS sınırı yalnızca gösterim · Bilgi
- **Nerede:** `isOOS` — NY 199 · Asian 198 · Master 260 · SB 234 · MR 233 · MTF 189. Sınır tabloyu ve grafik boyamasını
  sürer, parametre seçimini değil. Parametreler tüm veriye bakılarak seçildiyse OOS dönemi kirlenmiştir; gerçek ayrım
  walk-forward ile yapılır (§6.4, G3).

## 4. YAML denetimi

`config/risk.yaml` (13 satır) ve `config/venues/ftmo_2step_100k.yaml` (26 satır) PyYAML ile sorunsuz okunuyor; tipler
beklendiği gibi. Çapraz kontroller tutuyor: `daily_cap_pct` 1,0 ≤ 0,5 × `daily_loss_limit_pct` (2,5);
`risk_per_trade_pct` 0,25 ≤ 0,5.

| # | Nerede | Bulgu | Öneri |
|---|---|---|---|
| Y1 | depo | Şema/doğrulama kodu yok. Depoda Python projesi de yok (`pyproject.toml`, `src/`, `tests/` yok; `uv` kurulu değil; sistem Python'u 3.14, hedef 3.12). | Python iskeletiyle birlikte pydantic v2 modelleri. |
| Y2 | `config/` | `strategies.yaml` yok (CLAUDE.md dizin yapısında var). | İlk strateji portuyla birlikte. |
| Y3 | `risk.yaml:6` | `flatten_when_daily_buffer_below_pct: 40` = günlük limitin %60'ı tüketilince düzleştir. Denetim isteği %80 diyor. | Bölüm 5. |
| Y4 | `risk.yaml:3` | `risk_per_trade_pct: 0.25`, Pine varsayılanı 0,5. TradingView sonuçları botun riskinin iki katıyla üretilir. | Monte Carlo botun değerini kullanmalı. |
| Y5 | `risk.yaml:4` | `daily_cap_pct: 1.0`, Pine içi kesici %2. | Bilgi (CHANGELOG açık bulgu 14). |
| Y6 | `risk.yaml` | SPEC §8.1'deki `cost_buffer` için anahtar yok; spread koruması ve strateji önceliği için de yok. | Şema yazılırken SPEC ile birlikte tanımla. |
| Y7 | `ftmo…yaml:6, 17, 21, 23–26` | `rules_verified_at`, `min_trading_days`, `server_tz` ve dört kontrat alanı `null`. | Şema `null`'a izin vermeli; çalışma anında `null` → fail-closed. `min_trading_days` Monte Carlo için gerekli (VERIFY). |
| Y8 | `ftmo…yaml:15` | `daily_reset.time: "00:00"` tırnaklı — doğru (tırnaksız yazılırsa YAML sayı olarak okuyabilir). | Şemada `HH:MM` deseni ve `tz` için zoneinfo doğrulaması. |
| Y9 | `ftmo…yaml:11–16` | Enum ve aralık kontrolü gereken alanlar: `max_loss_type`, `daily_loss_basis`, `platform`, yüzde alanları, `daily_loss_limit_pct < max_loss_pct`. | Şemada zorunlu kıl. |
| Y10 | `risk.yaml:8–9` | `max_positions_total: 2`; beş strateji aynı gün sinyal verebilir, hangisinin önce alınacağı tanımlı değil. | SPEC §8.1'e öncelik kuralı (karar gerekir). |

## 5. Denetim isteğindeki risk kuralları ile SPEC

| İstek | Depodaki kural | Durum | Öneri |
|---|---|---|---|
| Günlük limitin %80'inde tüm pozisyonları kapat, günü kilitle | `flatten_when_daily_buffer_below_pct: 40` → %60'ta düzleştir; ayrıca günlük bütçe %1 (ön kontrol) | Depodaki kural daha sıkı | Sıkı olanı koru. Değişiklik `docs/adr/` kaydı ister (`risk.yaml` başlığı). |
| Taban: başlangıç bakiyesi ile gün başı özsermayeden yüksek olan | `daily_loss_basis: max_balance_equity_at_reset` (VERIFY) | Farklı tanım | Firmanın resmi kuralı esas; değer koda gömülmez (değişmez kural 10). |
| Maks. erimeye yaklaşırken riski dinamik küçült (dynamic Kelly) | §8.1: sabit oran, tavan %0,5; kural 4: kayıptan sonra risk artırma yasak | Küçültme uyumlu; Kelly ile büyütme uyumsuz | Yalnızca aşağı yönlü kısma, tabanın üstüne asla çıkmadan. Yeni kural → ADR + §8.1 güncellemesi. |
| Haber filtresi | `news_rule` + elle güncellenen `data/news.yaml` (site kazıma yok) | Uyumlu | Faz 2/3. |
| Spread / volatilite koruması | Tanımlı değil | Yeni gereksinim | Faz 3 (köprü spread'i okur); anahtar ve eşik için karar gerekir. |

## 6. Python optimizasyon ve stres testi paketi

Bu denetimde yazılmadı. Nedenleri:
- İstenen iş (1:1 pariteli Python motoru, walk-forward, Monte Carlo) projenin **Faz 2**'sidir (SPEC §6). CLAUDE.md faz
  geçişini kullanıcı kararına ve onaylı plana bağlıyor; Faz 0'ın kabulü (TradingView doğrulaması) henüz yapılmadı.
- Depoda fiyat verisi yok; walk-forward ve maliyet duyarlılık matrisi veri olmadan çalıştırılamaz.
- Pine dosyaları henüz derlenmedi. 1:1 parite hedefi, derleme düzeltmeleri bitene kadar hareketli.
- Bağımsız bir `optimizer.py`, projenin dizin yapısına (`src/propbot/backtest/`), test şartına ("testsiz risk/strateji
  kodu yok") ve parite testine (`tests/parity/`) aykırı olurdu.

Önerilen sıra:
1. Python iskeleti + `config/*.yaml` için pydantic şemaları (Y1–Y10).
2. Monte Carlo prop simülatörü (§6.5): girdi olarak TradingView "List of Trades" dışa aktarımı yeter; fiyat verisi
   gerekmez. Geçme / ihlal olasılığı, ihlal nedeni dağılımı, %95 ve %99 maks. erime.
3. Veri geldikten sonra: bar tabanlı motor ve parite (önce §3.2 ve §3.3), maliyet duyarlılık matrisi, walk-forward.

## 7. Önerilen kod değişiklikleri

| ID | Dosyalar | Değişiklik | Davranış |
|---|---|---|---|
| E11 | 6 dosya | `calc_on_every_tick`, `calc_on_order_fills`, `process_orders_on_close` = `false` açıkça yazılır | Değişmez |
| E12 | NY, Asian, Master, MR, MTF | `backtest_fill_limits_assumption = 1` | TP dolumları daha muhafazakâr |

Bilinçli olarak önerilmeyenler: `[1]` + `lookahead_off` (D1); giriş koşullarına `barstate.isconfirmed` (stratejiler zaten
yalnızca kapanmış barda çalışıyor ve alert'ler `freq_once_per_bar_close`; ek koruma derleme riskinden başka bir şey
getirmez); Pine'da seans sonu kapanış mantığının değiştirilmesi (D4 — bot tarafının işi).
