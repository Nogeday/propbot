# CHANGELOG

## Faz 0 — Pine düzeltmeleri (2026-11-02)

Kapsam: yalnızca `docs/SPEC.md` §4.1'deki B-maddeleri. Pine v5'te kalındı. Giriş/çıkış mantığı ve mevcut girdilerin
varsayılanları değişmedi; aşağıda "davranış notu" olarak işaretlenen yerler B-düzeltmesinin doğrudan sonucudur.
Tüm dosyalara eklenen iki girdi: **Quantity rounding** (B5; Master/NY/Asian/MR) ve **Webhook token** (B7; hepsi).

Commit'ler: `e26e1bb` baseline · `1035771` NY dosya adı · `aa4025d` NY · `7ad1979` Asian · `a14240f` Master ·
`7d3694a` Silver Bullet · `ab5ecf6` Mean Reversion · `8c0aa72` MTF · `cc6aa94` SPEC §5.1.

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

### `docs/SPEC.md` §5.1
- `strategy_id` değerleri, `cancel` action, yeni `setup` değerleri ve `order_type` (`market`/`limit`) alanı eklendi;
  olmayan sayısal alan `null`.

## Açık bulgular (düzeltilmedi — karar bekliyor)

1. **MTF derleme riski:** `riskPts := ...` (satır ~414 ve short simetriği) bildirimi olmayan bir değişkene `:=` ile atıyor;
   Pine v5'te bu normalde "Undeclared identifier" hatasıdır. Orijinalde de aynı. Derleme hatası verirse düzeltme
   `riskPts = ...` (bildirim) olur — onay gerekiyor.
2. **B4 sayısal değerleri:** Varsayılanlar değişmedi. FTMO `US500.cash` spread'i/komisyonu ve kullanılacak TV sembolü
   (CFD karşılığı) gelince her dosyada `commission_*`/`slippage` tek commit'le ayarlanacak.
3. **Master dolum barında yeniden fiyatlamıyor:** TP ve ATR stop sinyal kapanışına göre sabit; trailing R'si de
   `s1_entryEst`'e göre. NY/Asian tekil dosyalar dolum fiyatını kullanıyor → S1/S2 dosyalar arasında farklı, §3.1
   parite tanımından sapma. Faz 2 paritesinden önce karar verilmeli.
4. **Boyutlandırma güncel equity'ye göre** (bileşik) — NY/Asian/Master/MR/MTF; SB varsayılanı "Account Equity".
   §8.1 başlangıç bakiyesini istiyor.
5. **Risk varsayılanları:** SB ve MR `riskPct = 1.0` (§8.1 tavanı 0,5); SB günlük DD %4.
6. **Günlük işlem sınırı:** MR'de yok; MTF 3, SB 2 — §3.1 "strateji başına günde en fazla 1".
7. **Seansın son barında giriş:** NY/MR/MTF/SB yarım günde (veya yalnız-RTH grafikte) son barda piyasa girişi
   üretebilir → emir bir sonraki seansın açılışında dolar (gece taşıma). B3 yalnızca çıkış tarafını kapsıyor.
8. **`immediately` olmayan zorunlu kapanışlar:** günlük/toplam DD kapanışları ve "End of (test) range" bir sonraki
   barın açılışında doluyor.
9. **NY/Asian/Master başlıkları "SPY/SPX"** diyor; Asian ve S2 23 saatlik enstrüman gerektiriyor (§3.3).
10. **`pine/README.md`** dosya adları gerçek adlarla eşleşmiyor.
11. **`stop_update` sıklığı:** trailing her bar ilerleyebilir; `trail_min_step_ticks` filtresi (değişmez kural 8) Faz 1
    alıcısında uygulanmalı.
