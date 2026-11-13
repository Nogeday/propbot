# TradingView doğrulama listesi — Faz 0

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
- [ ] Inputs: `Risk base = Initial Capital`; risk `0.5`; günlük zarar kesicisi `2`; `Max trades per day = 1`
      (Silver Bullet, Mean Reversion, MTF); `Min trail step (ticks) = 4` (NY, Master S1, MTF);
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
- [ ] Stratejilerle aynı sembolün **1 dakikalık** grafiğine ekle; saat başlarında (New York saatiyle) barın arka planı
      hafif renkleniyor mu?
- [ ] Günlük grafikte "use an intraday chart of 60 minutes or less" çalışma zamanı hatası veriyor mu (vermeli)?
- [ ] Alarm oluştur: Condition = gösterge → **"Any alert() function call"**; Webhook URL = alıcı adresi. Seçenek adı
      farklıysa ne gördüğünü yaz.
- [ ] İlk saat başında Alert Log'da şu biçimde bir mesaj görünüyor mu:
      `{"v":1,"token":"…","strategy_id":"heartbeat","symbol":"CME_MINI:MES1!","tf":"1","bar_time":…,"action":"heartbeat"}`
- [ ] Bot çalışıyorsa Telegram'da `/saglik` → "son heartbeat" birkaç saniye/dakika önce görünüyor mu?
- [ ] Alarmın bitiş tarihini `config/tv_alerts.yaml` → `alerts` altına yazdın mı?

## Claude Code'a yapıştırılacaklar
1. Her dosya için derleme sonucu (hata/uyarı metni + satır).
2. Kullandığın zaman dilimi ve tarih aralığı; Properties tablosundan sapma varsa o değer.
3. C bölümündeki önce/sonra tablosu + maliyetsiz koşu sonucu.
4. D, E ve G maddelerinde geçen / kalan / test edilemeyen her madde.
5. En az bir alarm yükü örneği (token maskeli); mümkünse her stratejiden bir giriş gövdesi.
6. H bölümü: heartbeat göstergesinin derleme sonucu ve Alert Log'daki mesajı.

(B4 FTMO `US500.cash` değerleri Faz 2'de istenecek; Faz 0'da gerekmez.)
