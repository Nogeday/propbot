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

**Durum:** kod ve testler tamam (3416 test, ruff, mypy --strict). SPEC §5.7'deki 10 işlem günlük kabul koşusu **yapılmadı**:
Faz 0 script'leri TradingView'da henüz derlenmedi, alarmlar kurulmadı, bot bir sunucuya kurulmadı. Hiçbir şey gerçek
TradingView ya da gerçek Telegram'a karşı denenmedi (bu ortamdan ikisine de erişim yok).

Commit'ler (sırayla): `4257211` bağımlılıklar · `d09835f` saat yardımcıları · `701e363` şema · `209b858` + `41e9973` store ·
`e26ccee` tv_alerts config · `02c925b` sırlar ve log · `5678301` biçimleyiciler · `517d3ff` alıcı · `3380ecb` boyutlandırma ·
`fd699f1` mod kilidi · `47f2db0` izleme pencereleri · `0bcbe8a` + `958888f` Telegram · `f30dae7` + `bb0bf4c` işleyici ve
heartbeat · `10ef7fc` CLI serve + rapor · `fbe018e` Pine heartbeat · `7390635` dağıtım ve runbook.

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
| SEC-1 | Komut işleyici her mesajı PIN ile karşılaştırıp yanıtı ona göre seçiyordu: `/kill` ve `/devam` dışında, deneme sayacına takılmadan "bu PIN mi?" sorusunu yanıtlayan bir kehanet | Silinecek mesaj biçimine göre seçilir (6+ haneli sayı ya da `/kill`·`/devam` sonrası metin); yanıt sayıyı yinelemez; PIN yalnızca iki komutun içinde karşılaştırılır. Özellik testi: iki farklı PIN için yanıtlar aynı | `191ac8a` |
| SEC-3 | Kimliksiz istek alarmının saatlik kapısını yabancı bir istek açınca, eski token'la gönderen kendi alarmımız yalnızca sayılıyor, bir saat sonra başka bir ret gelmezse hiç bildirilmiyordu | Sayılar alarm gerçekten gidene kadar tutulur; izinli adresten gelen sonraki herhangi bir istek (geçerli heartbeat dahil) bekleyen alarmı tetikler; store reddederse 5 dk sonra yeniden denenir | `2f637cd` |
| LIM-1 | Limit giriş mesajı "İPTAL mesajı gelene kadar bekler" diyordu; TradingView'daki limit dolup kullanıcının emri dolmazsa İPTAL hiç gelmez, emir süresiz bekler | Limit giriş: "İPTAL ya da ÇIKIŞ mesajı gelene kadar, en geç seans sonuna kadar bekler" + "seans sonunda emrin hâlâ bekliyorsa SİL"; limit girişin (ya da görülmemiş girişin) `[ÇIKIŞ]` mesajı bekleyen emri de iptal ettirir | `1f189c7` |
| MON-1 | Kapalı kalma yalnızca 90 sn'den uzun boşlukta bildiriliyordu; birkaç saniyede bir çöküp kalkan servis (systemd `RestartSec=5`) Telegram'da görünmüyordu | İzlenen pencereye denk gelen kısa boşluk da bildirilir (en çok 30 dakikada bir); bastırılanlar sayılır, sonraki alarm "N. kez" der | `2fae82a` |
| CMD-1 | Yardım ve kullanım satırlarındaki `/atladim [id]` Telegram'da dokunulabilir bağlantıydı; dokununca çıplak `/atladim` gider ve **son** girişin riskini bütçeden düşerdi | `/atladim` ve `/aldim` geçtiği her yerde `code` olarak gider (dokununca gönderilmez) | `df9f74d` |
| SEC-2 | Token, grafikte script'in durum satırında ve (açıksa) alarmın e-posta/uygulama bildirimlerinde görünür | Heartbeat göstergesinde girdi `display.none` (derlenmedi, doğrulama listesi H); runbook §8.1–8.2: yalnızca Webhook URL bildirimi, paylaşım uyarısı, token yenileme adımları. Altı strateji dosyası için → açık bulgu 45 | `461abbe` |
| SYM-1 | Alert'teki TradingView sembolü venue sembolüyle eşleştirilmiyor | Karar gerekir → açık bulgu 46 | — |

### Testler
- 3453 test (`uv run pytest -q`), ruff, `mypy --strict` (36 kaynak dosya) temiz (uygulama denetimi sonrası; ilk teslimde 3416).
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

## Açık bulgular

Durum (2026-11-13): 1, 3–11 ve 24 kapandı; 2, 15 ve 23 kısmen. 12–23 ve 25–30 açık (18–23 denetimden, 25–27 Faz 2 ön
diliminden, 28–30 FTMO doğrulamasından). 31–44 Faz 1'den, 45–46 Faz 1 uygulama denetiminden.

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
    uygular (`f30dae7`; venue `tick_size` boşken yalnızca "iyileşme yok" süzülür).
12. **Kesiciler bar kapanışında:** yüzen günlük zarar kontrolü bar içi ihlali ancak bar kapanınca görür (Pine sınırı;
    `calc_on_every_tick` kapalı). Gerçek korumayı botun risk motoru sağlar (§8.1).
13. **Günlük zarar tabanı dosyalar arasında farklı:** NY/Asian/Master `min(gün başı özsermaye, başlangıç sermayesi)` × %;
    SB/MR/MTF gün başı özsermaye × %. Hizalanmadı (istenmedi).
14. **Pine %2 günlük kesici ≠ `config/risk.yaml` `daily_cap_pct: 1.0`:** botun günlük bütçesi daha sıkıdır; bilgi amaçlı.
15. **`exit` alert'inde neden alanı yok — kısmen:** seans sonu / günlük zarar ihlali / tarih sonu alıcıda ayırt
    edilemiyor. Alıcı şeması isteğe bağlı `reason` alanını kabul ediyor (2026-11-13 kararı); Pine dosyaları Faz 0 kabulü
    bitmeden değişmeyeceği için henüz göndermiyor.
16. **Kaldıraç / marjin sınırı:** dar stop'larda risk-temelli boyutun notional'ı hesabın kaldıraç sınırını aşabilir;
    venue profilinde kaldıraç alanı yok (VERIFY, Faz 2/3). TradingView tarafında marjin kontrolü kapalı (E9).
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
    kalanlar: `config/strategies.yaml` (Y2); `cost_buffer`, spread koruması ve strateji önceliği anahtarları (Y6, Y10) —
    risk motoruyla birlikte, karar gerekir.
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
31. **Dal geçmişinde anlık görüntü commit'i:** `b6fcbc8 WIP: uncommitted changes, moved to the cloud` (yalnızca
    bağımlılık satırları) `main-bz6vo7` dalında kaldı; yeniden yazmak zorla push gerektirir ve bu oturumda reddedildi.
    Birleştirirken squash edilebilir.
32. **Kurallar bayatken sinyal davranışı (karar gerekir):** `rules_verified_at` 30 günü geçince `signal` modu çalışmaya
    devam eder (SPEC §8.1); giriş mesajına "[KURALLAR DOĞRULANMADI …]" satırı eklenir ve günde bir alarm gider. Değişmez
    kural 3'ün harfi "yeni giriş yok" der. Girişleri tamamen kesmek istenirse tek satırlık değişiklik (ADR-0005).
33. **Asgari stop / azami kaldıraç kuralı yok:** çok dar stop çok büyük lot önerir (ör. 0,25 puan → 1000 lot). Mesaj
    nominal büyüklüğü ve hesaba oranını gösterir, ama otomatik "ATLA" yok; eşik için karar + ADR gerekir (bulgu 16 ile
    birlikte).
34. **Alert gövdeleri gerçek çıktıyla doğrulanmadı:** şema Pine kaynağı okunarak çıkarıldı. Sayı biçimleri (`5120.00`,
    `"rr":2`), `timeframe.period` değerleri ve özellikle `syminfo.tickerid`'nin uzatılmış seans / ayarlama açıkken
    `={"session":…}` biçiminde gelip JSON'u bozması (→ 400, saatlik uyarı) TradingView'da kontrol edilmeli
    (doğrulama listesi F).
35. **Mesafe uzayı kuralının gürültüsü:** TradingView dolumu sinyal barı kapanışından kötüyken mutlak seviyesi değişmeyen
    bir `stop_update` (MTF'de dolum barı yeniden fiyatlaması) kullanıcı için genişleme sayılır ve "[UYARI] … mevcut
    stop'unu KORU" mesajı üretir. Doğru ama sık olabilir; eşik uydurulmadı.
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
41. **Budama ve göç yok:** `data/propbot.db` büyür (yılda birkaç MB); şema sürümü değişirse eski dosya açılmaz.
42. **`pine/heartbeat_indicator.pine` derlenmedi** (doğrulama listesi H); alarm koşulunun adı ("Any alert() function
    call"), `timeframe.in_seconds()` davranışı ve token girdisindeki `display = display.none` parametresi TradingView'da
    doğrulanmalı (resmi Pine belgesine bu ortamdan erişilemedi).
43. **Master `exit` alert'inde `side` net pozisyondan gelir** (iki bacak aynı yönde olduğu için pratikte doğru); alıcı
    çıkışta yönü denetlemez, yön girişinkinden farklıysa çıkış mesajı girişe bağlanmadan iletilir.
44. **Dağıtım belgesinde doğrulanamayanlar:** Tailscale ve cloudflared kurulum komutları, Cloudflare hızlı tünel
    sınırları ve DuckDNS sayfası bu ortamdan okunamadı; runbook'ta (VERIFY) olarak işaretli.
45. **Strateji dosyalarında token girdisi durum satırında görünür (karar gerekir):** altı strateji script'inde
    `alertToken = input.string("", "Webhook token", …)` varsayılan görünürlükle tanımlı; değer grafikte script adının
    yanında yazar (ekran görüntüsü / paylaşılan grafik token'ı gösterir). Önerilen tek argümanlık değişiklik:
    `display = display.none` (heartbeat göstergesinde uygulandı; giriş/çıkış mantığına ve varsayılanlara dokunmaz). B/E
    maddelerinin dışında olduğu için Pine kuralı gereği uygulanmadı; onay verirsen altı dosyaya eklenir. O zamana kadar:
    runbook §8.1 (durum satırında girdileri gizle, yalnızca Webhook URL bildirimi).
46. **Alert sembolü venue sembolüyle eşleştirilmiyor (karar gerekir):** alıcı `symbol` alanının yalnızca biçimini denetler.
    Strateji başka bir grafiğe (ör. `CME_MINI:NQ1!` ya da bir hisse) eklenip alarm kurulursa sinyal iletilir; stop/TP
    mesafeleri **başka bir enstrümanın puan ölçeğinde** gelir ve `US500.cash`'e uygulanırsa anlamsız bir işlem olur (lot
    o mesafeye göre hesaplandığı için işlem başı risk tavanı yine aşılmaz). Mesaj başlığı sembolü gösterir, ama otomatik
    "ATLA" yok. Öneri: venue profiline (ya da `config/tv_alerts.yaml`'a) puanı
    venue sembolüyle aynı ölçekte olan TradingView sembollerinin listesi (ör. `CME_MINI:MES1!`, `CME_MINI:ES1!`);
    listede olmayan sembolün girişi "[SİNYAL — ATLA]" olur. Hangi sembollerde çalıştıracağını bilmeden liste
    uydurulmadı; 10 günlük kabul koşusundan önce karar verilmeli.
