# PropBot — Teknik Spesifikasyon

Sürüm 0.4 · 2026-11-19 (Faz 2 uygulandığı hâliyle; 2026-11-19 kullanıcı kararları işlendi) · Sahibi: Oğuz
`CLAUDE.md`'nin ayrıntısıdır. Sayı içeren her prop kuralı **VERIFY** etiketlidir: kullanılmadan önce firmanın
resmi sayfasından doğrulanır ve venue profilindeki `rules_verified_at` güncellenir.

---

## 1. Doğrulanmış bağlam (2026-11-01)

- **Topstep:** Türkiye, resmi uygunluk sayfasında "işlem yapamaz / ödeme alamaz" ülkeler listesinde (4 Eylül 2026). Kapsam dışı.
- **FTMO:** EA ve algoritmik işlem genel olarak serbest. Yasak: HFT, arbitraj, simülasyon açığı istismarı; günde 2.000'den
  fazla sunucu isteği yapan "hiperaktif" EA'lar; aynı işlem kalıbının birden çok hesapta tekrarı. VPN/VPS genel olarak
  serbest (MetaTrader/cTrader'da ABD konumu hariç).
- **FTMO 2-Step (doğrulandı 2026-11-13; kayıt `docs/compliance/2026-11-13-ftmo-kural-dogrulamasi.md`):** hedef %10 → %5;
  günlük kayıp %5 (limit = 00:00 Prag saatindeki **bakiye** − başlangıcın %5'i, özsermayeyle ölçülür); toplam kayıp %10
  statik; fazlarda en az 4 işlem günü. Haber kısıtı (±2 dk) yalnızca fonlu Standard hesapta. MT5 sunucu saati GMT+2,
  ABD yaz saati takvimiyle GMT+3 (New York + 7 saat).
- **FTMO 1-Step (bilgi, 2026-11-13):** hedef %10, günlük kayıp %3, toplam %10 gün sonu trailing; 12 Ekim 2026'dan sonraki
  siparişlerde "Best Day" yerine "Consistency" kuralı.
- **Funding Pips:** üçüncü taraf EA yalnızca trade/risk yöneticisi olarak serbest → kendi strateji otomasyonumuz için
  firmadan yazılı onay şart.
- **TradingView webhook:** ücretli plan + 2FA; yalnızca port 80/443; IPv4; 3 sn içinde yanıt yoksa istek iptal; dört sabit
  kaynak IP (listeyi her kurulumda TradingView destek sayfasından kopyala, bu belgeden değil). Değerler `config/tv_alerts.yaml`'da
  durur (2026-11-13'te kullanıcı resmi "About webhooks" sayfasından aktardı; mTLS desteklenmiyor).
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

### 2.2 Çekirdek tipler (uygulandığı hâliyle, 2026-11-18)
- **Bar ve seri** (`src/propbot/core/bars.py`): `Bar(ts_open_ms, open, high, low, close, volume)` — zaman damgası bar
  **açılış** anı, UTC epoch ms (TradingView `time` ile aynı). Sembol ve bar süresi seriye aittir:
  `BarSeries(symbol, tf_seconds, bars, source)`, açılış anına göre kesin artan. `DataSource(name, format, sha256,
  synthetic, bar_tz)`: dosya adı, biçim (`generic` · `mt5` · bellekte üretilen seri için `synthetic`), dosya
  baytlarının sha256'sı, **sentetik** işareti (ADR-0007) ve duvar saatlerinin hangi dilimle okunduğu.
- **Enstrüman** (`core/instrument.py`): `Instrument(symbol, kind, tick_size, point_value, qty_step, min_qty)`;
  `floor_qty` boyutu adıma aşağı yuvarlar, asla yukarı. Değerler `config/backtest.yaml` profilinden.
- **Strateji arayüzü** (`src/propbot/strategies/base.py`, ADR-0008): `on_bar(bar, ctx) → [Entry | Exit | Cancel |
  Close | CloseAll]` ve `on_fill(fill, ctx) → [Exit | Cancel]`; `ctx` (`StrategyContext`) salt-okunur hesap
  görünümüdür (Pine `strategy.position_size`, `position_avg_price`, `opentrades`, `closedtrades`, `equity`,
  `netprofit` karşılıkları, ayrıca enstrüman ve bar süresi). Kayıtlar (`src/propbot/backtest/model.py`):
  `TradeRecord` = bir çıkış dolumu (List of Trades satırı; kısmi çıkış ayrı satır, çıkış nedeni `stop` · `target` ·
  `close` · `end_of_data`); `EntryResult` = bir girişin bütün çıkışları (istatistik birimi).
- **Faz 3'e kalanlar:** `Signal`, `OrderIntent(client_id, …)`, `Fill`, `Position`, `AccountState` ve
  `MarketDataFeed` / `ExecutionAdapter` protokolleri henüz yoktur; canlı yürütme yolunda tanımlanacaklar. Faz 1'in
  sinyal kaydı `signals/schema.py` (`TvAlert`) ve `store/db.py`'dedir.

### 2.3 Bar döngüsü
**Backtest (uygulandı, `src/propbot/backtest/engine.py`):** her bar için (1) açılış: açılışın zaten geçtiği stop ve
limitler açılış fiyatından dolar (boşluk kuralı), sonra bekleyen piyasa girişleri açılıştan dolar; (2) bar, dolum
modelinin seçtiği sırayla iki uca ve kapanışa yürünür, emirler fiyatın ulaştığı sırayla tetiklenir (aynı fiyatta
önce stop, sonra hedef, sonra giriş); (3) kapanış: strateji `on_bar` ile kapanışta hesabı görür, eylemleri sırayla
uygulanır (`Close` / `CloseAll` bu kapanışta, gerisi sonraki bardan itibaren). Seans sonu bayrağı veriden türetilir
(§6.1). Değişmez kural 2 her barda denetlenir: stop'suz dolan giriş ya da stop'u zarar yönüne taşıyan bir parti
koşuyu `EngineError` ile durdurur.
**Canlı (Faz 3):** her kapanan 1m bar: bar doğrulama (sıra, boşluk, çift kayıt, saat sapması) → takvim/seans durumu →
`strategy.on_bar` → risk motoru (veto/boyut) → yürütme → kayıt + bildirim. Strateji kodu saat, dosya, ağ ya da ortam
değişkeni okumaz; her şey `ctx` ile gelir (iki döngüde de aynı strateji kodu).

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
  **Hangi risk%** (kullanıcı kararı 2026-11-19, ADR-0010): script'lerin risk girdisinin varsayılanı 0,5'tir ve yalnızca
  tekil strateji varsayımıdır. Canlı çalışma ve portföy düzeyindeki her değerlendirme `config/risk.yaml` →
  `risk_per_trade_pct` (0,25) ile yapılır: karar koşusu bu değeri betiğin risk girdisine (`risk_pct`; MTF'de
  `risk_percent`) yazar. Betiğin kendi değeri yalnızca parite koşusunda (§6.6) ve `propbot backtest --pine-risk`
  karşılaştırma koşusunda kullanılır. R boyuttan bağımsız olmadığı için (yuvarlama, atlanan işlem, yüzde kesicileri)
  iki riskle üretilen işlem listeleri aynı liste değildir. Planlanan risk başlangıç bakiyesinin yüzdesidir: risk
  tabanı `config/strategies.yaml`'da `Initial Capital` dışında bir değere çekilmişse karar koşusu yapılamaz (§6.4).
- Strateji başına günde en fazla 1 işlem (tüm script'lerde varsayılan). Seans sonunda pozisyon kesinlikle kapalı
  (takvim tabanlı; yarım günler dahil).
- **Zorunlu kapanışlar** (seans/pencere sonu, günlük zarar ihlali, tarih aralığı sonu) koşulun görüldüğü barın
  **kapanışında** dolar (Pine `immediately = true`) ve `exit` alert'i üretir; bar başına tek neden, tek emir.
- **Günlük zarar kesicisi (Pine içi, varsayılan %2):** gün içi zarar — gerçekleşen ya da bar kapanışı özsermayesiyle
  yüzen — eşiğe ulaşınca gün için yeni giriş yok ve (varsayılan) açık pozisyon kapatılır. Bar kapanışında
  değerlendirilir; bar içi ihlal ancak bar kapanınca görülür. Botun bağlayıcı günlük bütçesi §8.1'dir.
- **Betik bazında istisnalar** (Faz 2 portları ve denetimleri, 2026-11-18). Yukarıdaki ortak kurallarla bir script'in
  kodu ayrıldığında **Pine kodu kazanır** ve Python portu onu izler (parite, ADR-0007 madde 1). Satır numaraları Pine
  referansı `144abab`'a göredir; ayrıntı ve Pine bulguları `CHANGELOG.md` açık bulgular 59–64.
  - *Silver Bullet:* tahmini giriş sinyal barı kapanışı değil **limit seviyesidir**; boyut `f_qty(ent, stp)` ile
    limit seviyesinden (`ict_silver_bullet_strategy.pine` 396–404), TP limit seviyesinden fiyatlanır. Dolumda yeniden
    fiyatlama yoktur: yapısal stop (`sweepExtreme ± tickOffset`) ve TP her barda değişmeden yeniden verilir (448–451);
    "stop dolumun en az 1 tick zarar tarafında" kuralı uygulanmaz (CE girişinde risk 0,5 tick olabilir).
  - *Mean Reversion:* stop dolumda `position_avg_price ∓ atrMult × atrStored` olarak kesinleşir ve dolumun 1 tick
    zarar tarafına **zorlanmaz** (`mean_reversion_bb_kc_rsi2_strategy.pine` 303–310); lehte boşlukla dolan işlemde
    stop sinyal barındaki tahminden geniş olabilir.
  - *Boyut kapısı:* "aşağı yuvarla, < 1 ise atla" yalnızca tam birim modunda geçerlidir. "Fractional" (ya da vadeli
    olmayan sembolde "Auto") modunda script'ler küsuratlı boyutu `qty > 0` ile işler: NY 421/457, Asian 396/419
    (başlık 48 ve ipucu 124 "< 1 atla" der), Master 582/614/727/748, Silver Bullet 250 ve 401 (`roundToInt`
    kapalıyken), MR 176–178, MTF 373–374 / 401–402 (`roundUnits` kapalıyken). Portlar boyutu ayrıca enstrümanın lot
    adımına aşağı yuvarlar (`Instrument.floor_qty`) ve 0 ise atlar: varsayılan olmayan bu modlarda Python boyutu daha
    küçük olabilir.
  - *NY ORB — gün sıfırlaması:* yönetim durumu (`inDir`, stop/TP alanları) yeni günde yalnızca pozisyon
    yokken sıfırlanır (`ny_open_fvg_strategy.pine` 226–232); grafiğin ilk barı yeni gün sayılmaz (`ta.change` ilk
    barda na; 213). OR ayrıca `not na(orHigh)` ve `curMin <= sessEnd` ister (240); seans sonu kapanışı `closeAtEnd`'e
    bağlıdır (523). Wick modunda iki tarafı da kıran barda önce yukarı denenir (338–341).
  - *Günlük kesicinin ilk günü (NY, Asian, Master):* gün başı özsermaye ve gün başı net kâr ilk gün değişimine kadar
    na'dır; grafiğin ilk (kısmi) gününde kesici devre dışıdır (NY 305–307 ve 322–326, Master 476–480). Gün başı değeri
    günün **ilk barının kapanışında** alınır; o barda oluşan K/Z o günün kayıp hesabına girmez.
  - *MTF:* gün başı özsermaye günün ilk barının kapanışındaki `strategy.equity`'dir, açık K/Z dahil
    (`mtf_trend_continuation_strategy.pine` 296–297); başa baş biten işlem (`tradeNet == 0`) ardışık zarar sayacında
    **zarar** sayılır (314–318).
  - *MTF ısınma — beklenen güvenli sapma (kullanıcı onayı 2026-11-19):* Pine'da trail göstergeleri henüz na iken (ilk
    `chLen` / `atrTrailLen` bar) `math.max(be, na)` na verir ve runner **stop'suz** kalır (449, 465). Port bu durumda
    adayı başa baş alır (değişmez kural 2). Yalnızca serinin ilk ~22 barında 1,5R'ye ulaşan işlemi etkiler. Kullanıcı
    Python'un bu davranışını onayladı (prop risk yönetimi açısından güvenli olan budur): port değişmez, Pine dosyası da
    değişmez. Parite raporu ve artefaktı bu stratejide "Beklenen güvenli sapma" başlığıyla bunu yazar; sapma **tolerans
    değildir**, o penceredeki farklı dolumlar yine uyuşmazlık sayılır ve ısınma `--warmup-bars` ile dışarıda bırakılır
    (§6.6).
  - *Master:* S2 silahının geçersizleşmesi giriş kapılarından bağımsız çalışır (`master_prop_firm_portfolio.pine`
    708–712): durdurmada, günün işleminden sonra ya da öbür bacak engellerken de silah ölür. Bacak durumu kapanan
    işlem sayısından değil açık işlem taramasından sıfırlanır.

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
  sırası; Faz 2 denetimi: aynı barda ayı ve boğa FVG geometrik olarak oluşamaz, kural fiilen işlemez); yeni arming
  eskisinin üzerine yazar.
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
- **Alert:** S1 → `master_ny_orb_fvg`, S2 → `master_asian_sweep` (tek TradingView alarmı yeterli). Kimlikler bağımsız
  NY Open / Asian Sweep script'lerinkinden ayrıdır (E14): Master ile bağımsız script'ler aynı sembolde yan yana
  alarmlanabilir.
- **Değerlendirme (kullanıcı kararı 2026-11-19; açık bulgu 72 kapandı):** Go/No-Go'da Master hem iki bacağı üzerinden
  (`master_ny_orb_fvg`, `master_asian_sweep`) hem de birleşik koşusu üzerinden (`master_portfolio`) değerlendirilebilir;
  ikisi de kabul edilir. Bir raporda yalnızca biri verilebilir (uygulamanın getirdiği sınır, kararın parçası değil;
  §9.1). Planlanan risk (§3.1) iki motorun ortak `riskPct` girdisine bir kez yazılır; `maxConcurrentRiskPct` (1,0)
  betiğin kendi girdisi olarak kalır.

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
karşılaştırmasında orijinalle fark beklenir. Ayrıntı ve gerekçe: `CHANGELOG.md`. E13–E14, Faz 1 kabul koşusundan önce
verilen 2026-11-16 kullanıcı kararlarıdır; backtest sonuçlarını değiştirmezler (bir girdi görünürlüğü, bir alert kimliği).

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
| E13 | 6 dosya | Webhook token girdisi `display = display.none` ile tanımlı: değer grafiğin durum satırında yazmaz (kullanıcı kararı 45, 2026-11-16). Mantık ve varsayılan değişmez. |
| E14 | Master | Alert'lerde bacak kimlikleri `master_ny_orb_fvg` / `master_asian_sweep` (kullanıcı kararı 47, 2026-11-16). Yalnızca `f_alert` çağrılarının kimlik argümanı; giriş/çıkış mantığı değişmez. |

## 5. Faz 1 — TradingView → Telegram sinyal botu (emir yok)

> Uygulama durumu (2026-11-16): kod `src/propbot/signals/`, `notify/`, `store/`, `risk/sizing.py`; kararlar ADR-0000,
> ADR-0004, ADR-0005, ADR-0006; kurulum ve işletim `docs/runbook.md`. Bu bölüm uygulanan davranışı anlatır
> (2026-11-16 kullanıcı kararları 32, 33, 45, 46, 47, 51, 53 dahil; ayrıntı `CHANGELOG.md`).

### 5.1 Alert yükü (Pine `alert()` → JSON)
| Alan | Tip | Açıklama |
|---|---|---|
| `v` | int | şema sürümü (1) |
| `token` | str | yalnızca webhook'a özel rastgele değer (login bilgisi değil) |
| `strategy_id` | str | `ny_orb_fvg`, `asian_sweep`, `ict_silver_bullet`, `mr_bb_kc_rsi2`, `mtf_trend_cont`; Master bacakları: S1 = `master_ny_orb_fvg`, S2 = `master_asian_sweep`; heartbeat göstergesi: `heartbeat` |
| `symbol` | str | `syminfo.tickerid` (düz `BORSA:SEMBOL`; harf, rakam, `_ . : ! -`, en çok 64 karakter). Giriş için venue profilindeki `signal.tv_symbols` listesinde olmalı (§5.2) |
| `tf` | str | `timeframe.period`; yalnızca gün içi: dakika `1`–`1440`, saniye `1S`–`59S` |
| `bar_time` | int | sinyal barının açılış zamanı, epoch ms (`time`) |
| `action` | str | `entry` · `stop_update` · `exit` · `cancel` (bekleyen limit girişi geri çek) · `heartbeat` |
| `side` | str | `long` · `short` |
| `setup` | str | `FVG` · `IFVG` · `SWEEP_FVG` · `SB_FVG` · `BB_KC_RSI2` · `MTF_PULLBACK` (`stop_update`/`exit`'te boş olabilir) |
| `order_type` | str | `market` · `limit` (limit: `est_entry` = limit fiyatı; iptalde `cancel` gelir) |
| `est_entry` | float | `entry`: sinyal barı kapanışı (limit: limit fiyatı) · `stop_update`: TradingView ortalama dolumu · `exit`: bar kapanışı |
| `stop`, `tp` | float | seviye |
| `stop_dist_pts` | float | `abs(est_entry - stop)`; bilgi amaçlı, alıcı mesafeyi kendisi hesaplar |
| `rr` | float | bilgi amaçlı (girdi değeri ya da iki ondalıklı oran; `0` ya da `null` olabilir); mesajdaki R, seviyelerden hesaplanır |
| `reason` | str | isteğe bağlı (v1 uzantısı, `^[a-z_]{1,32}$`); Pine dosyaları henüz göndermez (açık bulgu 15) |

Fiyatları `str.tostring(x, format.mintick)` ile yaz; olmayan sayısal alan `null` (ör. `exit`'te `stop`/`tp`). Strateji başına TradingView'da bir alarm: koşul
"alert() function calls only", webhook URL'si alıcı adresi. **Bir `strategy_id`'nin (sembol başına) tek kaynağı olur:**
aynı script aynı sembolde iki grafikte alarmlanmaz. Master'ın bacakları kendi kimliklerini gönderir (E14, kullanıcı
kararı 47): Master ile bağımsız NY Open / Asian Sweep script'leri aynı sembolde **yan yana** alarmlanabilir; her biri
ayrı işlem olarak iletilir ve günlük bütçede ayrı sayılır. Sembolün verisi gerçek zamanlı olmalıdır; gecikmeli veride
her giriş bayattır.

Kurallar (`src/propbot/signals/schema.py`): bilinmeyen anahtar, yinelenen anahtar, `NaN`/`Infinity`, sayı yerine metin ya
da `true/false` reddedilir. `entry`: `side`, `setup`, `order_type`, `est_entry`, `stop` zorunlu; stop zarar tarafında,
(varsa) TP kâr tarafında. `stop_update`: `side`, `est_entry`, `stop` zorunlu (stop girişe eşit ya da kâr tarafında
olabilir). `heartbeat`: yalnızca `v`, `token`, `strategy_id`, `symbol`, `tf`, `bar_time`, `action`. Günlük ve üstü zaman
dilimleri reddedilir: bar kapanışı seans takvimine bağlıdır, tazelik denetlenemez.

### 5.2 Alıcı
- `POST /tv/webhook` (FastAPI). Doğrula → kuyruğa (SQLite) yaz → **hemen 200** (TradingView 3 sn'de yanıt alamazsa isteği
  iptal eder). İşleme asenkron ve işlemsel (ADR-0004).
- Doğrulama sırası (ilk hata yanıtı belirler): istemci IP'si → gövde ≤ 4 KB → JSON → token (`hmac.compare_digest`) →
  hız sınırı → şema → kayıt. Hız sınırı token'dan **sonradır**: dört kaynak adres bütün TradingView kullanıcılarının
  ortak adresidir; token'ı bilmeyen biri sınırı tüketip kendi alert'lerimizi 429'a düşürememelidir.
- İstemci IP'si: bağlantının karşı ucu güvenilir vekilse (`127.0.0.1`, `::1`) `X-Forwarded-For`'un en sağdaki adresi;
  değilse karşı ucun kendisi. Adres `config/tv_alerts.yaml` → `webhook.source_ips` içinde olmalı.
- Tazelik (2026-11-13 kullanıcı kararı): `bar_kapanışı = bar_time + tf süresi`; `now ≤ bar_kapanışı +
  signal_validity_seconds` (60) ise taze. Bayat giriş kaydedilir ve "[BAYAT SİNYAL — ATLA]" olarak bildirilir; bayat
  stop/çıkış/iptal "[GEÇ GELDİ]" önekiyle yine iletilir (risk azaltır).
- Giriş alert'i hesaplanan bar kapanışından > 2 sn **önce** gelirse sunucu saati TradingView ile uyuşmuyordur: sinyal
  "ATLA" olarak iletilir (değişmez kural 3).
- **Sembol listesi** (kullanıcı kararı 46, ADR-0006): stop/TP mesafeleri venue sembolüne olduğu gibi uygulandığı için
  giriş yalnızca puan ölçeği venue sembolüyle aynı olan grafiklerden kabul edilir: venue profili →
  `signal.tv_symbols`. Karşılaştırma birebirdir (büyük/küçük harf dahil, önek tahmini yok). Listede olmayan sembolün
  girişi "[SİNYAL — ATLA] Sembol kabul listesinde değil" olur ve bütçede sayılmaz. İletilmiş bir girişin
  stop/çıkış/iptal mesajları listeye bakılmadan iletilir (liste işlem açıkken değişmiş olabilir); listede olmayan bir
  sembolden gelen ve bu botun hiçbir girişine bağlanamayan takip alert'i iletilmez, 10 dakikada en çok bir alarm
  üretir. Heartbeat sembole bakılmadan sayılır.
- **Asgari stop** (kullanıcı kararı 33, ADR-0006): giriş ile stop arası `signal.min_stop_points`'ten (FTMO US500.cash:
  1,5 puan) küçükse giriş "[SİNYAL — ATLA] Stop çok dar" olur; eşitse iletilir. Kural `contract.*` boşken de geçerlidir
  (lotu kullanıcı elle hesaplarken en çok orada gerekir). Stop güncellemelerini etkilemez.
- Giriş karar sırası (ilk eşleşen): saat uyuşmazlığı → KILL → duraklatma → kopya beklemede (§5.6) → sembol listesi →
  bayat → stop çok dar → stop çok geniş (en küçük lot bütçeyi aşıyor) → günlük bütçe → iletildi.
- Idempotency: `UNIQUE(strategy_id, symbol, bar_time, action)`; yinelenen istek 200 `duplicate` alır, ikinci mesaj üretmez.
- Boyut önerisi: venue profili + `risk.yaml` ile lot hesabı (yalnızca öneri; Faz 1'de emir yok). `contract.*` boşsa
  mesajda "boyut hesaplanamadı" ve en fazla risk yazar (hata değildir).

### 5.3 Güvenlik katmanları
1. ufw: 443'e yalnızca TradingView kaynak IP'leri; 80 yalnızca sertifika doğrulaması (ACME) ve yönlendirme için; SSH yalnızca anahtar ile.
2. Caddy: otomatik TLS; yalnızca TradingView adreslerinden `POST /tv/webhook`'u geçirir. TradingView istemci sertifikası
   (mTLS) **desteklemez** (kullanıcı tarafından resmi sayfadan aktarıldı, 2026-11-13).
3. Bot: kaynak IP'yi yeniden doğrular; yalnızca loopback'i dinler; `X-Forwarded-For` dışında başlık kabul etmez.
4. Payload token'ı (`.env`, kolay rotasyon). Broker/Telegram kimlik bilgisi asla payload'da yok.
5. Hız sınırı (dakikada ≤ 30, token'ı doğru isteklerde), gövde boyutu sınırı, yapılandırılmış log (sırlar `***`).
   python-telegram-bot'un kendi kayıtları **değersiz** loglanır (ileti şablonu, istisna türleri ve konumları; argüman
   ve istisna metni yok): kütüphane ayrıştıramadığı güncellemeleri olduğu gibi loglar ve bir sohbet mesajı (yanlış
   yazılmış PIN, yabancının metni) bilinen bir sır değildir. Yakalanmayan istisnalar için üç yorumlayıcı kancası
   (`sys.excepthook`, `threading.excepthook`, `sys.unraisablehook`) ve olay döngüsü aynı maskeleyen günlüğe bağlıdır;
   `propbot serve`'ün bu bağlantıları giriş noktası üzerinden testlidir.
6. Kimliği doğrulanamayan istekler (yanlış token, bozuk JSON, büyük gövde) sayılır; izinli adresten gelenler için en
   fazla saatte bir Telegram uyarısı gider. Sayılar uyarı gerçekten gönderilene kadar tutulur ve izinli adresten gelen
   sonraki herhangi bir istek (geçerli bir heartbeat dahil) bekleyen uyarıyı tetikler: eski token'la gönderen kendi
   alarmımız, kapıyı önce açmış yabancı bir isteğin arkasında kaybolmaz.
7. Token'ın göründüğü yerler (alarm bildirimleri, Alert Log) ve yenileme adımları: `docs/runbook.md` §8.1–8.2.
   Alarmlarda yalnızca "Webhook URL" bildirimi açık olmalıdır. Yedi Pine dosyasında token girdisi
   `display = display.none` ile tanımlıdır (E13): değer grafiğin durum satırında yazmaz.

### 5.4 Telegram
- Yalnızca `TELEGRAM_ALLOWED_CHAT_ID` (pozitif = özel sohbet; grup/kanal kimlikleri reddedilir); diğer sohbetler yok
  sayılır ve sayılır/loglanır (metin loglanmaz).
- Mesaj şablonu (Türkçe, düz metin; `US500.cash` gibi alan-adı görünümlü sözcükler bağlantıya dönüşmesin diye `code`
  olarak işaretlenir; `/atladim`, `/aldim` ve `/devam` da — kimlikli, yer tutuculu ya da çıplak — her geçtiği yerde
  `code` olur: Telegram komutu bağlantı yapar ve dokununca **çıplak** gönderir; çıplak `/atladim` son girişin riskini
  düşer, çıplak `/devam` bilerek konmuş bir duraklatmayı kaldırır (kullanıcı kararı 51)):
```
[SİNYAL] {strateji} · {setup} · {LONG/SHORT} · {symbol}
Referans giriş ≈ {est_entry} (TradingView bar kapanışı)
Stop {stop} (−{mesafe} puan) · TP {tp} (+{tp mesafesi} puan, {R}R)
{VENUE} {sembol}'te PİYASA emri; SL/TP'yi emirle BİRLİKTE gir: giriş anındaki fiyata göre stop −{mesafe} puan, TP +{tp mesafesi} puan
Önerilen boyut: {lot} lot (≈ ${nominal} nominal, {x}× hesap) · risk ≈ ${risk} (%{risk_pct}) · günlük bütçe ${kullanılan}/${tavan} (bu sinyal dahil)
Geçerlilik: {TR saati}'ye kadar ve TradingView'da ({symbol}) fiyat {alt}–{üst} aralığında — değilse ATLA
TradingView'da stop/TP dolunca mesaj gelmez; pozisyonun hâlâ açıksa seans sonunda kendin kapat.
Not: {stratejiye özel not}
id {signal_id} · bar {NY saati} NY / {TR saati} TR
```
  Fiyat aralığı = `est_entry ± signal_max_drift_frac_of_stop × stop mesafesi`. Limit girişte (Silver Bullet) aralık yerine
  "Emri {TR saati}'ye kadar gir; İPTAL ya da ÇIKIŞ mesajı gelene kadar, en geç seans sonuna kadar bekler" yazar ve
  dolum satırı "… seans sonunda emrin hâlâ bekliyorsa SİL, pozisyonun açıksa kendin kapat" olur: TradingView'daki limit
  dolup kullanıcının kendi emri (başka enstrüman) dolmayabilir; o durumda İPTAL gelmez. Limit girişin (ya da botun
  görmediği bir girişin) `[ÇIKIŞ]` mesajı "limit emrin hâlâ bekliyorsa İPTAL ET" der. Diğer mesajlar: `[STOP GÜNCELLE]`
  (mesafe olarak: "stop = kendi giriş fiyatın −3.50 puan"), `[ÇIKIŞ]`, `[İPTAL]`, `[SİNYAL — ATLA]` (stop çok geniş ·
  stop çok dar · sembol listede değil · kopya beklemede · bütçe yetersiz · saat uyuşmazlığı), `[BAYAT SİNYAL — ATLA]`, `[İLETİLEMEDİ — ATLA]`, `[İLETİM BELİRSİZ]`, `[UYARI]`
  (stop genişletme reddedildi), `[ALARM]`.
- Günlük bütçe satırı temkinlidir (ADR-0005): iletilen her giriş prop günü boyunca sayılır; almadığın sinyali `/atladim`
  ile bildirirsin. Stop güncellemeleri mesafe uzayında karşılaştırılır; stop'u genişleten güncelleme iletilmez.
- `/atladim` id'siz yazılırsa mesajı Telegram'a gönderilmiş en yeni girişi alır. Ondan daha yeni, hâlâ sayılan bir
  girişin mesajı "belki ulaştı" durumundaysa (gönderim zaman aşımına uğradı) hiçbir şey serbest bırakılmaz ve id
  istenir: yanlış girişi serbest bırakmak o işlemin stop/çıkış mesajlarını da keser. id baştaki sıfırla yazılamaz.
- **Teslim kuşkusu:** bir gönderim, istek çıktıktan sonra zaman aşımına uğradıysa ya da yorumlanamayan bir yanıtla
  bittiyse (JSON olmayan / mesaj olmayan yanıt) mesaj ulaşmış olabilir. Böyle bir giriş mesajı süresi dolunca,
  duraklatma/KILL ile düşürülünce **ya da sonradan kalıcı hata alınca** bütçeden düşülmez; kullanıcıya
  "[İLETİM BELİRSİZ]" ile sorulur. Yalnızca Telegram'ın açık reddi (BadRequest, Forbidden, ChatMigrated, Conflict)
  ulaşmadığının kanıtıdır. Yeniden başlatmada yalnızca **önceki sürecin** bıraktığı bekleyen girişler kuşkulu sayılır.
- Kütüphanenin kuramadığı bir Telegram güncellemesi (tanımadığı mesaj biçimi) atlanır ve Telegram'a onaylanır; aynı
  partideki komutlar işlenir, kullanıcıya saatte en çok bir "N mesaj okunamadı ve atlandı" alarmı gider. Aşılamayan bir
  okuma hatasında polling 1–30 sn aralıkla yavaşlar ve "komutlar işlenmeyebilir" alarmı gider (önceden: aynı parti
  sonsuza dek yeniden istenir, hiçbir komut işlenmez, ham güncellemeler loglanırdı).
- Komutlar (ASCII): `/durum`, `/pnl`, `/risk`, `/duraklat`, `/devam`, `/kill <PIN>`, `/mod`, `/saglik`, `/atladim [id]`,
  `/aldim <id>`, `/yardim`. `/duraklat` ve `/kill` yalnızca yeni girişleri keser (stop/çıkış/iptal her zaman iletilir) ve
  gecikmeli ulaşsa da uygulanır; `/devam`, `/atladim`, `/aldim` 60 sn'den eskiyse reddedilir. KILL'den dönüş
  `/devam <PIN>`. 15 dk içinde 5 yanlış PIN, PIN girişini 15 dk kilitler (sunucu saati geri alınsa da kilit ilk
  denetimden bir süre sonra biter). Silinecek mesaj **biçimine** göre seçilir,
  PIN ile karşılaştırılarak değil: `/kill` ya da `/devam`'dan sonra bir şey yazılmışsa ya da mesajda 6+ haneli bir sayı
  varsa mesaj silinir ve yanıtlar o sayıyı yinelemez. PIN yalnızca bu iki komutun içinde karşılaştırılır; başka hiçbir
  yanıt "bu PIN'di / değildi" ayrımı taşımaz.
- Telegram hatası motoru asla bloklamaz (giden kutusu + üstel geri çekilme, en çok 30 sn).

### 5.5 Heartbeat ve alarm süresi
- `pine/heartbeat_indicator.pine` saat başı `{"action":"heartbeat"}` gönderir (stratejilerle aynı sembol, 1m grafik).
  İzlenen pencerelerde (`config/tv_alerts.yaml`, New York saati) 75 dk hiçbir geçerli alert gelmezse → Telegram:
  "TradingView alarm hattı sessiz". Pencerenin son saatinde kesilen hat, pencere kapandıktan sonra da bildirilir; hat
  geri gelince bilgi mesajı gider.
- Bot bir izleme penceresine denk gelen süre boyunca kapalı kaldıysa açılışta bunu bildirir (o aralıktaki alert'ler
  alınmadı): 90 sn'den uzun boşluk "Bot … arasında kapalıydı"; daha kısa boşluk (yeniden başlatma) "Bot … yeniden
  başladı" — en çok 30 dakikada bir, bastırılan yeniden başlatmalar sayılır ve sonraki alarm "N. kez" der (çökme
  döngüsü). Her yeniden başlatma, TradingView'ın hata yanıtı alıp alert'i yitirdiği bir deliktir.
  Bastırılan yeniden başlatmalar bir sonraki yeniden başlatmayı beklemez: kısıtlama bitince "N kez daha yeniden
  başladı" özeti gider. Aynı süreç iki denetim arasında 90 sn'den uzun çalışmadıysa (makine uykuya geçti, süreç
  askıda kaldı) bu da "Bot … arasında çalışmadı" diye bildirilir: o sırada alıcı da yanıt verememiştir.
- Heartbeat göstergesi, bar süresi aralığı tam bölen grafiklerde çalışır (1, 2, 3, 5, 10, 15, 30, 60 dk); yeni
  aralıkta kapanan ilk bar atım gönderir (sınırdaki bar yoksa bir bar geç). Heartbeat yalnızca kendi alarmının ve
  webhook yolunun çalıştığını kanıtlar; tek bir strateji alarmının durması görünmez (açık bulgu 49).
- Premium altı planlar için `config/tv_alerts.yaml` → `alerts` altında her alarmın bitiş tarihi (`expires_on`); bitişten
  7 gün önce ve sonrasında günde bir hatırlatma.

### 5.6 Dağıtım
Ubuntu · systemd (`Restart=always`) · chrony · journald. İki ücretsiz yol (`docs/runbook.md`): **A** DuckDNS alt alan adı +
Caddy (Let's Encrypt) + ufw; **B** kendi bilgisayarın + Tailscale Funnel (ya da yalnızca deneme için Cloudflare hızlı
tünel). Örnek dosyalar `deploy/`. Yeniden başlatmalar izlenen pencerelerin dışında yapılır. Dinleme soketi, hiçbir
arka plan görevi başlamadan önce bağlanıp dinlemeye alınır ve bir veritabanında tek `serve` çalışır (`<db>.lock`):
yanlışlıkla başlatılan ikinci kopya canlı veritabanına ve bota dokunmadan `HATA` ile çıkar. Aynı bot token'ıyla ikinci
kopya da engellenir (kullanıcı kararı 53, ADR-0004): iki dinleyici botun komutlarını rastgele paylaşır, `/kill` yanlış
kopyaya düşebilir. (1) Aynı makinede: bot kimliğine bağlı, makine genelinde bir kilit (Linux soyut soket adı; dosya
yok, `PrivateTmp`'ten etkilenmez) — ayrı `--db` ve `--port` ile başlatılan kopya afişten önce `HATA` ile çıkar.
(2) Kilidin görmediği yerde (başka makine, konteyner) Telegram'ın 409 Conflict yanıtı kullanılır. Bir kopya,
Telegram bir `getUpdates` isteğini ilk kez (ya da 60 saniyeden uzun bir sessizlikten sonra yeniden) yanıtladığında
"varmış" sayılır. Varışından sonraki 60 saniye içinde **iki** Conflict alan kopya yeni gelendir ve **beklemeye**
geçer: polling'i durdurur (komut almaz), giriş sinyali iletmez ("[SİNYAL — ATLA] … Bu kopya BEKLEMEDE"), stop
güncelleme / çıkış / iptal mesajlarını iletmeye devam eder, "[ALARM] Bu PropBot kopyası (makine adı) BEKLEMEDE"
mesajını gönderir (yeniden başlatılıp yine beklemeye düşerse 5 dakikada en çok bir "… yeniden başlatıldı, ama … yine
BEKLEMEDE" alarmı) ve **çalışmaya devam eder** (çıkmaz: systemd onu birkaç saniyede bir yeniden başlatır,
her seferinde öbür kopyanın güncellemelerini alır ve arada alıcı kapalı kalırdı). Tek bir Conflict hoş görülür
(elle gönderilmiş bir `getUpdates`, önceki sürecin Telegram'da kalmış isteği). Bekleme, sürecin ömrü boyunca sürer;
kopya yeniden başlatılınca yeniden dinler (o sırada giriş bildirimi "Bu kopya beklemeden çıkıyor … bekle" der),
Telegram'ın ilk yanıtından sonra 60 saniye boyunca Conflict görmeden yanıt alırsa "[BİLGİ] … yeniden ETKİN"
bildirimini gönderir ve girişleri açar (yeniden başlatmadan yaklaşık bir dakika, sessiz bir botta 70 saniye kadar
sonra). 60 saniyeden uzun süredir yanıt alan kopya botu tutar ve "Aynı bot token'ıyla başka bir süreç…" alarmını
verir; ancak öbür dinleyici yol vermezse (bu sürümden olmayan bir program, başka yerde kurulmuş bir webhook) ve kopya
**iki dakika boyunca Conflict'ten başka yanıt alamazsa** o da beklemeye geçer: komutlarını alabileceğinden emin
olamayan kopya giriş iletmez (değişmez kural 3). Beklemeye geçerken kutuda bekleyen bir giriş mesajı düşürülür ve
bu söylenir ("[İLETİLEMEDİ — ATLA] … bu kopya BEKLEMEDE", ya da gönderimi kuşkuluysa "[İLETİM BELİRSİZ] … bu kopya
BEKLEMEDE olduğu için yeniden gönderilmedi"). Varıştan sonraki ilk 3 saniyede (bir Conflict görüldüyse 10 saniye)
gelen komutlar bekletilir; kopya beklemeye geçerse `/kill` ve `/duraklat` yine uygulanır (yalnızca risk azaltır)
ve yanıt komutun öbür sürece ulaşmadığını söyler, öbür komutlar "yeniden gönder" yanıtıyla reddedilir. Süreler
duvar saatiyle değil, makinenin açılış saatiyle (uyku dahil, `CLOCK_BOOTTIME`) ölçülür. Deneme kopyası ayrı bot
token'ı ister (runbook §4).

### 5.7 Kabul
- 10 işlem günü kesintisiz; kaçan/çift sinyal 0 (TradingView Alert Log ile karşılaştırma); uçtan uca medyan gecikme < 5 sn.
  Kanıt: `uv run propbot signals report --since <gün> --csv reports/sinyaller.csv` (gün gün sayılar, gecikme medyanı,
  alarmlar). Rapor teslimi sayar, kararı değil: "iletilen giriş" mesajı Telegram'a gönderilmiş giriştir; dönemde
  süresi dolan ya da reddedilen tek bir mesaj ya da işlenemeyen tek bir sinyal ölçütü "SAĞLANMADI" yapar, bekleyen
  mesaj ya da kuyruktaki sinyal "EKSİK" (medyan yalnızca gönderilen mesajlardan hesaplanır, geçerliliğinden yavaş
  kalan mesaj örneğe hiç girmez).
- Tüm doğrulama hata yolları testli; `/kill` ve yetkisiz sohbet testleri yeşil.

## 6. Faz 2 — Python motoru ve backtest

> **Ön dilim (kullanıcı kararı, 2026-11-13):** proje iskeleti, `config/` şemaları (§8.1, §8.3) ve Monte Carlo prop
> simülatörü (§6.5) Faz 0 kabulü ve Faz 1 beklenmeden yapıldı; fiyat verisi ve derlenmiş Pine tabanı gerektirmezler.
>
> **Erken geçiş (kullanıcı kararı, 2026-11-18; ADR-0007):** Faz 2'nin kalan dilimleri (§6.1–6.4, §6.6) ve yerel web
> paneli (§6.8) Faz 0'ın TradingView derlemesi (§4.2 madde 5) ve Faz 1'in 10 günlük koşusu (§5.7) beklenmeden yapılır; o ikisi
> "operasyonel saha testi" olarak sürer. Pine referansı `144abab` commit'idir. Gerçek veri ve TradingView dışa aktarımı
> gelene kadar geliştirme deterministik **sentetik** barlarla yapılır; sentetik veriyle üretilen her rapor "SENTETİK
> VERİ" uyarısı taşır ve Go/No-Go'da PASS veremez. §6.7'nin veriye ve TradingView'a bağlı maddeleri o dosyalar gelince
> kanıtlanır.
>
> **Uygulama durumu (2026-11-18):** §6.1–6.6 ve §6.8 kodda (`src/propbot/data/`, `strategies/`, `backtest/`,
> `dashboard/`); komutlar ve sırası `docs/backtest.md`, panel işletimi `docs/runbook.md` §13. Depoda hâlâ gerçek veri ve
> TradingView dışa aktarımı yok: aşağıdaki her davranış sentetik barlarla ve elle yazılmış örnek dosyalarla test edildi.
>
> **Teslim onayı (kullanıcı kararı, 2026-11-19):** Faz 2 ve panel teslimatı onaylandı ve altı nokta karara bağlandı
> (açık bulgu 72 ve 82 dahil; altı karar ve her biri için yapılan: `CHANGELOG.md` Faz 2 bölümü, "Kullanıcı kararları
> (2026-11-19)"; işlem başı risk kuralı ADR-0010). Onay kodun teslimini kapsar: §6.7'nin parite ve veri maddeleri yine
> gerçek dosyalarla kanıtlanmayı bekler.

### 6.1 Veri
- **İçe aktarma** (`src/propbot/data/ohlcv.py`): yalnızca CSV; biçim başlık satırından tanınır. (a) Genel biçim
  (TradingView grafik dışa aktarımı, tedarikçi, propbot'un kendi dosyaları): virgülle ayrılmış `time,open,high,low,close`
  + isteğe bağlı `volume`, fazla sütunlar yok sayılır; `time` Unix saniye/milisaniye, ofsetli ya da ofsetsiz ISO 8601.
  (b) MT5 "Export Bars": sekmeyle ayrılmış `<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>`;
  `<VOL>` 0 ise hacim `<TICKVOL>`'dur. UTF-8 (BOM kabul) zorunlu; UTF-16 reddedilir. İki biçim de gerçek bir dışa
  aktarımla denenmedi (VERIFY). **Parquet desteklenmez** (ADR-0008 madde 7).
- **`bar_tz`:** ofsetsiz duvar saatinin nasıl okunacağı: `UTC` (varsayılan), bir IANA dilimi ya da `NY+7` (New York
  duvar saati + 7 saat: FTMO MT5 sunucu saati, GMT+2 / ABD yaz saatinde GMT+3; VERIFY: terminalde bilinen bir 09:30 NY
  barı 16:30 görünmeli). MT5 dosyası `bar_tz` verilmeden okunmaz (sunucu gerçekten UTC ise `Etc/UTC`). Yaz saati
  başındaki olmayan saat satır hatasıdır; sonundaki iki kez yaşanan saat önceki ana okunur (dosyanın artan kalması
  gerekiyorsa sonrakine). Venue profilindeki `server_tz` alanı hâlâ boştur ve kullanılmaz: saat dilimi komut
  satırında verilir (açık bulgu 30).
- **Hiçbir şey onarılmaz:** okunamayan satır, tutarsız OHLC, yinelenen, sıra dışı ya da üst üste binen bar dosyayı
  yüklenemez yapar; satır numaralarıyla (ilk 20) listelenir.
- **Doğrulama raporu** (`data/validate.py`): engelleyiciler (yukarıdakiler) + seans içi boşluklar (eksik bar sayısıyla)
  + aykırı bar (aralık > 25 × kayan medyan gerçek aralık, 240 bar; seans içi açılış sıçraması > 15 ×; ilk 30 bar ve
  seans ilk barı denenmez) + saat dilimi kayması (ABD ve AB yaz saati geçişlerinin iki yanındaki 5 seansın ilk barı
  farklı NY saatinde açılıyorsa) — bunlardan biri varsa rapor **temiz değil**. Bilgi olarak: kısa seanslar (tipik
  seansın %90'ından az bar), seansı olmayan hafta içi günler, hacmi sıfır bar sayısı. Seanslar veriden türetilir:
  iki bar arasında ≥ `session.min_gap_minutes` (30) dk boşluk seans sonudur; daha uzun bir veri deliği kısa seans
  görünür. Takvim kütüphanesi kullanılmaz (ADR-0008 madde 6).
- **Kaynak ve kapsam** her rapora yazılır: dosya adı, biçim, sha256, `bar_tz`, sentetik işareti, ilk/son bar.
- **Sentetik veri** (`data/synthetic.py`, ADR-0007): sabit tohumlu, platformdan bağımsız bayt bayt aynı üretim; ilk
  satır `#propbot-synthetic` işaretidir ve yükleyici `DataSource.synthetic`'i ondan kurar (başlıktan önceki herhangi bir
  `#` satırında). Seans şablonu bir varsayımdır (NY 18:00–17:00, tatil yok).

### 6.2 Dolum modeli (`backtest/engine.py`, ADR-0008)
- **Piyasa girişi:** sonraki barın açılışı + `slippage_ticks` (aleyhte). **Stop:** değdiği an stop fiyatı + slipaj;
  açılış seviyeyi aşmışsa açılış + slipaj. **Limit:** fiyat seviyeyi `limit_through_ticks` (1) tick **geçince**
  limit fiyatından, slipajsız; açılış seviyeyi en az 1 tick geçmişse açılıştan (iyi fiyat), 1 tick'ten az geçmişse
  açılışta dolmaz. **`Close` / `CloseAll`:** sinyal barının kapanışından, slipajlı (Pine'ın bu durumda slipaj uygulayıp
  uygulamadığı VERIFY).
- Açılışta önce zaten açık pozisyonun boşlukla geçilen çıkışları, sonra piyasa girişleri, sonra onların çıkışları dolar.
  Aynı fiyatta önce stop, sonra hedef, sonra giriş; aynı sırada olanlar veriliş sırasıyla.
- **`pessimistic`** (varsayılan, karar raporları): açılışta pozisyon varsa (ya da açılışta piyasa girişi dolduysa) bar
  önce aleyhte uca yürünür; düz başlayan barda bekleyen limit giriş varsa önce lehte uca (hedefe doğru hareket girişten
  önce harcanır). Dolum anında `on_fill` çağrılır; strateji stop/TP'yi gerçek dolumdan kurar.
- **`tv_ohlc`** (yalnızca parite): açılış yükseğe daha yakınsa O→H→L→C, değilse O→L→H→C (eşitlikte önce düşük);
  `on_fill` çağrılmaz, strateji dolum barının kapanışında yeniden fiyatlar. Sinyal barında kurulan tahmini parantez
  dolum barı boyunca geçerlidir (Pine'ın davranışı).
- **Pyramiding:** aynı yönde en çok `pyramiding` giriş (0 ve 1 = bir giriş; Master 2). Sınırı aşan giriş sessizce
  düşer ve sayılır. Ters yönlü giriş (çevirme) ve iki yönde birden bekleyen limitin dolması hata verir.
- **"İlk stop" ve R:** dolumdan sonraki ilk strateji çağrısından (`on_fill` ya da `tv_ohlc`'de dolum barının `on_bar`'ı)
  sonra yürürlükteki stop. `tv_ohlc`'de bu çağrı stop'u girişin ötesine taşırsa (MTF'de dolum barında TP1/1,5R) ilk
  stop odur ve R 1 tick riskle şişer (açık bulgu 77); karar koşuları etkilenmez.

### 6.3 Maliyet modeli (`config/backtest.yaml` → `instruments`)
- Enstrüman profili: `kind` (`futures` · `cfd`), `tick_size`, `point_value`, `qty_step`, `min_qty` ve `costs`
  (`commission_per_unit_side` taraf başı, `slippage_ticks`, `spread_points`, NY saatine göre `spread_windows`).
  **MES** dolu (taraf başı 0,50 USD, 1 tick, spread 0; Pine başlığıyla aynı). **US500.cash** alanları boş (VERIFY: MT5
  sembol şartnamesi); boş alanlı profille backtest çalışmaz.
- CFD barları **bid** kabul edilir: alım yönlü her dolum ve tetik ask'e (bid + spread) göre, satışlar bid'e göre. Spread
  fiyata gömülüdür. Açık K/Z long'u bid'den, short'u ask'ten değerler ve komisyon içermez (TradingView'ın açık K/Z'ye
  komisyon katıp katmadığı VERIFY).
- **Maliyet duyarlılık matrisi:** aynı koşu `cost_factors` (1,0 / 1,5 / 2,0) ile; 1,0 sütunu karar koşusunun kendisidir.
- **Maliyetsiz koşu** yalnızca karşılaştırmadır: özeti, artefaktı ve paneldeki kartı kırmızı "MALİYETSİZ KOŞU" taşır;
  Go/No-Go onu karar koşusu saymaz.

### 6.4 Analitik (`backtest/analytics.py`, `walkforward.py`, `runner.py`)
- **Birim giriştir** (`EntryResult`): bir girişin bütün çıkışları toplanır. R = net K/Z (maliyet sonrası) ÷
  (|dolum − ilk stop| × boyut × puan değeri). Veri sonunda açık kalan giriş (`end_of_data`) istatistiğe girmez, sayılır.
- Örneklem başına (tümü / IS / OOS): işlem sayısı, kazanma/kayıp/başa baş (net K/Z işaretiyle), kazanma %, ortalama ve
  medyan R, örneklem standart sapması, beklentinin t-istatistiği (n < 2 ya da sapma 0 ise yok), profit factor, toplam
  R ve $, ortalama kazanç/kayıp R, maks. düşüş (R; ve başlangıç sermayesi + kümülatif K/Z'nin zirvesine göre %), çıkış
  sırasıyla en uzun kayıp serisi (başa baş seriyi bitirir).
- **IS/OOS:** giriş zamanı `samples.oos_start`'tan (UTC gece yarısı) itibaren OOS (Pine `isOOS = time >= oosStart`).
  G3 oranı = OOS beklentisi ÷ IS beklentisi.
- **Zaman tabloları:** girişin New York duvar saatine göre hafta günü (Pzt–Cum her zaman, Cmt/Paz yalnızca varsa), saat
  (işlem olan saatler) ve ay (ilk ile son ay arası boş aylar sıfır satırla). Pine tablosundan **farkı:** Pine her
  `closedtrades` satırını (kısmi çıkış ayrı) ve brüt fiyat mesafesi R'sini sayar; buradaki tablolar giriş başına net R
  sayar. Sayılar maliyet ve kısmi çıkışlar kadar farklıdır.
- **Walk-forward:** tek koşunun girişleri, ilk tamamlanmış girişin NY gününden başlayan kayan takvim pencerelerine
  bölünür (`walk_forward`: IS 180, OOS 60, adım 60 gün; adım ≥ OOS). OOS penceresi verinin dışına taşan pencere
  alınmaz. **Parametre araması yoktur** (parametreler Pine varsayılanları, işlem başı risk koşunun riski; ADR-0008
  madde 9, ADR-0010). Çıktı: pencere başına IS/OOS istatistiği, pozitif OOS pencere oranı (işlemsiz pencere pozitif
  sayılmaz), birleşik OOS, verim oranı.
- **Master:** iki bacak (`master_ny_orb_fvg`, `master_asian_sweep`) ayrı istatistik ve artefakt alır; ayrıca birleşik
  görünüm `master_portfolio`. Piramitleme sınırında düşen girişler bacaklara dağıtılamaz, portföy toplamı yazılır.
  `propbot backtest --strategy all` yalnızca beş bağımsız portu koşar; Master ayrıca `--strategy master_portfolio`
  ile koşulur. Go/No-Go'ya ikisi de verilebilir: ya iki bacak kimliği ya da birleşik `master_portfolio` (kullanıcı
  kararı 2026-11-19; §9.1). Bacak ve birleşik artefaktlar, işlem listesi satırlarının özetini satırın taşıdığı kimlik
  başına kaydeder (`trades_digests`): birleşik koşunun satırları bacak kimliklerini taşır ve G4 listeyi onunla böyle
  bağlar.
- **İşlem başı risk (kullanıcı kararı 2026-11-19, ADR-0010):** `propbot backtest` koşuyu `config/risk.yaml` →
  `risk_per_trade_pct` ile boyutlar (değer betiğin risk girdisine yazılır ve artefaktın parametrelerinde kayıtlıdır);
  özet "İşlem başı risk" satırında kullanılan değeri, kaynağını ve Pine varsayılanını yazar. `--pine-risk` betiğin
  kendi değerini korur ve karşılaştırma koşusu üretir. `config/strategies.yaml` risk girdisini taşıyamaz (yükleme
  hatası); planlanan riski betiğin girdisi kabul etmiyorsa koşu hatayla durur, değer sessizce değiştirilmez. Farklı
  riskle yapılmış koşular tek işlem listesinde birleştirilmez. *Risk tabanı:* planlanan risk hesabın başlangıç
  bakiyesinin yüzdesidir (§8.1); `config/strategies.yaml` bir portun `risk_base` girdisini `Initial Capital` dışında
  bir değere çekmişse aynı yüzde başka bir risktir ve planlanan riskle koşu reddedilir (`backtest` çıkış kodu 1,
  `gonogo` 2; `config check` NOT yazar). Parite ve `--pine-risk` betiğin girdileriyle koşar ve risk satırında yüzdenin
  hesabın yüzdesi olmadığını söyler. *İşaretli liste:* betiğin kendi riskiyle yapılmış koşunun işlem listesi satır
  kimliklerinde ve köken satırında `-pinerisk` ekini taşır; G4 onu reddeder (§9.1).
- **Etiket politikası:** karar koşusu (`pessimistic`, maliyet açık, çarpan tam 1,0, config'teki OOS başlangıcı,
  planlanan risk) yalnızca bacak kimliğiyle etiketlenir; başka her koşu farkını bu sırayla ekler (`-tv`, `-free`,
  `-x1p5`, `-oos20261020`, `-pinerisk`). Karşılaştırma koşusunun özeti "KARAR KOŞUSU DEĞİL" ile başlar. Go/No-Go
  etikete değil kayıtlı alanlara bakar (§9.1).
- **Rapor artefaktları** (`backtest/artifacts.py`): `reports/<tür>_<etiket>_<YYYYMMDDTHHMMSSZ>.json`; türler `backtest`,
  `cost_matrix`, `walk_forward`, `monte_carlo`, `go_no_go`, `data_validation`, `parity`. Ortak zarf: tür, şema sürümü,
  oluşturulma anı, etiket, sentetik işareti, veri kaynağı, notlar. Dosya hiç üzerine yazılmaz; okuyucu hiçbir şeye
  güvenmez (boyut sınırı, katı UTF-8 ve JSON, NaN ve yinelenen anahtar yok, katı şema; sembolik bağ reddedilir).
  Modelin kendi kuralları: sentetik artefakt "SENTETİK VERİ" notunu, maliyetsiz backtest "MALİYETSİZ KOŞU" notunu
  taşımak zorundadır; Go/No-Go artefaktı yalnızca sentetik değilse ve yedi kapı PASS ise GO diyebilir; parite artefaktının
  oranı sayılarıyla tutarlı olmalıdır.
- Parametre optimizasyonu yoktur; açılması ayrı karar ister (açılırsa yalnızca IS'te ve deneme sayısı raporlanır).

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
- **Kanıt zinciri (2026-11-18):** `propbot backtest --trades-out` işlem listesinin ilk satırına köken satırı
  (`#propbot-trades …`) yazar: sentetik mi, maliyetsiz mi, dolum modeli, maliyet çarpanı, veri sha256'sı, stratejiler,
  oluşturulma anı. Okuyucu satırı yeniden kodlayıp birebir karşılaştırır (elle düzenlenmiş satır hatadır) ve satırda
  adı geçip listede tek işlemi olmayan stratejiyi reddeder. `propbot montecarlo` sonucu `monte_carlo` artefaktı olarak
  da yazar ve köken satırını taşır; G4 bu zinciri izler (§9). Köken satırı olmayan liste simüle edilir ama G4'ü geçemez.

### 6.6 Parite testi (`backtest/parity.py`)
- **Yöntem:** TradingView'dan aynı grafiğin bar verisi ve Strategy Tester "List of Trades" dışa aktarılır; Python aynı
  barlarda **`tv_ohlc`** ile çalışır (başka dolum modeli reddedilir; bu modelde §3.1'deki dolum anı/kapanış TP farkı
  oluşmaz). Karşılaştırma birimi bir çıkış dolumudur.
- **Eşleşme:** giriş zamanı, yön ve çıkış nedeni aynı; giriş ve çıkış fiyatı ≤ 1 tick farklı. Fiyat toleransı 1 tick'ten,
  zaman toleransı 1 bardan gevşetilemez (zaman toleransı varsayılan kapalıdır, açılırsa sayılır). Çıkış zamanı ve miktar
  bilgi amaçlıdır. Çıkış nedeni: "Signal" sütunu emrin yorumunu ya da kimliğini gösterebilir (VERIFY); ikisi de kabul
  edilir ve rapor hangisinin tuttuğunu sayar. Eşleştirme deterministik açgözlü atamadır; giriş zamanları en çok 3 bar
  arayla olan tek alanı farklı satırlar tek uyuşmazlık olarak, farklı alanların adıyla raporlanır.
- **Oran** = eşleşen ÷ (eşleşen + uyuşmayan çift + yalnızca Python + yalnızca TradingView). Geçme: oran ≥
  `min_parity_rate` (0,98) **ve** ısınma (bar sayısıyla) ve ortak pencere dışında bırakılan satırlar iki listenin hiçbirinde
  %2'yi aşmıyor. Python'un `end_of_data` satırları ile TradingView'ın açık işlemleri dışarıda bırakılır ve sayılır.
  Aynı Python satırının iki kez verilmesi hatadır.
- **Ayrıştırıcı:** başlık adlarıyla okur (takma adlar, yalnızca fiyat sütununda para birimi eki), UTF-8 (BOM'lu ya da
  değil), virgül / noktalı virgül / sekme ayırıcı, dosya başına tek ondalık işareti, binlik grupları denetlenir; ofsetsiz
  zamanlar grafiğin saat diliminde okunur, yaz saati geçişindeki belirsiz ya da olmayan saat reddedilir; satırlar her
  iki sırada olabilir; kısmi çıkışın iki düzeni desteklenir. Dışa aktarım düzeni **doğrulanmadı** (VERIFY,
  `docs/tv-verification-checklist.md` P bölümü).
- **Risk (2026-11-19 kullanıcı kararının uygulaması, ADR-0010):** parite her zaman **betiğin kendi işlem başı
  riskiyle** koşulur (Pine varsayılanı 0,5; `config/risk.yaml` riski burada kullanılmaz), çünkü TradingView betiği
  kendi girdileriyle çalıştırır. Rapor ve artefakt bunu tek satırda yazar. TradingView dışa aktarımı da risk girdisi
  varsayılandayken alınmalıdır. Bu yüzden TradingView'ın %0,5'teki sonuçları parite koşusuyla karşılaştırılabilir,
  %0,25'teki karar koşularıyla karşılaştırılamaz.
- **Beklenen güvenli sapma (kullanıcı kararı 2026-11-19):** portun Pine'dan bilerek ayrıldığı ve botun bir değişmez
  kuralını koruduğu yerler `parity.EXPECTED_DEVIATIONS` tablosundadır; bugün tek kayıt `mtf_trend_cont`'un trail
  ısınmasındaki başa baş stop'udur (§3.1). O stratejinin raporu ve artefakt notları bunu "Beklenen güvenli sapma"
  başlığıyla yazar. Kayıt açıklar, **hoş görmez**: hiçbir sayıyı ve hükmü değiştirmez, farklı dolumlar uyuşmazlık
  sayılır; ısınmayı karşılaştırma dışında tutmanın yolu `--warmup-bars`'tır (%2 dışarıda bırakma sınırı geçerlidir).
- **Sınırlar:** parite yalnızca `tv_ohlc` ile ölçülür, karar raporları `pessimistic` ile üretilir. Emülatörün
  belgelenmemiş köşeleri (pivot eşitliği, EMA tohumu, HTF kovası, kapanışta kayma, ızgara dışı fiyatlar) ilk gerçek
  koşuda fark çıkarabilir. MTF'nin HTF EMA'ları TradingView'ın grafikten önceki saatlik geçmişini görmez: ilk günler
  ısınma olarak dışarıda bırakılmalıdır.
- **Altın dosyalar** (`tests/parity/golden/`, 6 strateji × 2 sentetik fikstür × 2 dolum modeli = 24 dosya) yalnızca
  Python motorunun kendi çıktısını sabitler (regresyon); TradingView kanıtı değildir ve dosyalarda böyle yazar. Bilerek
  yeniden üretme: `PYTHONPATH=tests .venv/bin/python -m support.golden`.

### 6.7 Kabul
Parite raporu ≥ %98; veri doğrulama raporu temiz; Go/No-Go raporu (§9) tek komutla üretiliyor; panel (§6.8) kabul
maddeleri sağlanıyor. Parite ve veri maddeleri **gerçek** TradingView dışa aktarımı ve gerçek veriyle kanıtlanır
(ADR-0007): sentetik veriyle elde edilen sonuç kanıt sayılmaz, teslim raporunda "kanıt bekliyor" olarak listelenir.
Madde madde durum: `CHANGELOG.md` Faz 2 bölümü.

### 6.8 Yerel web paneli (`uv run propbot dashboard`; ADR-0009; işletim `docs/runbook.md` §13)
- **Çalışma:** ayrı süreç; yalnızca loopback (`--host` 127.0.0.1 ya da ::1, varsayılan port 8787). Başka adres
  seçeneksiz reddedilir; uzaktan bakış iki uçta aynı portla SSH tüneli (kullanıcı onayı 2026-11-19: panelin yalnızca
  `127.0.0.1` üzerinde kalması uygun, SSH tüneli yeterli; davranış değişmedi). Çalışırken hiçbir yapay zekâ/model
  çağrısı ve dış kaynak (CDN) yoktur. Arayüz Türkçedir; açık ve koyu tema.
- **Canlı Hesap & Sinyal ekranı** (`/`; canlı bölüm 5 sn'de bir yenilenir, sekme gizliyken durur): `data/propbot.db`
  (yoksa ya da boşsa **oluşturulmaz**, dokunulmaz) + venue/risk profilleri. Okuma yazma kilidi almadan tek bir anlık
  görüntüyle yapılır (`Store.read()`; dosya `create=False` ile açılır; dosya yeniden adlandırılırsa panel bunu görür).
  Gösterilenler: bot durumu (KILL > BEKLEMEDE > DURAKLATILDI > ETKİN; kurulu öbür bayraklar yanında listelenir) ve servis
  canlılığı (`last_alive_ms`, 90 sn sınır); bakiye (varsayılan `initial_balance` "varsayılan" etiketiyle ya da elle
  girilen değer, "elle girildi · tarih", prop günü değişince "güncel değil"); Faz 1 / Faz 2 hedef ilerlemesi; günlük
  tampon (botun risk defteri + beyan edilen günlük kayıp) ve toplam kayıp tamponu (`min(bakiye, özsermaye)`; gün sonu
  trailing türü için "hesaplanamıyor"); son 7 günün sinyalleri (en çok 1000 satır okunur, 25'i gösterilir) ve sinyal
  kayıtlarından türetilen açık/kapalı işlemler (en çok 50; "dolum değildir" notuyla). Ölçülmeyen değer ölçülmüş gibi
  gösterilmez.
- **Yerel kontrol:** Duraklat / Devam Et (yerel onay kutusu), Acil Kapat — KILL ve "KILL'i kaldır" (PIN; 5 yanlış / 15
  dk kilit Telegram ile ortak), "Telegram Durum/Test Özeti Gönder", elle bakiye girişi (onay kutusu). Aksiyonlar
  `Commands.handle` üzerinden Telegram komutlarıyla aynı kuraldan geçer; sonuç bayrakları geri okunarak doğrulanır
  (200 yapıldı · 409 yapılmadı · 500 sonucu bilinmiyor · 503 veritabanı meşgul). Her aksiyon giden kutusuna
  süresi dolmayan bir "[PANEL]" bildirimi yazar; `serve` çalışmıyorsa bildirim bekler. KILL açık pozisyonu kapatmaz.
  **Salt-okunur** açılış: sırlar okunamazsa, `config/tv_alerts.yaml` okunamazsa ya da veritabanı yok / boş / okunamaz
  ise. BEKLEMEDE kopyanın veritabanında özet ve bakiye reddedilir.
- **Analiz & Simülasyon ekranı** (`/analiz`): `reports/` altındaki artefaktlardan, her tür ve etiket için en yenisi:
  Go/No-Go (GEÇTİ/KALDI/DEĞERLENDİRİLEMEDİ rozetleri), Monte Carlo (olasılık çubukları, faz × neden tablosu, geçme süresi,
  maks. düşüş histogramı), backtest (tümü/IS/OOS, kümülatif R, zaman tabloları), maliyet matrisi, walk-forward, parite
  ve veri doğrulama kartları; sunucuda üretilen SVG. Sentetik kart "SENTETİK VERİ", maliyetsiz koşu "MALİYETSİZ KOŞU"
  taşır. Panel hüküm hesaplamaz; eşik çizgileri SPEC §9 kod tabanlarıdır. Rapor yoksa üreten komutlar listelenir,
  okunamayan dosya hata metniyle listelenir.
- **Güvenlik:** her istekte `Host` bu panel olmalı (yabancı 421, eksik/yinelenen 400); durum değiştiren her istek
  `POST` + aynı kaynak `Origin` (yoksa `Referer`) + süreç başına CSRF belirteci (sabit zamanlı); yalnızca urlencoded form,
  en çok 2048 bayt. Her yanıtta `default-src 'none'` tabanlı CSP, `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: same-origin`, `Cache-Control: no-store`; satır içi betik ve stil yok; çerez yok; erişim günlüğü
  kapalı; PIN loglanmaz ve sayfaya yazılmaz.
- **Kabul:** (1) loopback dışı adreste başlamıyor; (2) yabancı `Host`/`Origin` ve belirteçsiz `POST` reddediliyor;
  (3) veritabanı ya da `.env` yokken salt-okunur açılıyor; (4) panelden Duraklat/Devam/KILL, Telegram komutlarıyla
  aynı durumu üretiyor ve giden kutusuna bildirim yazıyor; yanlış PIN kilidi ortak; (5) analiz ekranı rapor yokken,
  bozuk rapor dosyasıyla ve sentetik raporla doğru davranıyor; (6) testler ağ ve veritabanı kenar durumlarını kapsıyor.

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
- `risk_per_trade_pct` varsayılan 0,25; kod tavanı 0,5. Başlangıç bakiyesi üzerinden. **İşlem başı riskin tek
  kaynağıdır** (kullanıcı kararı 2026-11-19, ADR-0010): canlı çalışma (Faz 1 mesajındaki lot önerisi, Faz 3 risk
  motoru) ve portföy düzeyindeki değerlendirmeler (karar koşuları, Monte Carlo, Go/No-Go) bu değeri kullanır. Pine
  script'lerindeki 0,5 yalnızca tekil strateji varsayımıdır; yalnızca paritede ve `--pine-risk` karşılaştırma
  koşusunda geçerlidir. `config/strategies.yaml` bir portun risk girdisini taşıyamaz; risk tabanını `Initial Capital`
  dışında bir değere çekerse o portun karar koşusu yapılamaz (§6.4).
- **Faz 1 (emir yok):** bot dolumu ve gerçekleşen K/Z'yi bilmediği için ön kontrol yerine temkinli bir defter kullanır:
  iletilen her giriş prop günü boyunca tam riskiyle sayılır, yalnızca `/atladim` ya da kanıtlanmış iletilememe serbest
  bırakır (ADR-0005). Haftalık tavan, ardışık zarar duraklatması ve eşzamanlılık sınırı Faz 3'te risk motoruyla gelir.
- Günlük bütçe = `min(daily_cap_pct, 0,5 × daily_loss_limit_pct) × initial_balance`.
- Ön kontrol: `realized_today + open_risk + new_risk + cost_buffer ≤ günlük bütçe`; değilse RED.
- Günlük taban = reset anındaki `max(balance, equity) − daily_loss_limit`. Kalan günlük tampon
  `flatten_when_daily_buffer_below_pct` altına inerse: tüm pozisyonları kapat, gün kapanır.
  Bu botun kendi tabanıdır ve firmanınkinden gevşek olamaz. Firmanın tabanı profildeki `daily_loss_basis` ile hesaplanır
  (FTMO: reset anındaki bakiye; reset'te yüzen kâr varsa botun tabanı daha yüksek, yani daha sıkıdır).
- Toplam kayıp: statik → `initial × (1 − max_loss_pct)`; gün sonu trailing → profilde tanımlı formül.
- Boyut: `lots = floor_to_step(risk_usd / (stop_points × value_per_point_per_lot))`; `< min_lot` → işlem atla
  (`RISK_TOO_WIDE`); asla yukarı yuvarlama. `stop_points < signal.min_stop_points` → işlem atla (çok dar stop çok büyük
  lot ister; ADR-0006). Azami kaldıraç / marjin sınırı ayrıca denetlenmez (açık bulgu 16).
- Strateji başına `max_consecutive_losses` → bir sonraki işlem günü durdur + rapor. Haftalık `weekly_cap_pct` → manuel `/devam`'a kadar dur.
- Eşzamanlılık: strateji başına 1, toplam `max_positions_total`.
- Haber: profil `news_rule`; kaynak `data/news.yaml` (haftalık elle güncellenir; site kazıma yok).
- `rules_verified_at` boş veya > 30 gün → `signal`/`paper` dışındaki modlar kilitli. `signal` modu çalışmaya devam eder:
  giriş mesajına "[KURALLAR DOĞRULANMADI …]" satırı eklenir ve günde bir alarm gider; girişler kesilmez (kullanıcı
  kararı 32, 2026-11-16: bot emir göndermez, mesajı gören kullanıcı karar verir; ADR-0005).
- Config yüklenirken doğrulanır (`src/propbot/risk`): tavanı aşan değer, yanlış tip ya da bilinmeyen anahtar **yükleme
  hatasıdır** (sessizce kırpılmaz); `null` yalnızca VERIFY / çalışma-anı alanlarında kabul edilir ve raporlanır (ADR-0003).

### 8.2 Zaman eşlemesi (Türkiye UTC+3, yaz saati yok)
| Olay | ABD kış saati (EST) | ABD yaz saati (EDT) |
|---|---|---|
| NY açılış 09:30 ET | 17:30 TR | 16:30 TR |
| NY strateji sonu 15:59 ET | 23:59 TR | 22:59 TR |
| Asya aralığı 20:00–00:00 ET | 04:00–08:00 TR | 03:00–07:00 TR |
| Asya aktif 03:00–11:00 ET | 11:00–19:00 TR | 10:00–18:00 TR |

FTMO günlük reset (Prag gece yarısı; doğrulandı 2026-11-13): CET'te 02:00 TR, CEST'te 01:00 TR. ABD ve AB yaz saati
geçişleri farklı haftalarda → bu haftalar için ayrı testler. Asya aktif ve NY işlemleri aynı prop gününe düşer →
günlük bütçe birlikte hesaplanır.

### 8.3 Venue profili
Örnek: `config/venues/ftmo_2step_100k.yaml`. Sembol özellikleri profilde boş bırakılır; çalışma anında terminalden okunur.
`signal` bölümü firmanın kuralı değil, botun kendi giriş süzgecidir (ADR-0006): `tv_symbols` (puan ölçeği `symbol` ile
aynı olan TradingView sembolleri; en az 1, en çok 32, yinelemesiz) ve `min_stop_points` (> 0). İkisi de zorunludur.

## 9. Go/No-Go kapısı (ücretli sınav öncesi; hepsi şart)
- **G1** ≥ 2 yıllık 1m veri, işlem yapılacak mekanın (veya eşdeğer) beslemesinde; veri raporu temiz.
- **G2** Her strateji için OOS'ta maliyet sonrası beklenti > 0 ve t-istatistiği ≥ 2,0.
  (RR 1:2'de σ ≈ 1,45R → +0,2R beklenti için ~220, +0,1R için ~840 işlem gerekir.)
- **G3** OOS beklentisi ≥ IS beklentisinin %50'si; gün/saat filtreleri yalnızca IS'te seçildi.
- **G4** Monte Carlo: seçilen risk ve venue kurallarıyla P(iki fazı geçme) ≥ %60 ve P(ihlal) ≤ %20. Seçilen risk
  `config/risk.yaml` → `risk_per_trade_pct`'tir; işlem listesi de o riskle boyutlanmış karar koşularından gelir (§9.1).
- **G5** Demo forward (Faz 3): §7.5 sağlandı.
- **G6** Parite ≥ %98 (§6.6).
- **G7** `rules_verified_at` ≤ 30 gün; gereken yerde firmadan yazılı otomasyon onayı `docs/compliance/` altında.
Rapor `reports/go_no_go_YYYYMMDD.md`; her madde PASS/FAIL ve kanıt bağlantısıyla.

### 9.1 Raporlayıcı (`backtest/gonogo.py`, `uv run propbot gonogo`; uygulandığı hâliyle 2026-11-18, son çapraz denetim ve kullanıcı kararlarıyla 2026-11-19)
- **Faz 2'de sonuç hiçbir zaman GO değildir:** G5 Faz 3'ün demo koşusunu ister ve her zaman DEĞERLENDİRİLEMEDİ'dir.
  Rapor yine de öbür kapıların durumunu gösterir. Çıkış kodu 0 yalnızca GO, 1 NO_GO, 2 hata.
- Her kapı GEÇTİ (PASS) / KALDI (FAIL) / DEĞERLENDİRİLEMEDİ (NOT_EVALUABLE). Her şüphe "geçmedi" yönüne çözülür:
  bir kapıda KALDI > DEĞERLENDİRİLEMEDİ > GEÇTİ. GO yalnızca yedi kapı GEÇTİ ve hiçbir girdi sentetik değilse
  mümkündür (artefakt modeli de bunu zorlar).
- **Girdi:** `reports/` altındaki **bütün** okunabilir artefaktlar (en yeni 300 dosya; sınıra takılınca veri kapıları
  DEĞERLENDİRİLEMEDİ). Okunamayan bir dosya, gizleyebileceği kapıları DEĞERLENDİRİLEMEDİ yapar (backtest → G1–G4,
  doğrulama → G1, Monte Carlo → G4, parite → G6). Dosya adı içerikle uyuşmuyorsa ya da iki girdi aynı adı taşıyorsa
  rapor üretilmez.
- **Seçim kuralları:** strateji eşleşmesi artefaktın `strategy_id`'siyle (etiket ve dosya adı kanıt değildir).
  - *Karar koşusu (G1–G3):* stratejinin en yeni **karşılaştırma olmayan** koşusu (karşılaştırma = `tv_ohlc`,
    maliyetsiz, kayıtlı çarpanı 1,0 olmayan, kayıtlı OOS başlangıcı `config/backtest.yaml` → `samples.oos_start`
    ile aynı olmayan ya da kayıtlı işlem başı riski planlanan riskten farklı olan; sonuncusu aşağıda, *Risk*). Başka
    bir OOS sınırı, sonuçlara bakılarak seçilmiş olabilecek bir "ya şöyle olsaydı" koşusudur
    (`--cost-factor` gibi): ne karar verir ne de daha eski bir karar koşusunu gizler; koşucu onu `-oos20261020`
    gibi bir etiket ekiyle ve "KARAR KOŞUSU DEĞİL" notuyla yazar. Yapılandırılmış OOS başlangıcı raporlayıcıya
    verilmezse hiçbir koşu karar koşusu olarak doğrulanamaz. Karar koşusu sayılması için `pessimistic`, maliyetli,
    çarpan **tam** 1,0 ve kayıtlı komisyon/slipaj/spread/spread pencereleri (pencerelerin saat dilimi `session.tz`
    dahil) ile enstrüman (tür, tick, puan değeri, `qty_step`, `min_qty`) `config/backtest.yaml`'daki profille
    **birebir** aynı olmalı. Başlangıç sermayesi venue profilinin `initial_balance` değerine, seans sonu boşluğu
    `session.min_gap_minutes`'a eşit olmalı (2026-11-19, kapı saldırısı K–M): boyutlar tam kontrattır ve `min_qty`
    altı atlanır (R boyuttan bağımsız değildir), seans sonu da stratejilerin okuduğu `session.islastbar`'ı belirler;
    üçü de işlemleri değiştirir. Yapılandırılmış boşluk raporlayıcıya verilmezse hiçbir koşu doğrulanamaz. Kayıtlı
    strateji parametreleri de `config/strategies.yaml`'ın bugün verdiği etkin parametrelerle (Pine varsayılanları + o
    dosyadaki değişiklikler + risk girdisinde planlanan risk) ad ve değer olarak birebir aynı olmalı (tip dahil: `1`,
    `1.0` ve `true` üç ayrı değerdir; Master bacağı `master_portfolio` parametrelerine bakar): başka parametrelerle
    yapılmış koşu başka bir stratejiyi test etmiştir.
    En yeni aday doğrulanamazsa (çarpan kayıtsız, profil yok ya da boş, maliyet, enstrüman ya da parametre farklı)
    strateji DEĞERLENDİRİLEMEDİ olur: **daha eski bir koşuya geçilmez** ve en iyi koşu asla seçilmez.
  - *Risk (kullanıcı kararı 2026-11-19, ADR-0010; açık bulgu 82 kapandı):* karar koşusu, kayıtlı risk girdisi
    (`risk_pct`; MTF'de `risk_percent`) planlanan riske (`risk.yaml` → `risk_per_trade_pct`) **eşit** olan koşudur.
    Başka riskle yapılmış koşu (betiğin kendi riski, `--pine-risk`) karşılaştırma koşusudur: karar vermez, eskisini
    gizlemez; kanıt iki değeri de yazar. Sınıflandırma etikete değil kayıtlı değere göredir. Kayıtlı risk bilinmiyorsa
    (girdinin adı raporlayıcıya verilmemiş, alan yok ya da sayı değil) koşu doğrulanamaz: DEĞERLENDİRİLEMEDİ, eskisine
    geçilmez. `risk.yaml` değişirse eski karar koşuları karşılaştırma koşusu olur; backtest, işlem listesi ve Monte
    Carlo yeniden üretilir. Beklenen parametreler koşucunun kayıtta kullandığı aynı fonksiyonla kurulur.
    Değerlendirilen bir stratejinin risk tabanı `config/strategies.yaml`'da `Initial Capital` dışındaysa beklenen küme
    kurulamaz ve komut rapor üretmeden durur (çıkış kodu 2; §6.4). Kod tavanının (§8.1) üstünde bir planlanan riskle
    de rapor üretilmez.
  - *Master (kullanıcı kararı 2026-11-19; açık bulgu 72 kapandı):* `--strategies` ya iki bacak kimliğini ya da birleşik
    `master_portfolio` kimliğini alır; ikisi de kabul edilir ve her biri kendi artefaktından yargılanır (bacaklar →
    `backtest_master_ny_orb_fvg_…` ve `backtest_master_asian_sweep_…`, birleşik → `backtest_master_portfolio_…`).
    Uygulamanın getirdiği sınır (kararın parçası değil): birleşik kimlik bir bacağıyla **aynı raporda** verilemez, iki
    görünüm iki ayrı raporla değerlendirilir. Aynı raporda aynı işlemler iki karar koşusunda sayılırdı; komut reddeder
    (çıkış kodu 2), saf katman G4'ü DEĞERLENDİRİLEMEDİ yapar. G4 işlem listesini birleşik koşuya satır kimliği başına
    özetlerle bağlar (liste satırları ve köken satırı bacak kimliklerini taşır); bu özetleri taşımayan eski birleşik
    artefakt bağlanamaz (DEĞERLENDİRİLEMEDİ). TradingView işlem listesi betik başına olduğundan parite raporu
    `master_portfolio` içindir:
    bir bacağın G6'sı betiğin raporunu okur ve kanıt, oranın bütün bacakları birlikte kapsadığını (bacak başına ayrı
    oran olmadığını) yazar. **Tek bacak** (kullanıcı kararı 2026-11-20; açık bulgu 87 kapandı): bir betiğin
    bacaklarının yalnızca bir kısmı verilirse (tek başına ya da bağımsız portla birlikte: `master_ny_orb_fvg,ny_orb_fvg`)
    komut hiçbir kapıyı çalıştırmadan reddeder (çıkış kodu 2, rapor yazılmaz) ve eksik bacağı, iki geçerli yolu ve aynı
    motorun bağımsız stratejisini söyler; betiğin işlem listesi bütün bacakları taşıdığından G4 tek bacakla bağlanamaz.
    Saf katman (`evaluate`) değişmedi: oraya yalnızca bu komuttan ulaşılır.
  - *Aynı saniye:* aynı saniyede oluşturulmuş iki aday (karar koşusu, aynı venue'nun Monte Carlo'su, aynı stratejinin
    paritesi, aynı sha'nın doğrulaması) varsa hiçbiri seçilmez (DEĞERLENDİRİLEMEDİ).
  - *G1:* karar koşusunun bar süresi ≤ 60 sn, kapsamı ≥ 2 yıl (365,25 günlük yıl), bar sayısı × bar süresi gerekli her
    yıl için ≥ 1500 saat, ve **aynı sha256'lı ve aynı okumalı** (bar saat dilimi, bar süresi, ilk ve son bar) en yeni
    doğrulama raporu temiz. Aynı dosyanın başka okuması (ör. `--bar-tz` NY+7 ile doğrulanıp UTC ile backtest edilmiş)
    başka barların doğrulamasıdır: o sha için uyan doğrulama yoksa KALDI, iki değer de cümlede yazılır.
  - *G2:* OOS'ta n ≥ 30, ortalama > 0, t ≥ 2,0; kayıtlı t ortalama/sapma/n'den yeniden hesaplanır ve küçük olan
    karar verir (elle düzenlenmiş değer KALDI). *G3:* IS ve OOS'ta n ≥ 30; oran yeniden hesaplanır, küçük olan karar verir.
  - *G4:* venue adı eşleşen en yeni Monte Carlo: planlanan riskle (`risk.yaml` → `risk_per_trade_pct`; **risk zinciri**:
    listenin arkasındaki her karar koşusu da aynı riskle boyutlanmış olmalı, zincir tutuyorsa kanıt bunu tek cümlede
    yazar, bir halka eksikse yazmaz ve G4 GEÇTİ olamaz), venue profilinin kurallarıyla, deneysel risk kademesi ve
    varsayılmış `min_trading_days` olmadan, ≥ 5000 yol, ≥ 100 işlem; olasılıklar kendi içinde tutarlı. **Köken
    zinciri:** işlem listesinin köken satırı olmalı (yoksa DEĞERLENDİRİLEMEDİ); sentetik değil (sentetikse KALDI),
    maliyetsiz değil, `pessimistic`, çarpan 1,0; köken satırında `-pinerisk` işaretli kimlik yok (betiğin kendi riskiyle
    yapılmış koşunun listesi işaret yüzünden reddedilir, satırları karar koşusununkilerle aynı olsa bile:
    DEĞERLENDİRİLEMEDİ); strateji kümesi değerlendirilen stratejilerle aynı (birleşik `master_portfolio` için: karar
    koşusunun satırlarının taşıdığı bacak kimlikleri); veri sha256'sı onların karar koşularıyla aynı; işlem sayısı
    karar koşularının tamamlanmış işlem toplamına eşit;
    listenin R toplamı ve mae_r toplamı karar koşularınınkine (`stats_all.total_r`, `mae_total_r`) eşit (yalnızca
    toplamların kayan nokta yuvarlaması kadar tolerans: CSV değerleri `repr()` ile yazılır, kendi yuvarlaması yoktur);
    listenin satırları (giriş/çıkış zamanı, R, mae_r, strateji) strateji başına sıralanıp sha256 ile özetlenir ve karar
    koşusunun kendi satırlarının özetine eşit olmalı (zamanı kaydırılmış satır sayıyı ve toplamları korur ama hangi
    işlemlerin aynı prop gününe düştüğünü değiştirir; 2026-11-19, kapı saldırısı P);
    liste en yeni karar koşusundan yeni, Monte Carlo'dan eski. **Kurallar:** Monte Carlo artefaktı simülasyon anındaki
    `max_loss_type`, `daily_loss_basis` ve `daily_reset` (saat dilimi ve saat) değerlerini kaydeder; hepsi bugünkü
    profille aynı olmalı, profil `static` dışı bir toplam kayıp türü taşıyorsa (simülatör onu modellemez) G4
    DEĞERLENDİRİLEMEDİ. Günlük kayıp bazı tek modelle hesaplanır (ADR-0002 madde 7); kanıt metni bunu yazar.
    **İşlem içi model** yalnızca kötümser `stop` olabilir (`closed` iyimserdir). **Tekrar üretilebilir koşu**
    (2026-11-19, kapı saldırısı N): tohum varsayılan (`20261113`), yol sayısı karar değeri (10.000 ya da config'teki
    daha sıkı `min_mc_paths`), ufuk varsayılan 1000 piyasa günü, işlem atlama olasılığı 0 olmalı. Bunlar firma kuralı
    değildir ama sonucu değiştirir: 5000 yolda P(geçme)'nin standart hatası ≈ 0,7 puandır, 40 tohum arasından en iyisi
    tipik olandan ≈ 1,2 puan yüksektir; kısa ufuk ihlalleri "sonuçlanmadı"ya iter.
  - *G6:* strateji başına en yeni parite raporu `passed`, oran (kayıtlı ile yeniden hesaplananın küçüğü) ≥ 0,98,
    karşılaştırılan işlem ≥ 100; Python tarafının kayıtlı parametreleri `config/strategies.yaml`'ın bugünkü etkin
    parametreleriyle **betiğin kendi riskinde** (parite planlanan riskle koşulmaz; karar kümesiyle karşılaştırılsaydı
    her dürüst rapor reddedilirdi; planlanan riskle yapılmış parite raporu ya da verilmemiş beklenen küme
    DEĞERLENDİRİLEMEDİ), enstrümanı (tür, tick, puan değeri, `qty_step`, `min_qty`) `config/backtest.yaml`'daki
    profille, seans sonu boşluğu ve başlangıç sermayesi karar koşusununkiyle aynı kurala göre birebir aynı ve
    karşılaştırılan barların veri bilgisi kayıtlı. TradingView geçmişi kısa olduğundan parite verisinin karar koşusunun
    dosyası olması istenmez; ikisi de kanıtta yazılır.
  - *G7:* `rules_verified_at` boş ya da > 30 gün → KALDI, ileri tarihli → DEĞERLENDİRİLEMEDİ;
    `automation_allowed: false` → KALDI; `docs/compliance/` dosyaları listelenir (göreli `--compliance-dir` çalışma
    dizinine göre çözülür; kanıt listelenen klasörün mutlak yolunu yazar, klasör bulunamazsa "klasör yok" der, "dosya
    yok" demez), yazılı onay elle teyit edilir.
- **Eski artefaktlar:** yukarıdaki kayıtları (OOS dışında: işlem içi model, R/mae_r toplamları, kural alanları,
  parite parametreleri/enstrümanı; 2026-11-19'dan: seans sonu boşluğu, spread saat dilimi, `qty_step`/`min_qty`,
  satır özetleri, paritenin boşluğu ve sermayesi) taşımayan eski dosyalar yine yüklenir ve panelde görünür; ilgili kapı
  DEĞERLENDİRİLEMEDİ olur ("eski rapor" cümlesiyle), asla GEÇTİ olmaz. 2026-11-19 kararından önce betiğin %0,5'iyle
  yapılmış backtest'ler karşılaştırma koşusu sayılır (karar koşusu yok); yeniden üretilirler.
- **Her artefakt için:** sentetik → o kapı KALDI ("SENTETİK VERİ"); 30 günden eski ya da rapor anından ileri tarihli →
  DEĞERLENDİRİLEMEDİ.
- **Elle teyit maddeleri** (kod denetleyemez, raporda yazar): verinin mekanın ya da eşdeğerinin beslemesinden geldiği
  (G1), gün/saat filtrelerinin yalnızca IS'te seçildiği (G3; GEÇTİ yalnızca oranı kapsar), Monte Carlo listesinin
  maliyet sonrası olduğu (G4), gereken yerde yazılı otomasyon onayı (G7).
- **SPEC değeri olmayan eşikler — mühendislik seçimi; kullanıcı 2026-11-19'da onayladı, değerler aynen kalır:** OOS'ta
  en az 30 işlem (G2/G3), gerekli her yıl için 1500 bar-saati (G1; NY normal seansı yılda ~1638 saat), en az 5000 Monte
  Carlo yolu ve 100 işlem (G4), artefakt yaşı en çok 30 gün (G1–G4, G6), paritede en az 100 karşılaştırılan işlem (G6;
  hiç fark yokken gerçek oranın %95 alt sınırı ≈ 1 − 3/n: 50 işlemde %94, 100 işlemde %97 — %98 tabanına yakın olan en
  küçük sayı).
  Değerleri `config/backtest.yaml` → `go_no_go`; SPEC §9 değerleri ve bu altısı kodda tabandır, config yalnızca
  sıkılaştırabilir (gevşek değer yükleme hatası).
- **Yazım:** JSON artefakt hiç üzerine yazılmaz; `go_no_go_YYYYMMDD.md` o günün en yeni hükmünü gösterir (işaret
  satırıyla; daha eski ya da aynı saniyedeki bir koşu onu değiştirmez, işaret satırı olmayan ya da sembolik bağ olan
  dosyaya dokunulmaz). Artefakt metni Markdown'a kaçışlanarak yazılır.

## 10. Zorunlu testler
Her maddenin bugün hangi test dosyalarında karşılandığı (2026-11-18; yollar `tests/` altında). Faz 3'e kalan
maddeler öyle yazılmıştır.
- **Strateji birim:** OR kurulumu; bias (Close/Wick); boğa/ayı FVG + min boyut; IFVG geçişleri ve ölüm; en yeniden eskiye
  tarama; aktivasyon barı kuralı; Asya penceresinin gece yarısını sarması; arming → sweep → giriş sırası; invalidasyon;
  günde ≤ 1 işlem. → `unit/test_strategy_<port>.py` ve Pine satırlarına bağlı izleme testleri
  `unit/test_strategy_<port>_pine_trace.py` (altı port: `ny_orb_fvg`, `asian_sweep`, `master_portfolio`,
  `ict_silver_bullet`, `mean_reversion`, `mtf_trend`); varsayılanların Pine girdileriyle eşliği
  `unit/test_pine_inputs.py`, `unit/test_strategy_registry.py`.
- **Takvim:** ABD DST 2026-03-08 ve 2026-11-01; AB DST 2026-03-29 ve 2026-10-25; yarım gün 2026-11-27 ve 2026-12-24;
  tatiller; eksik barlar. → `unit/test_propday.py`, `unit/test_session_clock.py`, `unit/test_sessions.py`,
  `unit/test_ohlcv_loader.py` (NY+7 ve yaz saati geçişleri), `unit/test_bar_validation.py` (saat dilimi kayması, kısa
  seans, seansı olmayan gün, seans içi boşluk), `unit/test_indicators.py` (HTF kovası DST haftalarında). Backtest'te
  takvim veriden türetilir (§6.1); canlı takvim Faz 3'e kaldı (açık bulgu 75).
- **Property (hypothesis):** stop daima zarar tarafında ve ≥ 1 tick uzakta; risk hiçbir zaman bütçeyi aşmaz; seans sonunda
  açık pozisyon yok; pozisyon sayısı limitleri. → `property/test_strategy_<port>_props.py` (Silver Bullet ve Mean
  Reversion'da stop kuralı §3.1 istisnalarına göre), `property/test_engine_props.py`, `property/test_sizing_props.py`.
- **Risk:** günlük bütçe matematiği, reset saat dilimi, taban/düzleştirme, ardışık kayıp ve haftalık duraklama,
  mod kilitleri (`auto` hiçbir koşul eksikken açılamaz). → `unit/test_sizing.py`, `unit/test_risk_config.py`,
  `unit/test_propday.py`, `unit/test_venue_profile.py`, `unit/test_modes.py`, `unit/test_processor.py` (Faz 1 defteri).
  Taban/düzleştirme, ardışık kayıp ve haftalık duraklama risk motoruyla Faz 3'te gelir; bugün yalnızca config şemaları
  testlidir.
- **Alıcı:** yanlış token, yanlış IP, bayat bar, çift istek, bozuk JSON, büyük gövde, 3 sn yanıt süresi.
  → `unit/test_receiver.py`, `unit/test_alert_schema.py`, `integration/test_service.py`.
- **Telegram:** yetkisiz sohbet, `/kill` onayı, ağ hatası motoru bloklamıyor. → `unit/test_telegram.py`,
  `unit/test_commands.py`.
- **Köprü (Faz 3):** bağlantı kopması, pozisyon açıkken yeniden başlatma, çift emir engeli, mutabakat uyuşmazlığı.
  → henüz yok (Faz 3).
- **Monte Carlo:** E = 0 sağlık testi, sabit tohumla tekrarlanabilirlik. → `unit/test_montecarlo.py`,
  `property/test_montecarlo_props.py`; köken satırı `unit/test_trades.py`, `unit/test_provenance.py`,
  `unit/test_provenance_attacks.py`.
- **Parite:** altın dosyalar. → `parity/test_golden.py` (24 dosya, yalnızca Python regresyonu; §6.6),
  `unit/test_parity.py`, `unit/test_parity_attacks.py`, `property/test_parity_props.py`.
- **Faz 2 ekleri:** motor `unit/test_engine.py`, `unit/test_engine_spec.py` (yalnızca sözleşmeden yazılmış kara kutu
  testleri), `unit/test_backtest_model.py`; veri `unit/test_ohlcv_loader.py`, `unit/test_bar_validation.py`,
  `unit/test_synthetic_bars.py`, `property/test_ohlcv_props.py`, `unit/test_cli_data.py`; analitik ve artefaktlar
  `unit/test_analytics.py`, `unit/test_walkforward.py`, `unit/test_artifacts.py`, `unit/test_runner.py`,
  `unit/test_runner_attacks.py`; Go/No-Go `unit/test_gonogo.py`, `unit/test_gonogo_attacks.py`,
  `property/test_gonogo_props.py`; işlem başı risk kuralı (ADR-0010) `unit/test_risk_rule.py`,
  `unit/test_risk_rule_attacks.py`, `unit/test_decisions_final_attacks.py`; panel `unit/test_dashboard_*.py`,
  `integration/test_dashboard.py`; salt-okunur
  okuma `unit/test_store_readonly_attacks.py`; komutlar `unit/test_cli_backtest.py`, `unit/test_cli_data.py`,
  `integration/test_pipeline.py` (veriden Go/No-Go'ya uçtan uca).

## 11. Operasyon (özet; ayrıntı `docs/runbook.md`)
- **Uçuş öncesi kontrol** (NY açılıştan 60 dk önce, otomatik): `rules_verified_at`, saat sapması, köprü heartbeat'i,
  veri tazeliği, `news.yaml` bu hafta güncel mi, günlük bütçe sıfırlandı mı, disk. Herhangi biri FAIL → mod `signal`'e
  düşer + Telegram.
- **Gün sonu raporu:** işlemler, R, $, kullanılan günlük bütçe, prop tamponları.
- **Haftalık:** SQLite yedeği, `news.yaml` hatırlatması, TradingView alarm süresi kontrolü.
- **Olay:** `/kill` → tüm pozisyonları kapat + yeni giriş kilidi; olay notu `docs/incidents/`.
