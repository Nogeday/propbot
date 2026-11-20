# TradingView doğrulama listesi — Faz 0 (Faz 2 ekleri: P ve M bölümleri)

Claude Code Pine'ı derleyemez ve çalıştıramaz; aşağıdakileri TradingView'da sen yaparsın ve sonuçları Claude Code
oturumuna yapıştırırsın. Sıra: **Master → NY → Asian → Silver Bullet → Mean Reversion → MTF**.
Her dosya için A–G adımlarını tamamla, sonra bir sonrakine geç.

> Bu sürüm B-maddelerine (hata düzeltmeleri) ek olarak E-maddelerini (kural ve varsayılan değişiklikleri, SPEC §4.3)
> içerir. Bu yüzden orijinal ↔ v2 farkı yalnızca hata düzeltmelerinden gelmez; C bölümündeki tablo "aynı çıkmalı" testi
> değil, referans kaydıdır.

## 0. Hazırlık (bir kez)
- [ ] Sembol: altı dosyada da **`CME_MINI:MES1!`**. Orijinal ve v2 için aynı zaman dilimi ve tarih aralığı; aşağıya yaz.
- [ ] Asian ve Master S2, 20:00–00:00 ET verisi ister → bu ikisinde grafikte "Extended trading hours" **açık** olmalı.
- [ ] Grafik saat dilimini `(UTC-5) New York` yap (script zaten `America/New_York` kullanıyor; kontrol kolaylaşır).
- [ ] **v2 script'lerinde Properties'e dokunma** — kod varsayılanları test standardıdır. Yapıştırdıktan sonra yalnızca
      aşağıdaki değerleri gördüğünü doğrula:

      | Properties | v2 varsayılanı |
      |---|---|
      | Initial capital | 100000 |
      | Commission | 0.5 — USD per contract (taraf başı → gidiş-dönüş $1,00) |
      | Slippage | 1 tick |
      | Margin for long / short positions | 0 % |
      | Verify price for limit orders | 1 tick (altı dosyada da) |
      | Recalculate: After order is filled / On every tick | ikisi de işaretsiz |
      | Fill orders: On bar close | işaretsiz |

- [ ] **Orijinal script'lerde** aynı ayarları elle gir (orijinal varsayılanları farklı: 10000, yüzde komisyon vb.).
      Marjin %100 kalırsa TradingView pozisyonun tüm notional'ını teminat ister; MES'te risk-temelli boyut bunu aşar →
      işlemler açılmayabilir ya da marjin çağrısıyla kapanabilir. Bu davranış doğrulanmadı; ne gördüğünü yaz.

| Dosya | Sembol | TF | Tarih aralığı | Seans (RTH / ETH) |
|---|---|---|---|---|
| master | CME_MINI:MES1! | 1m | | ETH |
| ny_open_fvg | CME_MINI:MES1! | 1m | | |
| asian_sweep | CME_MINI:MES1! | 1m | | ETH |
| ict_silver_bullet | CME_MINI:MES1! | 1m veya 5m | | |
| mean_reversion | CME_MINI:MES1! | 5m–15m | | |
| mtf_trend | CME_MINI:MES1! | 5m | | |

## A. Derleme ve kayıt
- [ ] Pine Editor → **yeni** script → `.pine` dosyasının tamamını yapıştır → **"… v2"** adıyla kaydet.
      Orijinalin üzerine yazma.
- [ ] "Add to chart" — derleme hatası varsa **mesajı ve satır numarasını** olduğu gibi kopyala.
- [ ] Uyarı (sarı) mesajları varsa onları da kopyala.
- [ ] **Orijinal** MTF'de "Undeclared identifier" (`riskPts`) hatası çıkıyorsa yaz; o zaman orijinal sütunu boş kalır.

## B. Varsayılanlar (v2)
- [ ] Properties tablosu (0. Hazırlık) tutuyor. Tutmayan değer varsa yaz.
- [ ] Strategy Tester işlem üretiyor (marjin %0 ile emirler reddedilmiyor, "Margin call" çıkışı yok).
- [ ] Inputs: `Risk base = Initial Capital`; risk `0.5` (parite dışa aktarımları da bu varsayılanla alınır, P35; botun
      karar koşuları ve canlı çalışma `config/risk.yaml`'daki %0,25'i kullanır, ADR-0010); günlük zarar kesicisi `2`;
      `Max trades per day = 1` (Silver Bullet, Mean Reversion, MTF); `Min trail step (ticks) = 4` (NY, Master S1, MTF);
      `Flatten open trade on breach` işaretli (NY, Asian, Master, Silver Bullet).
- [ ] Altı dosyada da "Verify price for limit orders" = **1 tick**; "Recalculate" kutuları ve "On bar close" **işaretsiz**.
      Bu kutulardan biri işaretliyse gerçek zamanlı davranış backtest'ten ayrışır (denetim D3).

## C. Önce/sonra tablosu (Strategy Tester → Overview / Performance Summary)
Orijinal ve v2 için aynı sembol, zaman dilimi, tarih aralığı ve Properties ile doldur. "Tot R" script'in kendi
tablosundan (MTF'de tablo farklı: "Full trades", "Avg runner R"; Silver Bullet'ta panodaki "Tot R").

| Dosya | Sürüm | İşlem sayısı | Kazanma % | Net kâr | Profit factor | Maks DD | Toplam R |
|---|---|---|---|---|---|---|---|
| | orijinal | | | | | | |
| | v2 | | | | | | |

- [ ] **Maliyetli ve maliyetsiz fark:** v2'yi bir kez de Commission = 0 ve Slippage = 0 ile çalıştır, net kâr ve PF'yi
      not et (yalnızca karşılaştırma için), sonra varsayılana dön.
- [ ] **2× maliyet koşusu:** Commission = 1.0 ve Slippage = 2 ile bir koşu; net kâr ve PF'yi not et, sonra varsayılana
      dön. Strateji 2× maliyette kâr bırakmıyorsa bunu açıkça yaz (denetim D7).
- [ ] (Premium varsa) **Bar Magnifier** açık bir koşu: işlem sayısı ve net kâr farkını not et — aynı barda stop + hedef
      varsayımının etkisini gösterir (denetim D6).

## D. Grafik ve işlem listesi (B-maddeleri)
- [ ] Pozisyon açıkken grafikte stop çizgisi (NY/Master S1: turuncu "Stop / Trail"; Asian/Master S2/MR/SB: stop
      çizgisi) ve yeşil TP çizgisi görünüyor.
- [ ] **List of Trades**'te SL ve TP çıkışları var (Exit L / Exit S / S1 ExitL / TP1 / Init / Run …), yalnızca
      "EOD close" / "Session end" değil. Özellikle NY ve Asian'da orijinalde neredeyse hepsi seans sonu kapanışıydı.
- [ ] Tablodaki "Tot R" 0 değil; tek tek işlemlerde R ≈ +rr ya da ≈ −1 (trailing, seans sonu ve slipaj hariç).
- [ ] Rastgele 3 işlemi aç: dolum barında (girişin dolduğu ilk bar) fiyat stop'a değdiyse işlem **o barda** stop'tan
      kapanmış olmalı (önceden dolum barı korumasızdı). Bulduğun bir örneğin tarih/saatini yaz.
- [ ] Silver Bullet: dolmayan bir limit girişte "Exit L/S" emri sonradan tek başına tetiklenmiyor.
- [ ] MTF: TP1 ile kısmi çıkış + "Run"/"RunB" runner çıkışları List of Trades'te görünüyor.

## E. Takvim / zaman
- [ ] **Yarım gün:** grafik geçmişinde 2025-11-28 veya 2025-12-24 varsa o gün pozisyonun seans sonrasına
      taşınmadığını kontrol et (ilgili işlemin çıkış saati ≤ seans kapanışı). İleride: 2026-11-27.
      Geçmiş yetmiyorsa "test edilemedi" yaz.
- [ ] **Yalnız-RTH grafik** (NY/Master/MR/MTF/SB): grafikte "Extended trading hours" kapalıyken seans sonu
      kapanışı aynı günün son barında oluyor, ertesi günün açılışında değil; son barda yeni giriş yok.
- [ ] **ABD kış saati (2026-11-01 sonrası):** 2026-11-02 ve sonrasında OR 09:30 ET'de (17:30 TR) kuruluyor
      (NY ve Master S1'de OR High/Low çizgileri 09:35'ten itibaren görünür).
- [ ] MTF: 1H EMA çizgileri bir 1H bar gecikmeli; gerçek zamanlı barda değer bar kapanmadan değişmiyor
      (canlı grafikte bir 1H bar boyunca izle).

## F. Alarm yükü (B7)
- [ ] Inputs → "Webhook token" alanına geçici bir değer yaz (ör. `test123`; gerçek token Faz 1'de üretilecek).
- [ ] Alarm oluştur: koşul = bu strateji, **"alert() function calls only"**. Webhook URL'si şimdilik boş kalabilir
      (Faz 1'de alıcı kurulacak).
- [ ] İlk alarm geldiğinde Alert Log'daki mesajı kopyala ve şunları kontrol et:
  - geçerli JSON (yerelde kontrol: mesajı bir dosyaya kaydet → `python3 -m json.tool dosya.json`; mesajı
    paylaşırken token'ı `***` yap),
  - `bar_time` 13 haneli tam sayı (bilimsel gösterim yok),
  - fiyatlarda binlik ayıracı yok, ondalık ayıracı nokta,
  - `exit` mesajında `"stop":null,"tp":null`.
- [ ] Aynı barda iki farklı alert gerekiyorsa (ör. Master'da iki bacağın kapanışı, ya da `stop_update` + `exit`) ikisi de
      geliyor mu? Gelmiyorsa hangisinin geldiğini yaz.
- [ ] Girdi değiştirince alarmı yeniden kurman gerektiğini not et (alarm, oluşturulduğu andaki girdilerle çalışır).
- [ ] `"symbol"` alanı düz `BORSA:SEMBOL` biçiminde mi (ör. `CME_MINI:MES1!`)? Grafikte uzatılmış seans ya da fiyat
      ayarlaması açıkken TradingView sembolü `={"session":…}` gibi bir yapıyla verebilir; bu, JSON'u bozar ve alıcı
      alert'i reddeder (400). Böyle görürsen mesajı (token maskeli) yapıştır.
- [ ] `"tf"` alanı gün içi bir değer mi (`1`, `5`, `15`, `60` …)? Alıcı günlük ve üstü zaman dilimlerini reddeder.
- [ ] Her strateji için **en az bir gerçek alert gövdesini** (giriş; varsa stop güncellemesi, çıkış, iptal) token'ı `***`
      yaparak sakla: alıcının şeması Pine kaynağından çıkarıldı, sayı biçimleri (ör. `5120.00`, `"rr":2`) gerçek
      çıktıyla doğrulanmadı.

## G. Genişletme kontrolleri (E-maddeleri)
- [ ] **E1 Master yeniden fiyatlama:** List of Trades'ten stop'la kapanan bir S1 ve bir S2 işlemi seç: FVG Buffer
      yönteminde çıkış fiyatı grafikteki stop çizgisinde; TP ile kapanan işlemde (çıkış − giriş) ≈ rr × (giriş − stop).
      Aynı sembol ve tarih aralığında tekil NY v2 ve Asian v2 ile Master'ın S1/S2 işlemlerini karşılaştır: giriş zamanı
      ve çıkış fiyatı aynı olmalı (iki bacağın çakıştığı ya da ters yön engelinin devreye girdiği günler hariç).
      Farklı çıkan işlemlerin tarihini yaz.
- [ ] **E2 Risk tabanı:** kârlı bir dönemden sonraki bir işlemde `miktar × stop mesafesi (puan) × 5 $` ≤ 500 $ ve
      500 $'a yakın (100000 × %0,5; tam kontrata aşağı yuvarlama). `Risk base = Account Equity` yapınca miktar
      özsermayeyle birlikte değişiyor; sonra `Initial Capital`'a geri al.
- [ ] **E4 Günde 1:** List of Trades'te aynı NY gününde aynı stratejiden ikinci giriş yok (Master'da motor başına 1).
- [ ] **E5 Son bar:** giriş (dolum) saatlerini süz — NY / Master S1: 15:58'den sonra giriş yok; Asian / Master S2:
      10:59'dan sonra giriş yok; Mean Reversion (5m): 15:40'tan sonra giriş yok; MTF: 15:50'den sonra giriş yok;
      Silver Bullet: 11:00 ve 15:00'te ya da sonrasında (pencere dışında) dolum yok.
- [ ] **E6 Günlük zarar kapanışı:** denemek için günlük zarar girdisini geçici olarak `0.1` yap → zarardaki işlem
      "Daily DD halt" / "Daily loss halt" yorumuyla **ihlal barının kapanışında** kapanıyor, kırmızı
      "TRADING HALTED" etiketi çıkıyor (NY, Asian, Master). Sonra girdiyi `2`'ye geri al.
- [ ] **E7 Trail adımı:** NY'de trailing ilerlerken turuncu stop çizgisi basamaklı ilerliyor (her adım ≥ 4 tick = 1 puan).
      `Min trail step = 1` yapınca daha sık ilerliyor; sonra `4`'e geri al.
- [ ] **E8 Tablolar:** MTF ve Silver Bullet'ta `Table scope` = In-Sample / Out-of-Sample → rakamlar değişiyor ve
      IS + OOS işlem sayısı = All. Silver Bullet panosunda "Tot R" satırı var.

## H. Heartbeat göstergesi ve alıcı (Faz 1 — `docs/runbook.md` ile birlikte)
- [ ] `pine/heartbeat_indicator.pine` dosyasını yeni bir **gösterge** olarak yapıştır, derle. Hata/uyarı varsa kopyala.
- [ ] Stratejilerle aynı sembolün **1 dakikalık** grafiğine ekle; her saatin **son** barı (saat başında KAPANAN bar:
      1 dakikalık grafikte HH:59 barı, New York saatiyle) hafif renkleniyor mu? Saat başını izleyen bar (HH:00)
      olağan saatlerde renklenmemeli. **Beklenen istisna:** barlarda boşluktan sonraki ilk bar renklenir ve atımı bir
      bar geç gönderir — seans arasından sonra (CME'de 18:00 New York barı), hafta açılışında ve HH:59 barı hiç
      oluşmadıysa.
- [ ] Günlük grafikte "use an intraday chart of 60 minutes or less" çalışma zamanı hatası veriyor mu (vermeli)?
- [ ] 45 dakikalık grafikte "the chart's bar length must divide the heartbeat interval" hatası veriyor mu (vermeli)?
      5 ve 15 dakikalık grafiklerde hata vermemeli.
- [ ] "Webhook token" girdisi `display = display.none` ile tanımlı (doğrulanamadı: resmi belgeye bu oturumdan
      erişilemedi). Derleyici `display` parametresini reddederse hata metnini kopyala. Derlendiyse: token'ı yazdıktan
      sonra grafikte gösterge adının yanındaki girdi değerleri arasında token **görünmemeli** — görünüyor mu?
- [ ] Alarm oluştur: Condition = gösterge → **"Any alert() function call"**; Webhook URL = alıcı adresi. Seçenek adı
      farklıysa ne gördüğünü yaz.
- [ ] Alarmın Notifications sekmesinde **yalnızca Webhook URL** işaretli mi (mesaj token'ı içerir)? Aynısını altı
      strateji alarmında da kontrol et.
- [ ] İlk saat başında Alert Log'da şu biçimde bir mesaj görünüyor mu:
      `{"v":1,"token":"…","strategy_id":"heartbeat","symbol":"CME_MINI:MES1!","tf":"1","bar_time":…,"action":"heartbeat"}`
- [ ] Bot çalışıyorsa Telegram'da `/saglik` → "son heartbeat" birkaç saniye/dakika önce görünüyor mu?
- [ ] **Veri gerçek zamanlı mı?** Alert Log'daki tetiklenme saati, o barın kapanışından yalnızca birkaç saniye sonra
      olmalı. Dakikalarca (ör. 10 dk) sonraysa sembolün verisi gecikmelidir: bot her girişi "[BAYAT SİNYAL]" sayar
      (`docs/runbook.md` 1. bölüm). Gördüğün farkı yaz.
- [ ] **Bir `strategy_id` tek kaynaktan:** aynı script aynı sembolde iki grafikte alarmlı DEĞİL. Master ile bağımsız
      NY Open / Asian Sweep script'leri yan yana alarmlanabilir (ayrı kimlikler, aşağıdaki madde).
- [ ] **E14 Master bacak kimlikleri (kullanıcı kararı 47):** Master'ın Alert Log'daki mesajlarında `strategy_id`
      S1 için `"master_ny_orb_fvg"`, S2 için `"master_asian_sweep"` yazıyor mu? Bağımsız NY Open `"ny_orb_fvg"`,
      bağımsız Asian Sweep `"asian_sweep"` göndermeye devam etmeli. Master alarmını bu sürümden önce kurduysan sil ve
      yeniden kur. Birer örnek gövdeyi (token maskeli) yapıştır.
- [ ] **E13 Token girdisi gizli (kullanıcı kararı 45):** altı strateji dosyasının her birinde "Webhook token" girdisi
      `display = display.none` ile tanımlı (doğrulanamadı: Pine burada derlenemiyor). Her dosya için: (1) derleniyor
      mu — derleyici `display` argümanını reddederse hata metnini ve satırı kopyala; (2) token'ı Inputs'a yazdıktan
      sonra grafikte script adının yanındaki durum satırında token **görünmüyor** mu; (3) öbür girdiler eskisi gibi
      görünüyor mu (yalnızca token gizlenmeli). Dosya dosya geçti / kaldı yaz.
- [ ] **Sembol listesi (kullanıcı kararı 46):** alarm kurduğun her grafiğin Alert Log'daki `"symbol"` değeri,
      `config/venues/ftmo_2step_100k.yaml` → `signal.tv_symbols` listesindeki yazımla **harfi harfine** aynı mı
      (ör. `CME_MINI:MES1!`)? `US500.cash` grafiğinde TradingView'ın tam olarak ne gönderdiğini (önek var mı:
      `FTMO:US500.cash` gibi) buraya yaz; farklıysa liste ona göre düzeltilecek.
- [ ] **Ölçek (kullanıcı kararı 46):** listedeki sembollerden kullandıklarının fiyatını aynı anda MT5'teki
      `US500.cash` fiyatıyla karşılaştır ve farkı puan olarak yaz (vadeli sözleşmede birkaç puanlık baz farkı olağan;
      fiyatın 10 katı / onda biri olması ölçeğin farklı olduğunu gösterir → o sembol listeden çıkarılır).
- [ ] Alarmın bitiş tarihini `config/tv_alerts.yaml` → `alerts` altına yazdın mı?

## P. Parite ve emülatör doğrulamaları (Faz 2)

Faz 2'nin Python portları ve backtest motoru (`docs/backtest.md`) TradingView'ın belgelenmemiş davranışlarında bazı
varsayımlar yaptı; bunlar bu ortamdan doğrulanamadı (VERIFY; ayrıntı `CHANGELOG.md` açık bulgular 65–69). Her maddeyi
**numarasıyla** raporla: "P7: strict", "P15: dolmadı" gibi. Kodda anahtarı olan maddelerde anahtar yazılıdır; olmayanlarda
fark çıkarsa düzeltmeyi Claude Code yapar (dosya adı verildi). Ölçümleri Faz 0 hazırlığındaki Properties ile
(0. bölüm) `CME_MINI:MES1!` grafiğinde yap; grafiğin saat dilimini not et. Kontroller için geçici bir **gösterge**
script'i kullan (strateji dosyalarına dokunma).

**Göstergeler** (`src/propbot/strategies/indicators.py`)
- [ ] **P1 `ta.ema` tohumu:** `plot(ta.ema(close, 9))`. Grafiğin **ilk** barında değer var ve o barın `close`'una eşit mi?
      Evet → Python ile aynı (ilk değerle tohum). İlk 8 bar boşsa (na) → Python `Ema` SMA tohumuna çevrilmeli; etkisi:
      MR'nin EMA200 trend filtresi ilk 200 barda, MTF'nin EMA21/50 bandı ve saatlik EMA20/50'si ısınmada.
- [ ] **P2 `ta.sma` / `ta.rma`:** `plot(ta.sma(close, 20))` ilk değeri bar indeksi 19'da mı? `plot(ta.rma(close, 14))`:
      ilk değer 14. barda (bar indeksi 13) ve ilk 14 kapanışın ortalaması mı?
- [ ] **P3 `ta.atr` / `ta.tr(true)`:** `plot(ta.tr(true))` ilk barda `high − low` mi; `plot(ta.atr(14))` = RMA(TR) mı?
- [ ] **P4 `ta.rsi`:** `plot(ta.rsi(close, 2))`. İlk değer bar indeksi 2'de mi; düz ya da tek yönlü koşuda 100 / 0 mı?
- [ ] **P5 `ta.stdev`:** `plot(ta.stdev(close, 20))` kitle (n'e bölünen) sapması mı; sabit fiyatta tam 0 mı?
- [ ] **P6 `ta.highest` / `ta.lowest`:** `plot(ta.highest(high, 5))`. Pencere o anki barı içeriyor mu; ilk 4 bar boş mu?
- [ ] **P7 Pivot eşitlik kuralı** (Silver Bullet): iki **bitişik eşit** tepeli bir yer bul;
      `plot(ta.pivothigh(5, 5), style = plot.style_circles, offset = -5)`. Hiçbiri işaretlenmiyorsa **strict** (Python
      bugün böyle); yalnızca sağdaki (sonraki) işaretleniyorsa **left_inclusive**; soldaki işaretleniyorsa iki kural da
      tutmuyor. Eşit diplerle `ta.pivotlow` için de aynı. Anahtar: `PivotHigh` / `PivotLow`'un `ties` parametresi
      (`strategies/indicators.py`, varsayılan `"strict"`; `strategies/ict_silver_bullet.py` varsayılanı kullanır).

**İlk bar ve na davranışı** (port varsayımları)
- [ ] **P8** `plot(ta.change(dayofmonth(time, "America/New_York")) != 0 ? 1 : 0)`: grafiğin ilk barında 0 mı? Python
      ilk barı yeni gün saymaz (NY, Master, SB, MR, MTF). NY ve Master'da bu yüzden ilk (kısmi) günde günlük kesici
      çalışmaz; SB, MR ve MTF gün başı değerini ilk barda `na()` denetimiyle kurar. Asian günü değil Asya döngüsünü
      kullanır (P9).
- [ ] **P9** Asya penceresinin (20:00–00:00 NY) içinde başlayan bir grafikte `x and not x[1]` ilk barda doğru mu?
      (`asianNow = <pencere içi>`; `plot(asianNow and not asianNow[1] ? 1 : 0)`) Python doğru sayar (Asian, Master S2).
- [ ] **P10** `kcLen > bbLen` iken KC henüz na ise `bbUpper < kcUpper` yanlış mı (sıkışma yok sayılır)? Yalnızca
      varsayılan olmayan girdilerde önemlidir (MR).
- [ ] **P11** `timestamp("America/New_York", 2026, 2, 31, 23, 59)` 3 Mart'a mı taşar? Python taşır (tarih aralığı
      girdileri).

**Üst zaman dilimi** (MTF; `HtfEma`)
- [ ] **P12** 5 dk grafikte `plot(request.security(syminfo.tickerid, "60", ta.ema(close, 20)[1], lookahead =
      barmerge.lookahead_on))`: değer bir saatin 12 barı boyunca sabit, bir sonraki saatin **ilk** barında mı değişiyor?
- [ ] **P13** Saatlik barlar tam saatte mi başlıyor (CME'de seans 18:00 NY)? Python saatleri UTC'de tam saate böler.
      `htfTf` 60 dışında (240, 90) TradingView seans başlangıcına hizalıyorsa Python farklıdır: parite yalnızca
      varsayılan 60 ile anlamlıdır.
- [ ] **P14** TradingView'ın saatlik geçmişi grafiğin ilk barından önce başlıyor mu (P12'deki çizgi grafiğin ilk
      saatinde de değer taşıyor mu)? Python'unki grafiğin ilk barında başlar. Başlıyorsa en az ~50 saatlik barı
      `propbot parity --warmup-bars` ile dışarıda bırak (5 dk grafikte 600 bar).

**Broker emülatörü** (`src/propbot/backtest/engine.py`; List of Trades'ten okunur)
- [ ] **P15** Açılış bir limitin (TP) ötesinde ama **1 tick'ten az** geçmişse: o açılışta dolmuyor, bar fiyatı seviyeyi tam
      1 tick geçince limit fiyatından mı doluyor? Python böyle yapar.
- [ ] **P16** Açılış boşlukla TP'nin ötesindeyse TP açılıştan (daha iyi fiyat) mı; stop'un ötesindeyse stop açılıştan +
      slipaj mı doluyor?
- [ ] **P17** `strategy.close` / `strategy.close_all(immediately = true)` dolumları (EOD close, Session end, Daily DD
      halt) barın kapanışından **slipajlı** mı? Python slipaj uygular. List of Trades'te kapanış fiyatı ile çıkış
      fiyatını karşılaştır (MES'te fark 0,25 olmalı).
- [ ] **P18** Tick ızgarasında olmayan emir fiyatları yuvarlanıyor mu? Örnekler: Silver Bullet CE girişi (iki 0,25'lik
      seviyenin ortası, ör. `x.125`), MTF TP1 (`ort ± 1,5 × risk`), MR stop'u (`ort ∓ 2 × ATR`). Python ham seviyeyi
      kullanır; fark 1 tick'ten küçüktür ama dolum barını değiştirebilir.
- [ ] **P19** Aynı fiyatta birden çok emir tetiklenirse sıra (Python: önce stop, sonra hedef, sonra giriş) ve açılış
      yükseğe ve düşüğe **tam eşit uzaklıktaysa** bar yönü (Python ve ADR-0008: önce düşük).
- [ ] **P20** MTF ölçekli modda `qty_percent` tam kontratta nasıl yuvarlanıyor (ör. 3 kontratın %50'si)? Python tam
      kesri kapatır (1,5). "Fractional" boyut modunda girişin küsurat hassasiyeti ne? (Python lot adımına aşağı yuvarlar.)
- [ ] **P21** `pyramiding = 2` (Master) aynı yönde **en çok 2** giriş mi demek (Python böyle: 0 ve 1 = bir giriş)?
- [ ] **P22** `strategy.close_all(immediately = true)` çağrıldığı barda, aynı script çalışmasında `strategy.position_size`
      ve `strategy.closedtrades` kapanışı gösteriyor mu? Python bir sonraki barda görür (emirleri değiştirmez; yalnızca
      durum sıfırlamasının barı). Ayrıca: kapanmış bir girişin kimliği yeniden kullanılınca eski `strategy.exit` yeni
      girişe bağlanıyor mu? Python eskisini giriş kapanınca siler.
- [ ] **P23** Açık K/Z ve `strategy.equity` girişte ödenen / çıkışta ödenecek komisyonu içeriyor mu (Python içermez)?
      `strategy.closedtrades.profit(i)` net mi brüt mü (Python net; Silver Bullet'ın ardışık zarar sayacı bunu kullanır)?
      CFD grafiğinde özsermaye long'u bid'den, short'u ask'ten mi değerliyor (Python böyle; Asian ve Master'ın yüzen
      günlük kesicisi)?
- [ ] **P24** Master: `strategy.opentrades.entry_price` slipajı içeriyor mu; `close_all` iki bacağı aynı barda
      kapatınca `closedtrades` sırası giriş sırası mı (yalnızca birleşik zarar serisini etkiler); girişi henüz dolmamış
      bacağa `strategy.close` hiçbir şey yapmıyor mu?

**Dışa aktarımlar** (`src/propbot/backtest/parity.py`, `src/propbot/data/ohlcv.py`; örnek dosyalar
`tests/fixtures/parity/`, `tests/fixtures/ohlcv/tv_export.csv` hatırlanan biçimden yazıldı)
- [ ] **P25 List of Trades başlıkları:** `Trade #`, `Type`, `Signal`, `Date/Time` (ya da `Date and time`),
      `Price <para birimi>`, `Contracts` / `Position size` / `Qty`, `Profit <para birimi>` / `Net P&L <para birimi>`
      mi? Bir boyut sütunu para birimi taşıyorsa (değer sütunudur, miktar değil) yaz. Dosyanın ilk 5 satırını
      (fiyatlar dahil) yapıştır.
- [ ] **P26** Her dolum bir satır mı (işlem başına giriş + çıkış satırı), `Type` tam olarak `Entry Long` / `Exit Long` /
      `Entry Short` / `Exit Short` mi, satırlar en yeniden eskiye mi?
- [ ] **P27** `Date/Time` grafiğin saat diliminde ofsetsiz mi, `YYYY-MM-DD HH:MM` biçiminde mi ve dolum barının
      **açılış** zamanı mı? Bilinen bir dolumu grafikle karşılaştır. Kapanış zamanıysa yaz (eşleştirme değişir).
      Öneri: dışa aktarmadan önce grafik saat dilimini **UTC** yap (yaz saati geçişindeki belirsiz saatler reddedilir).
- [ ] **P28** Çıkış satırında `Signal` ne gösteriyor: `strategy.exit` yorumu mu, kimliği mi (`Exit L`; MTF'de
      `Init stop`), `close_all` yorumu mu (`EOD close`, `Daily DD halt`)? Kimlikse stop ile hedef yalnızca fiyattan ayrılır.
- [ ] **P29** Veri sonunda açık kalan işlem nasıl görünüyor: çıkış satırı yok mu, yoksa `Signal = Open` ve/veya boş
      `Date/Time` mı?
- [ ] **P30** MTF kısmi çıkışı (TP1 + Init stop): tek `Trade #` altında birden çok çıkış satırı mı, ayrı `Trade #`'lar mı?
- [ ] **P31** Dosyanın kodlaması (UTF-8? BOM?), ayırıcısı (virgül / noktalı virgül / sekme) ve ondalık/binlik biçimi
      (Türkçe yerel ayarda virgül olabilir).
- [ ] **P32 Grafik verisi dışa aktarımı:** başlık `time,open,high,low,close,Volume` (+ gösterge sütunları) mı, `time`
      Unix saniye mi ISO mu, değer barın açılış zamanı mı? İlk 3 satırı yapıştır.
- [ ] **P33** Yarım günde (2026-11-27) ve veri boşluğunda `session.islastbar` hangi barda doğru? `propbot data check`
      raporu yarım günü "kısa seans" olarak listeler; seans sonu Python'da bir sonraki bara ≥ 30 dk boşluktan türetilir.
      Aynı bar mı?
- [ ] **P34** 1 dk ve 5 dk grafikte `int(timeframe.in_seconds() / 60)` 1 ve 5 mi (giriş-zamanı kapısındaki `barMin`)?

**Parite koşusu: risk girdisi ve beklenen sapma** (2026-11-19 kullanıcı kararları; `docs/backtest.md` 6. bölüm)
- [ ] **P35 Parite dışa aktarımı betiğin varsayılan risk girdisiyle:** "List of Trades"i dışa aktarırken Inputs'ta risk
      **`0.5`** (varsayılan) olsun; `config/risk.yaml`'daki %0,25'i TradingView'a **girme**. `propbot parity` her zaman
      betiğin kendi riskiyle koşar ve raporun başında bunu yazar (`İşlem başı risk: %0,5 — betiğin kendi değeri …`).
      Başka bir risk girdisiyle alınmış dışa aktarım başka boyutlar ve kısmen başka işlemler taşır (MTF'de tek/çift
      kontrat sayısı ölçekli modu değiştirir); o karşılaştırma parite değildir. Dışa aktarımdaki risk girdisini yaz.
- [ ] **P36 MTF trail ısınması — beklenen güvenli sapma (onaylandı 2026-11-19):** iz göstergesi henüz değer
      üretmemişken (varsayılan Chandelier izinde grafiğin ilk ~22 barı) TP1/1,5R'ye ulaşan bir işlemde Pine kaynağına
      göre runner stop'suz kalır (`math.max(be, na)`; 449, 465); Python portu stop'u başa başta tutar (değişmez
      kural 2). Python davranışı korunur, Pine dosyası değişmez. Parite raporunda göreceğin:
      (a) her `mtf_trend_cont` raporunda, farkların ardından "Beklenen güvenli sapma (mtf_trend_cont):" bölümü ve iki
      cümlesi (fark olsa da olmasa da; artefakt notlarında da);
      (b) o pencerede böyle bir işlem varsa runner'ın çıkışı farklı olabilir: Python'da runner başa baş (sonra iz)
      stop'uyla korunur ve fiyat ona değerse stop'tan çıkar; Pine kaynağına göre TradingView'da o runner'ın stop'u
      yoktur ve pozisyon bir zorunlu kapanışa (seans sonu, günlük zarar, bitiş tarihi) kadar açık kalır. Rapor bunu
      çıkış fiyatı ve/ya da nedeni farklı bir **uyuşmazlık** satırı olarak gösterir.
      Bu fark **sayılır**, hoş görülmez: bölüm hiçbir sayıyı ve hükmü değiştirmez. Isınmayı `--warmup-bars` ile
      dışarıda bırak (P14'teki 600 bar bu pencereyi de kapsar; dışarıda kalan satırlar %2 sınırına tabidir).
      Raporla: böyle bir işlem çıktı mı; TradingView runner'ı gerçekten stop'suz mu bıraktı (List of Trades'te "Run" /
      "RunB" çıkışı hangi barda, hangi fiyattan)? Pine derlenip denenmediği için TradingView tarafı VERIFY'dır
      (CHANGELOG açık bulgu 64a).

## M. MetaTrader 5 kontrolleri (Faz 2)

- [ ] **M1 "Export Bars" biçimi:** sekmeyle ayrılmış, başlık `<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL>
      <SPREAD>`, tarih `YYYY.MM.DD`, saat `HH:MM:SS` mi? Kodlama UTF-16 ise yükleyici reddeder ve dönüştürme komutunu
      söyler. İlk 3 satırı yapıştır.
- [ ] **M2 Sunucu saati = New York + 7 saat:** terminalde NY 09:30 barı (ABD kış saatinde 17:30 TR) sunucu saatinde
      **16:30** mı görünüyor? Hem yaz hem kış saatinde, mümkünse ABD ile AB'nin farklı olduğu haftalarda da
      (2026-03-08…03-29, 2026-10-25…11-01) bak. Sonra `uv run propbot data check --data <dosya> --bar-tz NY+7`:
      "saat dilimi kayması" çıkmamalı.
- [ ] **M3** CFD'de `<VOL>` 0 mı? (Python o zaman `<TICKVOL>`'u hacim sayar; MTF hacim filtresi bunu kullanır.)
- [ ] **M4 `US500.cash` şartnamesi** (Market Watch → sağ tık → Specification): digits / tick size, contract size, tick
      value, volume min / step, komisyon, tipik spread ve NY açılışındaki spread. Değerleri `config/backtest.yaml` →
      `instruments.US500.cash` ve venue profilindeki `contract.*` alanlarına yaz (`docs/backtest.md` 1. bölüm), sonra
      `uv run propbot config check`.
- [ ] **M5** `US500.cash` işlem saatleri ve günlük ara (sunucu saatiyle): ara 30 dakika ya da daha uzunsa seans sonu
      olarak okunur (`session.min_gap_minutes`). Ara saatini yaz.

## Claude Code'a yapıştırılacaklar
1. Her dosya için derleme sonucu (hata/uyarı metni + satır).
2. Kullandığın zaman dilimi ve tarih aralığı; Properties tablosundan sapma varsa o değer.
3. C bölümündeki önce/sonra tablosu + maliyetsiz koşu sonucu.
4. D, E ve G maddelerinde geçen / kalan / test edilemeyen her madde.
5. En az bir alarm yükü örneği (token maskeli); mümkünse her stratejiden bir giriş gövdesi.
6. H bölümü: heartbeat göstergesinin derleme sonucu ve Alert Log'daki mesajı.
7. P ve M bölümleri: her madde numarasıyla ("P7: strict", "M2: 16:30, geçti"; P35 için dışa aktarımdaki risk girdisi,
   P36 için ısınma penceresinde ne gördüğün) ve istenen dosya satırları (List of Trades, grafik verisi, MT5 dışa
   aktarımı; token ya da hesap bilgisi içermez).

(B4 FTMO `US500.cash` değerleri M4 maddesindedir; Faz 0'da gerekmez.)
