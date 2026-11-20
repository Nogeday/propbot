# ADR-0008 — Backtest motoru: Pine benzeri emir modeli, dolum ve maliyet modelleri, bağımlılıklar

Durum: kabul edildi · 2026-11-18 · Karar: Oğuz'un 2026-11-18 Faz 2 ilkeleri çerçevesinde · İlgili: SPEC §2.2, §2.3, §3,
§6.1–6.4, §6.6; değişmez kurallar 2, 4, 9; ADR-0000, ADR-0007

## Bağlam

Altı Pine stratejisi Python'a taşınacak ve TradingView ile karşılaştırılabilir kalacak (öncelik 2: parite). Script'ler
TradingView'ın emir çağrılarını kullanır: piyasa ve limit girişi (`strategy.entry`), her barda yeniden yazılan stop/limit
parantezleri ve yüzdeli kısmi çıkışlar (`strategy.exit`), iptal (`strategy.cancel`), bar kapanışında kapatma
(`strategy.close`, `strategy.close_all(immediately = true)`). Aynı strateji kodu Faz 3'te canlı beslemede de çalışacak.

## Seçenekler

**Strateji ↔ motor arayüzü**
- A. Vektörel backtest (pandas/numpy sütun işlemleri). Hızlı; ama durum makineleri (FVG kuyrukları, silahlanma,
  bar içi sıra) vektörleşmez ve canlıda aynı kod çalışmaz.
- B. Strateji "sinyal" döndürür, motor parantezi kendisi kurar. Sade; ama MTF'nin üç çıkış emri, Silver Bullet'in
  limit girişi + iptali ve Master'ın bacak bazlı kapanışı için motora strateji bilgisi sızar.
- C. **Pine benzeri eylem modeli:** strateji her kapanan barda saf bir fonksiyonla `Entry / Exit / Cancel / Close /
  CloseAll` eylemleri döndürür; motor TradingView broker emülatörünün emir yaşam döngüsünü uygular.

**Dolumda yeniden fiyatlama** (SPEC §3.1: stop/TP gerçek doluma göre): (a) yalnızca bar kapanışında, Pine gibi;
(b) **dolum anında bir `on_fill` kancası**, parite koşusunda kapalı.

**Bağımlılıklar:** pandas, pyarrow (Parquet), exchange_calendars eklemek ya da eklememek.

## Karar

1. **Arayüz C** (`src/propbot/strategies/base.py`). Strateji saat, dosya, ağ okumaz; hesap durumunu salt-okunur
   `StrategyContext` ile alır (Pine `strategy.position_size`, `opentrades`, `closedtrades`, `equity` karşılıkları).
   Emir yaşam döngüsü o dosyanın başında yazılıdır ve motorun testleriyle sabitlenir. Özet:
   - Bar N'de dönen eylemler bar N kapanışından sonra geçerlidir. `Close`/`CloseAll` bar N kapanışında dolar; piyasa
     girişi bar N+1 açılışında; limit girişi fiyat seviyeyi `limit_through_ticks` (1) tick **geçince**.
   - Aynı kimlikle yeniden dönen `Entry`/`Exit` çalışan emri günceller. `Exit`, `from_entry` girişine bağlıdır: giriş
     dolunca (dolum barının içinde de) etkinleşir, giriş tamamen kapanınca ya da giriş emri kalkınca silinir;
     tetiklenince `min(qty_percent × dolan giriş, girişten kalan)` kadar kapatır.
   - Ters yönlü giriş (pozisyon çevirme) desteklenmez: hiçbir script kullanmıyor; motor hata verir.
2. **Değişmez kural 2 backtest'te de zorunlu:** stop'u olan bir `Exit`'i bulunmayan giriş dolarsa motor hata verir;
   girişin ilk stop'u yürürlüğe girdikten sonra stop'u zarar yönüne taşıyan bir güncelleme de hatadır (strateji
   hatası olarak koşuyu durdurur). Martingale/grid/ortalama düşürme (kural 4) zaten ters yön ve pyramiding
   sınırıyla dışarıda kalır.
3. **İki dolum modeli** (`FillModel`):
   - `pessimistic` (varsayılan): pozisyon açıkken bar önce aleyhte uca yürünür (long: O→L→H→C), yani aynı barda stop ve
     hedefe değen bar **zarardır**. Bar düz başlıyor ve bekleyen limit giriş varsa sıra tersine döner: hedefe doğru
     hareket girişten **önce** sayılır (giriş barında hedef ancak kapanışa giden son bacakta dolabilir). Giriş dolduğu
     anda `on_fill` çağrılır; strateji stop/TP'yi gerçek dolum fiyatından kurar (SPEC §3.1).
   - `tv_ohlc` (yalnızca parite): TradingView emülatörü — açılış yükseğe daha yakınsa O→H→L→C, değilse O→L→H→C;
     `on_fill` çağrılmaz, strateji dolum barının kapanışında yeniden fiyatlar (Pine'ın yaptığı). Böylece SPEC §3.1'deki
     "bilinen küçük TP farkı" parite koşusunda oluşmaz.
   - Boşluk: açılış seviyeyi aşmışsa stop açılıştan (kötü fiyat), limit açılıştan (iyi fiyat) dolar.
4. **Maliyet modeli** (`CostModel`, değerler `config/backtest.yaml`): taraf başı komisyon, piyasa/stop dolumlarında
   aleyhte slipaj (limit dolumunda yok), CFD için alış–satış farkı. CFD barları **bid** kabul edilir: her alım
   (long girişi, short çıkışı, short stop'u) bid + spread'den, her satış bid'den işlem görür; alım yönlü emirlerin
   tetiklenmesi de ask çizgisine göre değerlendirilir (short stop'u spread kadar erken vurulur, alım limiti spread
   kadar geç dolar). Spread sabit ya da NY saatine göre pencere tablosudur (açılışta genişleme). Maliyet duyarlılık
   matrisi `CostModel.scaled(1.0 / 1.5 / 2.0)` ile aynı koşuyu üç kez yapar. Maliyetsiz koşu raporda kırmızı uyarı taşır.
5. **Kayıt birimi:** `TradeRecord` bir çıkış dolumudur (TradingView "List of Trades" satırı; kısmi çıkış ayrı satır).
   İstatistik, Monte Carlo ve kapılar **giriş** bazındadır (`group_by_entry`): R = net K/Z ÷ (|dolum − ilk stop| ×
   boyut × puan değeri). "İlk stop": dolumdan sonraki ilk strateji çağrısından (`on_fill` ya da `tv_ohlc`'de dolum
   barının `on_bar`'ı) sonra yürürlükteki stop; işlem o çağrıdan önce kapandıysa dolum anındaki stop. `mae_r` işlem
   açıkken görülen en kötü yüzen sonuçtur (Monte Carlo'nun işlem içi modeli bunu kullanır, ADR-0002).
6. **Seans sonu veriden türetilir.** Pine `session.islastbar` karşılığı, dosyadaki bir sonraki bara olan boşluktan
   (varsayılan eşik 30 dk; `config/backtest.yaml`) hesaplanır; serinin son barı da seans sonudur ve açık kalan işlem
   `end_of_data` nedeniyle kapanıp istatistik dışı bırakılır. `exchange_calendars` **eklenmedi**: backtest'te takvim
   verinin kendisidir (yarım günler ve tatiller dosyada nasılsa öyle), ve kütüphane pandas'ı da getirir. Canlı besleme
   (Faz 3) bir sonraki barı göremeyeceği için takvim kararı o faza kalır.
7. **Bağımlılık eklenmedi.** Yükleyici ve motor standart kütüphane + mevcut numpy ile yazıldı (CLAUDE.md: gereksiz
   bağımlılık yok). **Parquet ertelendi:** okumak için `pyarrow` gerekir; MT5 ve TradingView dışa aktarımları CSV'dir.
   İhtiyaç doğarsa ayrı bir kararla eklenir. `pandas` da eklenmedi.
8. **Göstergeler Pine tanımlarıyla** (`strategies/indicators.py`): `ta.ema` ilk değerle, `ta.rma` ilk `length`
   değerin SMA'sıyla tohumlanır; `ta.atr = rma(tr)`; `ta.stdev` kitle (biased) sapmasıdır. `request.security` (MTF,
   60 dk) bar açılış saatine göre saatlik kovalara toplanır ve yalnızca **kapanmış** üst bar okunur. Tanımlar Pine v5
   başvuru kılavuzundaki formüllerdir; bu ortamdan TradingView'a erişilemediği için sayısal eşleşme gerçek parite
   koşusunda doğrulanacak (VERIFY, ADR-0007).
9. **Optimizasyon yok.** Parametreler Pine girdi varsayılanlarıdır (`config/strategies.yaml` yalnızca açıkça
   değiştirileni taşır; testler varsayılanları Pine kaynağıyla karşılaştırır). Walk-forward, tek bir koşunun işlem
   listesini kayan IS/OOS pencerelerine böler; parametre araması yapmaz (SPEC §6.4: varsayılan kapalı).

## Sonuçlar

- Portlar Pine ile satır satır karşılaştırılabilir; strateji başına tek kod yolu hem backtest'te hem (Faz 3'te) canlıda
  çalışır. Bedeli: motor, TradingView emülatörünün davranışını yeniden uygular ve belgelenmemiş köşeleri VERIFY kalır.
- Saf Python bar döngüsü vektörel koddan yavaştır (2 yıllık 1 dk veri ≈ 700 bin bar, strateji başına saniyeler–on
  saniyeler). Maliyet matrisi üç koşu demektir; kabul edilebilir.
- Kötümser dolum, TradingView sonuçlarından sistematik olarak **daha kötü** sonuç verir; bu bilerek böyledir. Parite
  yalnızca `tv_ohlc` ile ölçülür; karar raporları (G2–G4) `pessimistic` ile üretilir.
- CFD bid/ask modeli spread'i fiyata gömer; ayrıca "spread maliyeti" satırı yoktur. FTMO `US500.cash` için spread,
  komisyon, tick ve puan değeri `config/backtest.yaml`'da **boş/VERIFY** durur (terminalden okunacak); doldurulmadan
  o enstrümanla backtest çalışmaz. Dosyadaki dolu profil Pine başlığındaki MES varsayımlarıdır.
- Erken kapanış günlerinde seans sonu, veride o günün son barı neyse odur. TradingView'ın o günlerde
  `session.islastbar`'ı nasıl verdiği doğrulanmadı (VERIFY).

## Uygulama notları (2026-11-18)

Karar metni değişmedi; uygulanan motor (`src/propbot/backtest/engine.py`) ve göstergeler şu noktalarda ayrıntılanır
ya da ayrılır. TradingView karşılığı doğrulanmamış olanlar `docs/tv-verification-checklist.md` P bölümündedir.

- **Pyramiding:** aynı yönde en çok `max(1, pyramiding)` giriş (Pine başvuru kılavuzundaki tanım; 0 ve 1 = bir giriş,
  Master 2). İlk sürüm `1 + pyramiding` kullanıyordu; Master denetiminde düzeltildi (`b0bc221`). VERIFY (P21).
- **Açılış sırası:** açılışta önce zaten açık pozisyonun boşlukla geçilen çıkışları, sonra piyasa girişleri, sonra
  onların çıkışları dolar (madde 1 bu sırayı belirtmez; ilk taslak girişleri önce dolduruyordu, denetimde güvenli
  okumaya çevrildi; hepsi açılış fiyatından dolduğu için yalnızca pyramiding sayımını etkileyebilir).
- **Limit:** açılış seviyeyi 1 tick'ten **az** geçmişse açılışta dolum yok; bar seviyeyi tam 1 tick geçince limit
  fiyatından dolar (P15).
- **Kapanış dolumlarında slipaj:** `Close`, `CloseAll` ve `end_of_data` dolumları da piyasa emri gibi slipaj öder
  (madde 4 yalnızca piyasa/stop der). Pine'ın aynı şeyi yapıp yapmadığı VERIFY (P17). Bu dolumların spread'i barın
  açılış dakikasının penceresinden alınır.
- **Değişmez kural 2'nin denetimi:** dolumda girişin tamamı stop taşıyan çıkışlarla kapsanmalıdır (`qty_percent`
  toplamı ≥ açık miktar); denetim açık miktar olan her barın sonunda çalışır. Kapanan girişin çıkışları hemen silinir;
  aynı kimlikle yeniden açılan giriş taze `Exit` olmadan dolarsa koşu durur.
- **"İlk stop" (`tv_ohlc`):** dolum barının `on_bar`'ı stop'u girişin ötesine taşırsa (MTF'de dolum barında TP1 ya da
  1,5R) o stop ilk stop sayılır ve R bir tick'lik riskle şişer (açık bulgu 77). Karar koşuları (`pessimistic`)
  etkilenmez.
- **Kayan nokta eşitliği:** TradingView yön kuralında ve aynı fiyattaki sıralamada eşitlik bir epsilon içinde
  değerlendirilir (0,1'lik ızgarada `4096.1` gibi değerlerde yanlış sıra denetimde bulundu ve düzeltildi).
- **Göstergeler:** sonlu olmayan girdi (NaN, inf) Pine'ın na'sı sayılmaz, hata verir. `ta.pivothigh/low` eşitlik kuralı
  bir parametredir (`ties`, varsayılan `"strict"`; P7). Üst zaman dilimi kovası UTC'de `floor(ts / htf)`; seans
  başlangıcına hizalama parametresi yoktur (varsayılan 60 dk'da TradingView'la aynı olması beklenir, 240 ya da 90'da
  farklı olabilir; ikisi de VERIFY, P12–P13). Python'un HTF geçmişi grafiğin ilk
  barında başlar (açık bulgu 71).
- **Boyut:** portlar Pine'ın boyutunu enstrümanın lot adımına aşağı yuvarlar (`Instrument.floor_qty`) ve 0 ise atlar;
  Pine'ın "Fractional" modunda küsuratlı boyutla işlem açtığı yerde Python daha küçük boyut kullanabilir (SPEC §3.1).
  Pine `f_qty` maliyetleri yok sayar; çok dar stop'ta gerçek kayıp risk bütçesini aşar (açık bulgu 70).
- **Seans sonu ve veri delikleri:** ≥ 30 dk'lık bir veri deliği seans sonu sayılır (doğrulama raporunda kısa seans
  görünür); takvim kütüphanesi yine eklenmedi. Canlı takvim kararı Faz 3'tedir (açık bulgu 75).
- **Bağımlılık:** pandas, pyarrow ve exchange_calendars eklenmedi (madde 6–7 aynen); 2026-11-18'de kullanıcı onayına
  bırakılan Faz 2 kararları arasındaydı, 2026-11-19'da onaylandı (sonraki madde; `CHANGELOG.md`).
- **Kullanıcı onayı (2026-11-19):** madde 6–7 onaylandı: yeni bağımlılık yok (pandas, pyarrow, exchange_calendars
  eklenmedi), Parquet içe aktarma ertelendi. MTF ısınmasında portun başa baş stop tutması (madde 2'nin sonucu; SPEC
  §3.1) "beklenen güvenli sapma" olarak onaylandı: port değişmez, parite raporu ve artefaktı bunu o stratejide yazar
  (`backtest/parity.py` → `EXPECTED_DEVIATIONS`), fark yine uyuşmazlık sayılır (tolerans değil).
- **İşlem başı risk (2026-11-19, ADR-0010):** madde 9'daki "parametreler Pine girdi varsayılanlarıdır" cümlesi karar
  koşularında risk girdisi dışında geçerlidir: karar koşusu `config/risk.yaml` riskini betiğin risk girdisine yazar,
  parite koşusu (`tv_ohlc`) betiğin kendi riskiyle kalır. `config/strategies.yaml` risk girdisini taşıyamaz. Backtest
  artefaktı işlem listesi satırlarının özetini satır kimliği başına da kaydeder (`trades_digests`; Master'ın birleşik
  koşusunu G4'te işlem listesine bağlar).
