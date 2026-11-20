# CHANGELOG

## Faz 0 — Pine düzeltmeleri (2026-11-02)

Kapsam: yalnızca `docs/SPEC.md` §4.1'deki B-maddeleri. Pine v5'te kalındı. Giriş/çıkış mantığı ve mevcut girdilerin
varsayılanları değişmedi; aşağıda "davranış notu" olarak işaretlenen yerler B-düzeltmesinin doğrudan sonucudur.
Tüm dosyalara eklenen iki girdi: **Quantity rounding** (B5; Master/NY/Asian/MR) ve **Webhook token** (B7; hepsi).

Commit'ler: `e26e1bb` baseline · `1035771` NY dosya adı · `aa4025d` NY · `7ad1979` Asian · `a14240f` Master ·
`7d3694a` Silver Bullet · `ab5ecf6` Mean Reversion · `8c0aa72` MTF · `cc6aa94` SPEC §5.1 ·
`e997dd5` MTF `riskPts` bildirimi.

### `pine/ny_open_fvg_strategy.pine` (eski adı `ny_open_fvg_strategy_2.pine`)
- Yeniden adlandırıldı: SPEC §3.2 bu adı kullanıyor.
- **B1 (kritik):** "Reset management vars" bloğu sinyal barında `inDir`'i sıfırlıyordu → `strategy.exit` hiç çağrılmıyor,
  işlemler yalnızca "EOD close" ile kapanıyordu. Sıfırlama artık yalnızca işlem gerçekten kapandığında yapılıyor
  (`position_size == 0 and closedtrades > closedtrades[1] and inDir != 0`).
- **B2:** Sinyal barında, girişle aynı barda `strategy.exit("Exit L/S")` kuruluyor; tahmini giriş = kapanış, stop
  dolumun en az 1 tick zarar tarafında. Dolum barında gerçek ortalama fiyatla yeniden fiyatlanıyor (aynı id → emir
  güncellenir). ATR yönteminde mesafe sinyal barında saklanıp (`inAtrDist`) dolum fiyatına uygulanıyor (§3.1);
  önceden dolum barının ATR'si kullanılıyordu.
- **B3:** Seans sonu `curMin == sessEnd` yerine `curMin >= sessEnd or session.islastbar`; `close_all(immediately = true)`.
  Yarım günlerde kapanıyor; yalnız-RTH grafikte ertesi günün açılışına kaymıyor.
- **B4:** Değer değişmedi (SPY profili, %0,01 taraf başı, 1 tick). Başlığa enstrümana göre Properties tablosu eklendi
  (komisyon her dolumda ayrı ayrı kesilir; slipaj yalnızca piyasa ve stop emirlerine eklenir; CFD'de spread/2 + slipaj).
- **B5:** `f_roundQty` + "Quantity rounding" girdisi (Auto = vadelide tam kontrat, aşağı yuvarlama). `qty < 1` →
  işlem atlanır. Önceden `qty > 0` kontrolü yoktu.
- **B6:** İşlem kayıtları (`stopArr`, `dirArr`) sinyal barında ekleniyor, dolum barında son eleman güncelleniyor →
  aynı barda giriş+çıkış olunca tablo indeksi kaymıyor.
- **B7:** `alert()` JSON (§5.1): `entry`, `stop_update` (trailing stop ilerleyince), `exit` (seans sonu kapanışı).
  `strategy_id = ny_orb_fvg`.

### `pine/asian_sweep_reversal_strategy.pine`
- **B1 (kritik), B2, B5, B6, B7:** NY ile aynı desen (`strategy_id = asian_sweep`, `setup = SWEEP_FVG`; `entry`, `exit`).
- **B3:** Aktif pencere çıkışı `(not activeNow and activeNow[1]) or (activeNow and session.islastbar)`,
  `immediately = true`. Normal günde kapanış zamanı pratikte aynı (11:00 barının kapanışı ≈ eski 11:01 açılışı).
- **B4:** Değer değişmedi; başlığa tablo + "SPY/SPX'te 20:00–00:00 verisi yok" notu.
- Davranış notu (B5): `qty` 0'a yuvarlanırsa silahlanma (`armDir`) korunur ve sonraki barda yeniden denenir —
  Master'daki S2 ile aynı.

### `pine/master_prop_firm_portfolio.pine`
- **B1, B2, B6 yok** (doğrulandı): bacak durumu `strategy.opentrades` entry id taramasıyla ve 1 bar korumasıyla
  sıfırlanıyor (329–339); exit'ler sinyal barında kuruluyor (yönetim bloğu giriş bloğundan sonra); stop kuyrukları
  sinyal barında, tabloda FIFO eşleme.
- **B3:** S1 `curMin == s1_sessEnd` → `>= s1_sessEnd or session.islastbar`; S2 aktif pencere çıkışına `islastbar`;
  `strategy.close(..., immediately = true)`.
- **B4:** Değer değişmedi; başlığa tablo + "S2 SPY'de çalışmaz" notu.
- **B5:** "Quantity rounding — both engines" girdisi; mevcut `qty > 0` kapıları korunuyor.
- **B7:** S1 → `ny_orb_fvg` (`entry`, `stop_update`, `exit`), S2 → `asian_sweep` (`entry`, `exit`). Tek alarm yeterli.

### `pine/ict_silver_bullet_strategy.pine`
- **B1 yok** (402: sıfırlama `phase == 3`'e bağlı). **B5 zaten mevcut** (`roundToInt`, `q > 0`).
- **B2 (kritik, limit giriş):** `strategy.exit` limit girişle aynı barda (silahlanma barı) kuruluyor; yalnızca giriş
  dolunca aktif olur. Giriş iptal edilince exit de iptal ediliyor — yalnızca pozisyon yokken (açık pozisyonun stop'u
  hiçbir koşulda iptal edilmez; DD durdurma bloklarında da bu koşul var).
- **B3:** `curMin >= flatMin` zaten doğruydu; `or session.islastbar` (yarım gün) ve `immediately = true` eklendi.
- **B4:** "round-turn" yorumu yanlıştı: `cash_per_contract = 2.0` taraf başıdır (gidiş-dönüş $4). Değer korunup yorum
  düzeltildi. `backtest_fill_limits_assumption = 1` eklendi (dokunma = dolum varsayımı iyimserdi).
- **B6:** `justEntered` artık `opentrades + closedtrades` artışıyla hesaplanıyor ve iptal bloğundan önce değerlendiriliyor;
  aynı barda dolup kapanan limit giriş artık sayılıyor (`tradesWindow`, `tradesToday`, kayıt dizileri) ve "dolmamış emir"
  sanılıp iptal edilmiyor.
- **B7:** `strategy_id = ict_silver_bullet`, `setup = SB_FVG`; `entry` (`order_type = limit`, `est_entry` = limit fiyatı),
  `cancel` (iptal / süre doldu / DD durdurması), `exit` (gün sonu). `rr` gerçek TP/risk oranı (likidite hedefinde ≠ girdi).

### `pine/mean_reversion_bb_kc_rsi2_strategy.pine`
- **B1 yok** (sıfırlama giriş bloğundan önce; `inDir` dolum barında atanıyor).
- **B2:** Girişle aynı barda `strategy.exit`: stop = kapanış ∓ ATR × çarpan, TP = canlı BB ortası (zarar tarafına geçemez);
  dolum barında mevcut kodla gerçek dolumdan yeniden fiyatlanıyor.
- **B3:** `closeAtEnd and (atOrPastEnd or session.islastbar)`, `immediately = true`.
  Davranış notu: "Use time filter" kapalıyken de seans son barında kapatır (önceden hiç kapatmıyordu); varsayılan açık.
- **B4:** Değer değişmedi; %0,02 açıkça "yer tutucu" olarak işaretlendi, başlığa tablo eklendi.
- **B5:** `f_qtyFromDist` içinde "Quantity rounding" (aşağı yuvarlama); mevcut `qty > 0` kapısı < 1'i atlar.
- **B6:** Kayıtlar sinyal barında, dolum barında güncelleme.
- **B7:** `strategy_id = mr_bb_kc_rsi2`, `setup = BB_KC_RSI2`; `entry`, `exit`.

### `pine/mtf_trend_continuation_strategy.pine`
- **B1 yok** (sıfırlama `position_size[1] != 0`'a bağlıydı). **B4/B5 zaten mevcut** (MES, $0,50/taraf, 1 tick; tam kontrat).
- **B8:** `request.security(..., [ema[1], ema[1]], lookahead_on)`; başlıktaki "gerçek zamanlıda da kapanmış barı görür"
  ifadesi yanlıştı (lookahead_off gerçek zamanlıda gelişen 1H değerini döndürür) → düzeltildi. Beklenen etki: HTF filtresi
  bir 1H bar gecikir; işlem sayısı ve sonuçlar değişebilir.
- **B2:** Sinyal barında TP1 + Init (Plan B'de "Stop") exit'leri tahmini seviyelerle kuruluyor; Plan B kararı gönderilen
  adetten (aynı kural). `entryQty` artık gönderilen adetten alınıyor: TP1 dolum barında dolarsa da algılanır.
- **B6:** Tam işlem sayımı `position_size` geçişi yerine "düz ve `closedtrades` arttı" ile; `eqAtEntry` sinyal barında
  (işlem öncesi düz equity; önceden dolum barı kapanışıydı → dolum barı K/Z'si ve giriş komisyonu sayılmıyordu).
  Durum sıfırlama bloğu giriş bloğunun **önüne** taşındı: aynı barda kapanış + yeni sinyal olursa yeni işlemin
  `pendingStop`'u ve bracket'i silinmiyor (orijinalde bu durumda stop `na` kalabiliyordu).
- **B3:** `flatAtEnd and ((useSession and curMin >= sEnd) or session.islastbar)`, `immediately = true`.
  Davranış notu: "Restrict entries to a session window" kapalıyken de seans son barında kapatır; varsayılan açık.
- **B7:** `strategy_id = mtf_trend_cont`, `setup = MTF_PULLBACK`, `rr = tp1RR`, `tp = TP1`; `entry`, `stop_update`
  (init → BE/trail), `exit`.
- **Derleme düzeltmesi (B-kapsamı dışı, kullanıcı onayıyla):** dolum bloklarındaki `riskPts := ...` (long ve short)
  bildirimi olmayan bir değişkene atıyordu → `float riskPts = ...` yerel bildirime çevrildi. Değer ve kullanım aynı.

### `docs/SPEC.md` §5.1
- `strategy_id` değerleri, `cancel` action, yeni `setup` değerleri ve `order_type` (`market`/`limit`) alanı eklendi;
  olmayan sayısal alan `null`.

## Faz 0 genişletme — E-maddeleri (2026-11-12)

Kullanıcı kararı: TradingView doğrulamasından önce script'ler nihai hâle getirildi; aşağıdaki "Açık bulgular"dan 2–11
Faz 0'a alındı (`docs/SPEC.md` §4.3). B-maddelerinden farklı olarak bunlar **kural ve varsayılan değişiklikleridir**:
orijinal script'lerle önce/sonra farkı beklenir. Pine v5'te kalındı; hiçbir dosya henüz TradingView'da derlenmedi.

Commit'ler: `683ffcc` E1 · `f000482` E2 · `52b6f6a` E3 · `ccfc655` E4 · `c6928a0` E5 · `e7c3b51` E6 · `a4ca1fb` E7 · `bd85d03` E8 · `491c8f1` E9 maliyet/başlık · `290936d` E9 sermaye/marjin.

### E1 — Master: dolum barında yeniden fiyatlama (`master_prop_firm_portfolio.pine`)
- Açık bacak taraması her bacağın kendi dolum fiyatını da okuyor (`strategy.opentrades.entry_price`). Bacağın defterde
  göründüğü ilk barda stop/TP sinyal barı tahmininden gerçek dolum fiyatına taşınıyor: FVG Buffer stop'u yapısal seviye
  (değişmez), ATR stop'u sinyal barındaki mesafenin doluma uygulanması, TP = dolum ± rr × gerçek risk; stop dolumun en az
  1 tick zarar tarafında. `s1_stopQueue` / `s2_stopQueue` son elemanı gerçek stop ile güncelleniyor.
- S1 trailing artık bacak dolduktan sonra başlıyor ve aktivasyon R'si gerçek dolum fiyatından ölçülüyor
  (`s1_entryEst` / `s2_entryEst` kaldırıldı). NY ve Asian tekil dosyalarıyla aynı kural (§3.1). → Açık bulgu 3.

### E2 — Risk tabanı (6 dosya)
- `Risk base` girdisi: `Initial Capital` (varsayılan) / `Account Equity` / `Fixed Balance` + `Fixed balance ($)` (10000).
  Tüm boyutlandırma (`f_qty`, `f_qtyFromDist`, `riskCash`) `riskBaseAmt` üzerinden.
  Davranış notu: risk artık bileşik değil; kazanç/kayıp sonrası işlem başı nakit risk sabit kalır (§8.1).
- Silver Bullet'ta girdi zaten vardı; `Initial Capital` seçeneği eklendi ve varsayılan oldu. → Açık bulgu 4.

### E3 — Risk varsayılanları (Silver Bullet, Mean Reversion)
- `riskPct` 1,0 → 0,5 (§8.1 tavanı). Günlük zarar kesicisi: SB 4,0 → 2,0; MR 3,0 → 2,0 (diğer dördü zaten 2,0). → Açık bulgu 5.

### E4 — Günde en fazla 1 işlem (Mean Reversion, Silver Bullet, MTF)
- MR: `Max trades per day` (1) + `tradesToday` sayacı (emir gönderilince artar, NY günü değişince sıfırlanır).
- SB `maxTrDay` 2 → 1 (davranış notu: iki pencere de açıkken AM'de işlem olduysa PM atlanır). MTF `maxTradesDay` 3 → 1.
  → Açık bulgu 6.

### E5 — Giriş-zamanı kapısı (6 dosya)
- Yeni `entryTimeOK` (Master: `s1_entryTimeOK` / `s2_entryTimeOK`): sinyal barı seansın son barı değilse ve dolum barı
  seans/pencere bitiminden önce açılıyorsa giriş var. Bar süresine duyarlı: `curMin + barMin < bitiş`
  (1m grafikte `curMin < sessEnd − 1`).
- Yalnızca **giriş** koşuluna eklendi; `canScan` / `canTrade` (bias, FVG, sweep taraması) pencereleri değişmedi
  (§3.2/§3.3 ile aynı; işlem sonucu açısından eşdeğer).
- Silver Bullet (limit giriş): av başlatma ve silahlanma `entryTimeOK` ister; dolmamış limit emir artık pencerenin
  **son barının kapanışında** (ya da seansın son barında) iptal ediliyor. Davranış notu: önceden iptal pencere dışındaki
  ilk barın kapanışında işlendiği için emir o barda da dolabiliyordu.
- Sınır: erken kapanış Pine'da önceden bilinemediği için yarım günde seansın sondan bir önceki barındaki sinyal 1 barlık
  işlem açabilir; pozisyon aynı seansta kapanır, geceye taşınmaz. → Açık bulgu 7.

### E6 — Yüzen günlük zarar kesicisi ve anında zorunlu kapanışlar (6 dosya)
- NY/Asian/Master: kesici artık `min(gerçekleşen, strategy.equity − gün başı özsermaye)` değerine bakıyor (bar
  kapanışında). Yeni girdi `Flatten open trade on breach` (varsayılan açık): ihlalde açık pozisyon o barın kapanışında
  kapatılır (Master: iki bacak birden). Girdi başlığı "Daily max realized loss" → "Daily max loss".
- Tüm dosyalarda zorunlu kapanışlar tek blokta: bar başına tek neden, `strategy.close_all(immediately = true)` ve tek
  `exit` alert'i. Günlük zarar ve tarih-sonu kapanışları önceden bir sonraki barın açılışında doluyor ve alert
  göndermiyordu.
- Master: ihlal/tarih-sonu barında motor bazlı seans-sonu kapanışları devre dışı kalır (bacak başına tek emir, tek alert).
- Silver Bullet: günlük/toplam DD bloklarındaki kapanışlara `immediately = true` + `exit` alert'i; `flatSent` aynı barda
  çift kapanışı önler. → Açık bulgu 8.

### E7 — Trailing min adımı (NY, Master S1, MTF)
- `Min trail step (ticks)` (4; Master'da `S1 min trail step (ticks)`): trailing stop yalnızca aday mevcut stop'u en az
  N tick iyileştirdiğinde ilerler; `stop_update` alert'i de yalnızca o zaman gider (`config/risk.yaml`
  `trail_min_step_ticks`, değişmez kural 8). MTF'de başa başa ilk çekiş bu filtreden muaf. → Açık bulgu 11.

### E8 — Tablolar (MTF, Silver Bullet)
- MTF: `Table scope` girdisi; tam-işlem istatistikleri mod (Scaled / Plan B) × örneklem (IS / OOS) kovalarında tutuluyor
  (`fullN`, `fullWin`, `fullPnl`), runner satırları giriş zamanına göre süzülüyor.
- Silver Bullet: panoda `Table scope` + `Tot R` satırı (toplanan ama gösterilmeyen `stopArr` / `dirArr` ile). Kazanma
  oranı ve işlem sayısı satırları kapsama göre; canlı durum satırları kapsamdan bağımsız.

### E9 — Başlıklar ve test varsayılanları (6 dosya)
- `strategy()` maliyet varsayılanları MES profili: `commission.cash_per_contract` 0,50 (taraf başı), slipaj 1 tick
  (NY/Asian/Master: %0,01'den; SB: 2,0 / 2 tick'ten; MR: %0,02 / 2 tick'ten; MTF zaten böyleydi).
  Uyarı: SPY'de bu varsayılan hisse başı $0,50 keser → Properties'ten değiştirilmeli (başlıklarda yazıyor).
- Başlangıç sermayesi 10.000 → 100.000 (FTMO 2-Step 100k profili; `Risk base = Initial Capital` ile işlem başı risk 500 $)
  ve `margin_long = margin_short = 0`. Gerekçe: 10.000 ile MES'te çoğu işlem 1 kontratın altında kalıp atlanır;
  TradingView'ın varsayılan %100 marjini pozisyonun tüm notional'ını teminat ister, risk-temelli vadeli boyutu bunu aşar.
  Marjin davranışı TradingView'da doğrulanmadı → doğrulama listesi B.
- NY ve Master başlığı "(S&P 500: MES / ES / US500.cash / SPY)"; Asian başlığında SPY yok (çalışmıyor). Asian ve
  Master S2 için "~23 saatlik seans gerekir" notu. → Açık bulgu 2 (kısmen) ve 9.

### E10 — Doküman
- `pine/README.md` gerçek dosya adlarıyla; `docs/SPEC.md` §3.1 (ortak kurallar), §3.4.1–3.4.4 (Master, Silver Bullet,
  Mean Reversion, MTF semantiği), §4.3 (E-maddeleri); `docs/tv-verification-checklist.md` yeni varsayılanlar ve
  E-kontrolleriyle. → Açık bulgu 10.

## Denetim sonrası sertleştirme — E11–E12 (2026-11-13)

Repaint / lookahead / bar-içi dolum denetimi: `docs/audits/2026-11-13-repaint-lookahead-denetimi.md`. Sinyal üretiminde
repaint ya da lookahead kaynağı bulunmadı; iki küçük sertleştirme yapıldı. Hiçbir dosya henüz TradingView'da derlenmedi.

Commit'ler: `a5af400` E11 · `159d883` E12.

### E11 — Yürütme modeli bayrakları açıkça yazıldı (6 dosya)
- `calc_on_every_tick = false`, `calc_on_order_fills = false`, `process_orders_on_close = false` artık her `strategy()`
  bildiriminde (NY, Asian ve Master'da hiçbiri yoktu; Silver Bullet ve Mean Reversion'da `calc_on_order_fills`, MTF'de son
  ikisi eksikti). Değerler zaten varsayılandı: **davranış değişmez**. Amaç: niyet kodda görünsün ve Properties'teki
  "Recalculate" kutuları yanlışlıkla açılırsa fark edilsin (doğrulama listesi B).

### E12 — Limit emirleri 1 tick geçince dolar (NY, Asian, Master, Mean Reversion, MTF)
- `backtest_fill_limits_assumption = 1` (Silver Bullet'ta B4 ile zaten vardı). TP limitleri artık fiyat seviyeye
  değdiğinde değil, **1 tick geçtiğinde** dolar. Davranış notu: tam TP seviyesinde dönen barlarda işlem TP ile kapanmaz;
  sonuçlar bir miktar kötüleşir (daha gerçekçi). MTF Plan B'de 1,5R eşiği emir değil gözlem olduğu için
  (bar high/low ≥ TP1) değişmedi.

## Faz 2 ön dilimi — iskelet, config şemaları, Monte Carlo (2026-11-13)

Kullanıcı kararı: denetimin önerdiği sırayla (`docs/audits/2026-11-13-repaint-lookahead-denetimi.md`, bölüm 6) Faz 2'nin
fiyat verisi ve derlenmiş Pine tabanı gerektirmeyen kısmı Faz 0 kabulü ve Faz 1 beklenmeden yapıldı (`docs/SPEC.md` §6
başındaki not). `pine/` ve `config/` dosyalarına dokunulmadı. Kararlar: `docs/adr/0002-…`, `docs/adr/0003-…`.
Kullanım: `docs/monte-carlo.md`.

Commit'ler: `5d9f2ac` iskelet · `79d7ecc` config şemaları · `1a5a365` prop günü · `063fc42` işlem listesi ·
`a45432e` Monte Carlo · `5f0e222` CLI ve rapor.

### Proje iskeleti
- Python 3.12 (uv kendi kopyasını yönetir; sistemdeki 3.14'e dokunulmaz), `uv_build`, ruff, mypy --strict (`src/`),
  pytest + hypothesis; sürümler `uv.lock` ile sabit. `uv` bu makineye `pipx install uv` ile kuruldu (0.13.8).
- Çalışma zamanı bağımlılıkları: pydantic, PyYAML, numpy. pandas, pydantic-settings ve diğerleri onlara ihtiyaç duyan
  faza bırakıldı. Yalnızca içinde kod olan paketler açıldı (`core`, `risk`, `backtest`).
- ruff Markdown dosyalarına dokunmaz (biçimleyicisi kod bloklarını yeniden yazıyor); pytest uyarıları hata sayar.

### Config şemaları (`src/propbot/risk/`) — denetim Y1, Y7–Y9
- `config/risk.yaml` ve `config/venues/*.yaml` için pydantic v2 modelleri: katı tipler (`true` ya da `"0.25"` sayı
  olmaz), bilinmeyen / eksik / yinelenen anahtar hatadır, bütün anahtarlar zorunludur.
- Enum'lar: `platform` (`mt5`), `max_loss_type` (`static`, `trailing_eod`), `daily_loss_basis`
  (`max_balance_equity_at_reset`, `balance_at_reset`). `daily_reset`: tırnaklı `"HH:MM"` + `zoneinfo` ile çözülen saat
  dilimi (tırnaksız `22:30` YAML'da 1350 sayısıdır; açıklayıcı hata verir).
- Kod tavanları: `risk_per_trade_pct ≤ 0,5` · `max_positions_per_strategy = 1` · `self_request_budget_per_day ≤ 300`.
  Dosyalar arası: `daily_cap_pct ≤ 0,5 × daily_loss_limit_pct` · `daily_loss_limit_pct < max_loss_pct` · istek bütçesi
  ≤ firma eşiği. Tavanı aşan değer **yükleme hatasıdır**, sessizce kırpılmaz (ADR-0003); günlük bütçe yine `min(…)`.
- `null` yalnızca VERIFY (`rules_verified_at`, `min_trading_days`, `server_tz`) ve çalışma-anı (`contract.*`)
  alanlarında; `unresolved_fields()` raporlar. `rules_freshness()`: boş / bayat (> 30 gün) / ileri tarihli / taze.
- `uv run propbot config check`: şema, çapraz kurallar, günlük bütçe, mod kilidi ve boş alanlar; sorun varsa çıkış kodu 1.

### Prop günü (`src/propbot/core/propday.py`)
- `prop_day()`: UTC anını venue'nun `daily_reset` saat dilimi ve saatine göre prop gününe çevirir (değişmez kural 9).
  ABD ve AB yaz saati geçişlerinin farklı haftaları (2026-03-08 / 03-29, 2026-10-25 / 11-01) testlidir: Prag gece
  yarısı New York'ta 18:00, yalnızca bir taraf yaz saatindeyken 19:00'dır.

### Monte Carlo prop simülatörü (`src/propbot/backtest/`) — SPEC §6.5
- Girdi: belgelenmiş genel CSV (`entry_time, exit_time, r` + isteğe bağlı `strategy_id, mae_r`). Gün bazında blok
  bootstrap (işlemsiz hafta içi günler havuzda), isteğe bağlı rastgele işlem atlama, işlem başına sabit R maliyet.
- Kurallar yalnızca venue profilinden: günlük kayıp (reset'teki bakiyeden), statik toplam kayıp, faz hedefleri, minimum
  işlem günü. İhlal özsermayeyle denetlenir; işlem içi yüzen zarar varsayılan olarak stop'a kadar sayılır (kötümser).
- Çıktı: P(faz 1), P(iki faz), P(ihlal) faz × neden, sonuçlanmayan yollar, geçme süresi (piyasa günü), maks. düşüşün
  %95 / %99 dilimleri ve beklenen kuyruk kaybı. Türkçe rapor: `uv run propbot montecarlo …`.
- Aynı tohum aynı raporu verir (varsayılan 10 000 yol). Sağlık testi: E = 0, RR 1:2 durumunda faz 1 için analitik
  0,4959, iki faz için 0,3270; simülasyon 4 standart hata içinde. Modelleme kararları ve sınırlar: ADR-0002.
- Rapor olasılık verir, Go/No-Go hükmü vermez. `tests/fixtures/trades/synthetic_trades.csv` uydurma bir listedir;
  yalnızca aracın çalıştığını gösterir.

### Bu dilimdeki kullanıcı kararları (2026-11-13)
- **Düzleştirme eşiği:** istekteki "günlük limitin %80'inde düzleştir" değerlendirildi; daha sıkı olan
  `flatten_when_daily_buffer_below_pct: 40` (limitin %60'ında) korundu. `config/risk.yaml` değişmedi.
- **Erimeye göre risk küçültme ("dinamik Kelly"):** yalnızca simülatörde, varsayılanı kapalı deneysel seçenek
  (`--dd-risk-steps`); bot kuralı değil → açık bulgu 27.
- **`min_trading_days`:** profilde boş kaldı; simülatör `--assume-min-trading-days N` olmadan çalışmaz ve raporda
  VARSAYIM yazar.
- **İşlem listesi:** şimdilik genel CSV → açık bulgu 26.

## FTMO kural doğrulaması (2026-11-13)

Kullanıcı kararı: "kullanıcı doğrulaması" şartı (SPEC başlığı, §8.1) için yetki Claude Code'a devredildi. FTMO'nun herkese
açık resmi sayfaları 2026-11-13'te okundu; alan alan sonuç ve kaynaklar:
`docs/compliance/2026-11-13-ftmo-kural-dogrulamasi.md`. Commit: `4fc7bbd` profil.

- `config/venues/ftmo_2step_100k.yaml`: `rules_verified_at: 2026-11-13` · `min_trading_days: 4` ·
  `daily_loss_basis: balance_at_reset` (önceki değer temkinli varsayımdı). Hedefler (%10 / %5), günlük %5, toplam %10
  statik, reset (Prag gece yarısı), haber kuralı (fonlu Standard, ±2 dk), EA izni ve günde 2.000 istek eşiği resmi
  sayfalarla aynı çıktı; değerleri değişmedi.
- `uv run propbot config check`: kurallar "taze (0 gün; sınır 30)". Boş kalanlar: `server_tz` (VERIFY) ve çalışma anı
  `contract.*` alanları.
- Botun kendi günlük tabanı değişmedi (SPEC §8.1: reset anındaki `max(balance, equity)`); profil artık firmanın gerçek
  bazını söylüyor. Stratejiler reset'te pozisyon taşımadığı için Monte Carlo sonucu etkilenmez.
- `docs/monte-carlo.md`: hızlı başlangıç komutunda `--assume-min-trading-days` artık gerekmiyor.
- Doğrulama 30 gün geçerli (2026-12-13). Yenileme tarayıcıda elle yapılır: FTMO'nun Şartlar ve Koşulları (madde 9.4.6)
  siteden otomatik araçlarla bilgi toplamayı yasaklıyor.

## Faz 1 — TradingView → webhook → Telegram sinyal botu (2026-11-13)

Kullanıcı kararı (2026-11-13): Faz 1 planı, dosya yapısı, ADR kararları ve commit sırası onaylandı; altı açık soru
cevaplandı. **Emir yolu yazılmadı; tek mod `signal`.** Kararlar: `docs/adr/0000`, `0004`, `0005`. Kurulum ve işletim:
`docs/runbook.md`. Spesifikasyon: `docs/SPEC.md` §5 (uygulanan davranışa göre güncellendi, sürüm 0.3).

**Durum:** kod ve testler tamam (uygulama denetimi sonrası 3593 test, ruff, mypy --strict; ilk teslimde 3416). SPEC §5.7'deki 10 işlem günlük kabul koşusu **yapılmadı**:
Faz 0 script'leri TradingView'da henüz derlenmedi, alarmlar kurulmadı, bot bir sunucuya kurulmadı. Hiçbir şey gerçek
TradingView ya da gerçek Telegram'a karşı denenmedi (bu ortamdan ikisine de erişim yok).

Commit'ler (sırayla): `cd00945` bağımlılıklar · `997847c` saat yardımcıları · `fc650f9` şema · `108823b` + `2a73b8e` store ·
`f3a4788` tv_alerts config · `db3b4d0` sırlar ve log · `00f4d0e` biçimleyiciler · `7698461` alıcı · `26e28c8` boyutlandırma ·
`a93af6f` mod kilidi · `4b8abce` izleme pencereleri · `efe354b` + `4130763` Telegram · `fb3508f` + `c68ce3c` işleyici ve
heartbeat · `81c8673` CLI serve + rapor · `ab368b4` Pine heartbeat · `74318c5` dağıtım ve runbook.

### Kullanıcı kararları (2026-11-13)
- **TradingView webhook kuralları** kullanıcı tarafından resmi sayfadan aktarıldı (bu ortamdan `tradingview.com`'a erişim
  yok): dört kaynak IPv4 adresi, yalnızca 80/443, 3 sn zaman aşımı, mTLS yok → `config/tv_alerts.yaml`.
- **Sözleşme değerleri boşken** hata yok: mesaj "boyut hesaplanamadı", en fazla riski ve formülü yazar.
- **Günlük bütçe temkinli:** iletilen her giriş sayılır; `/atladim` almadığın girişin riskini düşer (ADR-0005).
- **Tazelik** yükteki `tf`'den: bar kapanışı = `bar_time` + bar süresi; `now ≤ kapanış + 60 sn`.
- **Şema v1** kaldı; `reason` isteğe bağlı alan olarak kabul edilir. Altı Pine stratejisine dokunulmadı.
- **Sıfır maliyetli dağıtım:** DuckDNS + Caddy ya da kendi bilgisayarın + Tailscale Funnel / Cloudflare hızlı tünel.

### Ne yapıldı
- **Alıcı** (`signals/receiver.py`): `POST /tv/webhook`; sıra: istemci IP'si → gövde ≤ 4 KB → JSON → token → hız sınırı →
  şema → kayıt; hemen 200. Yinelenen anahtar `duplicate`, geç gelen `stale` olarak kaydedilir. Token veritabanına,
  günlüğe ve yanıta girmez. `GET /healthz` yalnızca doğrudan yerel isteğe yanıt verir.
- **Şema** (`signals/schema.py`, `core/timeframe.py`): altı stratejinin ve heartbeat göstergesinin alert kurucuları
  okunarak çıkarıldı (33 gövde biçimi testlerde); katı JSON, eylem başına kurallar; hata yalnızca alan **adlarını** taşır.
- **Store** (`store/db.py`): SQLite WAL, `synchronous=FULL`, tek transaction'da sonuç + bütçe + giden kutusu;
  `UNIQUE(strategy_id, symbol, bar_time, action)` (değişmez kural 5).
- **İşleyici** (`signals/processor.py`, `notify/messages.py`): karar tablosu saf fonksiyon; giriş için saat şüphesi →
  KILL/duraklat → bayat → stop çok geniş → bütçe → iletildi. Stop güncellemeleri **mesafe uzayında** karşılaştırılır:
  kullanıcının kendi stop'unu genişletecek güncelleme iletilmez (değişmez kural 2). İşlenemeyen sinyal `error` olarak
  kenara alınır, alarm gider, kuyruk durmaz.
- **Boyutlandırma** (`risk/sizing.py`): aşağı yuvarlama, `min_lot` altı "ATLA", tam (kesirli) aritmetik, özellik testleri.
- **Telegram** (`notify/commands.py`, `notify/telegram.py`): tek özel sohbet; `/durum /pnl /risk /duraklat /devam /kill
  /mod /saglik /atladim /aldim /yardim`; PIN kilidi ve PIN'li mesajın silinmesi; giden kutusu göndericisi (en çok 30 sn
  geri çekilme, "iletilemedi" ile "iletim belirsiz" ayrımı); `US500.cash` ve `/atladim 126` `code` olarak gider.
- **Alarm hattı izleyicisi** (`signals/heartbeat.py`, `signals/watch.py`): izleme penceresinde 75 dk sessizlik, hat geri
  gelince bilgi, bot kapalı kaldıysa açılışta alarm, alarm bitiş hatırlatması (09:00 TR'den sonra, günde bir).
- **Sırlar ve log** (`settings.py`, `logging_setup.py`): `.env` 0600 değilse başlamaz; hata metinleri değer içermez; tüm
  kayıtlar (kütüphaneler dahil) tek zincirden geçer ve sırlar `***` olur.
- **Servis ve CLI** (`signals/service.py`, `cli.py`): `propbot serve --mode signal` (diğer modlar gerekçesiyle
  reddedilir, değişmez kural 1); arka plan görevi ölürse süreç hata koduyla çıkar; `propbot signals report` kabul
  kanıtını üretir; `propbot config check` `tv_alerts.yaml`'ı da doğrular.
- **Pine:** `pine/heartbeat_indicator.pine` (gösterge, emir yok). **Derlenmedi.**
- **Dağıtım:** `deploy/propbot.service`, `deploy/Caddyfile`, `docs/runbook.md`.

### Tasarım denetimi (üç bağımsız mercek: güvenlik · doğruluk · uygunluk) — kabul edilen bulgular
Sözleşme uygulanmadan önce denetlendi; 26 bulgunun hepsi kabul edildi. Davranışı değiştirenler:
- Hız sınırı token'dan **sonraya** alındı: dört kaynak adres bütün TradingView kullanıcılarının ortak adresi; yabancı biri
  sınırı tüketip kendi alert'lerimizi 429'a düşüremesin. İzinli adresten gelen kimliksiz istekler için saatte en fazla
  bir alarm.
- İstemci IP başlığı yalnızca `X-Forwarded-For` olabilir (başka başlık bir tünelden olduğu gibi geçip taklit edilebilir).
- Zaman aşımına uğrayan gönderim "iletilmedi" sayılmaz ("[İLETİM BELİRSİZ]", bütçe sayılmaya devam eder).
- Stop genişletme denetimi TradingView seviyelerinde değil, kullanıcının uyguladığı mesafelerde yapılır.
- Bar kapanışından önce gelen giriş alert'i saat uyuşmazlığıdır: "ATLA" (değişmez kural 3).
- `/duraklat` ve `/kill` gecikse de uygulanır; bot kapalıyken yazılan komutlar atılmaz; `/atladim` yalnızca mesajı
  gönderilmiş girişi seçer ve yanıtında adını söyler.
- Heartbeat: pencerenin son saatinde kesilen hat da bildirilir; yeniden başlatma sessizliği gizlemez.
- `InvalidToken` gibi sır taşıyabilen istisnalar ham traceback olarak basılmaz.

### Uygulama denetimi (yazılan kod üzerinde karşıt mercekler) — bulgular ve düzeltmeler
Kod bittikten sonra bağımsız mercekler kodu kırmaya çalıştı; her bulgu ayrıca doğrulandıktan sonra düzeltildi.

| Bulgu | Sorun | Düzeltme | Commit |
|---|---|---|---|
| SEC-1 | Komut işleyici her mesajı PIN ile karşılaştırıp yanıtı ona göre seçiyordu: `/kill` ve `/devam` dışında, deneme sayacına takılmadan "bu PIN mi?" sorusunu yanıtlayan bir kehanet | Silinecek mesaj biçimine göre seçilir (6+ haneli sayı ya da `/kill`·`/devam` sonrası metin); yanıt sayıyı yinelemez; PIN yalnızca iki komutun içinde karşılaştırılır. Özellik testi: iki farklı PIN için yanıtlar aynı | `c1ee9d2` |
| SEC-3 | Kimliksiz istek alarmının saatlik kapısını yabancı bir istek açınca, eski token'la gönderen kendi alarmımız yalnızca sayılıyor, bir saat sonra başka bir ret gelmezse hiç bildirilmiyordu | Sayılar alarm gerçekten gidene kadar tutulur; izinli adresten gelen sonraki herhangi bir istek (geçerli heartbeat dahil) bekleyen alarmı tetikler; store reddederse 5 dk sonra yeniden denenir | `0c284b5` |
| LIM-1 | Limit giriş mesajı "İPTAL mesajı gelene kadar bekler" diyordu; TradingView'daki limit dolup kullanıcının emri dolmazsa İPTAL hiç gelmez, emir süresiz bekler | Limit giriş: "İPTAL ya da ÇIKIŞ mesajı gelene kadar, en geç seans sonuna kadar bekler" + "seans sonunda emrin hâlâ bekliyorsa SİL"; limit girişin (ya da görülmemiş girişin) `[ÇIKIŞ]` mesajı bekleyen emri de iptal ettirir | `fe0b484` |
| MON-1 | Kapalı kalma yalnızca 90 sn'den uzun boşlukta bildiriliyordu; birkaç saniyede bir çöküp kalkan servis (systemd `RestartSec=5`) Telegram'da görünmüyordu | İzlenen pencereye denk gelen kısa boşluk da bildirilir (en çok 30 dakikada bir); bastırılanlar sayılır, sonraki alarm "N. kez" der | `2d93617` |
| CMD-1 | Yardım ve kullanım satırlarındaki `/atladim [id]` Telegram'da dokunulabilir bağlantıydı; dokununca çıplak `/atladim` gider ve **son** girişin riskini bütçeden düşerdi | `/atladim` ve `/aldim` geçtiği her yerde `code` olarak gider (dokununca gönderilmez) | `6927709` |
| SEC-2 | Token, grafikte script'in durum satırında ve (açıksa) alarmın e-posta/uygulama bildirimlerinde görünür | Heartbeat göstergesinde girdi `display.none` (derlenmedi, doğrulama listesi H); runbook §8.1–8.2: yalnızca Webhook URL bildirimi, paylaşım uyarısı, token yenileme adımları. Altı strateji dosyası için → açık bulgu 45 | `19d4381` |
| SYM-1 | Alert'teki TradingView sembolü venue sembolüyle eşleştirilmiyor | Karar gerekir → açık bulgu 46 | — |

İkinci tur (2026-11-15): güvenlik ve işleyici mercekleri düzeltilmiş kodu yeniden taradı; Telegram, entegrasyon ve Pine
mercekleri ilk kez çalıştı (ilk denemede kullanım sınırı yüzünden yarıda kalmışlardı). Her bulgu için denetçinin yeniden
üretme betiği düzeltmeden sonra yeniden çalıştırıldı; kritik kurallar mutasyonla sınandı (bu turda 44 mutantın 44'ü yakalandı).

| Bulgu | Sorun | Düzeltme | Commit |
|---|---|---|---|
| SEC-4 | python-telegram-bot'un kuramadığı tek bir güncelleme: updater aynı partiyi sonsuza dek yeniden istiyor, hiçbir komut işlenmiyor (`/duraklat`, `/kill` dahil), uyarı yok, kütüphane bekleyen bütün mesajları (yanlış yazılmış PIN, yabancının metni) her seferinde logluyordu | `telegram.*` kayıtları değersiz loglanır (şablon + istisna türü/konumu); `ResilientBot` partiyi ham JSON olarak yeniden alır, güncellemeleri tek tek kurar, kuramadığını atlayıp Telegram'a onaylar; "N mesaj okunamadı" alarmı; aşılamayan hatada 1–30 sn geri çekilme + "komutlar işlenmeyebilir" alarmı | `d4d08cf`, `e81ab51` |
| SEC-5 | `propbot serve`'ün sırları maskeleyiciye vermesi ve istisna kancalarını kurması testsizdi: sır listesi boşaltılınca ya da kancalar kaldırılınca bütün testler yeşil kalıyordu | Gerçek giriş noktası (`main(["serve", …])`) üzerinden testler; olay döngüsü hata işleyicisi `run_service` ile test edilir | `7ce8c29` |
| SEC-6 | `sys.unraisablehook` varsayılanda kalıyordu: sonlandırıcıda / çöpe giden coroutine'de oluşan istisna metni maskelenmeden stderr'e yazılırdı | Üçüncü kanca da maskeleyen günlüğe bağlandı (asla istisna fırlatmaz) | `bce99b5` |
| SEC-7 | Birim dosyası, depo `/home` dışındaysa `ProtectHome=true` önerir; Ubuntu 22.04'te uv yorumlayıcıyı ev dizinine kurduğu için servis başlayamazdı | Yorum iki koşulu ve denetim komutunu söyler; runbook §4 ve §11 (`status=203/EXEC`) | `5251afe` |
| MON-2 | Kısıtlama altında sayılan yeniden başlatmalar yalnızca **sonraki** yeniden başlatmada bildiriliyordu: kendiliğinden biten çökme döngüsü "birkaç saniyelik tek yeniden başlatma" olarak kalıyor, günler sonraki olağan yeniden başlatma "120. kez" diye duyuruluyordu | Her izleyici denetimi, kısıtlama bitince bekleyen sayıyı "N kez daha yeniden başladı" diye bildirir | `b33eba9` |
| TEST-1/2 | İki kuralı kıran mutantlar yakalanmıyordu: stop güncellemesi önceki işlemin güncellemeleriyle karşılaştırılırsa; puan değeri ters uygulanırsa / tick değeri tick boyutu yerine kullanılırsa (bütün test sözleşmelerinde puan değeri 1 idi) | İşlem sınırı testi; puan değeri 5 $/puan ve 2 $/puan olan sözleşmelerle nominal, en küçük lot riski ve asgari trail adımı testleri | `1accf01` |
| TG-1 | SEC-1'in kalıntısı: `/atladim 0<tahmin>` yanıtı sayıyı sıfırsız yazdığı için maskeleme kullanıcının metnini bulamıyor, son çare satırı yalnızca tahmin PIN'e eşitse `***` basıyordu (sayaçsız, kilitsiz kehanet) | Baştaki sıfırla id reddedilir; PIN ile karşılaştıran satır kaldırıldı: `/kill` ve `/devam` dışında hiçbir kod yolu PIN'e bakmaz | `3985e11` |
| TG-2 | id'siz `/atladim`, en yeni girişin mesajı "belki ulaştı" durumundayken ondan **eski** girişi serbest bırakıyordu; işleyici de o (içinde olunan) işlemin stop/çıkış mesajlarını kesiyordu | Daha yeni, hâlâ sayılan ve teslimi kuşkulu bir giriş varsa hiçbir şey serbest bırakılmaz, id istenir | `23e3a2e` |
| TG-3 | Açılışta "belki ulaştı" işaretlemesi göndericinin ilk adımında, ilk `getMe`'den sonra yapılıyordu; Telegram yavaşken aynı sürecin işleyicisi araya yeni giriş koyabiliyor, hiç denenmemiş mesaj kuşkulu sayılıp bütçede kalıyordu | İşaretleme, işleyici başlamadan önce yapılır (yalnızca önceki sürecin bıraktıkları) | `61e1f61` |
| TG-4 | Kalıcı gönderim hatası teslim kuşkusuna bakmıyordu: önceki denemesi zaman aşımına uğramış (belki ulaşmış) giriş "GÖNDERİLEMEDİ" diye duyurulup bütçeden düşülüyordu | Kuşkulu giriş sayılmaya devam eder ve kullanıcıya sorulur; yorumlanamayan yanıtla biten gönderim de kuşkulu sayılır; yalnızca BadRequest / Forbidden / ChatMigrated / Conflict "ulaşmadı" kanıtıdır | `ecb55e4` |
| TG-5 | Sunucu saati ilerideyken oluşan PIN kilidi, saat düzeltilince hiç bitmiyordu (her denetimde "15 dk sonra") | Bir süreden uzak görünen kilit o anki saatle yeniden yazılır; geri sayar ve biter | `3960598` |
| REP-1 | Kabul raporu "iletilen giriş"i işleyicinin kararından sayıyordu; mesajı süresi dolan giriş de iletilmiş görünüyor, §5.7 satırı yalnızca gönderilebilen mesajların medyanına bakıp "SAĞLANDI" diyordu | Teslim, giden kutusundan okunur: "ulaşmayan" sütunu, "Telegram teslimi" satırı, CSV'de `delivery`; ulaşmayan mesaj varsa "SAĞLANMADI", bekleyen varsa "EKSİK" | `553e263` |
| MON-3 | Kapalı kalma denetimi yalnızca süreç başlangıcında çalışıyordu: uykuya geçen makine / duraklatılan VM aynı süreçle devam ettiği için izlenen penceredeki delik hiç bildirilmiyordu | Her denetim önceki denetimle arasındaki boşluğa bakar; 90 sn'den uzun boşluk "Bot … arasında çalışmadı" diye bildirilir | `813a3b4` |
| PORT-1 | Portu alamayan ikinci `serve` (yanlışlıkla başlatılan kopya), vazgeçmeden önce canlı veritabanında izleyici denetimi çalıştırıyor (sahte "yeniden başladı" alarmı), bekleyen girişi kuşkulu işaretliyor, kutunun başını kendisi gönderiyor ve polling başlatıyordu; sonra kod 3 ve boş stderr ile çıkıyordu | Soket her şeyden önce bağlanır (`HATA: … dinlenemiyor`, kod 1); veritabanı başına tek `serve` (`<db>.lock`); duran servis polling başlatmaz | `45b3eda` |
| TEST-3 | ADR-0004'ün ana kuralı (arka plan görevi ölürse süreç hata koduyla çıkar) testsizdi | Gerçek `run_service`, gerçek loopback portunda: Telegram token'ı reddedince `EXIT_FAILED` döner | `45b3eda` |
| DEP-1 | Birim dosyası "işaretli üç satırı ayarla" diyordu, `ReadWritePaths=/opt/propbot/data` ise bloğun dışındaydı; başka depo yolunda servis başlayamazdı | `ReadWritePaths` işaretli bloğa alındı (dört satır); test, blok dışında kurulum yolu kalmadığını denetler | `a28160e` |
| TV-3 | Heartbeat atımı yalnızca saat sınırını kapsayan bara bağlıydı: 1 dakikalık grafikte HH:59 barı yoksa o saatin atımı kayboluyor (2 saat boşluk → sahte "hat sessiz" alarmı); bar süresi 60'ı bölmeyen grafiklerde atımlar 90 dk aralıklıydı | Yeni aralıkta kapanan ilk bar atım gönderir; bar süresi aralığı bölmeyen grafik reddedilir. **Derlenmedi** (doğrulama listesi H) | `3443e2b` |
| TV-4 | "Pine'ın gönderebildiği her gövde" derlemesindeki MTF `before_tp1` stop güncellemesi gerçekte gönderilemez; test buna "genişletme reddi" bekliyordu | Yerine gerçek biçim (TP1 sonrası runner trail'i, hedefsiz) kondu; açık bulgu 35 düzeltildi | `30c6b83` |
| TV-1 | Master ile bağımsız NY Open / Asian Sweep script'leri aynı `strategy_id`'leri gönderir; belgeler hepsini alarmlamayı öneriyordu, bot iki akışı tek işlem sanar | Belgeler (runbook §8, `pine/README.md`, SPEC §5.1, `config/tv_alerts.yaml`): bir `strategy_id` tek kaynaktan → açık bulgu 47 | `d177962` |
| TV-2 | Grafikteki sembolün gerçek zamanlı veri gerektirdiği hiçbir yerde yazmıyordu; gecikmeli veride her giriş "BAYAT", takip mesajları sessizce düşer, heartbeat ise gelir | runbook §1 ön koşul (VERIFY), §11 sorun giderme, doğrulama listesi H → açık bulgu 48 | `d177962` |

Doğrulama turu (2026-11-15/16): iki bağımsız ajan ikinci turun düzeltmelerini dondurulmuş bir kopyada sınadı (kendi
betikleri ve mutasyonlarıyla). 18 düzeltmenin 17'si bulgusunu tam kapatıyordu, biri (REP-1) kısmen; ayrıca aşağıdakiler
bulundu ve düzeltildi. Bu turdan sonra yeni bir denetim turu **yapılmadı**.

| Bulgu | Sorun | Düzeltme | Commit |
|---|---|---|---|
| V-1 | Polling "takılı" durumdayken kapanış, updater'ın son (kısa) sorgusunda geri çekilmeyi (30 sn'ye kadar) bekliyor, Telegram görevi kapatılmak yerine iptal ediliyordu; kurulmuş güncellemeler geri çekilme bitene kadar tutuluyor, o sırada durdurulursa yitiyordu | Geri çekilme yalnızca uzun sorguda ve verilecek güncelleme yokken; "takılı" = parti bütün olarak onaylanamadı | `258961f` |
| V-2 | REP-1 kalıntısı: hiç mesaj üretmemiş sinyaller (işlenemeyen, kuyrukta bekleyen) hükmü etkilemiyordu: 5 girişin 3'ü iletilmişken "SAĞLANDI" | `hata` sonucu "SAĞLANMADI", kuyruktaki sinyal "EKSİK"; hüküm kaybedilen/açık kalan her şeyi adıyla yazar | `f557963` |
| V-3 | Soket bağlanıyor ama dinlemeye alınmıyordu: aynı anda başlatılan başka bir kopya aynı portu bağlayıp görevlerini başlatabiliyordu; açılamayan kilit dosyası ham traceback veriyordu | Soket bağlanır bağlanmaz dinler; kilit dosyası hatası `HATA` satırı | `bbb7338` |
| V-4 | **Benim eklediğim runbook cümlesi yanlıştı:** "denemek için ayrı `--db` ve `--port` ver". O kopya aynı bot token'ını dinler ve `/kill`'i canlı servisin elinden alabilir ("KILL etkin" yanıtı gelir, sinyaller sürer) | Cümle düzeltildi: önce servisi durdur; yan yana deneme için ayrı bot token'ı. Kod kilidi veritabanı başınadır, token başına değil (açık bulgu 53) | `8fe4f3c` |
| V-5 | Doğrulama listesi H "HH:00 barı renklenmemeli" diyordu; yeni atım kuralında seans arasından sonraki ilk bar (CME'de 18:00) renklenir | Madde beklenen istisnayı söyler | `8fe4f3c` |
| V-6 | Beş kural testle sabitlenmemişti (mutantlar hayatta kalıyordu): yeniden denemede ulaşan giriş `/atladim`'ın hedefidir; yanıtlar PIN'e göre yeniden yazılmaz; gönderimden önceki hata "belki ulaştı" değildir; `serve` kilidi servis bitene kadar tutar; kısa sorgu beklemez | Testler | `258961f`, `bbb7338`, `938f732` |

Mutasyon denetimi (kendi koşularım, üç turda): 55 mutantın 55'i yakalandı; doğrulayıcıların hayatta kalan mutantlarının her biri
için test eklendi.

### Testler
- 3593 test (`uv run pytest -q`; 1 test bu ortamda atlanır: IPv6 loopback yok), ruff, `mypy --strict` (36 kaynak dosya)
  temiz (uygulama denetiminin iki turu ve doğrulama turu sonrası; ilk teslimde 3416).
- Alıcının her adımı ve dalı; 33 gerçek gövde biçimi şemadan, alıcıdan ve işleyiciden geçer.
- `/kill`, PIN kilidi, yetkisiz sohbet, grup sohbeti testleri (`tests/unit/test_commands.py`, `test_telegram.py`).
- Uçtan uca (`tests/integration/test_service.py`): gerçek uygulama + store + işleyici + izleyici + python-telegram-bot;
  yalnızca TradingView (test istemcisi) ve Telegram'ın HTTP katmanı (çevrimdışı sahte) değiştirilir.
- Gerçek `propbot serve` süreci sahte sırlarla yerelde denendi (alıcı, yinelenen istek, IP ve token reddi, temiz kapanış).

### SPEC §5.7 kabul durumu
| Madde | Durum |
|---|---|
| 10 işlem günü kesintisiz; kaçan/çift sinyal 0; uçtan uca medyan gecikme < 5 sn | **Bekliyor** — kurulum ve TradingView alarmları gerekli (`docs/runbook.md` §12; ölçüm: `propbot signals report`) |
| Tüm doğrulama hata yolları testli | Sağlandı (`tests/unit/test_receiver.py`, `test_alert_schema.py`) |
| `/kill` ve yetkisiz sohbet testleri yeşil | Sağlandı (`tests/unit/test_commands.py`, `test_telegram.py`, `tests/integration/test_service.py`) |

### Sıradaki adımlar (kullanıcı) ve açık kararlar
Faz 1'in kodu bitti; kabul koşusu kullanıcının yapacağı işlere bağlı. Sırayla:

1. **TradingView'da derle ve doğrula:** altı strateji (Faz 0 doğrulama listesi A–G) ve `pine/heartbeat_indicator.pine`
   (liste H). Derleme hatalarını ve liste sonuçlarını Claude Code oturumuna yapıştır.
2. **MT5'ten sözleşme değerlerini yaz:** `config/venues/ftmo_2step_100k.yaml` → `contract.*` (runbook §3.3). Boşken bot
   çalışır, lot yerine "boyut hesaplanamadı" yazar.
3. **Kur:** runbook §1–§9 (Seçenek A ya da B), duman testi, heartbeat alarmı. TradingView verisi gerçek zamanlı olmalı
   (açık bulgu 48); alarm kurulan grafiğin sembolü venue profilindeki `signal.tv_symbols` listesinde olmalı.
4. **10 işlem günü kabul koşusu:** runbook §12; her gün `propbot signals report`.

Karar bekleyen yedi bulgu (32, 33, 45, 46, 47, 51, 53) 2026-11-16'da karara bağlandı ve uygulandı: aşağıdaki bölüm.

## Faz 1 — kullanıcı kararları 32, 33, 45, 46, 47, 51, 53 (2026-11-16)

Kullanıcı, Faz 1 teslim raporundaki yedi açık kararı verdi ve dal geçmişinin temizlenmesini istedi. Faz değişmedi (Faz 1,
tek mod `signal`, emir yolu yok). Kararlar: `docs/adr/0004` (güncelleme), `0005` (güncelleme), `0006` (yeni).

| Karar | Kullanıcının tercihi | Uygulama |
|---|---|---|
| 32 | Kurallar bayatken bugünkü davranış kalsın (uyarı satırı + günlük alarm; girişler kesilmesin) | Kod değişmedi. ADR-0005, SPEC §8.1, runbook §10 ve CLAUDE.md değişmez kural 3'e `signal` modu notu |
| 33 | Çok dar stop'ta otomatik "ATLA"; eşik 1,5 puan, config'te ayarlanabilir | Venue profili `signal.min_stop_points: 1.5`; sonuç `skip_stop_too_tight` |
| 45 | Token girdisi grafikte görünmesin | Altı strateji dosyasında `display = display.none` (E13) |
| 46 | Yedi TradingView sembolü `US500.cash` ile aynı ölçekte kabul edilsin | Venue profili `signal.tv_symbols`; listede olmayan sembolün girişi `skip_symbol` |
| 47 | Master bacaklarına ayrı kimlik; alıcı tanısın | `master_ny_orb_fvg`, `master_asian_sweep` (E14) |
| 51 | `/devam` mesajlarda dokunulamaz olsun | Her `/devam` geçişi `code` varlığı |
| 53 | Aynı bot token'ıyla ikinci kopyayı kod da engellesin (409 Conflict dahil) | Makine genelinde bot kilidi + varışında Conflict gören kopya beklemeye geçer (komut almaz, giriş iletmez) |

Commit'ler: `21da0db` /devam · `3d2b72f` sembol listesi ve asgari stop · `fb202c9` Master kimlikleri · `b5aa6cd` token
girdisi · `43c87d9` bot kilidi ve Conflict (ilk biçim) · `fe0c388` dokümanlar · `d264050` bekleme tasarımı (birinci
denetim turu) · `040f6ed` Pine çağrı sayımı testi · `6b569f2` dokümanlar · `96d5000` + `efc9381` ikinci denetim
turunun düzeltmeleri · `453a37f` + `6f38dad` üçüncü turunkiler (aşağıda "Karşıt denetim").

### Karar 46 ve 33 — giriş süzgeci (`signal` bölümü, ADR-0006)
- `config/venues/*.yaml`'a zorunlu `signal` bölümü: `tv_symbols` (1–32 sembol, yinelemesiz, alıcı şemasının kabul ettiği
  biçimde) ve `min_stop_points` (> 0). Eksik ya da hatalı bölüm yükleme hatasıdır (ADR-0003).
- Gerçek profildeki liste kullanıcının verdiği yedi semboldür: `CME_MINI:MES1!`, `CME_MINI:ES1!`, `US500.cash`,
  `OANDA:SPX500USD`, `FOREXCOM:SPXUSD`, `PEPPERSTONE:US500`, `CAPITALCOM:US500`. Eşleşme **birebirdir** (büyük/küçük
  harf dahil). Her beslemenin ölçeği kullanıcının beyanıdır; bu ortamdan doğrulanamadı (açık bulgu 55).
- İşleyici (`signals/processor.py`) giriş karar sırası: saat uyuşmazlığı → KILL → duraklatma → kopya beklemede (karar 53) →
  **sembol listesi** → bayat →
  **stop çok dar** → stop çok geniş → günlük bütçe → iletildi. İki yeni bildirim "[SİNYAL — ATLA]" başlığıyla gider,
  bütçede sayılmaz, kural alarmı taşımaz.
- Takip alert'leri: iletilmiş bir girişin stop/çıkış/iptali listeye bakılmadan iletilir (liste işlem açıkken değişmiş
  olabilir). Hiçbir girişe bağlanamayan **ve** listede olmayan sembolden gelen takip alert'i iletilmez; 10 dakikada en
  çok bir alarm (`symbol_not_listed`). Heartbeat sembole bakılmaz.
- Asgari stop tam ondalıkla karşılaştırılır (eşit olan geçer); `contract.*` boşken de geçerlidir; stop güncellemelerine
  uygulanmaz. Stop'u girişle aynı fiyata yazılmış girişi alıcı şeması daha önce reddeder (422, "Geçersiz alert gövdesi
  reddedildi (alanlar: stop)" alarmı, kaydedilmez); tick yuvarlamasından sonra girişe eşit basılan aşırı dar stop da oraya düşer
  ve "stop çok dar" sayımına girmez (denetim bulgusu; şema kuralı değiştirilmedi).
- Mesajda `signal.tv_symbols` yazılmaz, "venue profilindeki tv_symbols listesi" yazılır: Telegram `signal.tv`'yi alan
  adı sanıp bağlantı yapardı.
- **Veritabanı şema sürümü arttı** (`signals.outcome` denetimine iki sonuç eklendi: sürüm 2; bekleme sonucuyla sürüm 3,
  aşağıda karar 53). Eski sürümle oluşturulmuş dosya açılmaz; hata mesajı ve runbook §11 eski dosyanın yeniden
  adlandırılmasını söyler. Göç yazılmadı (kabul koşusu başlamadı).
- `propbot config check` "Sinyal süzgeci" bölümünü, `serve` afişi "sinyal süzgeci" satırını basar; `/durum` ve
  `signals report` yeni sonuçları adlandırır ("sembol listede değil", "stop çok dar").

### Karar 47 — Master bacak kimlikleri (E14)
- `pine/master_prop_firm_portfolio.pine`: `f_alert` çağrılarının kimlik argümanı `"ny_orb_fvg"` → `"master_ny_orb_fvg"`
  (5 çağrı + zorunlu düzleştirme), `"asian_sweep"` → `"master_asian_sweep"` (3 çağrı + zorunlu düzleştirme); başlık
  yorumu. Giriş/çıkış mantığı, girdiler ve varsayılanlar değişmedi. **Derlenmedi.**
- Alıcı şeması iki kimliği tanır; mesajlarda "Master NY ORB FVG" / "Master Asian Sweep". S1 bacağı bağımsız NY Open ile
  aynı notu taşır ("trailing stop güncellemeleri ayrı mesajla gelir").
- Master ile bağımsız script'ler aynı sembolde yan yana alarmlanabilir: aynı bardaki iki giriş iki ayrı sinyaldir, her
  birinin stop güncellemesi ve çıkışı kendi girişine bağlanır, günlük bütçede ikisi de sayılır.
- `tests/unit/test_pine_sources.py` (yeni, yalnızca kaynak metnini okur): her Pine dosyasının gönderdiği kimlikler,
  hiçbir kimliğin iki dosyada geçmediği ve alıcının tanıdığı kimliklerle birebir örtüştüğü sabitlendi.
- Kullanıcı Master'ı önceden alarmladıysa alarmı silip yeniden kurmalı (eski alarm eski kimlikleri gönderir).

### Karar 45 — token girdisi gizli (E13)
- Altı strateji dosyasında `alertToken = input.string("", "Webhook token", …, display = display.none, …)`. Tek argüman;
  mantık ve varsayılan değişmedi. Heartbeat göstergesinde zaten vardı. **Derlenmedi**: derleyicinin argümanı kabul
  ettiği ve değerin durum satırından kalktığı doğrulama listesi H'de (E13 maddesi).

### Karar 51 — `/devam` dokunulamaz
- `notify/telegram.py`: `/devam`, `/devam <PIN>`, `/devam 123456` her geçtiği yerde `code` varlığıdır (`/atladim` ve
  `/aldim` gibi). `/duraklat` ve `/durum` yanıtlarındaki `/devam` artık dokununca gönderilmez.

### Karar 53 — aynı bot token'ıyla ikinci kopya
- **Makine genelinde kilit** (`signals/service.py` `single_bot_instance`): bot kimliğiyle adlandırılmış soyut Unix
  soketi. `serve` bunu veritabanı kilidinden hemen sonra, afişten ve veritabanına dokunmadan önce alır; ikinci kopya
  `HATA: bu Telegram botuyla çalışan başka bir propbot serve var` ile çıkar. Dosya bırakmaz; `PrivateTmp` ve
  `ProtectSystem`'den etkilenmez; `deploy/propbot.service` değişmedi. Gerçek iki süreçle denendi (ayrı `--db`/`--port`).
- **Varışta Conflict → bekleme** (`notify/telegram.py`, ADR-0004): kilidin görmediği kopya (başka makine, konteyner)
  için. Kopya, Telegram bir `getUpdates` isteğini ilk kez ya da 60 sn'den uzun sessizlikten sonra yanıtladığında
  "varmış" sayılır; sonraki 60 sn'de **iki** Conflict alırsa yeni gelendir: polling'i durdurur, depoya `standby`
  işaretini yazar, "[ALARM] Bu PropBot kopyası (makine adı) BEKLEMEDE…" mesajını gönderir ve **çalışmaya devam
  eder** (yeniden başlatılıp yine beklemeye düşerse 5 dakikada en çok bir "… yeniden başlatıldı, ama … yine BEKLEMEDE"
  alarmı). Beklemede giriş sinyali iletilmez (`skip_standby`: "[SİNYAL — ATLA] … Bu kopya BEKLEMEDE"; bekleyen giriş
  mesajları düşürülür), stop güncelleme / çıkış / iptal iletilir. Yeniden başlatılan kopya 60 sn Conflict'siz yanıt
  alırsa "[BİLGİ] … yeniden ETKİN" der ve işareti kaldırır. Daha uzun süredir yanıt alan kopya botu tutar; onun "Aynı
  bot token'ıyla başka bir süreç…" alarmına "bu sırada gönderdiğin bir komut öbür sürece gitmiş olabilir: /durum ile
  kontrol et" cümlesi eklendi.
- **Yol vermeyen dinleyici:** zaten çalışan kopya, iki dakika boyunca Conflict'ten başka yanıt alamazsa (öbür süreç bu
  sürümden değil: başka bir program, webhook) o da beklemeye geçer; komutlarını alabileceğinden emin olamayan kopya
  giriş iletmez.
- Beklemeye geçerken kutuda bekleyen giriş mesajı düşürülür ve söylenir: "[İLETİLEMEDİ — ATLA] … (bu kopya BEKLEMEDE)",
  gönderimi kuşkuluysa "[İLETİM BELİRSİZ] … bu kopya BEKLEMEDE olduğu için yeniden gönderilmedi" (duraklatma / KILL
  kullanıcının kendi seçimidir; o durumda düşen giriş önceki gibi sessizdir).
- Depodaki işaretin iki değeri var: "beklemede" ve "yeniden başladı, dinliyor, henüz bir dakika yalnız kalmadı". Giriş
  bildirimi ikincisinde "Bu kopya beklemeden çıkıyor … yeniden başlatma, bekle" der.
- **Komutlar:** varıştan sonraki ilk 3 sn'de (bir Conflict görüldüyse 10 sn) gelen komut bekletilir. Kopya beklemeye
  geçerse `/kill` ve `/duraklat` o kopyada yine uygulanır ve yanıt komutun öbür sürece ulaşmadığını söyler; öbür
  komutlar "Bu kopya BEKLEMEDE … UYGULANMADI. Yeniden gönder" yanıtını alır (PIN içeren mesaj yine silinir).
- Süreler duvar saatiyle değil `CLOCK_BOOTTIME` ile ölçülür (`core/clock.py` `boot_clock_ms`): saat düzeltmesi pencereyi
  kaydırmaz, uyku sayılır.
- Veritabanı şema sürümü 3 (`skip_standby` sonucu); `/durum` ve `signals report` "kopya beklemede" sayısını gösterir.
- İlk biçim (`43c87d9`: yeni gelen kopya alarm verip **çıkıyordu**) karşıt denetimde yetersiz bulundu ve değiştirildi:
  aşağıda "Karşıt denetim".
- Kalan açıklar: açık bulgu 56.

### Karar 32 — kurallar bayatken sinyal
- Davranış aynı: girişler iletilir, başına "[KURALLAR DOĞRULANMADI …]" satırı, günde bir alarm. Belgelendi: ADR-0005,
  SPEC §8.1, runbook §10. CLAUDE.md değişmez kural 3'e not eklendi ("`signal` modunda kural dosyası eskimişse girişler
  kesilmez…"); `approve` / `auto` kilitleri (değişmez kural 1) değişmedi.

### Kullanıcı onayları (2026-11-17)
Teslim raporunda onaya sunulan üç seçim onaylandı ("olduğu gibi kalsın"):
- **Master bacak kimlikleri** `master_ny_orb_fvg` ve `master_asian_sweep` olarak kalır (bağımsız script'in kimliği +
  `master_` öneki; örnekteki `master_ny_open` kullanılmadı).
- **Yarım tick'ten dar stop:** Pine'ın tick'e yuvarlayıp girişle aynı fiyata yazdığı stop'u alıcı şeması reddetmeye devam
  eder ("Geçersiz alert gövdesi reddedildi (alanlar: stop)", kaydedilmez); "stop çok dar" sayımına girmez.
- **Master + bağımsız script yan yana:** aynı kurulum için iki giriş mesajı gelir ve günlük bütçede ikisi de sayılır;
  alınmayan `/atladim <id>` ile bildirilir.

Aynı gün `main-bz6vo7`, `main` dalına fast-forward ile aktarıldı (zorla push yok).

### Dal geçmişi
- `WIP: uncommitted changes, moved to the cloud` commit'i (`b6fcbc8`) bağımlılık commit'ine (`cd00945`) katıldı; dal
  `--force-with-lease` ile yeniden yazıldı (açık bulgu 31). Eski Faz 1 commit kimlikleri artık dalda yok.

### Karşıt denetim (2026-11-16 ve 17; üç tur, her bulgu bağımsız bir ajanca yeniden üretildi)
**Birinci tur** (`fe0c388` üzerinde, üç mercek). "Süzgeç" merceği ve çürütme adımı tamamlandı; "ikinci kopya"
merceğinin bulgularını, çürütme ajanı kullanım sınırına takıldığı için ben yeniden değerlendirdim; "doküman/Pine"
merceği o turda çalışamadı ve ikinci turda koşturuldu.

| # | Bulgu | Önem | Sonuç |
|---|---|---|---|
| D1 | Kendini durduran kopya systemd altında etkisiz değil: 5 sn'de bir yeniden başlar, her başlangıçta kutusundaki giriş mesajlarını gönderir, komut alabilir, arada alıcı kapalıdır; canlı servis o sırada yeniden başlarsa iki kopya birbirini sürekli düşürür. Alarm metni ("sinyal iletmiyor ve komut almıyor") doğru değildi | orta | Tasarım değişti: kopya çıkmaz, **beklemeye** geçer (yukarıda karar 53); yeniden deneme yok, döngü yok |
| D2 | "Yeni gelen" süreç yaşıyla belirleniyordu: ikisi de bir dakikadan eski iki kopya (ağ kesintisinden / uykudan dönen makine) hiç çözülmüyor; hiç yanıt almamış süreç "eski" sayılıyor; duvar saati kayması pencereyi bozuyor | orta | Varış, Telegram'ın yanıtlarından ve `CLOCK_BOOTTIME` ile ölçülüyor; sessizlikten dönen kopya yeni gelendir |
| D3 | Tick yuvarlamasından sonra girişe eşit basılan stop "Stop çok dar" olmuyor: alıcı 422 ile reddediyor; ADR-0006 ve CHANGELOG yanlış alarmı anıyordu | düşük | Belgeler düzeltildi (ADR-0006, runbook §11); davranış değişmedi (şema kuralı ayrı karar ister) |
| D4 | `/durum` ve `signals report` yeni sonuçları nasıl saydığı / adlandırdığı testle sabit değildi (3 mutant yaşadı) | düşük | Farklı sayılarla iki test |
| D5 | Pine kaynak testi yalnızca kimlik kümesine bakıyordu: yanlış bacak kimliğiyle giden ya da silinen bir Master çağrısı geçiyordu (2 mutant) | düşük | Bacak × eylem sayımı ve `s1_` / `s2_` satır kuralı testi |
| D6 | "Eşik değerine eşit stop geçer" kuralı yalnızca ikilide zararsız eşiklerle sabitti (1 mutant) | düşük | 1,1 puanlık eşik testi |
| D7 | Pencere değeri hiçbir testle sabit değildi (0, 60 ms ve 10 dk geçiyordu); polling'in önce durdurulması, pencerenin yeniden kurulması ve alarm kodlarının ayrılığı da | düşük | Yeni tasarımın sayıları ve davranışları testli |

**İkinci tur** (`6b569f2` üzerinde, iki mercek: bekleme tasarımı · doküman/Pine tutarlılığı; dört ajanın dördü de
tamamlandı). On bulgunun onu da çürütme adımında doğrulandı ve hepsi "düşük" önemde kaldı. Pine için sonuç: beş
bağımsız strateji dosyası temel sürümden yalnızca token girdisi satırında, Master yalnızca başlık yorumu, token satırı
ve on `f_alert` çağrısının kimlik argümanında ayrılıyor; heartbeat göstergesi değişmedi.

| # | Bulgu | Sonuç |
|---|---|---|
| E1 | Öbür dinleyici yol vermezse (her sorguda Conflict) python-telegram-bot yalnızca 30 sn'de bir sorar; o sırada tek bir başarısız istek 60 sn'lik sessizlik sayılıyor ve **zaten çalışan** kopya beklemeye geçiyordu: sonuç güvenli ama rastlantıya bağlıydı ve belgeler tersini söylüyordu | Kural açık yazıldı: iki dakika boyunca Conflict'ten başka yanıt alamayan kopya beklemeye geçer (ADR-0004); testli |
| E2 | Bir Conflict görüldükten sonra bekletilen komut, süreç o sırada durdurulursa bekleme notu olmadan uygulanıyordu ("KILL etkin" yanıtı, botu tutup tutmadığını bilmeyen kopyadan) | O durumda yanıt beklemedeki kopyanınki gibi verilir (`/kill` ve `/duraklat` notla uygulanır, öbürleri reddedilir); testli |
| E3 | Bekleme yüzünden düşürülen giriş mesajı "duraklatma/KILL nedeniyle" diyordu ve bu kopyaya ulaşamayacak `/atladim`'ı öneriyordu; gönderilmediği kesin olan giriş hiç söylenmeden düşüyordu | Düşme nedeni ayrıldı; iki yeni bildirim; testli |
| E4 | "Bir dakika içinde ETKİN bildirimi gelir" tutmuyordu (ölçülen 70 sn: süre Telegram'ın ilk yanıtından başlar); yeniden başlatılmış ve komut alan kopyanın giriş bildirimi hâlâ "komutlar bu kopyaya ulaşmaz … yeniden başlat" diyordu | Metinler "yaklaşık bir dakika sonra"; işaretin ikinci değeri ve "beklemeden çıkıyor … yeniden başlatma, bekle" bildirimi; testli |
| E5 | Varlık mantığının dört davranışı testle sabit değildi (boş uzun sorgunun yanıt sayılması, kurulamayan partinin yanıt sayılması, geç gelen Conflict'in ETKİN'i ertelemesi, Conflict + sessizliğin "bir dakika yalnız" sayılmaması) | Dört test; 55 mutantlık kendi koşum aşağıda |
| E6 | runbook'ta eski veritabanı yönergesi eksikti: güncelleme satırı veritabanından söz etmiyordu (servis sessizce yeniden deneme döngüsüne girer), `-wal` / `-shm` dosyalarının hangi adla taşınacağı yazmıyordu, eski dosyanın bu sürümün raporuyla okunamadığı yazmıyordu | runbook §10 ve §11 yeniden yazıldı (adım adım; KILL/duraklatma komutunu yeniden gönder) |
| E7 | CHANGELOG ve ADR-0006 giriş karar sırasını bekleme adımı olmadan veriyordu; iki alarm metni alıntısı koddaki metinle aynı değildi; bulgu 53 yanlış commit'e bağlanmıştı; bu bölüm "sonuç aşağıda" deyip sonucu yazmıyordu | Düzeltildi |
| E8 | `tv_symbols` üst sınırı (32) testte sabitten türetiliyordu (1 mutant) | Sayı testte açık yazıldı |

**Üçüncü tur** (`efc9381` üzerinde, yalnızca ikinci turun düzeltmeleri; bir denetçi + bir çürütücü). Üç bulgu, üçü de
doğrulandı.

| # | Bulgu | Önem | Sonuç |
|---|---|---|---|
| F1 | "Beklemeden çıkıyor" bildirimi ve runbook, öbür süreç hâlâ dinliyorsa "yeni bir BEKLEMEDE alarmı" geleceğini söylüyordu; kod o alarmı hiç göndermiyordu (yeniden başlatılan kopya sessizce yine bekliyordu) | orta | Davranış metne uyduruldu: "… yeniden başlatıldı, ama … yine BEKLEMEDE" alarmı, depoda 5 dakikada bire kısıtlı (çökme döngüsü yinelemesin); testli |
| F2 | Durdurulurken bekletilen komutun "bekleme notu" koşulundaki süre sınırı (10 sn) testle sabit değildi (2 mutant) | düşük | Milisaniyesine sınır testi |
| F3 | İşaretin "2" ya da okunamayan bir değerle devralınması ve göndericinin o değerlerde girişleri düşürmesi testle sabit değildi (2 mutant); `ResilientBot`'un yanıt bildirimi doğrudan test edilmiyordu (1 mutant) | düşük | Üç değerle parametreli testler; yanıt bildirimi testi |

### Testler
- 3988 test (`uv run pytest -q`; 1 test bu ortamda atlanır: IPv6 loopback yok), ruff, `mypy --strict` (36 kaynak
  dosya) temiz. Önceki durum 3593.
- Yeni: sembol listesi ve asgari stop karar tablosu (sıra, sınır, tam ondalık, takip alert'leri, alarm kısıtlaması);
  venue profili `signal` bölümü (7 yeni hatalı fixture); Master kimlikleriyle 12 yeni gövde biçimi (toplam 45) şemadan,
  alıcıdan ve işleyiciden geçer; Master + bağımsız script yan yana senaryoları; Pine kaynak denetimleri (kimlik kümeleri,
  bacak × eylem sayımı, token girdisi); `/devam` kod varlıkları; bot kilidi (süreçler arası, SIGKILL sonrası serbest
  kalma); varış kuralı (60 sn sınırı, tek Conflict'in hoş görülmesi, sessizlikten dönüş, hiç yanıt almamış kopya, boş
  uzun sorgunun yanıt sayılması), iki dakikalık Conflict kuralı, bekleme (polling'in durması, sürecin ayakta kalması,
  girişlerin kesilip çıkışların iletilmesi, düşen giriş mesajlarının bildirimi, komut bekletme ve beklemedeki `/kill`,
  durdurulurken bekletilen komut, yeniden başlatmada "yine BEKLEMEDE" alarmı ve kısıtlaması, ETKİN bildirimi ve onu
  erteleyen durumlar, işaretin üç olası değeri).
- Mutasyon denetimi (kendi koşularım, bekleme tasarımı): üç grupta 67 mutantın 67'si yakalanıyor. İlk koşularda
  beşi yaşadı (sessiz kopyanın kendini etkin sayması, süre saatinin duvar saati olması, kurulamayan partinin yanıt
  sayılması, iki dakikalık sınırın tam değeri, Conflict işaretinin gerçek sorgu döngüsünden geçmesi); her biri için
  test eklendi ya da sıkılaştırıldı. Denetçilerin yaşattığı gerçek boşluklar (üç turda 20 mutant) için de test var;
  eşdeğer ya da yalnızca zamanlama farkı sayılanlar (6) dışarıda bırakıldı.
- Benzetim (elle, depoya girmedi): iki gerçek `TelegramService` kopyası, "yeni `getUpdates` bekleyen uzun sorguyu
  409 ile keser" kuralını uygulayan sahte bir Bot API'ye karşı, gerçek saat ve gerçek bekletme süreleriyle. Dört
  senaryo: (1) canlı kopya çalışırken ikinci kopya başlar → ikinci beklemeye geçer, o sırada gönderilen 18 komutun
  15'ini canlı kopya uygular, 3'ü ikinci kopyada **reddedilir**, yanlış kopyada uygulanan komut 0; (2) ikisi de
  eskiyken karşılaşma (ağ kesintisinden dönen kopya) → dönen kopya bekler; (3) denetimin kilitlenme senaryosu:
  beklemedeki kopya varken canlı servis yeniden başlar → canlı servis etkin kalır, bekleyen kopya bir daha
  dinlemez; yeniden başlatılan bekleyen kopya "yine BEKLEMEDE" alarmıyla yine bekler; (4) iki kopya 5 sn arayla başlar →
  ikisi de bekler (açık bulgu 56d); (5) yol vermeyen yabancı bir dinleyici → çalışan kopya önce "başka bir süreç"
  alarmını verir, süre dolunca (benzetimde 25 sn'ye kısaltıldı) beklemeye geçer ve bir daha dinlemez. Benzetim,
  Telegram'ın 409 davranışı hakkındaki varsayıma dayanır (56a).
- Gerçek `propbot serve` ile (sahte sırlar; Telegram'a erişim yok): aynı bot kimliğiyle ikinci kopya (başka veritabanı
  ve port) afişten önce reddedildi; başka bot kimliğiyle kopya başladı. Son sürümde üç gerçek webhook isteği:
  `master_ny_orb_fvg` kimliği ve listedeki sembol → `forwarded`; `CME_MINI:NQ1!` → `skip_symbol`; 1 puanlık stop →
  `skip_stop_too_tight`; şema sürümü 3; günlükte sır yok; SIGTERM ile temiz kapanış.

## Faz 2 — Python motoru, backtest, parite altyapısı, Go/No-Go ve yerel web paneli (2026-11-18)

Kullanıcı kararı (2026-11-18, ADR-0007): Faz 2'nin kalan dilimleri ve yerel web paneli, Faz 0'ın TradingView derlemesi
(SPEC §4.2 madde 5) ve Faz 1'in 10 günlük koşusu (§5.7) beklenmeden yapıldı; o ikisi operasyonel saha testi olarak
sürer. Pine referansı `144abab` (`pine/` o günden beri değişmedi). **Depoda gerçek piyasa verisi ve TradingView dışa
aktarımı yok:** her şey deterministik sentetik barlarla ve elle yazılmış örnek dosyalarla geliştirildi ve test edildi;
sentetik sonuçlar kanıt değildir ve Go/No-Go'da hiçbir kapıyı geçemez. Faz 2'de Go/No-Go hiçbir girdiyle GO olamaz
(G5 Faz 3 ister). Kararlar: ADR-0007, ADR-0008, ADR-0009. Kullanım: `docs/backtest.md`; panel: `docs/runbook.md` §13.

**Teslim onayı (2026-11-19):** kullanıcı Faz 2 ve panel teslimatını onayladı ve altı açık noktayı karara bağladı
(aşağıda "Kullanıcı kararları (2026-11-19)"; işlem başı risk kuralı ADR-0010). Onay kodun teslimini kapsar: §6.7'nin
parite ve veri maddeleri yine gerçek dosyalarla kanıtlanmayı bekler.

Commit'ler (geçmiş 2026-11-20'de `144abab` üzerine mantıksal commit'ler olarak yeniden yazıldı): `27e2ca0` çekirdek
sözleşmeler (bar, enstrüman, seans saati, strateji eylem modeli) · `1870b63` veri katmanı · `b0bc221` motor ·
`1e23d1d` altı port, göstergeler, kayıt defteri · `cdeace5` analitik, maliyet matrisi, walk-forward, parite, Go/No-Go
ve kanıt zinciri · `a3233e3` panel ve salt-okunur okuma · `1456863` komutlar · dokümanlar ve ADR'ler: bu bölümü
taşıyan `docs:` commit'i.

### Ne yapıldı
- **Veri** (`src/propbot/data/`): `ohlcv.py` genel (TradingView grafik verisi) ve MT5 "Export Bars" CSV'sini başlıktan
  tanır; `bar_tz` (`UTC`, IANA dilimi, FTMO sunucu saati için `NY+7`); hiçbir şeyi onarmaz, satır numaralarıyla reddeder.
  `validate.py` doğrulama raporu (boşluk, aykırı bar, açılış sıçraması, saat dilimi kayması, kısa seans, seanssız gün).
  `synthetic.py` platformdan bağımsız, bayt bayt aynı sentetik barlar (`#propbot-synthetic` işareti).
  `sessions.py` seans sonunu veriden türetir (≥ 30 dk boşluk). Komutlar: `propbot data synth`, `propbot data check`.
- **Motor** (`src/propbot/backtest/engine.py`, `model.py`; ADR-0008): Pine benzeri eylem modeli (`Entry`, `Exit`,
  `Cancel`, `Close`, `CloseAll`), `pessimistic` ve `tv_ohlc` dolum modelleri, CFD bid/ask, taraf başı komisyon,
  slipaj, NY saatine göre spread pencereleri; değişmez kural 2 her barda denetlenir.
- **Göstergeler** (`src/propbot/strategies/indicators.py`, `history.py`): SMA, EMA, RMA, TR, ATR, RSI, stdev,
  highest/lowest, pivot, değişim ve üst zaman dilimi EMA'sı (MTF'de 60 dk); Pine v5 başvuru kılavuzu tanımlarıyla.
- **Altı port** (`src/propbot/strategies/`: `ny_orb_fvg.py`, `asian_sweep.py`, `master_portfolio.py`,
  `ict_silver_bullet.py`, `mean_reversion.py`, `mtf_trend.py`; `registry.py`; `config/strategies.yaml`): Pine
  script'lerinin blok sırasıyla saf, deterministik durum makineleri. Parametreler Pine girdi varsayılanlarıdır;
  `config/strategies.yaml` yalnızca açıkça değiştirileni taşır (bugün hepsi boş). Optimizasyon yok.
- **Analitik ve raporlar** (`backtest/analytics.py`, `walkforward.py`, `runner.py`, `report.py`, `artifacts.py`):
  giriş başına net R istatistikleri, IS/OOS ve G3 oranı, NY saatine göre zaman tabloları, maliyet duyarlılık matrisi
  (1,0 / 1,5 / 2,0), parametre aramasız walk-forward, Master için bacak + birleşik görünüm, yedi türde JSON artefakt
  (üzerine yazılmaz; okuyucu hiçbir şeye güvenmez). Komut: `propbot backtest`.
- **Parite** (`backtest/parity.py`): "List of Trades" ayrıştırıcısı, deterministik eşleştirici, ≥ %98 eşiği ve %2
  dışarıda bırakma sınırı; `tests/parity/golden/` 24 sentetik regresyon dosyası. Komut: `propbot parity`.
- **Monte Carlo** (`backtest/trades.py`, `backtest/command.py`, `cli.py`): işlem listesinin köken satırı (`#propbot-trades`) ve
  `monte_carlo` artefaktı (`docs/monte-carlo.md`).
- **Go/No-Go** (`backtest/gonogo.py`, `backtest/command.py`): G1–G7 raporlayıcısı, karar koşusu seçimi, kanıt zinciri,
  `reports/go_no_go_YYYYMMDD.md` + JSON. Komut: `propbot gonogo` (SPEC §9.1).
- **Config:** `config/backtest.yaml` (enstrümanlar ve maliyetler; `US500.cash` boş = VERIFY; seans; örneklem;
  walk-forward; maliyet çarpanları; Go/No-Go eşikleri ve kod tabanları), `config/strategies.yaml`. `propbot config check`
  ikisini de doğrular.
- **Yerel web paneli** (`src/propbot/dashboard/`; ADR-0009): yalnızca loopback; canlı hesap/sinyal ekranı, yerel
  kontrol (Duraklat/Devam/KILL/KILL'i kaldır/Özet/Bakiye; Telegram komutlarıyla aynı kod), analiz ekranı (sunucuda SVG).
  Komut: `propbot dashboard`.
- **Store** (`src/propbot/store/db.py`): `Store.read()` salt-okunur anlık görüntü işlemi, `recent_signals` sınırlı
  okuma, `Store(create=False)`.

### Denetim yöntemi
- **Portlar:** her portu bir ajan yazdı, **başka** bir ajan Pine kaynağıyla blok blok, satır numaralarıyla karşılaştırdı;
  uygulayıcının testlerinden en az beşini Pine'ı elle yürüterek yeniden hesapladı, açık kalan davranışları Pine satırına
  bağlı izleme testleriyle sabitledi (`tests/unit/test_strategy_<port>_pine_trace.py`). MR ve Asian denetimlerinde
  ayrıca tek operatörlük mutasyonlarla testlerin yakaladığı denetlendi.
- **Motor:** sözleşmeden (ADR-0008, `strategies/base.py`, SPEC) motoru görmeden yazılmış kara kutu testleri
  (`tests/unit/test_engine_spec.py`, 103 parametreli durum), sonra kod incelemesi; pytest dışında 3000 tohumluk
  özellik taraması.
- **Saldırı geçişleri:** panel (Host/Origin/CSRF, XSS, PIN, salt-okunur, sayılar), parite, Go/No-Go, salt-okunur store,
  kanıt zinciri; her kusur için önce eski kodda başarısız olan bir test, sonra düzeltme.
- **Gerçek tarayıcı:** panel Chromium'da sürüldü (formlar, telefon genişliği; `a3233e3`).

### Bulunan ve düzeltilenler
| # | Alan | Bulgu | Sonuç |
|---|---|---|---|
| F2-1 | Motor | Kötümser modelde yürüyüş yönü açılıştaki boşluk dolumlarından sonraki durumdan alınıyordu: açılışta kapanan pozisyon bekleyen limit girişin barını lehte yöne çeviriyordu | Yön barın açılışındaki durumdan; testli |
| F2-2 | Motor | Açılışta piyasa girişleri, açık pozisyonun boşlukla geçilen çıkışlarından önce doluyordu (pyramiding sayımı) | Önce çıkışlar; testli |
| F2-3 | Motor | Kural 2 denetimi ve `tv_ohlc` ilk stop kilidi yalnızca eylem dönen barlarda çalışıyordu | Açık miktar olan her barda |
| F2-4 | Motor | TradingView yön kuralında kayan nokta gürültüsü: 0,1'lik ızgarada eşit uzaklıktaki açılış yükseğe yakın sayılıyor, long stop yerine hedefi vuruyordu | Eşitlik epsilon içinde; testli |
| F2-5 | Motor | Aynı fiyattaki emir sıralamasında kayan nokta gürültüsü: limit giriş stop'un önüne geçip pyramiding sınırında düşüyordu | Epsilon içinde stop > hedef > giriş; testli |
| F2-6 | Motor | Her çıkışta bütün kapanan işlemler yeniden kopyalanıyordu (O(n²)) ve bir yol stratejiye motorun kendi listesini veriyordu | Yalnızca eklenen liste + salt-okunur görünüm; testli |
| F2-7 | Motor | Pyramiding `1 + pyramiding` giriş sayıyordu (Master 3); Pine tanımı "aynı yönde en çok N giriş" | `max(1, pyramiding)` (`b0bc221`) |
| F2-8 | Panel | Yanlış/boş PIN'li KILL, KILL sürerken Devam ve yazılamayan komut 200 "yapıldı" dönüyordu | Bayraklar geri okunuyor: 200 / 409 / 500 |
| F2-9 | Panel | Veritabanı yeniden adlandırılınca (runbook §11 geçişi) panel eski dosyayı gösterip KILL'i oraya yazıyordu | Aygıt/inode izleniyor |
| F2-10 | Panel | Her yenileme 7 günlük sinyaller için iki sorgu × satır ve karesel eşleştirmeyi yazma kilidi içinde yapıyordu (4000 satır 2,3 sn; 50 000 satır dakikalar sürerdi) | Sınırlı okuma; 50 000 satır 0,06 sn; sonra `Store.read()` ile kilitsiz (15 ms) |
| F2-11 | Panel | Bakiye formu BEKLEMEDE kopyanın veritabanına yazıyordu | 409, hiçbir şey yazılmaz |
| F2-12 | Panel | `max(bakiye, özsermaye)` tabanlı profillerde beyan edilen kalan günlük tampon yalnızca üst sınırdır | "kalan en çok" yazıyor |
| F2-13 | Panel | Negatif tampon "$-2000" görünüyordu; panel kodunda adsız kural benzeri sayılar | "−$2000"; sabitler adlandırıldı, tarayan test |
| F2-14 | Panel | Gerçek tarayıcıda her form 403 alıyordu: `Referrer-Policy: no-referrer` altında tarayıcı `Origin: null` gönderiyor | `same-origin` (`a3233e3`); geniş tablo sayfayı taşırmıyor |
| F2-15 | Panel | Eylem rotaları `kind` varsayılan argümanı alıyordu; FastAPI onu sorgu parametresi okurdu (`?kind=kill`) | Varsayılan argüman kaldırıldı; testli |
| F2-16 | Panel | SVG'de XML'e yasak denetim karakterleri (hypothesis buldu) | Değiştiriliyor |
| F2-17 | Store | Okuma açılırken `KeyboardInterrupt` gibi bir istisna bağlantıyı `query_only=ON` bırakıyordu: sonraki her yazma "readonly database" hatası | Temizlik `BaseException`'ı yakalıyor; testli |
| F2-18 | Parite | Ortak pencere ve ısınma kesimi listenin istenen kadarını dışarıda bırakıp geçirebiliyordu (100 Python satırına 10 satırlık dışa aktarım = oran 1,0) | Bir listenin %2'sinden fazlası dışarıdaysa geçemez |
| F2-19 | Parite | Fiyat ve zaman toleranslarının üst sınırı yoktu | 1 tick ve 1 bar |
| F2-20 | Parite | Boşluklu binlik grupları denetlenmiyordu (`45 00,25` = 4500,25) | 3 haneli gruplar zorunlu |
| F2-21 | Parite | Aynı Python satırının iki kez verilmesi eşleşme sayısını şişiriyordu | Hata |
| F2-22 | Go/No-Go | OOS'ta 1–29 işlemle G2/G3 GEÇTİ | n < 30 → DEĞERLENDİRİLEMEDİ |
| F2-23 | Go/No-Go | İki yıl arayla iki barlık dosya G1'i geçiyordu | Yıl başına 1500 bar-saati |
| F2-24 | Go/No-Go | Tutarlılık toleransı değeri tabana kaldırabiliyordu (t = 1,9999999998 → 2,0) | Kayıtlı ile yeniden hesaplananın küçüğü |
| F2-25 | Go/No-Go | Elle düzenlenmiş Monte Carlo olasılıkları G4'ü geçiyordu | Olasılık tutarlılık denetimi |
| F2-26 | Go/No-Go | Artefakt metni Markdown raporuna HTML, yorum, uzak görsel ya da sahte tablo hücresi sokabiliyordu | Kaçışlama; testli |
| F2-27 | Go/No-Go | Aynı dosya adlı iki girdi sentetik olanı düşürüyordu; adı içerikle uyuşmayan dosya kabul ediliyordu | Rapor üretilmez |
| F2-28 | Go/No-Go | `latest_artifacts` (tür + etiket başına en yeni) daha yeni bir karşılaştırma koşusuyla kötü karar koşusunu gizleyebiliyordu (seçerek gösterme) | Bütün artefaktlar okunuyor (`all_artifacts`); karar koşusu kuralı |
| F2-29 | Kanıt zinciri | Aynı saniyedeki iki aday dosya adına göre seçiliyordu | Hiçbiri seçilmez |
| F2-30 | Kanıt zinciri | Aynı maliyet sayılarıyla daha küçük tick / büyük puan değerli (R'de daha ucuz) koşu karar koşusu sayılıyordu | Enstrüman birebir karşılaştırılıyor |
| F2-31 | Kanıt zinciri | `artifacts_truncated` verilmezse "tam tarama" sayılıyordu | Verilmezse kesilmiş sayılır |
| F2-32 | Kanıt zinciri | Okunamayan bir `backtest_*` dosyası G4'ü engellemiyordu | G4 de DEĞERLENDİRİLEMEDİ |
| F2-33 | Kanıt zinciri | Listeden silinen satırlar (ör. kaybedenler) köken satırı bozulmadan G4'e ulaşabiliyordu | İşlem sayısı karar koşularıyla karşılaştırılıyor; satırda adı geçip işlemi olmayan strateji reddediliyor |
| F2-34 | Kanıt zinciri | Çok büyük sayılarla parite oranı tabanın 1e-9 üstünde saklanabiliyordu | G6 da küçük olanı kullanır |
| F2-35 | Parite artefaktı | Oran sayılarla tutarlı olmak zorunda değildi; uyuşmayan çift sayısı yalnızca notta | `mismatched` alanı ve tutarlılık doğrulayıcısı (`cdeace5`) |

Altı port denetiminin altısı da Pine ile **sapma bulmadı** (port kodu değişmedi); yalnızca test eklendi. Bulunan Pine
davranışları aşağıdaki açık bulgularda (59–64), SPEC ile Pine'ın ayrıldığı yerler SPEC §3.1 "Betik bazında
istisnalar"dadır.

### Son çapraz denetim (2026-11-19)
Her bulgu bağımsız bir ajanca yeniden üretildi; her biri için önce eski kodda başarısız olan test
(`tests/unit/test_gate_chain.py`), sonra düzeltme. Yeni alanları taşımayan eski artefaktlar yine yüklenir ve panelde
görünür; ilgili kapı "eski rapor" cümlesiyle DEĞERLENDİRİLEMEDİ olur, asla GEÇTİ olmaz.

| # | Alan | Bulgu | Sonuç |
|---|---|---|---|
| F2-36 | Go/No-Go G2/G3 | `--oos-start <herhangi bir tarih>` ile yapılan koşu karar koşusu sayılıyordu: G2/G3 kullanıcının seçtiği IS/OOS sınırıyla yargılanıyordu (başka bir tohumda KALDI → GEÇTİ) | Kayıtlı OOS başlangıcı `samples.oos_start`'tan farklı koşu karşılaştırma koşusudur (karar vermez, eskisini gizlemez); bilinmezse hiçbir koşu doğrulanamaz. Koşucu etikete `-oos20261020` ekler ve "KARAR KOŞUSU DEĞİL" yazar |
| F2-37 | Go/No-Go G4 | Profil `trailing_eod` ya da başka `daily_reset` olarak değiştirildikten sonra eski Monte Carlo G4'ü geçiyordu | Artefakt `max_loss_type`, `daily_loss_basis`, `daily_reset` kaydeder; hepsi profille aynı olmalı; `static` dışı profilde G4 DEĞERLENDİRİLEMEDİ. Günlük kayıp bazı tek modelle (ADR-0002 madde 7), kanıtta yazılı |
| F2-38 | Go/No-Go G4 | `--intratrade closed` (iyimser) koşu varsayılanla aynı girdileri yazıp G4'ü geçiyordu | İşlem içi model kaydedilir; yalnızca `stop` geçer |
| F2-39 | Go/No-Go G1 | Doğrulama raporu yalnızca sha256 ile bağlanıyordu: NY+7 ile doğrulanıp UTC ile backtest edilen MT5 dosyası G1'i geçiyordu | Bar saat dilimi, bar süresi, ilk/son bar da aynı olmalı; değilse KALDI, iki değer cümlede |
| F2-40 | Go/No-Go G6 | Bir günlük dosyadan tek işlemle, başka parametrelerle yapılmış ilgisiz parite raporu G6'yı geçiyordu | Parite artefaktı parametre, enstrüman ve veri bilgisini kaydeder; parametre ve enstrüman bugünkü config'le aynı olmalı; en az 100 karşılaştırılan işlem (`go_no_go.min_parity_trades`); parite ve karar koşusu verisi kanıtta yazılı |
| F2-41 | Go/No-Go G4 | R değerlerinin hepsi yeniden yazılmış liste (köken satırı ve satır sayısı aynı) G4'ü geçiyordu | Monte Carlo listenin R ve mae_r toplamlarını, backtest `mae_total_r`'yi kaydeder; toplamlar karar koşularınınkine eşit olmalı (tolerans yalnızca toplama yuvarlaması: CSV `repr()` ile yazılır) |
| F2-42 | Monte Carlo raporu | Köken satırı maliyetsiz olan liste `--r-is-net` ile "(maliyet sonrası)" ve "R değerleri maliyet dahil" yazıyordu | "MALİYETSİZ işlem listesi" uyarısı; köken satırı bayraktan üstün |
| F2-43 | Go/No-Go G7 | Başka çalışma dizininden `propbot gonogo`, dosyalar depoda varken "docs/compliance/: dosya yok" diyordu | Klasör bulunamazsa mutlak yoluyla "klasör yok"; kanıt listelenen yolu yazar |
| F2-44 | Go/No-Go G1–G4, G6 (kapı saldırısı K) | Venue `initial_balance`'ı değiştirip koşup geri almak karşılaştırılmıyordu; boyutlar tam kontrat ve `min_qty` altı atlandığı için sermaye işlemleri değiştirir (5 000'de MTF toplam R 10,8 → 3,4) | Karar koşusunun ve parite raporunun sermayesi bugünkü `initial_balance`'a eşit olmalı; değilse koşu karar koşusu değil (DEĞERLENDİRİLEMEDİ, eskisine geçilmez) |
| F2-45 | Go/No-Go G1–G4, G6 (kapı saldırısı L) | `session.min_gap_minutes` ve `session.tz` (spread pencerelerinin saat dilimi) kaydedilmiyordu; boşluk stratejilerin okuduğu `session.islastbar`'ı değiştirir (45 dk'lık veri deliğinde ORB toplam R −7,4 → −5,4) | Backtest `min_gap_seconds` ve `costs.spread_tz`, parite `min_gap_seconds` kaydeder; config'le aynı olmalı; bilinmezse DEĞERLENDİRİLEMEDİ (`tests/unit/test_gate_attacks_final.py`, gerçek CLI dahil) |
| F2-46 | Go/No-Go G1–G4, G6 (kapı saldırısı M) | Enstrümanın `qty_step` / `min_qty`'si kaydedilmiyordu; `min_qty: 4` geniş stoplu işlemleri atlar (5 000 sermayede MR 5 işlem → 0) | `InstrumentInfo` ikisini kaydeder, karar koşusu ve parite için birebir karşılaştırılır |
| F2-47 | Go/No-Go G4 (kapı saldırısı N) | `--seed`, `--paths`, `--max-days`, `--skip-prob` serbestti: tohum seçerek (5000 yolda standart hata ≈ 0,7 puan, 40 tohumun en iyisi ≈ +1,2 puan) ya da ufukla G4 geçirilebiliyordu | G4 yalnızca tekrar üretilebilir koşuyla geçer: varsayılan tohum ve ufuk, atlama 0, yol sayısı karar değeri (10 000 ya da daha sıkı `min_mc_paths`); açık bulgu 78'in denetim kısmı kapandı |
| F2-48 | Go/No-Go G4 (kapı saldırısı P) | İşlem listesinde zamanları kaydırmak sayıyı ve R/mae_r toplamlarını koruyor, günlere dağılımı (günlük kayıp ihlallerini) değiştiriyordu | Backtest `trades_digest`, Monte Carlo strateji başına `trades_digests` kaydeder (satırların sıralı sha256'sı; toplama dayalı özet seçilmiş satırlarla eşlenebilirdi); eşit olmalı |

Kapı saldırısı üçüncü turunda denenip **geçemeyen** yollar: maliyet çarpanı/maliyetsiz/tv_ohlc, etiketler, gizli koşular,
köken satırı, satır silme, parametre, OOS sınırı, kural alanları, işlem içi model (önceki turlar); `--cost-r` negatif
(komut reddeder); `--r-is-net` maliyetsiz listede (köken satırı üstün, F2-42) ve maliyetli listede (yalnızca beyan, R'yi
değiştirmez); `--dd-days` (yalnızca düşüş istatistiği, kapı okumaz; çekiliş akışını değiştirmez); `--trades-tz` (liste
`Z` ofsetli yazılır, ofsetsiz zamana uygulanır); `--assume-min-trading-days` (varsayım bayrağı G4'ü engeller); 3 dk'lık
dosyayı `--tf 1` ile okumak (doğrulama 247 860 gün içi boşluk bulur: KALDI); aynı satırı tekrarlamak (yükleyici reddeder);
Markdown raporu yalnızca artefakttan üretilir (JSON ile çelişemez; aynı günün dosyası en yeni hükümle değişir).

### SPEC ve Pine'dan sapmalar
- **Pine kodu SPEC'e karşı kazanır** (parite): Silver Bullet boyut ve TP'yi limit seviyesinden fiyatlar, dolumda yeniden
  fiyatlamaz; MR stop'u dolumun 1 tick zarar tarafına zorlanmaz; "Fractional" modda script'ler `qty > 0` ile işler;
  NY gün sıfırlaması pozisyon varken yönetimi korur; NY ve Master'da grafiğin ilk gününde günlük kesici çalışmaz; MTF
  gün başı özsermayesi ilk barın kapanışındadır ve başa baş işlem zarar sayılır; Master S2 silahı giriş kapılarından
  bağımsız ölür. SPEC §3.1 "Betik bazında istisnalar".
- **Port Pine'dan bilinçli ayrılır (iki yer):** (1) MTF ısınmasında Pine runner'ı stop'suz bırakırken port başa baş
  stop tutar (değişmez kural 2; 2026-11-19'da "beklenen güvenli sapma" olarak onaylandı, parite raporunda yazar);
  (2) portlar boyutu lot adımına aşağı yuvarlar ve 0 ise atlar.
- **Karar koşusu risk girdisinde Pine varsayılanından ayrılır (2026-11-19, ADR-0010):** `propbot backtest` betiğin risk
  girdisine `config/risk.yaml` riskini (%0,25) yazar; Pine varsayılanı %0,5 yalnızca paritede ve `--pine-risk`
  koşusunda kalır. Port kodu ve Pine dosyaları değişmedi.
- **Port girdileri Pine'dan dar doğrulanır:** MTF `htfTf` yalnızca dakika cinsinden (D/W reddedilir; grafiğin bar
  süresinden kısa HTF hatadır), MR yıl girdileri 1–9999; Pine'da bu sınırlar yoktur. Varsayılanlar etkilenmez.
- **Motor ADR-0008'den ayrıntıda ayrılır:** kapanış dolumları da slipaj öder; açılışta önce eldeki pozisyonun
  çıkışları; ADR-0008 "Uygulama notları".
- **Analitik Pine tablosundan ayrılır:** giriş başına net R; Pine her `closedtrades` satırını ve brüt mesafe R'sini
  sayar (SPEC §6.4).
- **Veri:** Parquet yok, takvim kütüphanesi yok (ADR-0008 madde 6–7).

### Kullanıcı kararları (2026-11-19) — Faz 2 teslim onayı
Kullanıcı Faz 2 ve yerel web paneli teslimatını onayladı ve altı noktayı karara bağladı. Her biri için yapılan:

1. **Panel adresi — onaylandı.** Karar: panelin yalnızca `127.0.0.1` üzerinde kalması uygun; uzaktan erişim için SSH
   tüneli yeterli; mevcut davranış korunur ve belgelenir. Yapılan: kod değişmedi (`--host` yalnızca loopback kabul
   eder, varsayılan `127.0.0.1`; başka adres reddedilir). Belge: ADR-0009 uygulama notu, SPEC §6.8, `docs/runbook.md`
   §13.4.
2. **Bağımlılıklar — onaylandı.** Karar: ek bağımlılık yok (pandas, pyarrow, exchange_calendars eklenmez), Parquet
   desteği ertelenir. Yapılan: kod ve `pyproject.toml` değişmedi. Belge: ADR-0008 uygulama notu; açık bulgu 74.
3. **Go/No-Go ek eşikleri — onaylandı, aynen kalır.** Karar: `config/backtest.yaml`'daki alt sınırlar (OOS'ta en az 30
   işlem, 5000 Monte Carlo yolu, en az 100 işlem, 30 gün tazelik ve öbürleri) uygun. Yapılan: hiçbir anahtar ya da
   değer değişmedi; yalnızca yorumlar "awaiting user confirmation" → "approved by the user on 2026-11-19". Belge: SPEC
   §9.1, `docs/backtest.md` 7. bölüm, `docs/monte-carlo.md`. Onaylanan altı eşik aşağıda.
4. **Master stratejisi (açık bulgu 72) — kabul edildi.** Karar: hem alt bacaklar üzerinden hem birleşik koşu üzerinden
   değerlendirme kabul edilir. Yapılan (kod): `propbot gonogo --strategies` iki bacak kimliğini ya da birleşik
   `master_portfolio` kimliğini alır, her biri kendi artefaktından yargılanır. Uygulamanın getirdiği sınır (kararın
   parçası değil): ikisi aynı raporda reddedilir (aynı işlemler iki kez sayılırdı), iki görünüm iki ayrı raporla
   değerlendirilir. Uygularken iki eksik bulundu ve giderildi (F2-53, F2-54). Belge: SPEC §3.4.1, §6.4, §9.1;
   `docs/backtest.md` 4.1 ve 7. bölüm. Bulgu 72 kapandı.
5. **Risk uyumsuzluğu (açık bulgu 82) — kesinleşti.** Karar: canlı çalışma ve portföy düzeyindeki değerlendirmelerde
   `config/risk.yaml`'daki **%0,25** işlem başı risk geçerlidir; Pine'daki %0,5 yalnızca tekil strateji varsayımıdır.
   Yapılan (kod): `propbot backtest` karar koşusunu `risk.yaml` riskiyle boyutlar (değer betiğin risk girdisine
   yazılır ve artefakta kaydedilir); `--pine-risk` betiğin kendi değeriyle etiketli bir karşılaştırma koşusu üretir;
   `propbot parity` her zaman betiğin kendi riskiyle koşar; `config/strategies.yaml` risk girdisini taşıyamaz;
   Go/No-Go karar koşusunun, işlem listesinin ve simülasyonun aynı riskte olduğunu denetler (F2-49…F2-52). Karşıt
   denetim üç yolu daha kapattı (F2-57…F2-59): risk tabanı `Initial Capital` dışındayken karar koşusu yapılmaz, kod
   tavanı (0,5) koşucuda ve raporlayıcıda da geçerlidir, betiğin riskiyle üretilmiş işlem listesi `-pinerisk`
   işaretini taşır ve G4'ü geçemez. Config:
   `config/risk.yaml` ve `config/strategies.yaml`'a kuralı söyleyen yorumlar (anahtar ve değer değişmedi). Belge:
   **ADR-0010**, SPEC §3.1, §6.4, §6.6, §8.1, §9.1; `docs/backtest.md` 4.0; `docs/monte-carlo.md`; CLAUDE.md. Sonucu:
   TradingView'ın %0,5'teki sayıları ile %0,25'teki karar koşuları karşılaştırılamaz (parite koşuları
   karşılaştırılabilir); 2026-11-19'dan önce %0,5 ile yapılmış backtest, işlem listesi ve Monte Carlo yeniden
   üretilmelidir. Bulgu 82 kapandı.
6. **MTF runner stop farkı — Python davranışı onaylandı.** Karar: iz göstergesi ısınırken Python'un başa baş stop'unu
   koruması prop risk yönetimi açısından daha güvenlidir; davranış korunur ve parite kılavuzunda/raporunda "beklenen
   güvenli sapma" olarak belgelenir. Yapılan (kod): port değişmedi; `backtest/parity.py` → `EXPECTED_DEVIATIONS`
   tablosu (tek kayıt `mtf_trend_cont`) parite raporuna ve artefakt notlarına "Beklenen güvenli sapma" bölümünü yazar;
   hiçbir sayıyı ve hükmü değiştirmez, farklı dolumlar yine uyuşmazlık sayılır (F2-56). Belge: SPEC §3.1 ve §6.6,
   `docs/backtest.md` 6. bölüm, doğrulama listesi P36, ADR-0008 uygulama notu; açık bulgu 64(a).

**Ek karar (2026-11-20) — Master'ın tek bacağı (açık bulgu 87).** Karar: `gonogo --strategies` içine tek bir Master
bacağı verildiğinde komut bunu G4'e bırakmadan baştan net bir hata mesajıyla reddetsin ve kullanıcıyı
`master_portfolio` / iki bacağa ya da bağımsız strateji kimliğine yönlendirsin. Yapılan (kod): `backtest/command.py`
→ `_parse_strategies` bir betiğin bacaklarının yalnızca bir kısmı verildiğinde çıkış kodu 2 ile reddeder, rapor
yazılmaz (F2-72). Belge: SPEC §9.1, `docs/backtest.md` 7. bölüm. Bulgu 87 kapandı.

**Onaylanan mühendislik eşikleri** (SPEC §9 değeri olmayanlar; `config/backtest.yaml` → `go_no_go`, kodda taban,
yalnızca sıkılaştırılabilir): OOS'ta en az 30 işlem (`min_oos_trades`), gerekli her yıl için 1500 bar-saati
(`min_bar_hours_per_year`), en az 5000 Monte Carlo yolu (`min_mc_paths`) ve 100 işlem (`min_mc_trades`), artefakt yaşı
en çok 30 gün (`max_artifact_age_days`), paritede en az 100 karşılaştırılan işlem (`min_parity_trades`; hiç fark yokken
gerçek oranın %95 alt sınırı ≈ 1 − 3/n: 50'de %94, 100'de %97).

Eski "onay bekleyen kararlar" listesinin kalan iki maddesi: **`Referrer-Policy: same-origin`** (`no-referrer` gerçek
tarayıcıda her formu kırıyor) kullanıcının altı kararı arasında değildir; ayrı bir karar verilmedi, teslimat bütün
olarak onaylandığı için aynen durur. **Daldaki "chore(wip)" checkpoint commit'leri:** kullanıcı 2026-11-19'da bunların
mantıksal `feat:` / `docs:` commit'lerine toplanmasını istedi; geçmiş 2026-11-20'de `144abab` üzerine sekiz commit
olarak yeniden yazıldı (bölümün başındaki commit listesi). Son ağaç yeniden yazımdan önceki ağaçla birebir aynıdır;
ara commit'lerin eski kimlikleri artık geçerli değildir.

### Kullanıcı kararlarının uygulanması (2026-11-19)
Kararları uygularken bulunanlar ve yapılanlar. Kanıt testleri: `tests/unit/test_risk_rule.py` (risk kuralı, gerçek CLI
dahil), `tests/unit/test_cli_backtest.py`, `tests/unit/test_gonogo.py`, `tests/unit/test_runner_attacks.py` (Master,
gerçek koşucu çıktısıyla), `tests/unit/test_parity.py` (beklenen sapma), `tests/unit/test_strategy_registry.py`,
`tests/unit/test_risk_rule_attacks.py` (karşıt denetim, F2-57…F2-61), `tests/unit/test_decisions_final_attacks.py`
(son bağımsız denetim, F2-63…F2-66; F2-67 ve F2-69 test düzeltmeleridir). Hepsi sentetik fikstürlerle: mekanizmayı sabitler, hiçbir kenarı ya da pariteyi
kanıtlamaz.

| # | Alan | Bulgu | Sonuç |
|---|---|---|---|
| F2-49 | Risk (karar 5) | Karar koşuları Pine varsayılanı %0,5 ile, Monte Carlo %0,25 ile koşuyordu; R boyuttan bağımsız değil. Gerçek port koduyla, sentetik 5 dk fikstürde: MTF'nin ilk girişi %0,5'te 17, %0,25'te 8 kontrat (8,5 aşağı yuvarlanır), çıkışlar `stop` yerine `target` + `stop`, R farklı; 10 000 sermayede MR 6 giriş → 5 (biri bir kontratın altında, atlanır) | `propbot backtest` varsayılanı `config/risk.yaml` riski: kayıt defteri her portun risk girdisini adlandırır (`risk_param`; MTF `risk_percent`, öbürleri `risk_pct`), koşucu değeri oraya yazar ve kaydeder; özet "İşlem başı risk" satırını taşır. `--pine-risk` betiğin değerini korur: etiket `-pinerisk`, "KARAR KOŞUSU DEĞİL". Planlanan riski girdi kabul etmiyorsa hata (sessiz düzeltme yok). Farklı riskli koşular tek işlem listesinde birleşmez |
| F2-50 | Risk | `config/strategies.yaml` risk girdisini taşıyabilirdi: ikinci bir kaynak, karar koşusu ile simülasyonu yeniden ayırırdı | Anahtar, değeri ne olursa olsun, yükleme hatası (`config/risk.yaml` ve `--pine-risk` adıyla); dosyayı okuyan her komut durur; öbür anahtarların hataları yine birlikte raporlanır |
| F2-51 | Go/No-Go G1–G4 | Planlanan riskten başka riskle (betiğin %0,5'i) boyutlanmış koşu karar koşusu sayılıyordu; G4 yalnızca simülasyonun riskine bakıyordu | Kayıtlı risk ≠ planlanan risk → karşılaştırma koşusu (karar vermez, eskisini gizlemez; etikete değil kayıtlı değere bakılır); kayıtlı risk bilinmiyorsa DEĞERLENDİRİLEMEDİ, eskisine geçilmez. G4 her karar koşusunun riskini ayrıca denetler ve zincir tutuyorsa "risk zinciri: …" cümlesini yazar. `risk.yaml` değişince eski koşular karar vermez |
| F2-52 | Go/No-Go G6 | Parite betiğin riskiyle koşulur; karar parametre kümesiyle (planlanan risk) karşılaştırılsa her dürüst parite raporu reddedilirdi | G6'nın kendi beklenen kümesi (betiğin riskinde; `expected_parity_params`). Planlanan riskle yapılmış parite raporu ya da verilmemiş küme DEĞERLENDİRİLEMEDİ; G6 karar kümesine düşmez. `propbot parity` ve G6 kanıtı kullanılan riski ve nedenini yazar |
| F2-53 | Master G4 (karar 4) | Birleşik `master_portfolio` kimliği komutta kabul ediliyordu ama G4'te hiçbir zaman bağlanamazdı: işlem listesinin satırları ve köken satırı bacak kimliklerini taşır | Backtest artefaktı satır özetlerini satırın taşıdığı kimlik başına kaydeder (`trades_digests`); G4 birleşik koşuyu listeye bununla bağlar. Alanı taşımayan eski birleşik artefakt bağlanamaz (DEĞERLENDİRİLEMEDİ) |
| F2-54 | Master G6 (karar 4) | Bacak kimliğinin parite raporu olamaz (TradingView işlem listesi betik başınadır, `propbot parity` bacak kimliği almaz): bacaklarla değerlendirmede G6 hep "parite raporu yok" derdi | Bacağın G6'sı betiğinin (`master_portfolio`) raporunu okur; kanıt, oranın bütün bacakları birlikte kapsadığını yazar. Betik eşlemesi verilmezse ödünç kanıt yok (DEĞERLENDİRİLEMEDİ) |
| F2-55 | Master (karar 4) | Birleşik kimlik bir bacağıyla aynı raporda verilirse aynı işlemler iki karar koşusunda sayılır | Komut reddeder (çıkış kodu 2, hangi kimliklerin verileceğini söyler); saf katmanda G4 DEĞERLENDİRİLEMEDİ. Bacak + bağımsız port (`master_ny_orb_fvg,ny_orb_fvg`) karışım değildir (2026-11-20'den beri bu, tek bacak olarak reddedilir: F2-72) |
| F2-56 | Parite (karar 6) | MTF ısınmasındaki bilinçli sapma yalnızca belgelerdeydi; parite raporunu okuyan onu göremezdi | `EXPECTED_DEVIATIONS` (yalnızca `mtf_trend_cont`): rapor ve artefakt notları "Beklenen güvenli sapma" bölümünü taşır. Sayılar ve hüküm değişmez; ısınmayı dışarıda tutmanın yolu `--warmup-bars` |
| F2-57 | Risk tabanı (karar 5; karşıt denetim) | Risk tabanı ikinci bir risk kaynağıydı: `config/strategies.yaml`'da `risk_base: Fixed Balance` + `fixed_bal: 200000` ile "karar koşusu" sabit 200 000'in %0,25'iyle, yani 100 000'lik hesabın %0,5'iyle boyutlanıyor, Go/No-Go kayıtlı 0,25'i okuyup risk zincirini onaylıyordu | Planlanan riskle koşu `Initial Capital` dışındaki tabanı reddeder: `propbot backtest` çıkış kodu 1, `propbot gonogo` 2; `config check` dosyayı GEÇERLİ sayar ve NOT yazar. Parite ve `--pine-risk` betiğin girdileriyle koşar; risk satırı "bu yüzde hesabın başlangıç bakiyesinin yüzdesi DEĞİLDİR" der. Kayıt defteri varsayılan tabanı `Initial Capital` olmayan portu kabul etmez |
| F2-58 | Risk tavanı (karşıt denetim) | Kod tavanının (0,5; SPEC §8.1) üstündeki planlanan riski yalnızca config yükleyicisi reddediyordu; koşucu ve saf Go/No-Go katmanı kabul ediyordu | Koşucu ve raporlayıcı da reddeder ("kod tavanını aşıyor"); tavanın kendisi geçerli bir planlanan risktir |
| F2-59 | Go/No-Go G4 (karşıt denetim) | `--pine-risk` işlem listesinin satırları karar koşusununkilerle aynı çıktığında (hiçbir işlem yuvarlanıp atlanmadığında) G4 listeyi karar koşusuna bağlıyordu; komut ise "G4 bu listeyle GEÇTİ olamaz" diyordu | Betiğin riskiyle yapılmış koşunun listesi satır kimliklerinde ve köken satırında `-pinerisk` ekini taşır (`ny_orb_fvg-pinerisk`); G4 için işaret tek başına ret nedenidir (DEĞERLENDİRİLEMEDİ). Hiçbir strateji ya da bacak kimliği bu eki taşımaz |
| F2-60 | Go/No-Go G4 metni (karşıt denetim) | Ret cümlesi riskleri iki ondalığa yuvarlıyordu: farklı iki risk (%0,25 ve %0,250000001) aynı okunuyordu | Riskler yuvarlanmadan yazılır |
| F2-61 | Notlar, eşit riskler (karşıt denetim) | Planlanan risk betiğin riskine eşitken notlar, hiçbir şeyi değiştirmeyecek olan `--pine-risk`'e yönlendiriyordu | `tv_ohlc` hatırlatması yalnızca koşunun riski betiğinkinden farklıyken yazılır. Eşit risk NOT'u, koşu başka bir seçenek (maliyetsiz, çarpan, `tv_ohlc`, başka OOS) yüzünden karşılaştırma koşusuysa ona "karar koşusu" demez; başka risk tabanında `--pine-risk` karşılaştırma koşusu olarak kalır |
| F2-62 | Panel, "Koşu türü" (açık bulgu 86) | Backtest kartı koşu türünü yalnızca dolum modeli, maliyet ve çarpandan çıkarıyordu; `-pinerisk` ve `-oos…` karşılaştırma koşularına "karar koşusu" diyordu | Koşucunun "KARŞILAŞTIRMA KOŞUSU (karar koşusu değil): …" notu varsa satır onu aynen yazar (ortak sabit `artifacts.COMPARISON_RUN_TAG`); not yoksa alanlara bakılır. Satır koşunun yapıldığı andaki durumudur; bugünkü config'e göre yargı Go/No-Go raporundadır |
| F2-63 | Go/No-Go, bozuk sayılar (son denetim) | Rapor dosyasındaki aşırı büyük sayılar `propbot gonogo`'yu Python hata dökümüyle bitiriyordu (400 basamaklı kayıtlı risk, 100 basamağı aşan tam sayılar, taşan toplam ve t-istatistiği paydası) | Kayıtlı risk bilinmiyor sayılır ve koşu engeller; 100 basamağı aşan tam sayı dosyayı "okunamayan" yapar (dosya adıyla, kapılar DEĞERLENDİRİLEMEDİ); toplam ve t-istatistiği istisnasız fail-closed |
| F2-64 | G4, simülasyon riski (son denetim) | Simülasyon riski planlanan riskle 1e-9 göreli toleransla karşılaştırılıyordu; risk zinciri cümlesi deneysel risk kademeleriyle (`--dd-risk-steps`) yapılmış koşuda da yazılıyordu | Karar koşularındaki gibi tam eşitlik; kademeli simülasyonda zincir cümlesi yazılmaz |
| F2-65 | Notlar (son denetim) | Maliyetsiz enstrüman profilinde eşit riskli `--pine-risk` notu karşılaştırma koşusuna "karar koşusu" diyordu; `montecarlo` `-pinerisk` işaretli listeyi hiçbir şey söylemeden simüle ediyordu; parite risk satırı başka risk tabanında ".;" yazıyordu | Not maliyetsiz profili de sayar; `montecarlo` "Köken" satırı ve artefakt notu listenin planlanan hesabın işlemleri olmadığını ve G4'ü GEÇTİ yapamayacağını yazar; nokta cümlenin sonunda |
| F2-66 | G4, Master birleşik kimlik (son denetim) | Birleşik kimliğin karar koşusu yokken G4, satır kimliklerini tahmin edip listenin bacaklarını "değerlendirilmeyen" diye yazıyordu (hüküm doğruydu, ilk cümle yanıltıcıydı) | Karar koşusu olmayan strateji varken kimlik karşılaştırması yapılmaz; yalnızca gerçek neden yazılır ("karar koşusu olmayan strateji: …") |
| F2-67 | Test (son denetim) | `tests/property/test_gonogo_props.py` G4 geçişini ham çekilişle sınıyordu; artefakt ihlal olasılığını `1 − geçiş` ile sınırlar, bu yüzden test yaklaşık 40 koşuda bir (ve Hypothesis örneği kaydedildikten sonra her koşuda) boşuna kalıyordu | Sav artefaktın taşıdığı (sınırlanmış) değere bakar; kapının davranışı değişmedi |
| F2-68 | Metinler ve tekrarlar (son denetim) | Parite risk satırı ve G6 kanıt cümlesi "pariteyi betiğin riskiyle koşma"yı kullanıcı kararı diye yazıyordu (karar hangi riskin geçerli olduğudur); risk metni, etiket eki ve `-pinerisk` yardım metni iki yerde ayrı ayrı yazılıydı | Cümleler "2026-11-19 kullanıcı kararının uygulaması, ADR-0010" der; `report.risk_shown`, `runner.label_suffix` ve `PINE_RISK_SUFFIX` tek kaynaktır |
| F2-69 | Test, Master özellik testi (son denetim) | `tests/property/test_strategy_master_portfolio_props.py` "motorun kilitlediği ilk stop dolumdan en az bir tick zarar tarafındadır" diyordu; `tv_ohlc`'de kilit dolum barının `on_bar`'ından sonra gelir ve S1 izi o barda bir adım atmış olabilir (Pine da dolum barında izler: `s1_trailActR` 0 iken hemen), stop girişe eşit ya da daha sıkı olur. Rastgele arama bu örneği ilk kez buldu (düz barlar, sıfır tampon, 0 R aktivasyon) | Motor ve port değişmedi (Pine ile aynı davranış; stop gevşemiyor). Test artık iki şeyi ayrı sınar: yeniden fiyatlanan stop dolumdan en az bir tick zarar tarafındadır (her iki dolum modeli); motorun ilk stop'u ona eşittir, yalnızca `tv_ohlc` + S1 izinde ondan daha sıkı olabilir, hiçbir durumda daha gevşek olamaz. 40 tohumla (2400 örnek) koşuldu |
| F2-70 | Test, salt-okunur depo (temiz klon doğrulaması) | `tests/unit/test_store_readonly_attacks.py` root altında dosya kiplerini yetkisiz bir alt süreçle (uid 65534) sınar; depo yalnızca root'un girebildiği bir dizindeyken (ör. `/root/...`) alt süreç yorumlayıcıya ulaşamıyor ve 3 test `PermissionError` ile kalıyordu (temiz klonda görüldü) | Ön koşul açıkça sınanır: yetkisiz kullanıcı yorumlayıcıyı çalıştırıp paketi içe aktaramıyorsa testler gerekçesiyle atlanır; normal kullanıcıyla ya da erişilebilir bir yoldan koşulduğunda eskisi gibi çalışır |
| F2-71 | Depo hijyeni (güvenlik taraması) | `.gitignore` yalnızca tam `.env` adını ve `*.db` dosyalarını dışlıyordu; `.env.local`, `*.sqlite`, anahtar dosyaları yanlışlıkla eklenebilirdi (izlenen böyle bir dosya yok) | `.env.*` (`.env.example` hariç), `*.sqlite`, `*.sqlite3`, `*.pem`, `*.key` eklendi |
| F2-72 | Go/No-Go komutu, Master'ın tek bacağı (açık bulgu 87; kullanıcı kararı 2026-11-20) | `gonogo --strategies master_ny_orb_fvg` kabul ediliyor ve rapor yazılıyordu; G4 bu durumda hiçbir zaman GEÇTİ olamıyordu (en iyi durumda DEĞERLENDİRİLEMEDİ, "değerlendirilmeyen: master_asian_sweep") ve mesaj ne yapılacağını söylemiyordu | Komut tek bacağı (tek başına, tekrarlı ya da başka stratejilerin yanında) hiçbir kapıyı çalıştırmadan reddeder (çıkış kodu 2, rapor yok): eksik bacağı, iki geçerli yolu (bütün bacaklar ya da birleşik kimlik) ve aynı motorun bağımsız stratejisini (`ny_orb_fvg`, `asian_sweep`; ad uymuyorsa bağımsız stratejilerin listesini) yazar; önerilen kimlikler `--strategies`'e yazıldığı gibi (virgülden sonra boşluksuz) verilir, karışım reddinin mesajı da öyle. İki bacak birlikte ve `master_portfolio` eskisi gibi değerlendirilir. Dört testin beklentisi bu kurala göre güncellendi (tek bacak veren adımlar iki bacak verir ya da reddi sınar). Test: `tests/unit/test_cli_backtest.py`, `tests/unit/test_decisions_final_attacks.py` |

Ayrıca: planlanan risk betiğin riskine eşitken (taban `Initial Capital` iken) `--pine-risk` koşuyu planlanan riskle
yapar, `-pinerisk` etiketi vermez ve bunu bir NOT ile söyler (Go/No-Go kayıtlı alana baktığı için "kullanılmaz" demek
yanlış olurdu); `--fill-model tv_ohlc` koşusu betiğinkinden farklı bir riskle boyutlanmışsa komut TradingView ile aynı
girdiler için `--pine-risk` gerektiğini hatırlatır; betiğin riskiyle üretilmiş `--trades-out` listesi "G4 bu listeyle
GEÇTİ olamaz" notuyla ve işaretli kimliklerle yazılır (F2-59). Altın dosyalar, portlar ve `pine/` değişmedi. Bu turda
görülen açık bulgu 86 (panelin "Koşu türü" satırı) aynı gün düzeltildi (F2-62).

### SPEC §6.7 ve §6.8 kabul durumu
| Madde | Durum |
|---|---|
| §6.7 Parite raporu ≥ %98 | **kanıt bekliyor** (gerekli dosyalar: her strateji için aynı TradingView grafiğinin bar verisi ve Strategy Tester "List of Trades" CSV'si; doğrulama listesi P25–P32). Altyapı hazır: `propbot parity`, `tests/unit/test_parity*.py`; altın dosyalar yalnızca regresyon |
| §6.7 Veri doğrulama raporu temiz | **kanıt bekliyor** (gerekli dosya: en az 2 yıllık 1 dk gerçek veri; MT5 "Export Bars" ya da TradingView dışa aktarımı; M1–M2). Araç hazır: `propbot data check`, `tests/unit/test_bar_validation.py` |
| §6.7 Go/No-Go raporu tek komutla | sağlandı (kanıt: `propbot gonogo`, `tests/unit/test_gonogo*.py`, `tests/unit/test_provenance*.py`, `tests/unit/test_cli_backtest.py`, `tests/integration/test_pipeline.py` uçtan uca sentetik zincir); hüküm Faz 2'de her zaman NO_GO |
| §6.8 (1) loopback dışı adreste başlamıyor | sağlandı (kanıt: `tests/unit/test_dashboard_security.py`, `tests/integration/test_dashboard.py`) |
| §6.8 (2) yabancı Host/Origin ve belirteçsiz POST reddi | sağlandı (kanıt: `tests/unit/test_dashboard_security.py`, `tests/unit/test_dashboard_attacks.py`, gerçek uvicorn soketi üzerinde) |
| §6.8 (3) veritabanı ya da `.env` yokken salt-okunur | sağlandı (kanıt: `tests/unit/test_dashboard_actions.py`, `tests/unit/test_dashboard_pages.py`, `tests/unit/test_dashboard_attacks.py`) |
| §6.8 (4) Duraklat/Devam/KILL Telegram ile aynı durum + bildirim; ortak PIN kilidi | sağlandı (kanıt: `tests/unit/test_dashboard_actions.py` ikiz store testi); bildirim ayrı işlem (açık bulgu 73) |
| §6.8 (5) analiz ekranı: rapor yok / bozuk dosya / sentetik | sağlandı (kanıt: `tests/unit/test_dashboard_analysis.py`) |
| §6.8 (6) ağ ve veritabanı kenar durumları | sağlandı (kanıt: `tests/unit/test_dashboard_attacks.py`, `tests/unit/test_dashboard_live.py`, `tests/unit/test_store_readonly_attacks.py`) |

### Testler
- Test sayısı (2026-11-20, tüm paket, kullanıcı kararlarının uygulanması, son bağımsız denetim ve bulgu 87 kararından sonra): 7021 geçti, 1 atlandı (IPv6 loopback bu makinede yok) — `uv run pytest -q`;
  `ruff check` ve `mypy --strict` (77 kaynak dosya) temiz.
- Faz 2 test dosyaları SPEC §10'da maddelere göre listelidir.

## Açık bulgular

Durum (2026-11-16): 1, 3–11, 24, 31, 32, 33, 45, 46, 47, 51 ve 53 kapandı; 2, 15, 16 ve 23 kısmen. 12–23 ve 25–30 açık
(18–23 denetimden, 25–27 Faz 2 ön diliminden, 28–30 FTMO doğrulamasından). 31–44 Faz 1'den, 45–54 Faz 1 uygulama
denetiminden, 55–58 2026-11-16 kararlarının uygulamasından. 59–81 Faz 2'den (2026-11-18): 59–64 Pine bulguları
(script başına), 65–69 TradingView/MT5 doğrulama grupları (VERIFY; doğrulama listesi P ve M), 70–81 bilinen boşluklar.
82–85 kapı saldırısının üçüncü turundan (2026-11-19); 78 o turda kısmen kapandı.

Durum (2026-11-19, kullanıcı kararları): 72 kabul edildi ve 82 çözüldü (ikisi de kapandı; 82: ADR-0010); 64(a) "beklenen
güvenli sapma" olarak onaylandı; 74'teki erteleme onaylandı. 86 kararların uygulanması sırasında eklendi ve aynı gün
düzeltildi (F2-62). 87 son bağımsız denetimde eklendi; kullanıcı 2026-11-20'de karar verdi ve aynı gün çözüldü
(F2-72). 88 bu düzeltmenin denetiminde eklendi (önceden beri var, düşük öncelik).

1. ~~**MTF derleme riski:** `riskPts := ...` bildirimi olmayan değişkene atama.~~ **Çözüldü** (`e997dd5`).
2. **B4 sayısal değerleri — kısmen:** kod varsayılanı artık MES test profili (E9). FTMO `US500.cash` spread'i/komisyonu
   Faz 2'de verilecek.
3. ~~**Master dolum barında yeniden fiyatlamıyor.**~~ **Çözüldü** (E1).
4. ~~**Boyutlandırma güncel equity'ye göre.**~~ **Çözüldü** (E2).
5. ~~**Risk varsayılanları** (SB/MR `riskPct` 1,0; SB günlük DD %4).~~ **Çözüldü** (E3).
6. ~~**Günlük işlem sınırı** (MR'de yok; MTF 3, SB 2).~~ **Çözüldü** (E4).
7. ~~**Seansın son barında giriş.**~~ **Çözüldü** (E5).
8. ~~**`immediately` olmayan zorunlu kapanışlar.**~~ **Çözüldü** (E6).
9. ~~**NY/Asian/Master başlıkları "SPY/SPX".**~~ **Çözüldü** (E9).
10. ~~**`pine/README.md` dosya adları.**~~ **Çözüldü** (E10).
11. ~~**`stop_update` sıklığı.**~~ Pine tarafında **çözüldü** (E7); Faz 1 işleyicisi aynı filtreyi mesafe uzayında ayrıca
    uygular (`fb3508f`; venue `tick_size` boşken yalnızca "iyileşme yok" süzülür).
12. **Kesiciler bar kapanışında:** yüzen günlük zarar kontrolü bar içi ihlali ancak bar kapanınca görür (Pine sınırı;
    `calc_on_every_tick` kapalı). Gerçek korumayı botun risk motoru sağlar (§8.1).
13. **Günlük zarar tabanı dosyalar arasında farklı:** NY/Asian/Master `min(gün başı özsermaye, başlangıç sermayesi)` × %;
    SB/MR/MTF gün başı özsermaye × %. Hizalanmadı (istenmedi).
14. **Pine %2 günlük kesici ≠ `config/risk.yaml` `daily_cap_pct: 1.0`:** botun günlük bütçesi daha sıkıdır; bilgi amaçlı.
15. **`exit` alert'inde neden alanı yok — kısmen:** seans sonu / günlük zarar ihlali / tarih sonu alıcıda ayırt
    edilemiyor. Alıcı şeması isteğe bağlı `reason` alanını kabul ediyor (2026-11-13 kararı); Pine dosyaları Faz 0 kabulü
    bitmeden değişmeyeceği için henüz göndermiyor.
16. **Kaldıraç / marjin sınırı — kısmen:** dar stop'larda risk-temelli boyutun notional'ı hesabın kaldıraç sınırını
    aşabilir; venue profilinde kaldıraç alanı yok (VERIFY, Faz 2/3). TradingView tarafında marjin kontrolü kapalı (E9).
    2026-11-16: asgari stop (`signal.min_stop_points`, 1,5 puan) en uç durumları keser, ama bir kaldıraç denetimi
    değildir (ADR-0006: 1,5 puanlık stop hâlâ hesabın birkaç katı nominal önerebilir).
17. **Yarım günlerde 1 barlık işlem:** E5 sınırı — sondan bir önceki bardaki sinyal son barda dolup aynı barın
    kapanışında kapanır.
18. **Zorunlu kapanış fiyatı (denetim D4):** `immediately = true` kapanışları barın kapanış fiyatından dolar. Seansın son
    barında (`session.islastbar`: yarım gün, erken kapanış) canlıda bu fiyat alınamaz; düzleştirme botta takvim tabanlı ve
    kapanıştan önce yapılmalı (§3.1, §7.2). Faz 1 alıcısı yarım günlerde `exit` alert'ine güvenmemeli.
19. **Stop fiyatın yanlış tarafında (denetim D5):** MTF runner stop'u (başa baş / Chandelier) bar kapanışında fiyatın
    yanlış tarafına düşebilir; backtest sonraki açılışta doldurur, gerçek broker stop değişikliğini reddeder. Faz 3
    yürütme kuralı: böyle durumda piyasa emriyle kapat (§3.4.4).
20. **Aynı barda SL + TP (D6) ve sabit 1 tick slipaj (D7):** bar içi yol bilinmez; NY açılışı ve haber barlarında slipaj
    iyimser. Python'da `pessimistic` dolum ve maliyet duyarlılık matrisi (§6); TradingView'da 2× maliyet koşusu (liste C).
21. **`session.islastbar` gerçek zamanlıda (D9):** erken kapanış günlerinde canlı `exit` alert'inin gelip gelmediği
    doğrulanmadı.
22. **MTF hacim filtresi (D10):** MES'te gerçek hacim, FTMO CFD'sinde tick hacmi; yürütme sinyali venue verisinde
    hesaplanacağı için (ADR-0000) parite ve G1 açısından izlenmeli.
23. **Config doğrulaması (denetim Y1–Y10) — kısmen:** şema ve doğrulama geldi (`src/propbot/risk`, ADR-0003). Açık
    kalanlar: ~~`config/strategies.yaml` (Y2)~~ (Faz 2'de geldi: dosya ve doğrulayan yükleyici, `propbot config
    check`); `cost_buffer`, spread koruması ve strateji önceliği anahtarları (Y6, Y10) — risk motoruyla birlikte,
    karar gerekir.
24. ~~**FTMO kuralları doğrulanmadı (VERIFY):** `rules_verified_at` boş; `min_trading_days` ve `daily_loss_basis`
    doğrulama bekliyor.~~ **Çözüldü** (`4fc7bbd`, 2026-11-13): en az 4 işlem günü ve "reset anındaki bakiye" bazı resmi
    sayfadan doğrulandı. Geçerlilik 30 gün.
25. **Monte Carlo sınırları:** botun kendi günlük bütçesi / haftalık tavanı / ardışık kayıp duraklaması / düzleştirme
    eşiği, günler arası bağımlılık, lot yuvarlama ve gün sonu trailing toplam kayıp modellenmiyor (ADR-0002). Risk motoru
    yazılınca simülatör aynı ön kontrol kodunu çağırmalı.
26. **TradingView dönüştürücüsü yok:** simülatör belgelenmiş genel CSV okur (`docs/monte-carlo.md`). "List of Trades"
    dışa aktarımı için dönüştürücü, v2 script'ler derlenip örnek dosya gelince yazılacak. Liste E12 sonrası sürümden
    alınmalı: daha eski sürüm TP'de "dokunma = dolum" saydığı için daha iyimser sonuç verir.
27. **Risk küçültme kademesi deneysel:** `--dd-risk-steps` yalnızca simülatörde karşılaştırma içindir. Bot kuralı olması
    ADR ve SPEC §8.1 değişikliği ister; değişmez kural 4 gereği yalnızca küçültme olabilir.
28. **"Gap trading" maddesi profilde yok (FTMO, bütün aşamalar):** ilgili piyasanın en az 2 saat kapalı kalacağı bir
    kapanıştan 2 saat ya da daha az önce (ve büyük planlı haberlerde) pozisyon açmak yasak uygulamalar listesinde. Cuma
    günü haftalık kapanıştan önceki son 2 saatte NY girişleri bu maddeye girebilir. `US500.cash` seans saatleri terminalden
    okunup risk motoruna "kapanıştan N dakika önce yeni giriş yok" kuralı eklenmeli (şema alanı + test).
29. **Fonlu Standard hesapta haber ve piyasa arası kuralları:** haber penceresinde (±2 dk) SL/TP dolumu da ihlal sayılır;
    2 saatten uzun piyasa aralarında pozisyon taşınamaz. `news_rule` yalnızca pencereyi tutuyor; fonlu aşama için
    "kısıtlı haberden önce düzleştir" davranışı ya da Swing hesap tipi kararı gerekir (Faz 3/4).
30. **`server_tz` biçimi:** FTMO MT5 sunucu saati New York + 7 saat (GMT+2, ABD yaz saatinde GMT+3). Şemadaki tek IANA
    dilimi bunu ifade edemiyor; alan boş kaldı. Bar içe aktarma (§6.1) yazılırken biçim kararlaştırılmalı (ör. çapa
    dilim + saat farkı) ve terminaldeki bilinen bir 09:30 NY mumuyla doğrulanmalı.
31. ~~**Dal geçmişinde anlık görüntü commit'i:** masaüstünden taşınan `WIP: uncommitted changes, moved to the cloud`
    commit'i `main-bz6vo7` dalında kalmıştı.~~ **Çözüldü (2026-11-16, kullanıcı isteği):** içeriği (bağımlılık
    satırları) `cd00945` commit'ine katıldı, dal geçmişi yeniden yazılıp `--force-with-lease` ile push edildi. Bu
    dosyadaki Faz 1 commit kimlikleri yeni geçmişe göre güncellendi; eski kimlikler artık dalda yok.
32. ~~**Kurallar bayatken sinyal davranışı (karar gerekir):** `rules_verified_at` 30 günü geçince `signal` modu çalışmaya
    devam eder (SPEC §8.1); giriş mesajına "[KURALLAR DOĞRULANMADI …]" satırı eklenir ve günde bir alarm gider. Değişmez
    kural 3'ün harfi "yeni giriş yok" der.~~ **Karara bağlandı (2026-11-16):** davranış kalır; ADR-0005 ve CLAUDE.md
    değişmez kural 3 notu.
33. ~~**Asgari stop / azami kaldıraç kuralı yok:** çok dar stop çok büyük lot önerir (ör. 0,25 puan → 1000 lot). Mesaj
    nominal büyüklüğü ve hesaba oranını gösterir, ama otomatik "ATLA" yok.~~ **Çözüldü (2026-11-16, `3d2b72f`):** asgari
    stop 1,5 puan (`signal.min_stop_points`, ADR-0006). Azami kaldıraç ayrı kalır: bulgu 16.
34. **Alert gövdeleri gerçek çıktıyla doğrulanmadı:** şema Pine kaynağı okunarak çıkarıldı. Sayı biçimleri (`5120.00`,
    `"rr":2`), `timeframe.period` değerleri ve özellikle `syminfo.tickerid`'nin uzatılmış seans / ayarlama açıkken
    `={"session":…}` biçiminde gelip JSON'u bozması (→ 400, saatlik uyarı) TradingView'da kontrol edilmeli
    (doğrulama listesi F).
35. **Mesafe uzayı kuralının gürültüsü:** stop güncellemesi kullanıcının çerçevesinde (stop − TradingView dolumu)
    değerlendirilir. TradingView dolumu sinyal barı kapanışından kötüyse ve ilk trail adımı bu kaymadan küçükse güncelleme
    kullanıcı için genişleme sayılır ve "[UYARI] … mevcut stop'unu KORU" mesajı üretir (NY / Master S1; asgari trail
    adımı 4 tick olduğu için yalnızca kayma 1 puanı aşarsa). Doğru ama gürültü olabilir; eşik uydurulmadı. *(Düzeltme
    2026-11-15: MTF dolum barında stop güncellemesi göndermez; önceki metindeki örnek yanlıştı — TV-4.)*
36. **Stop/TP dolumu bildirilmiyor:** TradingView yalnızca zorunlu kapanışları alert eder. Bot bir pozisyonun kapandığını
    bilemez; bütçe bu yüzden temkinli sayılır ve her giriş mesajı "pozisyonun hâlâ açıksa seans sonunda kendin kapat" der.
    TradingView'da iptal edilen bir limit emir FTMO'da dolmuşsa o pozisyon için hiçbir mesaj gelmez. Tersi (TradingView'da
    dolan, FTMO'da bekleyen limit) için giriş ve çıkış mesajları emri seans sonunda sildirir (LIM-1); bot seans sonunu
    bilmez, saati kullanıcı izler.
37. **Sıra dışı teslimat:** kendi giriş alert'inden önce işlenen bir takip alert'i (stop/çıkış) "ilgili işlem yok" olarak
    düşer. İşlenemeyip `error` olan bir girişin takip mesajları da iletilmez (alarm kullanıcıyı TradingView'a yönlendirir).
38. **Heartbeat alarmı tatil bilmez:** tatil ve erken kapanış günlerinde "alarm hattı sessiz" uyarısı beklenen bir yanlış
    alarmdır (borsa takvimi Faz 2'de `exchange_calendars` ile gelir).
39. **Gerçek Telegram'a karşı doğrulanmayanlar:** `code` işaretinin `US500.cash` bağlantısını engellediği; botun özel
    sohbette kullanıcının PIN'li mesajını silebildiği (silemezse uyarı gönderir); 4096 sınırı. İlk kurulumda kontrol et.
40. **python-telegram-bot iç ayrıntısına bağımlılık:** çalışırken iptal edilen token, 22.8'in özel polling görev adından
    algılanıyor; ad değişirse yalnızca bir log satırı kalır (token reddi bir sonraki gönderimde yine fark edilir).
    `ResilientBot`, kuramadığı partiyi ham almak için kütüphanenin korumalı `Bot._post` çağrısını kullanır (açık
    `do_api_request` aynı uç nokta için uyarı üretir); sürüm `uv.lock` ile sabit ve yol testlidir, yükseltmede bakılmalı.
41. **Budama ve göç yok:** `data/propbot.db` büyür (yılda birkaç MB); şema sürümü değişirse eski dosya açılmaz.
    2026-11-16'da sürüm 3 oldu: daha eski sürümle oluşturulmuş bir dosya (yerel deneme, duman testi) adım adım taşınmalı
    (runbook §11); duraklatma/KILL durumu ve günlük bütçe defteri yeni dosyada sıfırdan başlar.
42. **`pine/heartbeat_indicator.pine` derlenmedi** (doğrulama listesi H); alarm koşulunun adı ("Any alert() function
    call"), `timeframe.in_seconds()` davranışı, token girdisindeki `display = display.none` parametresi, yeni atım kuralı
    (`slotClose != slotClose[1]`, `dayofmonth(time_close, tz)`) ve bar süresi denetimi TradingView'da doğrulanmalı (resmi
    Pine belgesine bu ortamdan erişilemedi).
43. **Master `exit` alert'inde `side` net pozisyondan gelir** (iki bacak aynı yönde olduğu için pratikte doğru); alıcı
    çıkışta yönü denetlemez, yön girişinkinden farklıysa çıkış mesajı girişe bağlanmadan iletilir.
44. **Dağıtım belgesinde doğrulanamayanlar:** Tailscale ve cloudflared kurulum komutları, Cloudflare hızlı tünel
    sınırları ve DuckDNS sayfası bu ortamdan okunamadı; runbook'ta (VERIFY) olarak işaretli.
45. ~~**Strateji dosyalarında token girdisi durum satırında görünür (karar gerekir).**~~ **Çözüldü (2026-11-16,
    `b5aa6cd`):** altı dosyada `display = display.none` (E13). Derlenmedi; doğrulama listesi H.
46. ~~**Alert sembolü venue sembolüyle eşleştirilmiyor (karar gerekir).**~~ **Çözüldü (2026-11-16, `3d2b72f`):** venue
    profili `signal.tv_symbols`; listede olmayan sembolün girişi "[SİNYAL — ATLA]" (ADR-0006). Listenin doğruluğu:
    bulgu 55.
47. ~~**Master ile bağımsız NY Open / Asian Sweep aynı `strategy_id`'leri gönderir (karar gerekir).**~~ **Çözüldü
    (2026-11-16, `fb202c9`):** Master bacakları `master_ny_orb_fvg` / `master_asian_sweep` (E14). Derlenmedi; doğrulama
    listesi H.
48. **Gerçek zamanlı veri gereği doğrulanmadı (VERIFY):** CME vadelilerinin TradingView'da aboneliksiz gecikmeli geldiği
    ve alarmların gecikmeli barlarda tetiklendiği bu ortamdan doğrulanamadı (bilgi bellekten). Alıcı tarafı kanıtlı:
    10 dk gecikmeli alert'te giriş "BAYAT", takip mesajları `no_active_trade`. Doğrulama listesi H'de ölçüm maddesi var.
49. **Heartbeat yalnızca kendi alarmını kanıtlar:** TradingView tek bir strateji alarmını durdurursa (script çalışma zamanı
    hatası, tetikleme sınırı) hat sağlıklı görünmeye devam eder. Kabul koşusunda her gün Alerts panelinde bütün alarmların
    "Active" olduğuna bak; otomatik denetim yok.
50. **Silver Bullet: gün/pencere sıfırlamasında iptal alert'i yok (Pine, B/E dışı):** `newDay` / `newWindow` sıfırlaması,
    silahlı ama dolmamış bir limit için `strategy.cancel` çağırmadan ve `cancel` alert'i göndermeden `phase`'i sıfırlar.
    Yalnızca pencerenin son barı eksikse erişilir (olağan durumda 4. blok önce iptal eder). Ayrıca genel düşüş kesicisi
    `cancel` gönderip `phase`'i sıfırlamıyor (pozisyon yokken erişilemez). Kaynak okunarak bulundu, derlenip denenmedi.
51. ~~**`/devam` mesajlarda dokunulabilir (karar gerekir).**~~ **Çözüldü (2026-11-16, `21da0db`):** her `/devam`
    geçişi `code` varlığı.
52. **Düzenlenen mesajlar alınmıyor:** bot yalnızca `message` güncellemelerini ister; eski bir mesaja sonradan düzenlemeyle
    yazılan PIN silinmez (komut olarak da işlenmez). PIN'i düzenlemeyle yazma.
53. ~~**Aynı bot token'ıyla ikinci kopya kodla engellenmiyor (karar gerekir).**~~ **Çözüldü (2026-11-16/17):** makine
    genelinde bot kilidi (`43c87d9`) + varışında Conflict gören kopya beklemeye geçer (`d264050`, `96d5000`,
    `453a37f`; ADR-0004). Kalanlar: bulgu 56.
54. **`systemctl stop` sonrası çıkış kodu:** uvicorn düzgün kapanıştan sonra sinyali yeniden yükseltir; süreç 143 ile biter,
    `run_service`'in dönüş değeri ve `Store.close()` çalışmaz (WAL ve çekirdek kilidi bunu taşır; veri kaybı yok).
    `Restart=always` olduğu için `systemctl stop` dışında davranışı etkilemez; gözlem doğrulayıcıdan, sistemde denenmedi.
55. **Sembol listesi doğrulanmadı (VERIFY):** `signal.tv_symbols`'taki yedi sembol kullanıcının 2026-11-16 kararıdır;
    bu ortamdan TradingView'a erişilemediği için (a) her beslemenin puan ölçeğinin `US500.cash` ile aynı olduğu ve
    (b) TradingView'ın sembolü alert'te tam olarak nasıl yazdığı doğrulanmadı. Özellikle `US500.cash` öneksiz
    yazılmıştır; `syminfo.tickerid` çoğunlukla `BORSA:SEMBOL` biçimindedir, bu yüzden o grafikten gelen girişler
    "Sembol kabul listesinde değil (…)" olarak gelebilir — mesaj doğru yazımı gösterir, listeye o eklenir. Eşleşme
    bilerek birebirdir (ADR-0006). Doğrulama listesi H'de iki madde.
56. **İkinci kopya korumasının kalan açıkları:** (a) Telegram'ın 409 Conflict'i hangi isteğe verdiği bu ortamdan
    gözlenemedi; kural iki dinleyicinin de sırayla 409 aldığı davranışa dayanır. Yalnızca eski süreç alıyorsa yeni kopya
    beklemeye geçmez; eski kopya iki dakika sonra "yol vermeyen dinleyici" kuralıyla bekler ve bot yeni kopyada kalır.
    (b) Öbür süreç 3 saniyelik bekletmeden geç ortaya çıkarsa (hata sonrası uzun bekleme aralığındaysa) yeni gelen kopya
    o arada aldığı bir komutu uygulamış olur. (c) Canlı servis, başka makinede aynı token'la **dinleyen** bir kopya
    varken yeniden başlarsa beklemeye geçen canlı servis olur: girişler kesilir (her giriş "[SİNYAL — ATLA] … BEKLEMEDE"
    olarak gelir), yeniden başlatılana kadar öyle kalır. (d) İki kopya aynı dakikada varırsa ikisi de beklemeye geçer.
    (e) Beklemedeki kopya kendiliğinden etkinleşmez (öbür kopya kaybolsa da); yeniden başlatma gerekir. (f) Bot kilidi
    Linux'a özgüdür (soyut soket) ve ağ ad alanıyla sınırlıdır; yerel başka bir kullanıcı aynı adı önceden bağlayarak
    servisin başlamasını engelleyebilir (tek kullanıcılı sunucuda tehdit modeli dışı). (g) Bot başka bir yerde webhook'a
    bağlanırsa ya da token'ı PropBot olmayan bir program da kullanırsa Telegram sürekli 409 verir: kopya iki dakika
    sonra beklemeye geçer ve yeniden başlatılsa da (öbür dinleyici sürdükçe) beklemede kalır; token'ı bilen biri canlı
    servisin girişlerini bu yolla kesebilir (alarmla; token sızıntısı zaten BotFather → /revoke gerektirir). (h) Yol
    vermeyen dinleyiciyle geçen o iki dakikada komutlar iki süreç arasında rastgele paylaşılır (karar 53 öncesindeki
    durum); alarm bunu söyler.
57. **Master + bağımsız script yan yana: bütçe iki kez sayılır — kabul edildi (kullanıcı kararı, 2026-11-17).** Aynı
    kurulumu iki script de görürse iki giriş mesajı gelir ve günlük bütçede ikisi de sayılır (bot hangisinin alındığını
    bilemez); kullanıcı almadığını `/atladim <id>` ile bildirir. İki mesajın aynı işlem fikri olduğu mesajda yazmaz.
58. **Dar stop eşiği tek değer:** `min_stop_points` venue profili başınadır; strateji ya da zaman dilimi başına ayrı eşik
    yok. Kabul koşusunda `signals report` "atla: stop çok dar" sayısına bakılıp eşik gözden geçirilmeli.
59. **`ny_open_fvg_strategy.pine` — Faz 2 port denetiminin Pine bulguları (port aynısını yapar):** (a) gün başı
    özsermaye ve net kâr ilk gün değişimine kadar na: grafiğin ilk (kısmi) gününde günlük kesici çalışmaz (305–307,
    322–326); (b) gün başı değeri günün ilk barının **kapanışında** alınır, o barda oluşan K/Z (ör. geceden taşınan
    işlemin o barda stop olması) o günün kayıp hesabına girmez; (c) bir işlem K barında bar içinde kapanır ve aynı barda
    yeni giriş sinyali gelirse yönetim sıfırlaması (531–537) yeni `inDir`'i siler: yeni işlem sinyal anı parantezinde
    kalır, hiç yeniden fiyatlanmaz ve iz sürülmez (yalnızca `closeAtEnd` kapalıyken); (d) dolum barında sinyal anında
    tahmin edilen parantez geçerlidir: açılış tahmini stop'un ötesine boşluk yaparsa işlem açılışta kapanır (`tv_ohlc`
    böyle, `pessimistic` dolumda yeniden fiyatlar); (e) `newDay` yalnızca ayın gününü karşılaştırır (213): tam bir ay
    süren bir veri deliği yeni gün sayılmaz; (f) başlık ve SPEC "< 1 ise atla" der, kod `qty > 0` ile işler (421/457).
60. **`asian_sweep_reversal_strategy.pine` — Pine bulguları:** (a) başlık 48 ve ipucu 124 "< 1 ise atla" der, kod
    `qty > 0` ile işler (396/419; "Fractional" ya da vadeli olmayan sembolde "Auto"); (b) `closeAtEnd` kapalıyken önceki
    döngüden taşınan işlem yeni döngünün giriş sinyali barında kapanırsa 472–477 sıfırlaması yeni `inDir`'i siler: yeni
    işlem sinyal anı parantezinde kalır; (c) sinyal barı parantezi (`estStop`) boşlukla dolan işlemde kâr tarafında
    kalabilir: stop dolum barının açılışında, yeniden fiyatlamadan önce dolar (`tv_ohlc`); (d) "ATR Based" stopta ATR
    sinyal barında na ama dolum barında değilse FVG mesafesi ATR mesafesi gibi yeniden uygulanır (393/416/425/452;
    yalnızca grafiğin ilk `atrLenInit` barında); (e) döngü başına tek işlem sınırı yüzünden ardışık zarar kesicisi ancak
    döngü sıfırlamasından sonra katlanan bir zararla tetiklenebilir (ipucu bunu söyler); (f) `riskPct` girdisinin üst
    sınırı yok (Pine %0,5'i aşmaya izin verir; botun kendi tavanı ayrıca geçerlidir).
61. **`master_prop_firm_portfolio.pine` — Pine bulguları:** (a) grafiğin ilk New York gününde günlük zarar kesicisi yok:
    `g_dayStartEquity` / `g_dayStartNet` ilk `newDay`'e kadar na, K/Z terimleri 0'a zorlanır (476–480); (b) giriş
    kapıları dolum barının tarih aralığında olup olmadığına bakmaz: `endDate`'ten önceki son bardaki sinyal aralık
    dışındaki barda dolar ve aynı barın kapanışında "End of test range" ile kapanır (tek barlık işlem); (c) bulgu 60(d)'nin
    aynısı (`s1_inAtrDist` / `s2_inAtrDist`); (d) `newDay` ayın gününü karşılaştırır.
62. **`ict_silver_bullet_strategy.pine` — Pine bulguları:** (a) faz 2'deki "kapanış stop'un ötesinde" iptali
    `backtest_fill_limits_assumption = 1` ile fiilen erişilemez: kapanış stop'un ötesindeyse fiyat önce limiti geçmiş ve
    giriş dolmuştur; (b) `entryFill = CE` iken boşluğun dibi sweep ucunun altındaysa risk 0,5 tick olabilir: limit ancak
    fiyat stop'u geçtikten sonra dolar ve dolum barında anında zararla kapanır (rastgele yürüyüşlerde görüldü); (c) faz 1
    silahlanması yalnızca `entryTimeOK`'a bakar; `haltedToday`, `haltedPerma`, gün filtresi ve tarih aralığı yalnızca av
    başlatmayı keser, genel düşüş durdurması fazı sıfırlamaz (pratikte durdurma işlem kapanınca olduğu için gizli);
    (d) `newDay` ayın gününü karşılaştırır; (e) gün/pencere sıfırlamasında iptal yok: bulgu 50 (aynı bekleyen limit,
    veri deliğinde, yeni bir avın ters yönlü girişiyle birlikte kalabilir; motor bunu hata olarak durdurur).
63. **`mean_reversion_bb_kc_rsi2_strategy.pine` — Pine bulguları:** (a) sinyal barında tahmin edilen parantez dolum barı
    boyunca geçerlidir: dolum tahmini hedefin ötesine boşluk yaparsa (long, `max(orta, close + tick)` üstü) TradingView
    işlemi dolum barının açılışında kapatır, `pessimistic` model dolumda yeniden fiyatlayıp pozisyonu tutar (iki modelin
    bilinen farkı); (b) asgari stop mesafesi yok: ATR(14) yarım tick'in altındaysa stop dolumdan 1 tick'ten yakın olur
    (SPEC §3.1 istisnası); (c) `isNewDay` NY takvim günüdür (gece yarısı), vadeli seansı (18:00) değil; aynı ay gününe
    düşen veri deliği sıfırlamayı kaçırır.
64. **`mtf_trend_continuation_strategy.pine` — Pine bulguları:** (a) trail ısınması: TP1/1,5R ilk `chLen`
    (`atrTrailLen`) bardan önce gelirse `math.max(be, na)` na verir ve runner **stop'suz** kalır (449/465); port başa baş
    tutar — **onaylandı (2026-11-19, kullanıcı kararı 6): beklenen güvenli sapma**; parite raporu bunu yazar, fark yine
    uyuşmazlık sayılır (doğrulama listesi P36); (b) `scaleOutPct < 5` iken TP1 dolumu %95 kuralıyla hiç algılanmaz ve TP1
    çıkışı dolduktan sonra yeniden verilir; (c) başa baş işlem (`tradeNet == 0`) ardışık zarar sayacında zarar sayılır
    (314–318); (d) gün başı özsermaye ilk barın kapanışındaki `strategy.equity`'dir, açık K/Z dahil (296–297);
    (e) `newDay` ayın gününü karşılaştırır.
65. **Göstergeler ve ilk bar davranışı doğrulanmadı (VERIFY; doğrulama listesi P1–P11):** `ta.ema` tohumu (ilk değer mi,
    SMA mı), `ta.rma`, `ta.atr`, `ta.rsi`, `ta.stdev`, `ta.highest/lowest`, `ta.pivothigh/low` eşitlik kuralı (Python
    `strict`), ilk barda `na != 0`'ın yanlış olduğu, bool serinin `x and not x[1]`'inin ilk barda doğru olduğu, na KC ile
    sıkışma karşılaştırması, `timestamp` ay taşması. Bunlar Pine v5 başvuru kılavuzu tanımlarından alındı.
66. **Üst zaman dilimi kovası doğrulanmadı (VERIFY; P12–P13):** Python saatlik barları UTC'de tam saate böler;
    TradingView gün içi HTF barlarını seans başlangıcına hizalıyorsa `htfTf` 60 dışında (240, 90) Python farklıdır.
67. **Broker emülatörü ayrıntıları doğrulanmadı (VERIFY; P15–P22, P24):** açılış limitin 1 tick'ten az ötesindeyken
    dolum; boşlukta hedefin açılıştan, stop'un açılış + slipajdan dolması; `close` / `close_all` dolumlarında slipaj;
    ızgara dışı emir fiyatlarının yuvarlanması; aynı fiyatta ve eşit uzaklıkta sıra; `qty_percent`'in tam kontratta ve
    küsuratlı boyutun yuvarlanması; `pyramiding` anlamı; `close_all(immediately = true)` sonrasında aynı çalışmada
    `position_size` / `closedtrades`; Master'da `opentrades.entry_price`'ın slipajı ve iki bacağın kapanış sırası; kapanan
    girişin kimliği yeniden kullanıldığında eski `strategy.exit`'in yeni girişe bağlanıp bağlanmadığı (motor eskisini siler).
68. **Hesap değerleri doğrulanmadı (VERIFY; P23):** açık K/Z ve `strategy.equity` komisyon içeriyor mu (Python
    içermez); `strategy.closedtrades.profit` net mi brüt mü (Python net); CFD grafiğinde özsermayenin long'u bid'den, short'u
    ask'ten değerleyip değerlemediği (Asian ve Master yüzen günlük kesicisi).
69. **Dışa aktarım biçimleri ve takvim kenarları doğrulanmadı (VERIFY; P25–P34, M1–M5):** "List of Trades" başlıkları,
    satır düzeni ve sırası, `Date/Time`'ın saat dilimi ve dolum barının açılış zamanı olup olmadığı, `Signal` sütununun
    yorum mu kimlik mi gösterdiği (MTF `Exit L` / `Init stop`), açık işlemin ve kısmi çıkışın görünüşü, kodlama ve yerel
    sayı biçimi; TradingView grafik verisi dışa aktarımı; MT5 "Export Bars" biçimi, FTMO sunucu saatinin NY+7 olduğu
    (bulgu 30'daki biçim sorusu `--bar-tz NY+7` ile çözüldü, `server_tz` alanı hâlâ boş ve kullanılmıyor), CFD'de `<VOL>`
    0 iken `<TICKVOL>`; `US500.cash` sözleşme değerleri (`config/backtest.yaml` boş); yarım günde `session.islastbar`;
    `timeframe.in_seconds()`. Örnek dosyalar (`tests/fixtures/parity/`, `tests/fixtures/ohlcv/`) hatırlanan biçimden
    yazıldı.
70. **Pine `f_qty` maliyetleri yok sayar (altı script; port aynısını yapar):** boyut `risk / (stop mesafesi × puan
    değeri)`; çok dar stop'ta komisyon ve slipaj riskin büyük kısmı olur ve gerçek kayıp bütçeyi aşar. Örnek (sentetik
    1 dk, NY ORB): 0,75 puanlık stop'la 133 MES'lik short, 500 $ bütçeye karşı 964 $ kaybetti. Botun Faz 1 süzgeci
    (`signal.min_stop_points` 1,5) canlı mesajlarda en uç durumu keser (ADR-0006); backtest'te böyle işlemler büyük
    negatif R olarak görünür.
71. **MTF HTF geçmişi ısınması:** TradingView `request.security`'si grafiğin ilk barından önceki saatlik geçmişi
    görebilir (VERIFY, P14); Python HTF EMA'larını yalnızca grafik barlarından kurar. Backtest'in ilk günlerinde HTF eğilimi farklı olabilir;
    paritede `--warmup-bars` ile dışarıda bırakılmalı (P14).
72. ~~**Master: Go/No-Go'ya bacaklar mı birleşik görünüm mü (karar gerekir):** backtest iki bacak
    (`master_ny_orb_fvg`, `master_asian_sweep`) ve birleşik `master_portfolio` artefaktı yazar. `gonogo --strategies`'e
    hangisinin verileceği karara bağlanmadı.~~ **Kabul edildi (2026-11-19, kullanıcı kararı 4):** ikisi de kabul
    edilir. `gonogo --strategies` ya iki bacak kimliğini ya da birleşik `master_portfolio`'yu alır; her kimlik kendi
    artefaktından yargılanır (G1–G3), G4 işlem listesini bacak koşularına ya da birleşik koşuya satır kimliği başına
    özetlerle bağlar, G6 her iki yolda da betiğin (`master_portfolio`) parite raporunu okur. **Karışım:** birleşik
    kimlik bir bacağıyla aynı raporda verilemez (aynı işlemler iki kez sayılırdı): komut reddeder (çıkış kodu 2), saf
    katman G4'ü DEĞERLENDİRİLEMEDİ yapar (F2-53…F2-55). Değişmeyen sınır: piramitleme sınırında düşen girişler bacaklara
    dağıtılamaz, bacak artefaktları portföy toplamını yazar; parite oranı betik düzeyindedir, bacak başına ayrı oran
    yoktur.
73. **Panel aksiyonu ile Telegram bildirimi iki ayrı işlem:** Duraklat/Devam/KILL/KILL'i kaldır/Özet'te
    `Commands.handle` kendi işlemini açar; bildirim hemen ardından ikinci işlemde yazılır. Bildirim yazılamazsa durum
    değişmiş kalır, sayfa "YAZILAMADI" der. Atomik yapmak `notify/commands.py`'de değişiklik ister.
74. **Parquet içe aktarma yok:** `pyarrow` eklenmedi (ADR-0008 madde 7); yalnızca CSV. Komut örneklerindeki
    `.parquet` yolu artık `.csv`'dir. Erteleme ve "yeni bağımlılık yok" 2026-11-19'da onaylandı (kullanıcı kararı 2);
    bilinen sınır olarak durur.
75. **Canlı takvim yok:** backtest'te seans sonu veriden türetilir (≥ 30 dk boşluk); 30 dk'dan uzun bir veri deliği
    seans sonu sayılır ve doğrulama raporunda kısa seans görünür. Canlı besleme (Faz 3) bir sonraki barı göremez:
    yarım gün ve tatil takvimi kararı o fazdadır. Bulgu 38'deki "`exchange_calendars` ile gelir" notu bu yüzden geçerli
    değil; heartbeat yine tatil bilmez.
76. **uvicorn'un kendi 400 yanıtı güvenlik başlıksız:** yinelenen `Host` başlığını uvicorn uygulamaya ulaşmadan reddeder;
    bu yanıt panelin CSP / `X-Frame-Options` başlıklarını taşımaz. Gövde düz metindir ve istek verisi içermez; tarayıcı
    böyle istek göndermez.
77. **`tv_ohlc`'de MTF ilk stop'u:** TP1 ya da 1,5R dolum barının içinde olursa stratejinin dolum barında verdiği ilk stop
    zaten başa baş runner stop'udur; kayıttaki ilk stop giriş fiyatı olur ve R 1 tick'lik riskle şişer. Yalnızca parite
    koşusunun istatistiklerini etkiler (karar koşuları `pessimistic`); parite eşleşmesi fiyatlarla yapıldığı için
    eşleşmeyi değiştirmez.
78. **G4 ufku firma kuralına bağlı değil — kısmen kapandı (2026-11-19, F2-47):** G4 artık yalnızca varsayılan ufuk
    (1000 piyasa günü), atlama 0, varsayılan tohum ve karar yol sayısıyla geçer; seçerek geçirmek mümkün değil. Açık
    kalan: 1000 gün bir mühendislik seçimidir; FTMO programında süre sınırı olup olmadığı resmi sayfadan doğrulanmalı
    (VERIFY) ve ufuk gerekirse ona göre değiştirilmeli (kodda tek sabit, `DEFAULT_MAX_DAYS`). Monte Carlo artefaktı
    R'lerin maliyet sonrası olup olmadığını kaydetmez (`--r-is-net` bir beyan); G4'te elle teyit maddesidir. 2026-11-19
    (F2-42): köken satırı maliyetsiz diyorsa beyan yok sayılır ve rapor "MALİYETSİZ işlem listesi" der; G4 maliyetsiz
    listeyi zaten kabul etmez.
79. **Analiz ekranının eşik çizgileri SPEC tabanlarından:** G3/G4 çizgileri `config/backtest.yaml`'dan değil kod
    tabanlarından çizilir (ekran bunu yazar). Config daha sıkıysa karar Go/No-Go raporundakidir.
80. **Doğrulayıcı eşikleri sabit:** aykırı bar 25 × ve açılış sıçraması 15 × kayan medyan gerçek aralık (240 bar), kısa
    seans tipik seansın %90'ı; config'e taşınmadı. Gerçek veride yanlış alarm ya da kaçan bozukluk görülürse gözden
    geçirilmeli. Sentetik üretecin seans şablonu tatil içermez.
81. **`config/strategies.yaml` değişince karar koşuları eskir:** Go/No-Go karar koşusunun kayıtlı parametrelerini
    dosyanın **bugünkü** etkin değerleriyle birebir karşılaştırır (`propbot gonogo` beklenen değerleri dosyadan kurar;
    Master bacağı `master_portfolio` parametrelerine bakar). Dosyada bir değer değişirse o stratejinin en yeni koşusu
    karar koşusu sayılmaz ve strateji DEĞERLENDİRİLEMEDİ olur; daha eski bir koşuya geçilmez. Yeni karar koşusu, işlem
    listesi ve Monte Carlo yeniden üretilmeli; 2026-11-19'dan (F2-40) beri parite raporu da (G6 aynı karşılaştırmayı
    yapar). `samples.oos_start` değişince de aynısı (F2-36); 2026-11-19'dan beri `config/risk.yaml` →
    `risk_per_trade_pct` değişince de (karar koşuları o riskle boyutlanır; ADR-0010, parite raporu etkilenmez). Bilerek
    böyle (başka parametreyle yapılmış koşu başka bir stratejiyi test etmiştir); kullanıcıya görünür etkisi budur.
82. ~~**Strateji riski ile planlanan risk ayrı (karar gerekir; kapı saldırısı 3. tur):** karar koşuları Pine varsayılanı
    `risk_pct` 0,5 ile, Monte Carlo `risk.yaml` → `risk_per_trade_pct` 0,25 ile koşar. "R boyuttan bağımsızdır" ancak
    kısmen doğrudur: komisyon ve kayma R'de boyuttan bağımsızdır, ama (a) boyut tam kontrata yuvarlanır ve `min_qty`
    altı atlanır, (b) stratejilerin kendi yüzde eşikleri (ör. NY ORB `daily_dd_pct` 2: 0,5 %'de 4 kayıp, 0,25 %'de 8)
    hangi işlemlerin var olduğunu değiştirir. Liste 0,5 % boyutla üretilmiş günleri 0,25 % riskle simüle eder.~~
    **Çözüldü (2026-11-19, kullanıcı kararı 5; ADR-0010):** canlı çalışma ve portföy düzeyindeki değerlendirmelerde
    `config/risk.yaml` riski (%0,25) geçerlidir; Pine'daki %0,5 yalnızca tekil strateji varsayımıdır. Kod: karar koşusu
    `risk.yaml` riskini betiğin risk girdisine yazar (`propbot backtest` varsayılanı), `--pine-risk` etiketli
    karşılaştırma koşusudur, parite betiğin riskinde kalır, `config/strategies.yaml` risk girdisini taşıyamaz ve G4
    "karar koşuları → işlem listesi → simülasyon" zincirinin tek riskte olduğunu denetler (F2-49…F2-52); risk tabanı
    `Initial Capital` dışındayken karar koşusu yapılmaz ve betiğin riskiyle üretilmiş liste işaretlidir (F2-57, F2-59).
    Bulgunun sıraladığı iki yoldan ikincisi (G4 eşitliği) uygulandı; birincisi (`strategies.yaml`'da eşitleme) yerine
    tek kaynak seçildi, böylece parite için TradingView girdisinin değişmesi gerekmez. Kalan sınır: portun %0,25'teki
    davranışı ayrı bir TradingView dışa aktarımıyla karşılaştırılmaz (parite betiğin %0,5'indedir).
83. **Kod sürümü kaydedilmiyor (bilerek):** artefaktlar paket sürümünü ya da strateji kodunun özetini taşımaz; bir
    strateji hatası düzeltilmeden önce yapılmış koşu 30 güne kadar (artefakt yaşı sınırı) karar koşusu sayılabilir. Kapı
    koşulu yapılmadı: paket sürümü (0.x) Faz 2 boyunca değişmez, ayırt etmez; kaynak dosyalarının özeti ise her yorum ya
    da ilgisiz düzenlemede bütün kanıtı geçersiz kılar ve git dışı kurulumda kararsızdır. Kural: bir strateji ya da
    motor düzeltmesinden sonra backtest, işlem listesi, Monte Carlo ve parite yeniden üretilir (düzeltme commit'i
    CHANGELOG'da bunu söyler). Faz 3'te `auto` kilidi açılmadan önce yeniden değerlendirilmeli.
84. **Kodun göremeyeceği yalanlar (kapatılamaz):** (a) uydurma veri: aynı ayın kaydırılmış 24 kopyasından yapılmış
    "2 yıllık" dosya tekrar zaman damgası taşımaz, doğrulamadan temiz geçebilir ve G2'nin t-istatistiğini ≈ √24 kat
    şişirir; G1'in elle teyit maddesi (verinin mekanın beslemesinden geldiği) bunun için vardır. (b) `rules_verified_at`
    resmi sayfalar okunmadan bugüne çekilirse ya da `automation_allowed` elle `true` yapılırsa G7 bunu ayırt edemez;
    `docs/compliance/` kayıtları elle teyit maddesidir. (c) İşaret satırı silinmiş sentetik barlar gerçek sayılır
    (araç dosyanın kaynağını bilemez; ADR-0007). Üçü de kullanıcının beyanına dayanır.
85. **Go/No-Go dosyası bir belgedir, yetki değil (Faz 3 için):** `go_no_go_*.json` şemaya uyan elle yazılmış bir dosya
    "GO" diyebilir (model yalnızca iç tutarlılığı denetler) ve başka bir günün eski `go_no_go_YYYYMMDD.md`'si yeni bir
    NO_GO'nun yanında durur. Faz 2'de hiçbir kod bu dosyalardan yetki okumaz (G5 her zaman DEĞERLENDİRİLEMEDİ). Faz 3'te
    `auto` kilidi (değişmez kural 1) dosyaya güvenmemeli: aynı anda `evaluate`'i bugünkü artefaktlar ve config üzerinde
    yeniden çalıştırmalı.
86. ~~**Panelin "Koşu türü" satırı planlanan riski ve OOS sınırını bilmez (2026-11-19):** analiz ekranındaki backtest
    kartı koşu türünü yalnızca dolum modeli, maliyet ve çarpandan çıkarır (`dashboard/analysis.py` → `run_kind`);
    `-pinerisk` (betiğin riski) ve `-oos…` (başka OOS sınırı) etiketli karşılaştırma koşularında "karar koşusu" yazar,
    aynı kartın notlarında ise koşucunun "KARŞILAŞTIRMA KOŞUSU (karar koşusu değil): …" satırı durur.~~
    **Düzeltildi (2026-11-19, F2-62):** koşucunun notu varsa "Koşu türü" satırı onu aynen yazar; panel yine hüküm
    hesaplamaz ve config okumaz. Kalan sınır: satır koşunun yapıldığı andaki durumudur; sonradan `config/risk.yaml`
    ya da OOS başlangıcı değişirse eski bir karar koşusu Go/No-Go'da karşılaştırma koşusu sayılır, kart bunu bilmez.
    Test: `tests/unit/test_dashboard_analysis.py`, `tests/unit/test_risk_rule.py` (gerçek koşucu artefaktlarıyla).
87. ~~**Master'ın tek bacağı tek başına değerlendirilebiliyor (2026-11-20, karar gerekir):** `gonogo --strategies
    master_ny_orb_fvg` (yalnızca bir bacak) reddedilmez; SPEC §9.1 "iki bacak ya da birleşik kimlik" der. Gerçek
    komutlarla G4 bu durumda GEÇTİ olamaz (işlem listesi her zaman iki bacağı taşır, liste değerlendirilen
    stratejilerle aynı değildir: DEĞERLENDİRİLEMEDİ), yani fail-closed'dur.~~ **Çözüldü (2026-11-20, kullanıcı
    kararı; F2-72):** komut tek bacağı hiçbir kapıyı çalıştırmadan reddeder (çıkış kodu 2, rapor yazılmaz) ve
    kullanıcıyı iki bacağa, `master_portfolio`'ya ya da aynı motorun bağımsız stratejisine yönlendirir. Saf katmanın
    tek bacaktaki davranışı (G4 DEĞERLENDİRİLEMEDİ) değişmedi ve testle sabittir.
88. **Standart akışlar kapalıyken komutlar temiz bitmiyor (2026-11-20, son denetim; önceden beri var):** stdout
    kapalıyken (`propbot … >&-`) hata satırı yazıldıktan sonra `cli.py`'deki flush `AttributeError` dökümüyle biter
    (çıkış kodu 1); stderr kapalıyken hata satırı stdout'a gider; `2>/dev/full` sessizce 1 döner. Bütün komutlar
    için geçerlidir, hiçbir durumda rapor yazılmaz. Etkileşimli kullanımda ve systemd altında görülmez; düzeltilmedi.
