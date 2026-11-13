# PropBot — Teknik Spesifikasyon

Sürüm 0.2 · 2026-11-12 · Sahibi: Oğuz
`CLAUDE.md`'nin ayrıntısıdır. Sayı içeren her prop kuralı **VERIFY** etiketlidir: kullanılmadan önce firmanın
resmi sayfasından doğrulanır ve venue profilindeki `rules_verified_at` güncellenir.

---

## 1. Doğrulanmış bağlam (2026-11-01)

- **Topstep:** Türkiye, resmi uygunluk sayfasında "işlem yapamaz / ödeme alamaz" ülkeler listesinde (4 Eylül 2026). Kapsam dışı.
- **FTMO:** EA ve algoritmik işlem genel olarak serbest. Yasak: HFT, arbitraj, simülasyon açığı istismarı; günde 2.000'den
  fazla sunucu isteği yapan "hiperaktif" EA'lar; aynı işlem kalıbının birden çok hesapta tekrarı. VPN/VPS genel olarak
  serbest (MetaTrader/cTrader'da ABD konumu hariç).
- **FTMO 2-Step (VERIFY):** hedef %10 → %5, günlük kayıp %5, toplam kayıp %10 statik, süre sınırı yok. Günlük reset saati
  kaynaklar arasında çelişkili (CE(S)T gece yarısı / UTC gece yarısı) → resmi sayfadan doğrula.
- **FTMO 1-Step (VERIFY):** hedef %10, günlük kayıp %3, toplam %10 gün sonu trailing, "Best Day" kuralı.
- **Funding Pips:** üçüncü taraf EA yalnızca trade/risk yöneticisi olarak serbest → kendi strateji otomasyonumuz için
  firmadan yazılı onay şart.
- **TradingView webhook:** ücretli plan + 2FA; yalnızca port 80/443; IPv4; 3 sn içinde yanıt yoksa istek iptal; dört sabit
  kaynak IP (listeyi her kurulumda TradingView destek sayfasından kopyala, bu belgeden değil).
- **TradingView alarmları:** Premium altı planlarda alarm süreli (~1–2 ay). Alarm, oluşturulduğu andaki script ve girdi
  değerleriyle çalışır → girdi değişince alarm yeniden kurulur (VERIFY).
- **Deep Backtesting** (Premium+): sonuçlar yalnızca Strategy Report'ta; grafikteki istatistik tablosu deep moddan beslenmez.
- **SPK:** izinsiz yatırım danışmanlığı SPKn m.109/2 kapsamında suç; bazı prop firmalarına erişim engeli getirildi
  (Aralık 2024). Sinyaller yalnızca kişisel kullanım içindir.

## 2. Mimari

### 2.1 Akış
```
Faz 1 : TradingView alert() ─HTTPS:443─► Caddy ─► receiver (FastAPI) ─► kuyruk ─► boyut önerisi ─► Telegram
Faz 2+: MarketDataFeed ─► Strategy.on_bar() ─► Signal ─► RiskEngine (veto) ─► ExecutionAdapter ─► broker
                                                   └──────────► Store (SQLite) ─► Notifier (Telegram)
Faz 3 : MT5 terminal ⇄ bridge EA (MQL5) ⇄ Python (MarketDataFeed + ExecutionAdapter uygulaması)
```

### 2.2 Çekirdek tipler (`src/propbot/core`)
- `Bar(ts_open_utc, symbol, tf, open, high, low, close, volume)` — zaman damgası bar **açılış** anı (TradingView `time` ile aynı).
- `Signal(id, strategy_id, symbol, bar_ts, side, setup, entry_ref, stop, tp, rr, stop_method, meta)`
- `OrderIntent(client_id, signal_id, side, qty, sl, tp, kind)` — `client_id` idempotency anahtarıdır.
- `Fill`, `Position`, `AccountState(balance, equity, day_start_balance, day_start_equity, realized_today, open_risk)`.

### 2.3 Bar döngüsü (her kapanan 1m bar)
1. Bar doğrulama (sıra, boşluk, çift kayıt, saat sapması). 2. Takvim/seans durumu. 3. `strategy.on_bar(bar, ctx)` →
sinyaller ve pozisyon yönetimi niyetleri. 4. Risk motoru (veto/boyut). 5. Yürütme. 6. Kayıt + bildirim.
Strateji kodu saat, dosya, ağ ya da ortam değişkeni okumaz; her şey `ctx` ile gelir.

## 3. Strateji semantiği (parite spesifikasyonu)

### 3.1 Tüm stratejiler için
- Karar yalnızca **kapanmış barda**; repaint ve lookahead yok.
- Sinyal bar N kapanışında; piyasa emri bar N+1 **açılışında** dolar (Pine `process_orders_on_close=false`).
- **Giriş-zamanı kapısı:** dolum bir sonraki barda olduğundan, sinyal barı grafik seansının son barıysa
  (`session.islastbar`) ya da dolum barı seans/pencere bitiminde veya sonrasında açılacaksa giriş yok:
  `curMin + bar süresi < bitiş` (1m grafikte `curMin < sessEnd − 1`). Pencere modellerinde (Asian, Silver Bullet)
  dolum barı pencerenin içinde olmalıdır. Tarama ve durum makinesi pencereleri bundan etkilenmez.
- SL+TP sinyal anında bracket olarak kurulur (Faz 0 sonrası Pine ile aynı). Dolumda:
  - FVG Buffer stop'u **yapısal seviyedir** (değişmez); ATR stop'u **mesafedir** (dolum ∓ sinyal barındaki ATR × çarpan).
  - TP = dolum ± rr × |dolum − stop|. Stop, dolumun en az 1 tick zarar tarafında zorlanır.
  - Bilinen küçük parite farkı: Pine TP'yi dolum barının kapanışında günceller, Python dolum anında. Parite testinde
    tolerans olarak belgelenir.
- Aynı barda hem SL hem TP'ye değilirse backtest varsayılanı `fill_model: pessimistic` (önce SL). `tv_ohlc` modu
  TradingView emülatörünü taklit eder (açılış yükseğe daha yakınsa O→H→L→C, değilse O→L→H→C) — yalnızca parite testinde.
- **Limit emirleri** (Silver Bullet girişi ve tüm TP'ler) fiyat seviyeyi **1 tick geçince** dolar
  (Pine `backtest_fill_limits_assumption = 1`); dokunma dolum sayılmaz.
- **Boyut:** `qty = risk% × risk tabanı / (stop mesafesi × point value)`; risk tabanı varsayılanı **başlangıç bakiyesi**
  (Pine `Risk base = Initial Capital`, §8.1). Tahmini giriş = sinyal barı kapanışı; aşağı yuvarlama, < 1 ise işlem atlanır.
- Strateji başına günde en fazla 1 işlem (tüm script'lerde varsayılan). Seans sonunda pozisyon kesinlikle kapalı
  (takvim tabanlı; yarım günler dahil).
- **Zorunlu kapanışlar** (seans/pencere sonu, günlük zarar ihlali, tarih aralığı sonu) koşulun görüldüğü barın
  **kapanışında** dolar (Pine `immediately = true`) ve `exit` alert'i üretir; bar başına tek neden, tek emir.
- **Günlük zarar kesicisi (Pine içi, varsayılan %2):** gün içi zarar — gerçekleşen ya da bar kapanışı özsermayesiyle
  yüzen — eşiğe ulaşınca gün için yeni giriş yok ve (varsayılan) açık pozisyon kapatılır. Bar kapanışında
  değerlendirilir; bar içi ihlal ancak bar kapanınca görülür. Botun bağlayıcı günlük bütçesi §8.1'dir.

### 3.2 NY Open ORB + FVG/IFVG (`pine/ny_open_fvg_strategy.pine`)
- **OR:** NY saatiyle [09:30, 09:30 + orMinutes) aralığında açılan barların en yüksek high / en düşük low'u; `orEnd`'den itibaren hazır.
- **Tarama penceresi:** [orEnd, sessEnd) (varsayılan 09:35–15:59). NY takvim günü değişince tüm durum sıfırlanır.
  Giriş için ayrıca §3.1 giriş-zamanı kapısı: 1m'de son giriş sinyali 15:57 barı (dolum 15:58).
- **Bias:** pencerede Close modunda ilk kapanış OR dışına çıkınca (Wick modunda fitil) belirlenir; gün boyu sabit.
  Yön kapalıysa (ör. allowLong=false) o yöndeki kırılım bias üretmez, tarama sürer.
- **FVG tespiti** (yalnızca bias belirlendikten sonra; iki yön de izlenir; FIFO en fazla 60 kayıt):
  - Boğa: `low[0] > high[2]` → bölge [high[2], low[0]]. Ayı: `high[0] < low[2]` → bölge [high[0], low[2]]. `minFvgTicks` filtresi.
- **Durum makinesi** (her bar, giriş kontrolünden ÖNCE): fresh → inverted → dead.
  - Boğa FVG: fresh ∧ close < alt → inverted (ayı IFVG; flip_bar = bu bar); inverted ∧ close > üst → dead.
  - Ayı FVG: fresh ∧ close > üst → inverted (boğa IFVG); inverted ∧ close < alt → dead.
- **Giriş (bias = +1):** kayıtlar en yeniden eskiye taranır; ilk uygun bölge kullanılır:
  `(boğa ∧ fresh ∧ useFVG) ∨ (ayı ∧ inverted ∧ useIFVG)` ve aktivasyon barından (form ya da flip) sonraki bir barda
  `low ≤ üst` → long. Kısa taraf simetrik: `(ayı ∧ fresh) ∨ (boğa ∧ inverted)` ve `high ≥ alt`.
- **Stop:** FVG Buffer → long: alt − slBuf tick; short: üst + slBuf tick. ATR → giriş ∓ ATR(atrLenInit) × atrMultInit.
- **TP:** giriş ± rr × risk.
- **Trailing (opsiyonel, varsayılan açık):** kâr ≥ trailActR × R olunca; Swing: `lowest(low, swingLen) − buffer`
  (short: `highest(high, swingLen) + buffer`), ATR step: `close ∓ ATR(atrLenT) × atrMultT`,
  Percent: `close × (1 ∓ trailPct/100)`. Stop yalnızca kâr yönüne gider; güncelleme bar başına ≤ 1 ve
  yalnızca aday mevcut stop'u ≥ `trail_min_step_ticks` (4) tick iyileştiriyorsa (Pine `trailMinStepTicks`). Trailing
  dolumdan sonra başlar; aktivasyon R'si gerçek dolum fiyatından ölçülür.
- **Seans sonu:** sessEnd'e ulaşılınca veya seansın son barında kapat.

### 3.3 Asian Range Sweep & Reversal (`pine/asian_sweep_reversal_strategy.pine`)
- **Asya penceresi:** NY saatiyle [20:00, 00:00) (gece yarısını sarabilir). Pencerenin ilk barı = **yeni döngü**: aralık,
  sweep, arming ve günlük işlem hakkı sıfırlanır. Pencere bitince aralık kesinleşir.
- **Aktif pencere:** [03:00, 11:00). Hafta günü filtresi aktif pencere barının gününe uygulanır.
- **Bar içi sıra ÖNEMLİ:** (1) onay/arming → (2) sweep tespiti → (3) giriş. Böylece FVG her zaman sweep'ten sonraki
  bir barda değerlendirilir.
- **Sweep yukarı:** `high > asianHigh ∧ high[1] ≤ asianHigh`, ya da aktif pencerenin ilk barında `high > asianHigh`
  → `sweep_hi_bar`. Aşağı simetrik.
- **Short arming:** `sweep_hi_bar`'dan ≤ maxBars bar içinde ayı FVG (`high < low[2]`) ∧ `close < asianHigh` ∧ boyut
  filtresi → bölge [high, low[2]]; sweep tüketilir. Long simetrik. Aynı barda ikisi birden olursa long kazanır (Pine
  sırası); yeni arming eskisinin üzerine yazar.
- **Giriş:** önce invalidasyon (long: close < alt; short: close > üst → iptal), sonra arming barından sonraki bir barda
  temas (long: `low ≤ üst`; short: `high ≥ alt`). Dolum barı aktif pencerenin içinde olmalı (§3.1): 1m'de son giriş
  sinyali 10:58 barı.
- **SL/TP:** sabit (trailing yok). Aktif pencere bitince kapat.
- **Enstrüman:** SPX/SPY'de 20:00–00:00 ET verisi yok → yalnızca ~23 saat işlem gören enstrümanlarda (ES/MES, US500 CFD) anlamlı.

### 3.4 Diğer stratejiler
§3.1 hepsi için geçerlidir. Parantez içindeki sayılar girdi varsayılanlarıdır. Python'a portlama sırası: önce 3.2 ve 3.3.

#### 3.4.1 Master Prop Firm Portfolio (`pine/master_prop_firm_portfolio.pine`)
- **Kapsam:** S1 = §3.2 (NY ORB + FVG/IFVG, trailing dahil), S2 = §3.3 (Asian Sweep) tek script'te. Motor girdileri
  `s1_` / `s2_` öneklidir, varsayılanları tekil dosyalarla aynıdır; `riskPct` ve `rr` iki motor için ortaktır.
  Sinyal, stop, TP, dolum barı yeniden fiyatlaması (§3.1) ve trailing kuralları tekil dosyalarla **aynıdır**; farklar
  aşağıdadır. Grafik 1m; S2 için ~23 saat işlem gören enstrüman gerekir.
- **Bar içi sıra:** (1) açık bacak taraması, kapanan bacağın durum sıfırlaması → (2) dolum barı yeniden fiyatlaması →
  (3) küresel kesiciler → (4) S1: bias, FVG, durum makinesi, giriş, yönetim, seans sonu → (5) S2: arming, sweep,
  invalidasyon, giriş, yönetim, pencere sonu → (6) portföy düzeyi zorunlu kapanış.
- **Tek net pozisyon:** `pyramiding = 2`. İki bacak yalnızca **aynı yönde** birlikte açık olabilir; diğer motor açıkken
  (ya da emri beklerken) ters yönlü giriş engellenir. Aynı barda çatışmada önce çalışan S1 kazanır.
- **Eşzamanlı risk:** (diğer bacak açıksa `riskPct`, değilse 0) + `riskPct` ≤ `maxConcurrentRiskPct` (1,0) değilse giriş yok.
- **Bacak takibi:** `strategy.opentrades.entry_id` ("S1 …" / "S2 …") ile; bacak defterden çıkınca durumu sıfırlanır
  (giriş barında 1 bar koruma). Dolum fiyatı bacak bazında `strategy.opentrades.entry_price` ile okunur;
  `position_avg_price` iki bacağın harmanı olduğu için kullanılmaz.
- **Günlük işlem:** motor başına 1 (S1: NY takvim günü; S2: Asya döngüsü).
- **Küresel kesiciler (NY takvim günü, iki motor birlikte):** günlük zarar — gerçekleşen ya da bar kapanışı özsermayesiyle
  yüzen — ≥ `dailyDDPct` (2) × min(gün başı özsermaye, başlangıç sermayesi) → yeni giriş yok; `flattenBreach` açıksa iki
  bacak o barın kapanışında kapatılır. Kapanış sırasına göre birleşik ardışık zarar ≥ `maxConsecLoss` (2) → yeni giriş yok.
- **Zorunlu kapanışlar:** S1 seans sonu ve S2 aktif pencere sonu bacak bazında; günlük zarar ihlali ve End date tüm
  pozisyon. Hepsi barın kapanışında. İhlal/End date barında motor bazlı kapanışlar devre dışıdır (bacak başına tek emir).
- **Alert:** S1 → `ny_orb_fvg`, S2 → `asian_sweep` (tek TradingView alarmı yeterli).

#### 3.4.2 ICT Silver Bullet (`pine/ict_silver_bullet_strategy.pine`)
- **Grafik:** 1m veya 5m. **Pencereler (NY):** AM [10:00, 11:00) ve PM [14:00, 15:00); ayrı ayrı açılıp kapatılabilir.
- **Pivotlar:** `ta.pivothigh/low(pivotLen, pivotLen)` (5); pivot `pivotLen` bar sonra onaylanır. Son onaylı pivotlar
  `lastPH` / `lastPL`; her yön için son 25 pivot likidite havuzudur (FIFO).
- **FVG izleme (yalnızca pencere içi barlar):** boğa `low > high[2]` → [high[2], low]; ayı `high < low[2]` → [high, low[2]];
  `minFvgTicks` filtresi. Her yön için yalnızca **en son** boşluk tutulur; yeni pencerede önceki boşluklar geçersizdir.
- **Durum makinesi:** 0 boşta → 1 sweep (MSS/FVG bekleniyor) → 2 silahlı (limit bekliyor) → 3 işlemde → 0.
  Gün değişiminde (pozisyon yoksa) ve yeni pencere başında (pozisyon yok, faz ≠ 3) faz 0'a döner.
- **Emir-yerleştirme kapısı (`entryTimeOK`):** pencere içi ∧ grafik seansının son barı değil ∧ **bir sonraki bar da aynı
  pencerenin içinde**. Av başlatma, silahlanma ve bekleyen emrin yaşaması bu kapıya bağlıdır.
- **Av başlatma:** kapı ∧ tarih aralığı ∧ gün filtresi ∧ durdurma yok ∧ pozisyon yok ∧ faz 0 ∧
  `tradesWindow < maxTrWindow` (1) ∧ `tradesToday < maxTrDay` (1).
- **1) Sweep** (iki pivot da tanımlıyken; önce short denenir): `high > lastPH` → short, `sweepExtreme = high`,
  `mssLevel = lastPL`; değilse `low < lastPL` → long, `sweepExtreme = low`, `mssLevel = lastPH`.
- **2) MSS** (faz 1, pencere içi; sweep barı dahil): her barda `sweepExtreme` güncellenir (short: max high; long: min low).
  MSS = **kapanışla** kırılım (short: `close < mssLevel`; long: `close > mssLevel`). Sweep'ten sonra
  `maxBarsSweepMSS` (10) bardan fazla geçerse av iptal.
- **3) FVG → silahlanma:** MSS'ten sonra en çok `maxBarsMSSFvg` (5) bar içinde, yönle uyumlu ve sweep barında ya da
  sonrasında oluşmuş en son FVG varsa ve kapı açıksa. Giriş seviyesi: Proximal (short: alt; long: üst) / %50 CE
  (varsayılan) / Distal (short: üst; long: alt). Stop = `sweepExtreme` ± `tickOffset` (1) tick. Risk = |giriş − stop| > 0.
  Sweep, MSS ve FVG aynı barda tamamlanabilir.
- **Emir:** **limit** giriş + aynı anda bracket (stop + TP); bracket yalnızca giriş dolunca aktiftir. TP: Fixed R:R
  (`rr` = 2) ya da "Next opposing liquidity" (girişin ötesindeki en yakın pivot ∓ `liqBuf` (2) tick; mesafe <
  `minRRliq` (1) × risk ise Fixed R:R). Limit dolumu için fiyatın seviyeyi 1 tick geçmesi gerekir
  (`backtest_fill_limits_assumption = 1`). Boyut: §3.1, tam kontrata aşağı yuvarlama; < 1 ise silahlanma yok.
- **İptal (faz 2, dolmamış):** kapanış stop'un ötesindeyse (short: `close > stop`; long: `close < stop`), kapı kapandıysa
  (pencerenin son barının kapanışı ya da seansın son barı) ya da `maxBarsToFill` (20) bar geçtiyse → limit ve bracket
  iptal, `cancel` alert'i. Böylece emir pencere dışında dolamaz.
- **Dolum tespiti:** `opentrades + closedtrades` artışı (aynı barda dolup kapanan işlem dahil) → faz 3; pencere ve gün
  sayaçları +1.
- **Yönetim:** stop ve TP sabit; trailing yok.
- **Korumalar:** günlük: (özsermaye − gün başı özsermaye) / gün başı özsermaye ≤ −`maxDailyDDPct` (2) → gün için dur,
  bekleyen emri iptal et, `flattenBreach` ile pozisyonu kapat. Ardışık `maxConsec` (3) zarar → gün için dur (sayaç her gün
  sıfırlanır). Zirveden düşüş ≤ −`maxOverallPct` (8) → kalıcı dur. Gün sonu: `curMin ≥ 15:55` ya da seansın son barı → kapat.

#### 3.4.3 Mean Reversion BB/KC + RSI(2) (`pine/mean_reversion_bb_kc_rsi2_strategy.pine`)
- **Grafik:** 5m–15m. **Göstergeler:** BB = SMA(close, 20) ± 2,5 × stdev(close, 20); KC = EMA(close, 20) ± 1,5 × ATR(20);
  RSI(2); EMA(200); ATR(14).
- **Sıkışma filtresi (`useKcFilter`, açık):** squeeze = BB tamamen KC'nin içinde (`bbUpper < kcUpper ∧ bbLower > kcLower`);
  setup yalnızca squeeze **değilken** geçerli.
- **Setup barı:** long: `close < bbLower ∧ RSI2 < rsiOS` (10); short: `close > bbUpper ∧ RSI2 > rsiOB` (90); + sıkışma filtresi.
- **Onay barı (setup'ın hemen ardındaki bar):** long: `close > bbLower` ∧ trend filtresi `close ≥ EMA200`; short:
  `close < bbUpper` ∧ `close ≤ EMA200`. `requireStrong` (kapalı) açıksa mum işlem yönünde ve kapanış aralığın reddeden
  yarısında olmalı. Bantlar ve EMA onay barının kendi değerleridir.
- **Giriş izni:** tarih aralığı ∧ zaman penceresi [10:00, 15:45) ∧ giriş-zamanı kapısı (§3.1) ∧ gün filtresi ∧ günlük
  durdurma yok ∧ `tradesToday < maxTradesDay` (1) ∧ pozisyon yok. Onay barı kapanışında piyasa emri.
- **Stop:** dolum ∓ `atrMult` (2,0) × **sinyal barındaki** ATR(14); sabit, trailing yok. (Sinyal barı bracket'i: kapanış ∓
  aynı mesafe.)
- **TP:** canlı BB ortası (SMA20), her bar güncellenir; girişin zarar tarafına geçemez (long: max(orta, dolum + 1 tick);
  short: min(orta, dolum − 1 tick)). TP limit emridir.
- **Boyut:** §3.1; stop mesafesi = `atrMult` × ATR.
- **Günlük kesici:** (gün başı özsermaye − özsermaye) / gün başı özsermaye ≥ `maxDailyLoss` (2) → gün için yeni giriş yok;
  `closeOnHalt` (açık) ile pozisyon kapatılır. **Seans sonu:** `curMin ≥ 15:45` ya da seansın son barı → kapat.

#### 3.4.4 MTF Trend Continuation (`pine/mtf_trend_continuation_strategy.pine`)
- **Grafik:** 5m; üst zaman dilimi 60. **HTF trend:** 1H EMA(20) ve EMA(50), **son kapanmış** 1H bardan
  (`request.security(..., [ema[1], ema[1]], lookahead_on)`): boğa = EMA20 > EMA50; ayı = EMA20 < EMA50.
- **Değer alanı:** 5m EMA(21) ile EMA(50) arasındaki bant; barın aralığı bandı kesiyorsa içeride (`low ≤ üst ∧ high ≥ alt`).
- **Hacim (`useVolFilter`, açık):** `volume < SMA(volume, 20)`.
- **Silahlanma (her bar):** boğa ∧ içeride ∧ hacim → long silahlı; ayı simetrik; `armBar` = bu bar (her uygun barda
  yenilenir). NY günü değişince silah sıfırlanır. Geçersizleşme: trend artık o yönde değil ya da
  `bar − armBar > armValidityBars` (10).
- **Tetik:** long: `close > open ∧ close > high[1]`; short: `close < open ∧ close < low[1]`. Silahlanma barında da olabilir.
- **Giriş izni:** tarih aralığı ∧ seans [09:30, 15:55) ∧ giriş-zamanı kapısı (§3.1) ∧ durdurma yok ∧
  `tradesToday < maxTradesDay` (1) ∧ pozisyon yok. Girişten sonra silah sıfırlanır.
- **Stop (yapısal):** long: min(`lowest(low, 10)` − 4 tick, close − 1 tick); short: max(`highest(high, 10)` + 4 tick,
  close + 1 tick). Dolumda seviye korunur, dolumun en az 1 tick zarar tarafına çekilir.
- **Boyut:** §3.1; tam kontrata aşağı yuvarlama (`roundUnits`), 0 ise işlem atlanır.
- **Ölçekli mod (çift sayılı kontrat):** TP1 = dolum ± `tp1RR` (1,5) × risk'te pozisyonun `scaleOutPct` (%50) kadarı limit
  emirle kapanır; kalan (runner) için stop başa başa çekilir, sonra trailing.
- **Plan B (tek sayılı / 1 kontrat):** ölçek yok; barın high/low'u TP1 seviyesine değince tüm pozisyonun stop'u başa başa
  çekilir, sonra trailing (TP emri yok).
- **Trailing:** aday = long: max(başa baş, iz); short: min(başa baş, iz). İz = Chandelier (`highest(high, 22)` −
  ATR(22) × 3,0; short simetrik) ya da ATR (`close ∓ ATR(14) × 2,5`). Başa başa ilk çekiş koşulsuz; sonrası yalnızca aday
  mevcut stop'u ≥ `trailMinStepTicks` (4) tick iyileştiriyorsa. Stop yalnızca kâr yönüne gider.
  Aday stop bar kapanışında fiyatın yanlış tarafında kalırsa (long'da stop ≥ fiyat) Pine emri sonraki barın açılışında
  doldurur; canlı yürütmede bu, stop değişikliği yerine **piyasa emriyle kapatma** olarak uygulanır (broker böyle bir
  stop'u reddeder).
- **Kesiciler:** günlük zarar (gün başı özsermaye − bar kapanışı özsermayesi) ≥ `dailyLossPct` (2) → gün için dur +
  (`flatOnDailyLoss`) kapat; isteğe bağlı ardışık zarar durdurması (kapalı). **Seans sonu:** `curMin ≥ 15:55` ya da seansın
  son barı → kapat.
- **Tam işlem sonucu:** giriş sinyali anındaki özsermayeye göre net K/Z (ölçek çıkışı dahil); tablo mod (Scaled / Plan B)
  ve örneklem (IS / OOS) bazında gösterir.

## 4. Faz 0 — Pine düzeltmeleri

### 4.1 Bilinen hatalar
| ID | Önem | Sorun | Düzeltme |
|---|---|---|---|
| B1 | KRİTİK | "Reset management vars once flat" bloğu **sinyal barında** da çalışıyor: emir bir sonraki barda dolduğu için `strategy.position_size` o barda hâlâ 0 → `inDir` aynı barda sıfırlanıyor → yönetim bloğu hiç çalışmıyor → `strategy.exit` hiç çağrılmıyor. Pozisyonlar SL/TP'siz; yalnızca "EOD close" / "Session end" ile çıkıyor. | Sıfırlamayı yalnızca bir işlem gerçekten kapandığında yap: `strategy.position_size == 0 and strategy.closedtrades > strategy.closedtrades[1] and inDir != 0` |
| B2 | Yüksek | Çıkış emirleri dolum barının kapanışında kuruluyor → dolum barı korumasız (iyimser yanlılık). Limit girişte en ağırı: limit, fiyat stop'a doğru giderken dolar. | `strategy.entry`'den hemen sonra aynı barda `strategy.exit(stop=, limit=)` (tahmini giriş = close); dolum barında gerçek ortalama fiyatla yeniden hesapla. ATR mesafesini sinyal barında sakla. Limit girişte seviyeler zaten bilinir: exit, arming barında entry ile birlikte kurulur; entry iptal edilince exit de iptal edilir. |
| B3 | Yüksek | Seans sonu `curMin == sessEnd` + sonraki bar açılışında dolum: yarım günlerde hiç kapanmıyor; yalnız-RTH grafikte ertesi günün açılışında kapanıyor. | `curMin >= sessEnd or session.islastbar` ve `strategy.close_all(comment = ..., immediately = true)`. Asya'da aktif pencere çıkışı için aynı. |
| B4 | Yüksek | Komisyon ve slipaj 0 ya da enstrümanla uyumsuz. | Enstrümana göre girdi: CFD → spread + slipaj tick; MES/ES → kontrat başı nakit komisyon + 1 tick slipaj; SPY → yüzde komisyon. TradingView komisyonu **girişte ve çıkışta ayrı ayrı** keser (`cash_per_contract` = taraf başı); girdi açıklamaları buna göre yazılır. Slipaj yalnızca piyasa ve stop emirlerine eklenir; limit girişli stratejilerde `backtest_fill_limits_assumption` ≥ 1 tick (dokunma = dolum varsayımı iyimser). |
| B5 | Orta | `percent_of_equity` boyutlandırma ya da vadelide küsuratlı kontrat. | Sabit-fraksiyonel: `qty = equity × risk% / (stop mesafesi × point value)`; girdi: risk %. Vadeli için tam sayıya aşağı yuvarlama girdisi; sonuç < 1 ise işlem atlanır. |
| B6 | Orta | Yeni işlem tespiti ve kayıtlar `strategy.position_size` geçişine bağlı: aynı barda giriş+çıkış (B2 düzeltmesinden sonra mümkün) kaçırılır → tablo indeksi kayar, faz/işlem sayaçları artmaz, limitler aşılabilir. | Yeni işlemi `strategy.opentrades + strategy.closedtrades` artışıyla tespit et. Kayıtları sinyal barında ekle, dolum barında son elemanı güncelle. Sayaçları ve faz geçişlerini bu tespite bağla. |
| B7 | — | `alert()` yok. | §5.1 şemasında JSON, `alert.freq_once_per_bar_close`. Webhook token'ı girdi olarak. |
| B8 | Yüksek | `request.security(..., lookahead_off)` ile üst zaman dilimi verisi: geçmiş barlarda yalnızca onaylı değer, gerçek zamanlı barlarda onaylanmamış (gelişen) değer döner → canlı sinyaller backtest'ten farklı (realtime repaint). | `request.security(syminfo.tickerid, tf, [ifade1[1], ifade2[1]], lookahead = barmerge.lookahead_on)` — `[1]` ve `lookahead_on` birbirine bağlıdır, biri olmadan diğeri kullanılmaz (`[1]` olmadan `lookahead_on` geleceği okur). Geçmiş sonuçlarda HTF değeri saat başına bir bar gecikir; beklenen küçük fark. |

### 4.1a Dosya bazında ön analiz (2026-11-02, Claude sohbetinde yapıldı; planda satır numaralarıyla doğrula)
`pine/` altındaki altı dosya (strateji seti tam; hepsi Pine v5, eksiksiz):

| Dosya | Satır | Beklenen maddeler | Not |
|---|---|---|---|
| `master_prop_firm_portfolio.pine` | 809 | B3 (S1 `curMin == s1_sessEnd`, 15:59; yalnız-RTH SPY grafiğinde kapanış emri ertesi gün açılışta doluyor), B4 (SPY'ye göre yüzde komisyon; vadeli/CFD için girdi), B5 (`f_qty` küsuratlı; vadeli için yuvarlama girdisi), B7 | **Ana dosya.** B1 ve B2 yok: bacak durumu `strategy.opentrades` ile izleniyor, stop/TP sinyal barında kuruluyor. B6 muhtemelen yok (FIFO stop kuyruğu, `entry_id` eşlemesi) — doğrula. |
| `ny_open_fvg_strategy_2.pine` | 566 | B1 (kritik), B2, B3 (`curMin == sessEnd`), B4 (SPY), B5 (yuvarlama), B6, B7 | NY tekil sürümün son hâli (devre kesicili). İstenirse `ny_open_fvg_strategy.pine` olarak yeniden adlandır. |
| `asian_sweep_reversal_strategy.pine` | 502 | B1 (kritik), B2, B3, B4 (SPY), B5 (yuvarlama), B6, B7 | Asian tekil sürümün son hâli. |
| `ict_silver_bullet_strategy.pine` | 510 | B2 (kritik, limit giriş), B3 (yarım gün), B4 (limit dolum varsayımı; "round-turn" yorumlu `commission_value = 2.0` aslında taraf başı → $4 gidiş-dönüş), B6 (`justEntered` aynı bar giriş+çıkışı kaçırır), B7 | B1 yok: exit `position_size`'a bağlı. B5 mevcut. |
| `mean_reversion_bb_kc_rsi2_strategy.pine` | 424 | B2, B3 (yarım gün), B4 (yüzde komisyon yer tutucu), B5 (tam sayı yuvarlama yok), B6, B7 | B1 yok: sıfırlama bloğu giriş bloğundan önce. |
| `mtf_trend_continuation_strategy.pine` | 590 | B2, B3 (yarım gün), B6, B7, B8 | B1 yok. B4/B5 mevcut (MES). Başlıktaki "gerçek zamanlıda da kapanmış 1H barı görür" ifadesi yanlış (B8). |

Master, NY ve Asian tekil sürümlerinin birleşimidir; S1/S2 mantığı üç dosyada da aynı kalmalı. Bir düzeltme
mantığa dokunuyorsa üç dosyada tutarlı uygula.

B1/B2/B6 kalıbı (long gösterildi; short simetrik; Asian'a uyarla):
```pine
var float inAtrDist = na

// Entry block, right after strategy.entry("Long", ...):
float estStop = stopMethod == "ATR Based" and not na(atrInit) ? close - atrInit * atrMultInit : b - buffer
estStop := math.min(estStop, close - syminfo.mintick)
inAtrDist := close - estStop   // ATR method: distance re-applied to the real fill price
strategy.exit("Exit L", from_entry = "Long", stop = estStop, limit = close + rr * (close - estStop))
array.push(stopArr, estStop)   // B6: record at signal ...
array.push(dirArr, 1)

// Fill-bar finalize (existing `if na(inTakeProfit)` block):
//   rawStop = stopMethod == "ATR Based" ? avg - inAtrDist : inFvgStop
//   array.set(stopArr, array.size(stopArr) - 1, inInitialStop)   // ... refine at fill

// Reset ONLY after a trade has actually closed (never on the signal bar):
if strategy.position_size == 0 and strategy.closedtrades > strategy.closedtrades[1] and inDir != 0
    inDir := 0
    // ...clear the remaining management vars (inFvgStop, inInitialStop, inTakeProfit, inTrailStop, inAtrDist)
```

### 4.2 Faz 0 görevleri
0. Klasör git deposu değilse `git init`; dosyaların ilk hâli "baseline: TradingView originals" olarak commit'lenir.
1. §4.1'deki B-maddelerini `pine/` altındaki **tüm** stratejilere uygula (desen aynıysa; zaten çözülmüş maddeleri
   "mevcut değil" diye raporla; §4.1a'yı kontrol listesi olarak kullan). Her dosya ayrı commit.
2. Kapsam: Pine v5'te kal; B-maddeleri dışında mantık veya varsayılan değişikliği yok. Diğer bulgular yalnızca raporlanır.
   (2026-11-12: kapsam kullanıcı kararıyla §4.3'teki E-maddeleriyle genişletildi.)
3. `CHANGELOG.md` (dosya bazında, gerekçesiyle; sonunda "Açık bulgular").
4. `docs/tv-verification-checklist.md` — kullanıcı TradingView'da sırayla kontrol eder:
   - Script derleniyor (hata varsa mesaj ve satır numarası Claude Code'a iletilir).
   - Orijinal korunur: düzeltilmiş kod yeni bir script olarak "… v2" adıyla kaydedilir.
   - Aynı sembol, zaman dilimi ve tarih aralığında orijinal ve v2 için önce/sonra tablosu: işlem sayısı, kazanma %,
     net kâr, profit factor, maks DD, toplam R.
   - Grafikte turuncu Stop ve yeşil TP çizgileri görünüyor.
   - List of Trades'te SL ve TP çıkışları var (yalnızca "EOD close" / "Session end" değil).
   - Tablodaki "Tot R" 0 değil; tek tek işlemlerde R ≈ +rr veya ≈ −1 (trail ve seans sonu hariç).
   - Grafik geçmişinde bir yarım gün varsa (ör. 2025-11-28, 2025-12-24; ileride 2026-11-27) pozisyon o gün seans
     sonrasına taşınmıyor; geçmiş yetmiyorsa "test edilemedi" yazılır.
   - ABD kış saatine geçiş (2026-11-01) sonrası ilk günlerde OR 09:30 ET'de (17:30 TR) kuruluyor.
   - Maliyetli ve maliyetsiz sonuç farkı not edildi.
5. **Kabul:** kullanıcı checklist'i TV'de geçti ve önce/sonra tablosunu paylaştı.

### 4.3 Faz 0 genişletme — E-maddeleri (kullanıcı kararı, 2026-11-12)
TradingView doğrulamasından önce script'lerin nihai hâle gelmesi için `CHANGELOG.md` "Açık bulgular" 2–11 Faz 0'a alındı.
B-maddelerinden farklı olarak bunlar hata düzeltmesi değil, **kural ve varsayılan hizalamasıdır**; önce/sonra
karşılaştırmasında orijinalle fark beklenir. Ayrıntı ve gerekçe: `CHANGELOG.md`.

| ID | Kapsam | Değişiklik |
|---|---|---|
| E1 | Master | Bacak bazında dolum barı yeniden fiyatlaması (`strategy.opentrades.entry_price`); tekil dosyalarla parite (§3.1). |
| E2 | 6 dosya | `Risk base` girdisi; varsayılan `Initial Capital` (§8.1). |
| E3 | SB, MR | `riskPct` 1,0 → 0,5; günlük zarar kesicisi 4,0 / 3,0 → 2,0. |
| E4 | MR, SB, MTF | Günde en fazla 1 işlem (MR'ye sayaç; SB ve MTF varsayılanı 1). |
| E5 | 6 dosya | Giriş-zamanı kapısı (§3.1). SB: dolmamış limit pencerenin son barının kapanışında iptal. |
| E6 | 6 dosya | NY/Asian/Master'da yüzen günlük zarar kesicisi + `flattenBreach`; tüm zorunlu kapanışlar `immediately = true` + `exit` alert'i. |
| E7 | NY, Master S1, MTF | `trailMinStepTicks` (4): trailing ve `stop_update` yalnızca ≥ N tick iyileşmede. |
| E8 | MTF, SB | Tablo kapsamı (All / In-Sample / Out-of-Sample); SB panosunda Tot R. |
| E9 | 6 dosya | `strategy()` varsayılanları MES test profili (kontrat başı $0,50 taraf başı, 1 tick; sermaye 100.000, marjin %0); başlıkta enstrüman notları. |
| E10 | Doküman | `pine/README.md`, §3.4 semantiği, CHANGELOG, doğrulama listesi. |
| E11 | 6 dosya | Yürütme modeli bayrakları açıkça yazıldı (`calc_on_every_tick`, `calc_on_order_fills`, `process_orders_on_close` = `false`); davranış değişmez. |
| E12 | NY, Asian, Master, MR, MTF | `backtest_fill_limits_assumption = 1`: TP limitleri fiyat seviyeyi 1 tick geçince dolar (Silver Bullet'ta zaten vardı). |

## 5. Faz 1 — TradingView → Telegram sinyal botu (emir yok)

### 5.1 Alert yükü (Pine `alert()` → JSON)
| Alan | Tip | Açıklama |
|---|---|---|
| `v` | int | şema sürümü (1) |
| `token` | str | yalnızca webhook'a özel rastgele değer (login bilgisi değil) |
| `strategy_id` | str | `ny_orb_fvg`, `asian_sweep`, `ict_silver_bullet`, `mr_bb_kc_rsi2`, `mtf_trend_cont` (Master: S1 = `ny_orb_fvg`, S2 = `asian_sweep`) |
| `symbol` | str | `syminfo.tickerid` |
| `tf` | str | `timeframe.period` |
| `bar_time` | int | sinyal barının açılış zamanı, epoch ms (`time`) |
| `action` | str | `entry` · `stop_update` · `exit` · `cancel` (bekleyen limit girişi geri çek) · `heartbeat` |
| `side` | str | `long` · `short` |
| `setup` | str | `FVG` · `IFVG` · `SWEEP_FVG` · `SB_FVG` · `BB_KC_RSI2` · `MTF_PULLBACK` (`stop_update`/`exit`'te boş) |
| `order_type` | str | `market` · `limit` (limit: `est_entry` = limit fiyatı; iptalde `cancel` gelir) |
| `est_entry` | float | sinyal barı kapanışı |
| `stop`, `tp` | float | seviye |
| `stop_dist_pts` | float | `abs(est_entry - stop)` |
| `rr` | float | |

Fiyatları `str.tostring(x, format.mintick)` ile yaz; olmayan sayısal alan `null` (ör. `exit`'te `stop`/`tp`). Strateji başına TradingView'da bir alarm: koşul
"alert() function calls only", webhook URL'si alıcı adresi.

### 5.2 Alıcı
- `POST /tv/webhook` (FastAPI). Doğrula → kuyruğa yaz → **hemen 200** (TradingView 3 sn'de yanıt alamazsa isteği iptal eder).
  İşleme asenkron.
- Doğrulama sırası: kaynak IP (ufw + uygulama), token (`hmac.compare_digest`), şema (pydantic, gövde ≤ 4 KB), tazelik
  (`now − (bar_time + 60 s) ≤ 90 s`, aksi halde `STALE` kaydı + bildirim), idempotency
  (`UNIQUE(strategy_id, symbol, bar_time, action)`).
- Boyut önerisi: venue profili + `risk.yaml` ile lot hesabı (yalnızca öneri; Faz 1'de emir yok).

### 5.3 Güvenlik katmanları
1. ufw: 443'e yalnızca TradingView kaynak IP'leri; SSH yalnızca anahtar ile.
2. Caddy: otomatik TLS. TradingView istemci sertifikası (mTLS) doğrulaması destekleniyorsa ekle (VERIFY).
3. Payload token'ı (`.env`, kolay rotasyon). Broker/Telegram kimlik bilgisi asla payload'da yok.
4. Rate limit (dakikada ≤ 30 istek), gövde boyutu sınırı, yapılandırılmış log (token maskeli).

### 5.4 Telegram
- Yalnızca `TELEGRAM_ALLOWED_CHAT_ID`; diğer sohbetler yok sayılır ve loglanır.
- Mesaj şablonu (Türkçe):
```
[SİNYAL] {strategy} · {setup} · {LONG/SHORT} · {symbol}
Referans giriş ≈ {est_entry} (bar kapanışı)
Stop {stop} (−{stop_dist_pts} puan) · TP {tp} ({rr}R)
FTMO'da uygula: giriş anındaki US500 fiyatına göre stop −{stop_dist_pts}, TP +{rr × stop_dist_pts} puan
Önerilen boyut: {lots} lot · risk ≈ ${risk_usd} (%{risk_pct}) · günlük bütçe ${used}/${cap}
Geçerlilik: {valid_until TR} ve fiyat sapması ≤ {0.25 × stop_dist_pts} puan — aşıldıysa ATLA
id {signal_id} · bar {NY saati} / {TR saati}
```
- Komutlar (ASCII): `/durum`, `/pnl`, `/risk`, `/duraklat`, `/devam`, `/kill` (onay kodu ister), `/mod`, `/saglik`.
- Telegram hatası motoru asla bloklamaz (kuyruk + üstel geri çekilme).

### 5.5 Heartbeat ve alarm süresi
- Ayrı küçük bir Pine göstergesi saatte bir `{"action":"heartbeat"}` gönderir. İzlenen pencerelerde 75 dk sessizlik →
  Telegram: "TradingView alarm hattı sessiz".
- Premium altı planlar için `config/tv_alerts.yaml`'da alarm oluşturma tarihleri; bitişten 7 gün önce hatırlatma.

### 5.6 Dağıtım
Ubuntu sunucu (küçük VPS; FTMO VPS'e genel olarak izin veriyor, Faz 1'de zaten emir yok) · Caddy · ufw ·
systemd (`Restart=always`) · chrony · log rotasyonu.

### 5.7 Kabul
- 10 işlem günü kesintisiz; kaçan/çift sinyal 0 (TradingView Alert Log ile karşılaştırma); uçtan uca medyan gecikme < 5 sn.
- Tüm doğrulama hata yolları testli; `/kill` ve yetkisiz sohbet testleri yeşil.

## 6. Faz 2 — Python motoru ve backtest

> **Ön dilim (kullanıcı kararı, 2026-11-13):** proje iskeleti, `config/` şemaları (§8.1, §8.3) ve Monte Carlo prop
> simülatörü (§6.5) Faz 0 kabulü ve Faz 1 beklenmeden yapıldı; fiyat verisi ve derlenmiş Pine tabanı gerektirmezler.
> §6.1–6.4 ve §6.6, veri ve doğrulanmış Pine tabanı gelince "Faz 2'ye geç" kararıyla yapılır; §6.7 kabulü o zaman aranır.

### 6.1 Veri
- İçe aktarma: CSV/Parquet (MT5 "Bars → Export", TradingView grafik verisi dışa aktarımı, tedarikçi verisi).
  MT5 bar saatleri **sunucu saatindedir** → `server_tz` ile UTC'ye çevir (VERIFY: terminalde bilinen bir 09:30 NY mumuyla karşılaştır).
- Doğrulama raporu: çift bar, sıra dışı bar, gün içi boşluklar, seans kapsaması, DST geçişleri, aykırı fiyat sıçramaları.
- Veri kaynağı ve kapsama tarihleri her rapora yazılır.

### 6.2 Dolum modeli
Piyasa emri: sonraki bar açılışı + `slippage_ticks`. Stop: değdiği an stop fiyatı (boşlukla açılışta açılış fiyatı) + slipaj.
Limit: fiyat seviyeyi 1 tick geçince limit fiyatından (slipaj yok; Pine `backtest_fill_limits_assumption = 1` ile aynı). Aynı barda SL+TP → §3.1.

### 6.3 Maliyet modeli (venue/enstrüman bazlı, config)
CFD: spread (sabit ya da saat dilimine göre tablo; NY açılışında genişleme) + slipaj + komisyon (varsa).
MES/ES: kontrat başı taraf başı nakit + 1 tick slipaj. Maliyetsiz koşu yalnızca karşılaştırma içindir ve raporda kırmızı uyarı taşır.

### 6.4 Analitik
R ve $ bazında: işlem sayısı, kazanma %, ortalama R, beklenti, profit factor, maks DD (R ve %), en uzun kayıp serisi,
beklentinin t-istatistiği, IS/OOS ayrımı, hafta günü ve saat tabloları (Pine tablosuyla aynı tanım), aylık tablo,
walk-forward (kayan pencereler). Parametre optimizasyonu varsayılan olarak kapalı; açılırsa yalnızca IS'te ve deneme sayısı raporlanır.

### 6.5 Monte Carlo prop simülatörü
- Girdi: giriş/çıkış zamanlı işlem R listesi (CSV; biçim `docs/monte-carlo.md`), venue profili, risk %, maliyetler,
  isteğe bağlı rastgele işlem atlama olasılığı. Gün bazında blok bootstrap (yerine koyarak): aynı prop gününün işlemleri
  birlikte örneklenir; işlemsiz hafta içi günler de havuzdadır.
- Kurallar: günlük kayıp (reset saati ve baz), toplam kayıp (statik / gün sonu trailing), hedef(ler), minimum gün, faz sırası.
  İhlal özsermayeyle denetlenir: işlem içi yüzen zarar varsayılan olarak stop'a kadar (−1R) sayılır (`mae_r` varsa o).
  Hedef yalnızca açık pozisyon yokken sayılır. Gün sonu trailing toplam kayıp henüz desteklenmez (profilde formül yok).
- Çıktı: P(faz 1), P(iki faz), P(ihlal) ve faz × neden dağılımı, sonuçlanmayan yol oranı, geçme süresi dağılımı
  (piyasa günü), maks. düşüşün %95 / %99 dilimleri ve beklenen kuyruk kaybı (ES). Modelleme kararları: ADR-0002.
- Sağlık testi: E = 0, RR 1:2 durumunda sonuç analitik sınır-geçme olasılığıyla tutarlı (tolerans testte tanımlı).

### 6.6 Parite testi
Aynı barlar üzerinde: TradingView'dan (Faz 0 sonrası Pine) grafik verisi + "List of Trades" dışa aktar → Python aynı
barlarda çalışır → giriş zamanı / yön / çıkış nedeni eşleşmesi ≥ %98; fiyat toleransı 1 tick (§3.1'deki TP farkı hariç).
Uyuşmazlıklar tek tek raporlanır. Altın dosyalar `tests/parity/golden/`.

### 6.7 Kabul
Parite raporu ≥ %98; veri doğrulama raporu temiz; Go/No-Go raporu (§9) tek komutla üretiliyor.

## 7. Faz 3 — MT5 köprüsü, paper ve approve (yalnızca demo hesap)

### 7.1 Spike (ADR-0001)
Ubuntu'da MetaQuotes'un resmi Linux (Wine) kurulumuyla MT5 (alternatif: Windows VPS). MQL5 ↔ Python seçenekleri:
(a) MQL5 yerel soket → 127.0.0.1, (b) `WebRequest` → yerel HTTP, (c) ortak `Files` klasöründe JSON dosya köprüsü,
(d) Python `MetaTrader5` paketi (Windows Python gerektirir; Wine altında kırılgan).
Her biri için: kurulum zorluğu, gecikme (p50/p99), yeniden başlatma davranışı, hata modları. En basit güvenilir seçenek seçilir.

### 7.2 Bridge EA (ince)
- Her yeni M1 barında kapanan barı gönderir (OHLCV + sunucu saati); hesap/pozisyon durumunu raporlar.
- Python'dan gelen niyeti `client_id` ile tekilleştirir; emri SL ve TP **ile birlikte** gönderir; sonucu raporlar.
- Python 30 sn sessizse yeni giriş kabul etmez. Seans sonu düzleştirmesini Python'dan **bağımsız** kendi zamanlayıcısıyla
  da uygular (çift katman).
- Sembol özelliklerini (tick boyutu, tick değeri, min lot, lot adımı) terminalden okur ve gönderir; Python bunları sabit kodlamaz.
- Kendi istek sayacı: günlük bütçe aşılırsa yalnızca kapatma işlemlerine izin verir.

### 7.3 Mutabakat
Her 10 sn: broker pozisyonları ↔ beklenen durum. Uyuşmazlık → yeni giriş yok + Telegram alarmı + (config) düzleştir.

### 7.4 Modlar
`paper`: canlı barlar, sanal dolum. `approve`: demo hesapta gerçek emir; her emir için Telegram inline [Onayla]/[Atla];
60 sn zaman aşımı = atla; onay anında geçerlilik kontrolü (§5.4).

### 7.5 Kabul
Demo hesapta ≥ 8 hafta ve ≥ 60 işlem; son 4 haftada operasyonel olay 0 (kaçan/çift/korumasız emir);
canlı sonuçlar backtest'in %95 güven aralığında.

## 8. Risk motoru ve venue profilleri

### 8.1 Kurallar (kodda zorunlu)
- `risk_per_trade_pct` varsayılan 0,25; kod tavanı 0,5. Başlangıç bakiyesi üzerinden.
- Günlük bütçe = `min(daily_cap_pct, 0,5 × daily_loss_limit_pct) × initial_balance`.
- Ön kontrol: `realized_today + open_risk + new_risk + cost_buffer ≤ günlük bütçe`; değilse RED.
- Günlük taban = reset anındaki `max(balance, equity) − daily_loss_limit`. Kalan günlük tampon
  `flatten_when_daily_buffer_below_pct` altına inerse: tüm pozisyonları kapat, gün kapanır.
- Toplam kayıp: statik → `initial × (1 − max_loss_pct)`; gün sonu trailing → profilde tanımlı formül.
- Boyut: `lots = floor_to_step(risk_usd / (stop_points × value_per_point_per_lot))`; `< min_lot` → işlem atla
  (`RISK_TOO_WIDE`); asla yukarı yuvarlama.
- Strateji başına `max_consecutive_losses` → bir sonraki işlem günü durdur + rapor. Haftalık `weekly_cap_pct` → manuel `/devam`'a kadar dur.
- Eşzamanlılık: strateji başına 1, toplam `max_positions_total`.
- Haber: profil `news_rule`; kaynak `data/news.yaml` (haftalık elle güncellenir; site kazıma yok).
- `rules_verified_at` boş veya > 30 gün → `signal`/`paper` dışındaki modlar kilitli.
- Config yüklenirken doğrulanır (`src/propbot/risk`): tavanı aşan değer, yanlış tip ya da bilinmeyen anahtar **yükleme
  hatasıdır** (sessizce kırpılmaz); `null` yalnızca VERIFY / çalışma-anı alanlarında kabul edilir ve raporlanır (ADR-0003).

### 8.2 Zaman eşlemesi (Türkiye UTC+3, yaz saati yok)
| Olay | ABD kış saati (EST) | ABD yaz saati (EDT) |
|---|---|---|
| NY açılış 09:30 ET | 17:30 TR | 16:30 TR |
| NY strateji sonu 15:59 ET | 23:59 TR | 22:59 TR |
| Asya aralığı 20:00–00:00 ET | 04:00–08:00 TR | 03:00–07:00 TR |
| Asya aktif 03:00–11:00 ET | 11:00–19:00 TR | 10:00–18:00 TR |

FTMO günlük reset (CE(S)T gece yarısı varsayımı, VERIFY): CET'te 02:00 TR, CEST'te 01:00 TR. ABD ve AB yaz saati
geçişleri farklı haftalarda → bu haftalar için ayrı testler. Asya aktif ve NY işlemleri aynı prop gününe düşer →
günlük bütçe birlikte hesaplanır.

### 8.3 Venue profili
Örnek: `config/venues/ftmo_2step_100k.yaml`. Sembol özellikleri profilde boş bırakılır; çalışma anında terminalden okunur.

## 9. Go/No-Go kapısı (ücretli sınav öncesi; hepsi şart)
- **G1** ≥ 2 yıllık 1m veri, işlem yapılacak mekanın (veya eşdeğer) beslemesinde; veri raporu temiz.
- **G2** Her strateji için OOS'ta maliyet sonrası beklenti > 0 ve t-istatistiği ≥ 2,0.
  (RR 1:2'de σ ≈ 1,45R → +0,2R beklenti için ~220, +0,1R için ~840 işlem gerekir.)
- **G3** OOS beklentisi ≥ IS beklentisinin %50'si; gün/saat filtreleri yalnızca IS'te seçildi.
- **G4** Monte Carlo: seçilen risk ve venue kurallarıyla P(iki fazı geçme) ≥ %60 ve P(ihlal) ≤ %20.
- **G5** Demo forward (Faz 3): §7.5 sağlandı.
- **G6** Parite ≥ %98 (§6.6).
- **G7** `rules_verified_at` ≤ 30 gün; gereken yerde firmadan yazılı otomasyon onayı `docs/compliance/` altında.
Rapor `reports/go_no_go_YYYYMMDD.md`; her madde PASS/FAIL ve kanıt bağlantısıyla.

## 10. Zorunlu testler
- **Strateji birim:** OR kurulumu; bias (Close/Wick); boğa/ayı FVG + min boyut; IFVG geçişleri ve ölüm; en yeniden eskiye
  tarama; aktivasyon barı kuralı; Asya penceresinin gece yarısını sarması; arming → sweep → giriş sırası; invalidasyon;
  günde ≤ 1 işlem.
- **Takvim:** ABD DST 2026-03-08 ve 2026-11-01; AB DST 2026-03-29 ve 2026-10-25; yarım gün 2026-11-27 ve 2026-12-24;
  tatiller; eksik barlar.
- **Property (hypothesis):** stop daima zarar tarafında ve ≥ 1 tick uzakta; risk hiçbir zaman bütçeyi aşmaz; seans sonunda
  açık pozisyon yok; pozisyon sayısı limitleri.
- **Risk:** günlük bütçe matematiği, reset saat dilimi, taban/düzleştirme, ardışık kayıp ve haftalık duraklama,
  mod kilitleri (`auto` hiçbir koşul eksikken açılamaz).
- **Alıcı:** yanlış token, yanlış IP, bayat bar, çift istek, bozuk JSON, büyük gövde, 3 sn yanıt süresi.
- **Telegram:** yetkisiz sohbet, `/kill` onayı, ağ hatası motoru bloklamıyor.
- **Köprü (Faz 3):** bağlantı kopması, pozisyon açıkken yeniden başlatma, çift emir engeli, mutabakat uyuşmazlığı.
- **Monte Carlo:** E = 0 sağlık testi, sabit tohumla tekrarlanabilirlik.
- **Parite:** altın dosyalar.

## 11. Operasyon (özet; ayrıntı `docs/runbook.md`)
- **Uçuş öncesi kontrol** (NY açılıştan 60 dk önce, otomatik): `rules_verified_at`, saat sapması, köprü heartbeat'i,
  veri tazeliği, `news.yaml` bu hafta güncel mi, günlük bütçe sıfırlandı mı, disk. Herhangi biri FAIL → mod `signal`'e
  düşer + Telegram.
- **Gün sonu raporu:** işlemler, R, $, kullanılan günlük bütçe, prop tamponları.
- **Haftalık:** SQLite yedeği, `news.yaml` hatırlatması, TradingView alarm süresi kontrolü.
- **Olay:** `/kill` → tüm pozisyonları kapat + yeni giriş kilidi; olay notu `docs/incidents/`.
