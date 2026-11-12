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

## Açık bulgular

Durum (2026-11-12): 1 ve 3–11 kapandı; 2 kısmen. 12–17 yeni.

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
11. ~~**`stop_update` sıklığı.**~~ Pine tarafında **çözüldü** (E7); Faz 1 alıcısı aynı filtreyi ayrıca uygulamalı.
12. **Kesiciler bar kapanışında:** yüzen günlük zarar kontrolü bar içi ihlali ancak bar kapanınca görür (Pine sınırı;
    `calc_on_every_tick` kapalı). Gerçek korumayı botun risk motoru sağlar (§8.1).
13. **Günlük zarar tabanı dosyalar arasında farklı:** NY/Asian/Master `min(gün başı özsermaye, başlangıç sermayesi)` × %;
    SB/MR/MTF gün başı özsermaye × %. Hizalanmadı (istenmedi).
14. **Pine %2 günlük kesici ≠ `config/risk.yaml` `daily_cap_pct: 1.0`:** botun günlük bütçesi daha sıkıdır; bilgi amaçlı.
15. **`exit` alert'inde neden alanı yok:** seans sonu / günlük zarar ihlali / tarih sonu alıcıda ayırt edilemiyor
    (§5.1 şema v1). Faz 1'de `reason` alanı değerlendirilebilir.
16. **Kaldıraç / marjin sınırı:** dar stop'larda risk-temelli boyutun notional'ı hesabın kaldıraç sınırını aşabilir;
    venue profilinde kaldıraç alanı yok (VERIFY, Faz 2/3). TradingView tarafında marjin kontrolü kapalı (E9).
17. **Yarım günlerde 1 barlık işlem:** E5 sınırı — sondan bir önceki bardaki sinyal son barda dolup aynı barın
    kapanışında kapanır.
