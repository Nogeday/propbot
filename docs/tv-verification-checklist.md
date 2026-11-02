# TradingView doğrulama listesi — Faz 0

Claude Code Pine'ı derleyemez ve çalıştıramaz; aşağıdakileri TradingView'da sen yaparsın ve sonuçları Claude Code
oturumuna yapıştırırsın. Sıra: **Master → NY → Asian → Silver Bullet → Mean Reversion → MTF**.
Her dosya için A–F adımlarını tamamla, sonra bir sonrakine geç.

## 0. Hazırlık (bir kez)
- [ ] Faz 0 testlerinin hepsi **`CME_MINI:MES1!`** üzerinde (altı dosya). Önce/sonra karşılaştırmasında orijinal ve
      v2 için **aynı** zaman dilimi ve tarih aralığını kullan ve aşağıya yaz.
- [ ] Asian ve Master S2, 20:00–00:00 ET verisi ister → bu ikisinde grafikte "Extended trading hours" **açık** olmalı.
- [ ] **Maliyet ayarları (Properties sekmesi, altı dosyada da, orijinal ve v2'de aynı):**

      | Ayar | Değer |
      |---|---|
      | Commission | **0.50**, tip: cash per contract (USD/kontrat, taraf başı → gidiş-dönüş $1,00) |
      | Slippage | **1** tick |

      Bu değerler koddaki varsayılanlar değildir; her script'te Properties'te elle girilir. FTMO `US500.cash`
      değerleri Faz 2'de gelecek (CHANGELOG "Açık bulgular" 2).
- [ ] Grafik saat dilimi fark etmez (script `America/New_York` kullanıyor), ama kontrolü kolaylaştırmak için
      grafiği `(UTC-5) New York` yap.

| Dosya | Sembol | TF | Tarih aralığı | Seans (RTH / ETH) |
|---|---|---|---|---|
| master | CME_MINI:MES1! | 1m | | ETH |
| ny_open_fvg | CME_MINI:MES1! | 1m | | |
| asian_sweep | CME_MINI:MES1! | 1m | | ETH |
| ict_silver_bullet | CME_MINI:MES1! | 1m veya 5m | | |
| mean_reversion | CME_MINI:MES1! | 5m–15m | | |
| mtf_trend | CME_MINI:MES1! | 5m | | |

## A. Derleme ve kayıt
- [ ] Pine Editor → **yeni** script → düzeltilmiş `.pine` dosyasının tamamını yapıştır → **"… v2"** adıyla kaydet.
      Orijinalin üzerine yazma.
- [ ] "Add to chart" — derleme hatası varsa **mesajı ve satır numarasını** olduğu gibi kopyala.
  - MTF için özel not: v2'de `riskPts` bildirimi düzeltildi (`e997dd5`). **Orijinal** MTF'de "Undeclared identifier"
    hatası çıkıyorsa bunu yaz; o zaman orijinal sütunu boş kalır (karşılaştırma yapılamaz).
- [ ] Uyarı (sarı) mesajları varsa onları da kopyala.

## B. Maliyet ayarları (Properties sekmesi)
- [ ] Commission = 0.50 cash per contract, Slippage = 1 tick (bkz. 0. Hazırlık). Orijinal ve v2'de **aynı**.
- [ ] Silver Bullet: Properties'te "Verify price for limit orders" = **1 tick** göründüğünü kontrol et.
- [ ] Kullandığın değerleri yaz: komisyon tipi/değeri, slipaj (tick), limit doğrulama.

## C. Önce/sonra tablosu (Strategy Tester → Overview / Performance Summary)
Orijinal ve v2 için aynı ayarlarla doldur. "Tot R" script'in kendi tablosundan (MTF'de tablo farklı: "Full trades",
"Avg runner R").

| Dosya | Sürüm | İşlem sayısı | Kazanma % | Net kâr | Profit factor | Maks DD | Toplam R |
|---|---|---|---|---|---|---|---|
| | orijinal | | | | | | |
| | v2 | | | | | | |

- [ ] **Maliyetli ve maliyetsiz fark:** v2'yi bir kez de Commission = 0 ve Slippage = 0 ile çalıştır, net kâr ve PF'yi
      not et (yalnızca karşılaştırma için).

## D. Grafik ve işlem listesi
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
      kapanışı aynı günün son barında oluyor, ertesi günün açılışında değil.
- [ ] **ABD kış saati (2026-11-01 sonrası):** 2026-11-02 ve sonrasında OR 09:30 ET'de (17:30 TR) kuruluyor
      (NY ve Master S1'de OR High/Low çizgileri 09:35'ten itibaren görünür).
- [ ] MTF: 1H EMA çizgileri artık bir 1H bar gecikmeli; gerçek zamanlı barda değer bar kapanmadan değişmiyor
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
- [ ] Girdi değiştirince alarmı yeniden kurman gerektiğini not et (alarm, oluşturulduğu andaki girdilerle çalışır).

## Claude Code'a yapıştırılacaklar
1. Her dosya için derleme sonucu (hata/uyarı metni + satır).
2. Kullandığın sembol/TF/tarih ve maliyet ayarları.
3. C bölümündeki önce/sonra tablosu + maliyetsiz koşu sonucu.
4. D ve E maddelerinde geçen/kalan/test edilemeyen her madde.
5. En az bir alarm yükü örneği (token maskeli).

(B4 FTMO `US500.cash` değerleri Faz 2'de istenecek; Faz 0'da gerekmez.)
