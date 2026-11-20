# Backtest çalışma kılavuzu — veri dosyasından Go/No-Go raporuna

Bu belge Faz 2 araçlarını sırayla kullanmayı anlatır: veriyi dışa aktar → doğrula → backtest (karar koşusu ve
karşılaştırmalar) → işlem listesi ve Monte Carlo → parite → Go/No-Go → panelde bak. Kararlar: ADR-0007 (sentetik veri
ilkesi), ADR-0008 (motor), ADR-0009 (panel), ADR-0010 (işlem başı risk). Spesifikasyon: `docs/SPEC.md` §6 ve §9. Monte
Carlo ayrıntısı: `docs/monte-carlo.md`.

> **Komutların güncel seçenekleri:** her komutun `--help` çıktısı. Bu belgeyle çelişirse `--help` doğrudur ve belge
> düzeltilmelidir.

**Bugünkü durum (2026-11-18):** depoda gerçek piyasa verisi ve TradingView dışa aktarımı yok. Araçlar deterministik
**sentetik** barlarla geliştirildi ve test edildi; sentetik veriyle üretilen her rapor "SENTETİK VERİ" yazar ve hiçbir
şey kanıtlamaz. Faz 2'de Go/No-Go raporu hiçbir durumda GO vermez: G5 (demo forward) Faz 3 ister.

**Kullanıcı kararları (2026-11-19):** teslimat onaylandı. Bu kılavuzu etkileyenler: karar koşuları `config/risk.yaml`
riskiyle yapılır (4.0), Master iki bacağıyla da birleşik koşusuyla da değerlendirilebilir (7. bölüm), MTF ısınmasındaki
başa baş stop "beklenen güvenli sapma"dır (6. bölüm), Go/No-Go'nun ek eşikleri aynen kalır (7. bölüm). Tam liste:
`CHANGELOG.md` Faz 2 bölümü.

## 0. Dosyalar nereye yazılır

| Klasör | İçerik | Git |
|---|---|---|
| `data/` | senin veri dosyaların (`data/us500_1m.csv` gibi), botun veritabanı `data/propbot.db` | yok sayılır (`.gitignore`: `/data/`) |
| `reports/` | JSON rapor artefaktları, işlem listeleri, Go/No-Go Markdown'ı | yok sayılır (`/reports/`) |

- Artefakt adı `<tür>_<etiket>_<YYYYMMDDTHHMMSSZ>.json` (UTC). Türler: `backtest`, `cost_matrix`, `walk_forward`,
  `monte_carlo`, `go_no_go`, `data_validation`, `parity`. Bir artefakt **hiçbir zaman üzerine yazılmaz**; her koşu yeni
  dosyadır. Elle düzenlenmiş ya da bozuk dosyayı okuyucu reddeder ve panel onu "Okunamayan raporlar" altında listeler.
- Go/No-Go ayrıca `reports/go_no_go_YYYYMMDD.md` yazar (o günün en yeni hükmü; aynı gün yeni bir koşu onu değiştirir,
  her koşunun JSON'u kalır).
- Veri dosyasını değiştirmek (yeniden dışa aktarmak, satır silmek) sha256'sını değiştirir: o dosyayla üretilmiş
  raporlar yeni dosyaya bağlanmaz, adımlar baştan yapılır.

## 1. Önce enstrüman profili (yalnızca CFD için)

`config/backtest.yaml` → `instruments` iki profil taşır:

- **`MES`** (dolu): Pine başlığındaki test profili. Tick 0,25 · puan değeri 5 USD · tam kontrat · taraf başı 0,50 USD
  komisyon · 1 tick slipaj · spread 0. TradingView'daki MES grafiğiyle karşılaştırma bu profille yapılır.
- **`US500.cash`** (boş): FTMO MT5 endeks CFD'si. Her alan `null` = **VERIFY**. Boş alanı olan profille backtest
  çalışmaz; komut durur ve hangi alanın boş olduğunu söyler. Go/No-Go da boş profili "beklenen maliyet" saymaz.

US500.cash ile backtest yapmadan önce MT5 terminalinde **Market Watch → US500.cash → sağ tık → Specification**
(menü adı terminal sürümüne göre değişebilir) penceresinden oku ve yaz:

| Alan | Anlamı | Nereden |
|---|---|---|
| `tick_size` | en küçük fiyat adımı | Specification: tick size / digits |
| `point_value` | 1,00 puanlık hareketin 1,00 lot için hesap para birimindeki değeri | tick value ÷ tick size |
| `qty_step`, `min_qty` | lot adımı, en küçük lot | volume step, minimal volume |
| `costs.commission_per_unit_side` | lot başına **taraf başı** komisyon (yoksa 0) | FTMO'nun komisyon tablosu (VERIFY) |
| `costs.slippage_ticks` | piyasa ve stop dolumlarına eklenen aleyhte kayma | senin kararın; 1 tick'ten iyimser yazma |
| `costs.spread_points` | pencereler dışındaki tipik spread, puan | terminalde gözlem (VERIFY) |
| `costs.spread_windows` | NY saatine göre geniş spread pencereleri, ör. `{start: "09:25", end: "09:40", spread_points: 1.2}` | gözlem |

Aynı sayılar Faz 1'in venue profilinde de vardır (`config/venues/ftmo_2step_100k.yaml` → `contract.*`); ikisi aynı
şartnameden doldurulur ve tutarlı kalmalıdır. Yazdıktan sonra `uv run propbot config check`.

CFD barları **bid** kabul edilir; alım yönlü her dolum bid + spread'den olur (ADR-0008 madde 4). Bu yüzden ayrı bir
"spread maliyeti" satırı yoktur, spread fiyata gömülüdür.

## 2. Veriyi dışa aktar

Yükleyici dosya biçimini başlık satırından tanır (dosya adından değil). Parquet okunmaz (ADR-0008 madde 7: `pyarrow`
eklenmedi); CSV kullan. Hangi düğmeye basılacağı senin bilgin; araçların beklediği dosya aşağıdadır. İki dışa aktarım
biçimi de gerçek bir dosyayla denenmedi (VERIFY; `docs/tv-verification-checklist.md` P ve M maddeleri).

**TradingView grafik verisi** (genel biçim):
- Virgülle ayrılmış; başlıkta en az `time,open,high,low,close`, isteğe bağlı `volume` (büyük/küçük harf fark etmez).
  Gösterge sütunları yok sayılır.
- `time`: Unix saniye (ya da milisaniye), ofsetli ISO 8601 ya da ofsetsiz ISO 8601. **Ofsetsizse** saatler
  `--bar-tz` ile okunur: grafiğin saat dilimini ver (ör. `America/New_York`). Unix zamanı saat diliminden bağımsızdır;
  seçenek varsa onu tercih et.
- Zaman damgası barın **açılış** anıdır (TradingView `time`). Dışa aktarımın da öyle olduğu doğrulanmadı (VERIFY).
- Zaman dilimi: Go/No-Go G1 **1 dakikalık** ve en az 2 yıllık veri ister. Asian Sweep ve Master S2 için 20:00–00:00 ET
  barları gerekir (grafikte uzatılmış seans açık).

**MetaTrader 5 "Export Bars":**
- Sekmeyle ayrılmış; başlık `<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>`; tarih `YYYY.AA.GG`.
  Sütun sırası başlıktan okunur.
- Kodlama UTF-8 olmalı. UTF-16 dosya reddedilir ve dönüştürme komutu söylenir: `iconv -f UTF-16 -t UTF-8 girdi > çıktı`.
- Saatler brokerin **sunucu saatidir**; `--bar-tz` vermeden MT5 dosyası okunmaz. FTMO için `--bar-tz NY+7` (New York
  duvar saati + 7 saat; ABD ile aynı günlerde yaz saatine geçen GMT+2/GMT+3). **VERIFY:** terminalde bilinen bir
  09:30 NY barı sunucu saatinde 16:30 görünmeli. Sunucu gerçekten UTC ise `Etc/UTC` yaz (düz `UTC` MT5'te reddedilir).
- `<VOL>` CFD'de 0'dır; o zaman hacim olarak `<TICKVOL>` kullanılır (varsayım; MTF hacim filtresini etkiler).

Yükleyici hiçbir şeyi onarmaz (sıralama, tekilleştirme, fiyat düzeltme yok): sıra dışı, yinelenen, üst üste binen ya da
tutarsız (high < close gibi) bar dosyayı **yüklenemez** yapar ve satır numaralarıyla listelenir. Yaz saati geçişinde
olmayan saat satır hatasıdır; iki kez yaşanan saat önceki ana okunur (dosya artan kalacaksa sonrakine).

### Sentetik veri (yalnızca deneme)

```bash
uv run propbot data synth --out data/sentetik_5m.csv
```

Seçenekler: `--tf` (dakika, 60'ı tam bölmeli; 5), `--sessions` (250), `--start` (ilk seansın kapanış günü; 2024-01-02),
`--seed` (1), `--symbol` (`SYNTH-MES`), `--force` (var olan dosyanın üzerine bilerek yaz). Aynı tohum her seferinde aynı
baytları üretir. Dosyanın ilk satırı `#propbot-synthetic` işaretidir; yükleyici onu görür ve o dosyayla üretilen her
rapor "SENTETİK VERİ" taşır. Seans şablonu bir varsayımdır (NY 18:00–17:00, tatil yok). Bu dosya komutları denemek
içindir; hiçbir stratejiyi, pariteyi ya da veri kalitesini kanıtlamaz (ADR-0007).

## 3. Veriyi doğrula

```bash
uv run propbot data check --data data/us500_1m.csv --bar-tz NY+7 --symbol US500.cash
```

Seçenekler: `--tf` (bar süresi, TradingView yazımıyla `1`, `5`; verilmezse dosyadaki en sık aralık; verilen değer
dosyadakinden farklıysa `data check` dosyayı o süreyle denetler ve fark boşluk olarak raporlanır, `backtest` ve
`parity` ise dosyayı reddeder), `--bar-tz` (`UTC` varsayılan · `NY+7` · IANA dilimi), `--symbol` (varsayılan dosya
adı), `--config-dir`, `--reports-dir`, `--no-artifact` (rapor dosyası yazma).
Çıkış kodu: **0** temiz · **1** temiz değil · **2** dosya okunamadı.

Rapor (`reports/data_validation_…json` ve ekran):
- **Engelleyiciler:** okunamayan satır, tutarsız OHLC, yinelenen bar, sıra dışı bar, üst üste binen bar.
- **Temizliği bozanlar:** seans içi boşluklar (eksik bar sayısıyla), aykırı bar (bar aralığı > 25 × kayan medyan gerçek
  aralık, 240 barlık pencere; ilk 30 bar denenmez), seans içi açılış sıçraması (> 15 × medyan; seansın ilk barında
  denenmez), **saat dilimi kayması** (ABD ya da AB yaz saati geçişinin iki yanındaki 5 seansın ilk barı farklı NY
  saatinde açılıyorsa dosya yanlış `--bar-tz` ile okunmuştur).
- **Bilgi (temizliği bozmaz):** kısa seanslar (tipik seansın %90'ından az bar: yarım gün mü?), seansı olmayan hafta
  içi günler (tatil mi?), hacmi sıfır bar sayısı. Takvim verinin kendisidir: seans sonu bir sonraki bara olan ≥ 30 dk
  boşluktan türetilir (`config/backtest.yaml` → `session.min_gap_minutes`); 30 dk ve üstü bir veri deliği de seans
  sonu gibi görünür ve kısa seans olarak listelenir.
- Kaynak bilgisi: dosya adı, biçim, sha256, `bar_tz`, sentetik işareti, kapsama tarihleri.

G1, backtest'in veri dosyasıyla **aynı sha256'lı ve aynı okumalı** (bar saat dilimi, bar süresi, ilk ve son bar) en
yeni doğrulama raporunu arar; temiz değilse G1 KALDI. Dosyayı backtest'te hangi `--bar-tz` / `--tf` ile okuyacaksan
doğrulamayı da onunla yap: NY+7 ile doğrulanıp UTC ile backtest edilen MT5 dosyası yedi saat kaymış barlardır ve G1'i
KALDI yapar (cümle iki değeri de yazar).

## 4. Backtest

```bash
uv run propbot backtest --strategy ny_orb_fvg --data data/us500_1m.csv --bar-tz NY+7 --instrument US500.cash
uv run propbot backtest --strategy all --data data/mes_1m.csv --bar-tz America/New_York --matrix --walk-forward \
    --trades-out reports/trades_mes.csv
```

Seçenekler: `--strategy ID|all` — kimlikler `ny_orb_fvg`, `asian_sweep`, `ict_silver_bullet`, `mr_bb_kc_rsi2`,
`mtf_trend_cont`, `master_portfolio`; `all` yalnızca **beş bağımsız portu** koşar (Master'ın bacakları bunlardan
ikisinin aynısıdır; Master ayrıca `--strategy master_portfolio` ile koşulur). Ayrıca `--tf`, `--bar-tz`, `--symbol`,
`--instrument` (varsayılan `default_instrument: MES`), `--venue` (venue profilinin adı, ör. `ftmo_2step_100k`;
başlangıç sermayesi onun `initial_balance`'ı; tek profil varsa gerekmez), `--oos-start YYYY-AA-GG` (varsayılan
`samples.oos_start: 2025-01-01`, UTC gece yarısı; giriş zamanı bu andan itibaren OOS; config'ten farklı bir tarih
karşılaştırma koşusu üretir, bkz. 4.1), `--fill-model
pessimistic|tv_ohlc`, `--cost-factor F` ya da `--cost-free`, `--pine-risk` (işlem başı riski `config/risk.yaml`'dan
alma, betiğin kendi değeriyle koş; karşılaştırma koşusu, bkz. 4.0), `--matrix`, `--walk-forward`, `--trades-out PATH`,
`--config-dir` (varsayılan `config`), `--reports-dir` (varsayılan `reports`), `--no-artifact`.
`--matrix` yalnızca maliyetli ve çarpanı 1,0 olan koşuyla kabul edilir. `--trades-out` var olan bir dosyanın üzerine
yazmaz (başka yol ver ya da dosyayı kendin sil).

Parametreler Pine girdi varsayılanlarıdır; `config/strategies.yaml` yalnızca açıkça değiştirileni taşır (bugün hepsi
boş). Oraya yazılan her değer TradingView script'inden bir sapmadır ve parite yalnızca script'te aynı değer girilirse
geçerlidir. Optimizasyon yoktur. Koşu kullandığı etkin parametreleri artefakta yazar; Go/No-Go yalnızca parametreleri
`config/strategies.yaml`'ın **bugünkü** değerleriyle aynı olan koşuyu karar koşusu sayar: dosyayı değiştirdiysen karar
koşusunu yeniden yap. Tek istisna işlem başı risk girdisidir: o dosyaya yazılamaz, kaynağı `config/risk.yaml`'dır (4.0).

### 4.0 İşlem başı risk: hangi koşu hangi riskle (2026-11-19 kullanıcı kararı, ADR-0010)

Kural: **canlı çalışma ve portföy düzeyindeki her değerlendirme `config/risk.yaml` → `risk_per_trade_pct` (%0,25) ile
yapılır; Pine script'lerindeki %0,5 yalnızca tekil strateji varsayımıdır.**

| Koşu | İşlem başı risk | Sonuç |
|---|---|---|
| `propbot backtest` (varsayılan) | `config/risk.yaml` riski; betiğin risk girdisine (`risk_pct`, MTF'de `risk_percent`) yazılır | karar koşusu olabilir |
| `propbot backtest --pine-risk` | betiğin kendi değeri (Pine varsayılanı %0,5) | karşılaştırma koşusu, etiketi `-pinerisk` |
| `propbot parity` | her zaman betiğin kendi değeri | G6'nın girdisi (6. bölüm) |
| `propbot montecarlo` | `config/risk.yaml` riski (`--risk-pct` ile değişir; G4 planlanan riski ister) | G4'ün girdisi |

- **Özet ne yazar.** Her backtest özeti ve artefakt notu bir "İşlem başı risk" satırı taşır. Karar koşusunda:
  `İşlem başı risk: %0,25 — planlanan risk (config/risk.yaml risk_per_trade_pct), betiğin risk_pct girdisine yazıldı;
  Pine varsayılanı %0,5`, parametre satırı da `Pine varsayılanları (işlem başı risk dışında)` der. `--pine-risk`
  koşusunda özet `! KARAR KOŞUSU DEĞİL (…): işlem başı risk Pine varsayılanı (%0,5); karar koşusu config/risk.yaml
  riskiyle yapılır` ile başlar. Master'da satır ayrıca eşzamanlı risk tavanının (`max_concurrent_risk_pct`, %1,0)
  betiğin kendi girdisi olarak kaldığını söyler: planlanan risk yalnızca ortak `risk_pct`'ye yazılır.
- **Neden iki risk aynı listeyi vermez.** R boyuttan bağımsız değildir: boyut tam birime aşağı yuvarlanır, asgari
  boyutun altı atlanır ve script'lerin kendi yüzde kesicileri başka sayıda kayıptan sonra tetiklenir. Portların gerçek
  koduyla, **sentetik** fikstürde (yalnızca mekanizmayı gösterir): MTF'nin ilk girişi %0,5'te 17, %0,25'te 8 kontrattır
  (8,5 aşağı yuvarlanır); tek sayılı 17 bütün olarak stop'a gider, çift sayılı 8 önce TP1'de yarıyı kapatır; çıkışlar
  ve R farklıdır. 10 000 sermayede MR %0,5'te 6, %0,25'te 5 işlem açar (`tests/unit/test_risk_rule.py`).
- **TradingView'ın sayılarıyla kıyas.** TradingView betiği kendi %0,5'iyle çalıştırır. Strategy Tester'daki sonuçlar
  ile %0,25'teki karar koşuları **karşılaştırılamaz** (başka boyutlar, kısmen başka işlemler; üstüne kötümser dolum
  modeli ve farklı R tanımı, 4.2). TradingView ile karşılaştırma `propbot parity` ile yapılır (6. bölüm). Betiğin
  riskiyle Python'da bakmak istersen `--pine-risk`; `--fill-model tv_ohlc` koşusunu `--pine-risk` olmadan yaparsan
  komut bunu bir NOT ile hatırlatır.
- **Sınırlar.** `config/strategies.yaml`'a risk girdisi (`risk_pct` / `risk_percent`) yazmak, değeri ne olursa olsun,
  yükleme hatasıdır ve o dosyayı okuyan her komutu durdurur. Planlanan riski betiğin girdisi kabul etmiyorsa (girdinin
  alt sınırının altı) koşu hatayla durur; değer sessizce değiştirilmez. `config/risk.yaml`'daki risk betiğin riskine
  eşitse `--pine-risk` risk bakımından karşılaştırılacak fark bırakmaz: koşu planlanan riskle yapılır, `-pinerisk`
  etiketi almaz ve bir NOT bunu söyler (başka bir seçenek onu karşılaştırma koşusu yapmıyorsa karar koşusudur).
- **Risk tabanı.** Planlanan risk hesabın **başlangıç bakiyesinin** yüzdesidir (SPEC §8.1); script'lerin `Risk base`
  girdisinin varsayılanı da budur (`risk_base: Initial Capital`). `config/strategies.yaml`'da başka bir taban
  (`Fixed Balance` + `fixed_bal`, `Account Equity`) yazılıysa aynı yüzde başka bir risktir (sabit 200 000'in %0,25'i
  100 000'lik hesabın %0,5'idir): o strateji için **karar koşusu yapılamaz**. `propbot backtest` (`--pine-risk`
  olmadan) hatayla durur (çıkış kodu 1), `propbot gonogo` da (çıkış kodu 2); `propbot config check` dosyayı GEÇERLİ
  sayar ama bunu bir NOT ile yazar. Betiğin kendi girdileriyle yapılan koşular (`propbot parity`, `--pine-risk`)
  çalışır ve risk satırlarına "bu yüzde hesabın başlangıç bakiyesinin yüzdesi DEĞİLDİR" uyarısını ekler.
- **`risk.yaml` değişirse** eski karar koşuları başka bir hesap için boyutlanmıştır: Go/No-Go onları karşılaştırma
  koşusu sayar. Backtest, işlem listesi ve Monte Carlo yeniden üretilir. 2026-11-19'dan önce %0,5 ile yapılmış
  backtest'ler için de aynısı geçerlidir.

### 4.1 Karar koşusu ve karşılaştırma koşuları (etiket politikası)

- **Karar koşusu:** `pessimistic` dolum, maliyetler açık, maliyet çarpanı tam 1,0, OOS başlangıcı
  `config/backtest.yaml` → `samples.oos_start`, işlem başı risk `config/risk.yaml` → `risk_per_trade_pct` (4.0).
  Etiketi yalnızca strateji (bacak) kimliğidir:
  `backtest_ny_orb_fvg_<zaman>.json`. Go/No-Go G1–G3 ve G4'ün kanıt zinciri **yalnızca** bunu kullanır.
- **Karşılaştırma koşuları** farkı adında taşır: `-tv` (`tv_ohlc`), `-free` (maliyetsiz), `-x1p5` / `-x2` (çarpan),
  `-oos20261020` (config'ten farklı `--oos-start`), `-pinerisk` (betiğin kendi riski; bu sırayla birleşir). Başka bir
  IS/OOS sınırı sonuçlara bakılarak seçilmiş olabilir: G2/G3'ü o sınırla yargılamak seçicilik (cherry-picking)
  olurdu. Böyle bir koşu ne karar verir ne de daha eski bir karar koşusunu gizler. Özetleri "KARAR KOŞUSU DEĞİL
  (Go/No-Go bu koşuyu kullanmaz)" uyarısıyla başlar; maliyetsiz koşu ayrıca kırmızı "MALİYETSİZ KOŞU" taşır.
- **Master portföyü** iki bacağa ayrılır: `master_ny_orb_fvg` ve `master_asian_sweep` (her biri kendi istatistiği ve
  artefaktıyla) ve ayrıca birleşik görünüm `master_portfolio`. Go/No-Go ikisini de kabul eder (2026-11-19 kullanıcı
  kararı): ya iki bacak kimliği ya da birleşik kimlik. Aynı raporda ikisi birden verilemez; bu sınır kararın değil
  uygulamanın getirdiğidir (7. bölüm).
- Go/No-Go bir strateji için **en yeni** olası karar koşusunu kullanır, en iyisini değil. Yeni bir karar koşusu eskisini
  her zaman geçersiz kılar; "beğenmediğim koşuyu sileyim" işe yaramaz ve yapılmamalıdır.

### 4.2 Raporda ne var

- Tüm / IS / OOS için: işlem sayısı, kazanma oranı, ortalama ve medyan R, standart sapma, beklentinin t-istatistiği,
  profit factor, toplam R ve $, ortalama kazanç/kayıp R, maks. düşüş (R ve başlangıç sermayesi + kümülatif K/Z'nin
  zirvesine göre %), en uzun kayıp serisi; OOS/IS beklenti oranı (G3).
- NY saatine göre giriş hafta günü / saat tabloları ve aylık tablo; kümülatif R eğrisi (OOS sınırı işaretli).
- Eksik (veri bittiğinde açık kalan, `end_of_data`) ve piramitleme sınırında düşen girişlerin sayısı; bunlar istatistiğe
  girmez.
- Birim **giriştir**: bir girişin bütün çıkışları (kısmi çıkışlar dahil) toplanır; R = net K/Z (maliyet sonrası) ÷
  (|dolum − ilk stop| × boyut × puan değeri). Pine'ın grafikteki tablosu her `closedtrades` satırını ve brüt fiyat
  mesafesi R'sini sayar; sayılar bu yüzden TradingView tablosundan farklı çıkar (maliyet ve kısmi çıkış kadar).

### 4.3 Maliyet matrisi (`--matrix`)

Aynı koşu maliyetler 1,0 / 1,5 / 2,0 ile çarpılarak üç kez yapılır (`cost_factors`); 1,0 sütunu karar koşusunun
kendisidir. İki artefakt: tüm girişler ve yalnızca OOS (`<strateji>-cost-all`, `<strateji>-cost-oos`). Okuma: kenar
1,5× maliyette sıfırın altına iniyorsa gerçek maliyet tahminindeki küçük bir hata kenarı siler. Yüksek maliyet dolumları
da değiştirebilir (spread bir limiti doldurmayabilir); sütunlar aynı işlemlerin yalnızca daha pahalısı değildir.

### 4.4 Walk-forward (`--walk-forward`)

Tek koşunun girişleri NY takvim günlerine göre kayan pencerelere bölünür: IS 180 gün, OOS 60 gün, adım 60 gün
(`walk_forward`). **Parametre araması yapılmaz**: her pencerede koşunun aynı parametreleri vardır. Rapor: pencere başına
IS/OOS istatistiği, pozitif OOS pencere oranı, birleşik OOS ve verim oranı (OOS ortalamalarının ortalaması ÷ IS
ortalamalarının ortalaması). Veri 240 günden kısaysa pencere oluşmaz ve rapor bunu söyler.

### 4.5 İşlem listesi (`--trades-out`)

Monte Carlo girdisi. Tamamlanmış her giriş bir satır (`entry_time, exit_time, r, strategy_id, mae_r`; R net) ve ilk
satırda **köken satırı** (`#propbot-trades …`: sentetik mi, maliyetsiz mi, dolum modeli, çarpan, veri sha256'sı,
stratejiler, oluşturulma zamanı). Biçim ve kuralları: `docs/monte-carlo.md`. Birden çok strateji tek listeye ancak aynı
veri, aynı dolum modeli, aynı çarpan, aynı enstrüman, aynı maliyet ve aynı işlem başı riskle girer. `--pine-risk` ile
üretilen liste yazılır ama komut "planlanan hesabın işlemleri değildir, Go/No-Go G4 bu listeyle GEÇTİ olamaz" notunu
düşer ve liste işaretlidir: satırlarının `strategy_id`'si ve köken satırındaki kimlikler `-pinerisk` ekini taşır
(`ny_orb_fvg-pinerisk`). G4 işaretli listeyi, satırları karar koşusununkilerle aynı çıksa bile reddeder; G4'e gidecek
liste varsayılan (planlanan riskli) koşudan alınır. Bir bacağında tamamlanmış işlem olmayan
strateji listeye alınmaz ve komut bunu "NOT: … işlem listesine alınmadı" diye yazar; o liste G4'te değerlendirilen
strateji kümesiyle tutmaz. Listeyi elle düzenleme: G4 satır sayısını, R toplamını ve mae_r toplamını karar
koşularıyla (`stats_all.total_r`, `mae_total_r`) karşılaştırır.

## 5. Monte Carlo

```bash
uv run propbot montecarlo --trades reports/trades_mes.csv --venue config/venues/ftmo_2step_100k.yaml --r-is-net
```

Backtest listesindeki R değerleri maliyet sonrasıdır: `--r-is-net` ver, `--cost-r` ekleme (maliyet iki kez düşülür).
G4'ün kabul ettiği koşu: risk `config/risk.yaml` → `risk_per_trade_pct` (planlanan risk) ile aynı **ve listenin
arkasındaki karar koşuları da aynı riskle boyutlanmış** (risk zinciri, 4.0; zincir tutuyorsa G4 kanıtı bunu tek cümlede
yazar), en az 5000 yol (varsayılan 10 000), en az 100 işlem, `--dd-risk-steps` yok, `--intratrade stop` (varsayılan;
`closed` iyimserdir ve G4'ü geçemez), profilde `min_trading_days` dolu (varsayım bayrağı yok), listenin **tamamı**
değiştirilmeden simüle edilmiş. Komut sonucu `reports/` altına `monte_carlo` artefaktı olarak da yazar; artefakt
listenin köken satırını, işlem içi modeli, listenin R ve mae_r toplamlarını ve venue profilinin o anki
`max_loss_type`, `daily_loss_basis` ve `daily_reset` değerlerini taşır. Profil sonradan değişirse eski koşu G4'ü
geçemez: yeniden koş. Köken satırı maliyetsiz bir koşuyu gösteriyorsa rapor "MALİYETSİZ işlem listesi" der ve
`--r-is-net` yok sayılır. Artefaktın venue adı `--venue` dosyasının adıdır (`ftmo_2step_100k.yaml` →
`ftmo_2step_100k`); `gonogo --venue` aynı adla eşleşir. Adında `synthetic` ya da `sentetik` geçen bir işlem dosyası
sentetik sayılır (G4 KALDI). G4 yalnızca **tekrar üretilebilir koşuyla** geçer (2026-11-19): `--seed`, `--max-days`
(1000 piyasa günü) ve `--paths` (10 000; config'te daha sıkı `min_mc_paths` varsa o) varsayılan, `--skip-prob` 0.
Başka değerle yapılan koşu bir "ya şöyle olsaydı"dır: birçok tohum ya da yol sayısı arasından en iyisini seçmek sonucu
seçmektir. FTMO programında süre sınırı olup olmadığı yine resmi sayfadan doğrulanmalı (VERIFY; açık bulgu 78).
Artefakt listenin satırlarının strateji başına özetini (zamanlar dahil, sha256) de taşır; karar koşusunun kendi
satırlarının özetiyle aynı olmalı, yani listede zaman kaydırmak da G4'ü DEĞERLENDİRİLEMEDİ yapar.

**Karar koşusu ayrıca bugünkü ayarlarla boyutlanmış ve seanslanmış olmalı (2026-11-19):** başlangıç sermayesi venue
profilinin `initial_balance`'ı, `qty_step` ve `min_qty` enstrüman profilininki, seans sonu boşluğu
`session.min_gap_minutes`, spread pencerelerinin saat dilimi `session.tz`. Bunlardan birini değiştirip koşup sonra geri
almak, koşuyu karar koşusu olmaktan çıkarır (o strateji DEĞERLENDİRİLEMEDİ olur, daha eski koşuya geçilmez): boyutlar
tam kontrattır ve `min_qty` altı atlanır, seans sonu da stratejilerin giriş kapısını ve zorunlu kapanışını belirler.
Parite raporu da Python tarafının boşluğunu ve sermayesini kaydeder; ikisi de aynı kurala tabidir.

## 6. Parite (TradingView ile)

Amaç: Python portunun aynı barlarda TradingView ile aynı işlemleri ürettiğini göstermek (SPEC §6.6; G6 ≥ %98).

1. TradingView'da script'in **depodaki `pine/` sürümü** (Pine referansı `144abab`; Faz 0 derlemesi bir dosyayı
   değiştirirse Python portu da değişir) Properties'i `docs/tv-verification-checklist.md` 0. bölümündeki
   (Hazırlık) değerlerle, girdileri varsayılanla (ya da `config/strategies.yaml`'daki değerle) çalışsın. **Risk girdisi
   varsayılanda (0,5) kalsın:** parite her zaman betiğin kendi riskiyle koşulur. Sembol MES ise `--instrument MES`.
2. Aynı grafikten iki dosya: bar verisi (2. bölüm) ve Strategy Tester → **List of Trades** dışa aktarımı. Grafiğin
   saat dilimini not et; **UTC** önerilir (yerel saatte yaz saati geçişindeki belirsiz ve olmayan saatler reddedilir).
3. Karşılaştır:
   ```bash
   uv run propbot parity --strategy ny_orb_fvg --data data/tv_mes_1m.csv --tv-trades data/tv_ny_trades.csv --tv-tz UTC \
       --warmup-bars 300
   ```
   Çıkış kodu 0 = geçti, 1 = geçmedi, 2 = hata. Python `tv_ohlc` modeliyle koşar (başka model reddedilir); başlangıç
   sermayesi venue profilinin `initial_balance`'ıdır (TradingView'daki Initial capital ile aynı olmalı: FTMO profili
   ve script varsayılanı 100 000). `--tv-tz`, dışa aktarım anında grafiğin saat dilimidir (IANA adı).
   `--instrument`, `--venue`, `--tf`, `--bar-tz`, `--symbol` backtest'teki gibidir.

**Parite her zaman betiğin kendi işlem başı riskiyle koşulur** (2026-11-19 kullanıcı kararının uygulaması, ADR-0010;
`--pine-risk` gerekmez, seçeneği de yoktur): TradingView betiği kendi girdileriyle çalıştırdığı için Python tarafı da
öyle koşar, `config/risk.yaml`'daki planlanan risk burada kullanılmaz. Rapor bunu ilk satırlarında yazar:
`İşlem başı risk: %0,5 — betiğin kendi değeri (risk_pct); parite TradingView ile betiğin girdilerinde karşılaştırılır …`
(MTF'de `risk_percent`). G6 parite raporunu bu yüzden karar koşularının parametre kümesiyle değil, betiğin kendi
riskindeki kümeyle karşılaştırır.

**Beklenen güvenli sapma (MTF; 2026-11-19 kullanıcı onayı).** `mtf_trend_cont` raporu, farkların ardından ve
hatırlatmalardan önce şu bölümü taşır (artefakt notlarında da aynı cümleler):

```text
Beklenen güvenli sapma (mtf_trend_cont):
- İz göstergesi ısınırken (henüz değer üretmeden) Python portu runner'ın stop'unu başa başta tutar; Pine betiği o pencerede runner'ı stopsuz bırakır (…), bu yüzden o penceredeki dolumlar farklı olabilir.
- Bu farklar yine de uyuşmazlık olarak SAYILIR, hiçbiri sessizce hoş görülmez; ısınmayı karşılaştırmanın dışında tutmanın yolu --warmup-bars seçeneğidir.
```

Anlamı: Python tarafı açık pozisyonu hiçbir zaman stop'suz bırakmaz (değişmez kural 2); prop hesabı için güvenli olan
budur ve port bu yüzden Pine'dan bilerek ayrılır. Bu bir **tolerans değildir**: bölüm hiçbir sayıyı ve hükmü
değiştirmez. Sapma yalnızca iz göstergesi değer üretmeden (grafiğin ilk ~22 barı) TP1/1,5R'ye ulaşan işlemde görülür;
MTF için zaten önerilen `--warmup-bars` (HTF ısınması, doğrulama listesi P14: 5 dk grafikte 600 bar) o pencereyi de
dışarıda bırakır. Dışarıda bırakılan satırlar sayılır ve %2 sınırı geçerlidir. Başka hiçbir stratejide bu bölüm yoktur.

Parite artefaktı Python tarafının etkin parametrelerini, enstrümanını (tür, tick, puan değeri) ve karşılaştırılan
barların veri bilgisini (sha256, bar saat dilimi, kapsam) kaydeder. G6 bunları bugünkü `config/strategies.yaml` ve
`config/backtest.yaml` ile karşılaştırır ve en az 100 karşılaştırılan işlem ister (`go_no_go.min_parity_trades`).
Parite verisinin karar koşusunun dosyası olması gerekmez (TradingView geçmişi kısadır); rapor ikisini de yazar.

Kurallar: bir çıkış dolumu bir satırdır; giriş zamanı, yön ve çıkış nedeni aynı, giriş ve çıkış fiyatı ≤ 1 tick farklı
ise eşleşir. Tolerans 1 tick'ten ve 1 bardan gevşetilemez. Çıkış nedeninde "Signal" sütunu emrin yorumu ya da kimliği
olabilir; ikisi de kabul edilir ve rapor hangisinin tuttuğunu sayar. Isınma (`--warmup-bars`, bar sayısı) ve ortak
pencere dışında kalan satırlar sayılır; herhangi bir listenin %2'sinden fazlası dışarıda kalırsa sonuç **geçemez**.
Python'da veri sonunda açık kalan işlem (`end_of_data`) TradingView'da açık işlemdir; ikisi de karşılaştırma dışıdır ve
sayılır. Her uyuşmazlık hangi alanın farklı olduğuyla listelenir.

Sınırlar: parite yalnızca `tv_ohlc` ile ölçülür; karar raporları `pessimistic` ile üretilir ve TradingView'dan
sistematik olarak daha kötüdür. TradingView emülatörünün belgelenmemiş köşeleri (pivot eşitliği, EMA tohumu, HTF
kovası, kapanışta kayma …) ilk gerçek koşuda farklı çıkabilir: `docs/tv-verification-checklist.md` P bölümü hangi
kontrolün hangi kod anahtarına karşılık geldiğini yazar. `tests/parity/golden/` dosyaları TradingView kanıtı değildir.

## 7. Go/No-Go

```bash
uv run propbot gonogo --strategies ny_orb_fvg,asian_sweep --venue ftmo_2step_100k
```

Çıkış kodu: 0 yalnızca GO · 1 NO_GO · 2 hata. Yazar: `reports/go_no_go_YYYYMMDD.md` + JSON. **Faz 2'de sonuç her
zaman NO_GO'dur** (G5 Faz 3 ister; ekran ve Markdown `Karar: NO-GO` yazar, JSON'daki alan değeri `NO_GO`'dur). Rapor
yine de G1–G4, G6, G7'nin nerede durduğunu söyler.

`--strategies` virgülle ayrılmış strateji kimlikleridir. `--venue` venue profilinin adıdır. Ayrıca `--config-dir`,
`--reports-dir`, `--compliance-dir` (varsayılan `docs/compliance`).

**Master (2026-11-19 kullanıcı kararı; açık bulgu 72 kapandı):** iki yol da kabul edilir. Bir raporda yalnızca biri
verilebilir (bu sınır kararın parçası değil, uygulamanın getirdiğidir; nedeni aşağıda):

```bash
uv run propbot gonogo --strategies master_ny_orb_fvg,master_asian_sweep   # bacaklar üzerinden
uv run propbot gonogo --strategies master_portfolio                       # birleşik koşu üzerinden
```

- Her kimlik kendi artefaktından yargılanır: bacak `backtest_master_ny_orb_fvg_…` / `backtest_master_asian_sweep_…`,
  birleşik `backtest_master_portfolio_…` (üçünü de tek `propbot backtest --strategy master_portfolio` koşusu yazar).
- Birleşik kimliği bir bacağıyla **aynı raporda** verirsen komut reddeder (çıkış kodu 2): aynı işlemler iki karar
  koşusunda sayılır ve G4 işlem listesini onlarla iki kez karşılaştırırdı. Bir bacağı bağımsız portla
  (`master_ny_orb_fvg,ny_orb_fvg`) vermek karışım değildir; onlar ayrı script'lerdir.
- G4: işlem listesinin satırları ve köken satırı her iki yolda da **bacak kimliklerini** taşır. Birleşik koşu
  satırlarının özetini bacak başına kaydeder; G4 listeyi onunla böyle bağlar. Bu alanı taşımayan eski birleşik artefakt
  bağlanamaz (DEĞERLENDİRİLEMEDİ): backtest'i yeniden koş.
- G6: TradingView işlem listesi betik başınadır, yani parite raporu `propbot parity --strategy master_portfolio` ile
  üretilir. Bacak kimliğiyle değerlendirirken G6 betiğin o raporunu okur ve oranın bütün bacakları birlikte kapsadığını
  (bacak başına ayrı oran olmadığını) kanıta yazar.

Rapor `reports/` altındaki **bütün** okunabilir artefaktları (en yeni 300 dosya) okur ve her kapı için kanıtı şöyle
seçer:
- Strateji eşleşmesi artefaktın içindeki `strategy_id` ile yapılır; dosya adı ve etiket kanıt sayılmaz.
- G1–G3: stratejinin **en yeni** karşılaştırma olmayan koşusu. Kayıtlı maliyetleri `config/backtest.yaml`'daki
  enstrüman profiliyle (çarpan 1,0), kayıtlı enstrüman (tick, puan değeri, tür) profille ve kayıtlı parametreler
  `config/strategies.yaml`'ın bugünkü etkin değerleriyle (risk girdisinde planlanan riskle) birebir aynı değilse ya da
  çarpanı kayıtlı değilse strateji DEĞERLENDİRİLEMEDİ; daha eski bir koşuya geçilmez. Kayıtlı OOS başlangıcı
  config'tekinden ya da kayıtlı işlem başı riski planlanan riskten (`config/risk.yaml`) farklı olan koşu karşılaştırma
  koşusudur (karar vermez, eskisini gizlemez); kayıtlı risk okunamıyorsa koşu doğrulanamaz (DEĞERLENDİRİLEMEDİ).
  Sınıflandırma etikete değil kayıtlı değere göredir. Aynı saniyede iki aday varsa hiçbiri seçilmez. Veri
  doğrulaması sha256 ve aynı okumayla (bar saat dilimi, bar süresi, ilk/son bar) bağlanır.
- G4: o venue için en yeni Monte Carlo; köken satırı değerlendirilen stratejilerin karar koşularına bağlanmalı (aynı
  veri sha256'sı, aynı strateji kümesi, işlem sayısı ile R ve mae_r toplamları = karar koşularınınki, liste karar
  koşularından yeni ve Monte Carlo'dan eski, sentetik değil, maliyetsiz değil, `pessimistic`, çarpan 1,0); işlem içi
  model `stop`; kaydedilen `max_loss_type`, `daily_loss_basis`, `daily_reset` bugünkü profille aynı. Risk zinciri:
  karar koşuları, onların listesi ve simülasyon aynı planlanan riskte olmalı (4.0); `-pinerisk` işaretli liste
  (4.5) bu yüzden hiçbir zaman GEÇTİ vermez.
- G6: strateji başına en yeni parite raporu; en az 100 karşılaştırılan işlem, parametreler (betiğin kendi riskinde;
  planlanan riskle yapılmış bir parite raporu DEĞERLENDİRİLEMEDİ) ve enstrüman bugünkü config ile aynı.
- G7: venue profilinin `rules_verified_at` tazeliği (≤ 30 gün) ve `docs/compliance/` dosyaları (göreli yol çalışma
  dizinine göre çözülür; rapor listelediği mutlak yolu yazar, klasör yoksa "klasör yok" der).
- Bu alanları taşımayan eski artefaktlar yüklenir ve panelde görünür; ilgili kapı "eski rapor" cümlesiyle
  DEĞERLENDİRİLEMEDİ olur. Yeniden üret.
- Her artefakt için: sentetikse KALDI, 30 günden eskiyse ya da rapor anından ileri tarihliyse DEĞERLENDİRİLEMEDİ.
- Elle teyit maddeleri raporda yazar: verinin işlem yapılacak mekanın (ya da eşdeğer) beslemesinden geldiği (G1),
  gün/saat filtrelerinin yalnızca IS'te seçildiği (G3), Monte Carlo listesinin maliyet sonrası olduğu (G4), gereken
  yerde firmanın yazılı otomasyon onayı (G7).

SPEC §9 dışındaki eşikler (OOS'ta en az 30 işlem, gerekli her yıl için 1500 bar-saati, en az 5000 Monte Carlo yolu ve
100 işlem, artefakt yaşı en çok 30 gün, paritede en az 100 karşılaştırılan işlem) mühendislik seçimidir; **2026-11-19'da
onayladın, değerler aynen kalır**. `config/backtest.yaml`'da yazar, yalnızca sıkılaştırılabilir.

## 8. Panelde bak

```bash
uv run propbot dashboard
```

`http://127.0.0.1:8787/analiz`: Go/No-Go, Monte Carlo, backtest, maliyet matrisi, walk-forward, parite ve veri doğrulama
kartları (her tür ve etiket için en yenisi). Sentetik kart kırmızı "SENTETİK VERİ" rozeti, maliyetsiz koşu "MALİYETSİZ
KOŞU" bandı taşır. Panel hiçbir hüküm hesaplamaz: GEÇTİ/KALDI yalnızca Go/No-Go artefaktından gelir. Kartlardaki G3/G4
eşik çizgileri SPEC §9 kod tabanlarıdır; `config/backtest.yaml` daha sıkıysa karar raporununki geçerlidir. Backtest
kartının "Koşu türü" satırı koşucunun kendi notunu yazar: `-pinerisk`, `-oos…`, maliyetsiz, çarpanlı ve `tv_ohlc`
koşuları "KARŞILAŞTIRMA KOŞUSU (karar koşusu değil): …" olarak görünür. Satır koşunun yapıldığı andaki durumudur;
bugünkü `config/` değerlerine göre yargı yalnızca Go/No-Go raporundadır. Panelin kurulumu ve güvenliği:
`docs/runbook.md` §13.

## 9. Raporlar nasıl OKUNMAZ

- **Sentetik veri:** "SENTETİK VERİ" yazan hiçbir sonuç bir stratejinin kârlı ya da zararlı olduğunu söylemez. Sentetik
  barlar açılış boşluklarını, haber barlarını ve gerçek spread'i taşımaz. `tests/fixtures/ohlcv/synthetic_*.csv` ve
  `tests/parity/golden/` yalnızca kodun kendisiyle tutarlılığını sabitler.
- **Maliyetsiz koşu:** yalnızca maliyetin ne kadar kenar yediğini görmek içindir; hiçbir karar girdisi olamaz.
- **`tv_ohlc` koşusu:** TradingView'ın iyimser bar içi sırasını taklit eder; parite içindir. Karar `pessimistic`
  koşusundadır (aynı barda stop ve hedef = zarar).
- **Küçük örneklem:** t-istatistiği ve OOS/IS oranı az işlemle anlamsızdır (G2/G3 30 işlemin altında
  DEĞERLENDİRİLEMEDİ). RR 1:2'de +0,2R beklenti için ~220, +0,1R için ~840 işlem gerekir (SPEC §9).
- **Tek koşu:** walk-forward ve maliyet matrisi aynı işlemlerin başka kesitleridir, bağımsız kanıt değildir.
- **Pine tablosuyla kıyas:** R tanımı ve sayma birimi farklıdır (4.2). Parite için `propbot parity` kullan.
- **TradingView'ın %0,5'teki sonuçları:** karar koşuları `config/risk.yaml` riskiyle (%0,25) boyutlanır; TradingView
  betiği kendi %0,5'iyle çalıştırır. İki sayı kümesi karşılaştırılamaz, biri öbürünün yarısı da değildir (4.0).
- **Dar stop:** strateji boyutu maliyetleri yok sayar (Pine `f_qty`); çok dar stop'ta gerçek kayıp bütçeyi aşar
  (CHANGELOG açık bulgu 70). Raporda büyük negatif R'li tek tük işlemler buradan gelebilir.
