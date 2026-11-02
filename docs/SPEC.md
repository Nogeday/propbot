# PropBot — Teknik Spesifikasyon

Sürüm 0.1 · 2026-11-01 · Sahibi: Oğuz
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
- SL+TP sinyal anında bracket olarak kurulur (Faz 0 sonrası Pine ile aynı). Dolumda:
  - FVG Buffer stop'u **yapısal seviyedir** (değişmez); ATR stop'u **mesafedir** (dolum ∓ sinyal barındaki ATR × çarpan).
  - TP = dolum ± rr × |dolum − stop|. Stop, dolumun en az 1 tick zarar tarafında zorlanır.
  - Bilinen küçük parite farkı: Pine TP'yi dolum barının kapanışında günceller, Python dolum anında. Parite testinde
    tolerans olarak belgelenir.
- Aynı barda hem SL hem TP'ye değilirse backtest varsayılanı `fill_model: pessimistic` (önce SL). `tv_ohlc` modu
  TradingView emülatörünü taklit eder (açılış yükseğe daha yakınsa O→H→L→C, değilse O→L→H→C) — yalnızca parite testinde.
- Strateji başına günde en fazla 1 işlem. Seans sonunda pozisyon kesinlikle kapalı (takvim tabanlı; yarım günler dahil).

### 3.2 NY Open ORB + FVG/IFVG (`pine/ny_open_fvg_strategy.pine`)
- **OR:** NY saatiyle [09:30, 09:30 + orMinutes) aralığında açılan barların en yüksek high / en düşük low'u; `orEnd`'den itibaren hazır.
- **Tarama penceresi:** [orEnd, sessEnd) (varsayılan 09:35–15:59). NY takvim günü değişince tüm durum sıfırlanır.
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
  Percent: `close × (1 ∓ trailPct/100)`. Stop yalnızca kâr yönüne gider; güncelleme bar başına ≤ 1 ve ≥ `trail_min_step_ticks`.
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
  temas (long: `low ≤ üst`; short: `high ≥ alt`).
- **SL/TP:** sabit (trailing yok). Aktif pencere bitince kapat.
- **Enstrüman:** SPX/SPY'de 20:00–00:00 ET verisi yok → yalnızca ~23 saat işlem gören enstrümanlarda (ES/MES, US500 CFD) anlamlı.

### 3.4 Diğer stratejiler
BB/KC/RSI(2) mean reversion, ICT Silver Bullet ve Master Portfolio script'i kullanıcı `pine/` altına ekler.
Portlamadan önce bu bölüme 3.2/3.3 düzeyinde semantik özet yaz ve kullanıcı onayı al. Sıra: önce 3.2 ve 3.3.

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

## 5. Faz 1 — TradingView → Telegram sinyal botu (emir yok)

### 5.1 Alert yükü (Pine `alert()` → JSON)
| Alan | Tip | Açıklama |
|---|---|---|
| `v` | int | şema sürümü (1) |
| `token` | str | yalnızca webhook'a özel rastgele değer (login bilgisi değil) |
| `strategy_id` | str | `ny_orb_fvg`, `asian_sweep`, … |
| `symbol` | str | `syminfo.tickerid` |
| `tf` | str | `timeframe.period` |
| `bar_time` | int | sinyal barının açılış zamanı, epoch ms (`time`) |
| `action` | str | `entry` · `stop_update` · `exit` · `heartbeat` |
| `side` | str | `long` · `short` |
| `setup` | str | `FVG` · `IFVG` · `SWEEP_FVG` |
| `est_entry` | float | sinyal barı kapanışı |
| `stop`, `tp` | float | seviye |
| `stop_dist_pts` | float | `abs(est_entry - stop)` |
| `rr` | float | |

Fiyatları `str.tostring(x, format.mintick)` ile yaz. Strateji başına TradingView'da bir alarm: koşul
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

### 6.1 Veri
- İçe aktarma: CSV/Parquet (MT5 "Bars → Export", TradingView grafik verisi dışa aktarımı, tedarikçi verisi).
  MT5 bar saatleri **sunucu saatindedir** → `server_tz` ile UTC'ye çevir (VERIFY: terminalde bilinen bir 09:30 NY mumuyla karşılaştır).
- Doğrulama raporu: çift bar, sıra dışı bar, gün içi boşluklar, seans kapsaması, DST geçişleri, aykırı fiyat sıçramaları.
- Veri kaynağı ve kapsama tarihleri her rapora yazılır.

### 6.2 Dolum modeli
Piyasa emri: sonraki bar açılışı + `slippage_ticks`. Stop: değdiği an stop fiyatı (boşlukla açılışta açılış fiyatı) + slipaj.
Limit: değdiğinde limit fiyatı (slipaj yok). Aynı barda SL+TP → §3.1.

### 6.3 Maliyet modeli (venue/enstrüman bazlı, config)
CFD: spread (sabit ya da saat dilimine göre tablo; NY açılışında genişleme) + slipaj + komisyon (varsa).
MES/ES: kontrat başı taraf başı nakit + 1 tick slipaj. Maliyetsiz koşu yalnızca karşılaştırma içindir ve raporda kırmızı uyarı taşır.

### 6.4 Analitik
R ve $ bazında: işlem sayısı, kazanma %, ortalama R, beklenti, profit factor, maks DD (R ve %), en uzun kayıp serisi,
beklentinin t-istatistiği, IS/OOS ayrımı, hafta günü ve saat tabloları (Pine tablosuyla aynı tanım), aylık tablo,
walk-forward (kayan pencereler). Parametre optimizasyonu varsayılan olarak kapalı; açılırsa yalnızca IS'te ve deneme sayısı raporlanır.

### 6.5 Monte Carlo prop simülatörü
- Girdi: işlem R dizisi (gün bazında blok bootstrap — aynı günün işlemleri birlikte örneklenir), venue profili, risk %, maliyetler.
- Kurallar: günlük kayıp (reset saati ve baz), toplam kayıp (statik / gün sonu trailing), hedef(ler), minimum gün, faz sırası.
- Çıktı: P(geçme), P(ihlal), geçme süresi dağılımı (işlem günü), ihlal nedeni dağılımı.
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
