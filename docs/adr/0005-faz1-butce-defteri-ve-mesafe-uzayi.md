# ADR-0005 — Faz 1'de günlük bütçe defteri temkinlidir; seviyeler ve stop karşılaştırmaları mesafe uzayındadır

Durum: kabul edildi · 2026-11-13 · Karar: Oğuz (2026-11-13 cevapları: boyut yedeği, temkinli bütçe + `/atladim`) +
tasarım denetimi bulguları · İlgili: SPEC §5.4, §8.1; değişmez kurallar 2, 3, 4; ADR-0000, ADR-0004

## Bağlam

Faz 1'de bot emir göndermez; dolumu, gerçekleşen kâr/zararı ve pozisyonun hâlâ açık olup olmadığını bilemez. Bildiği tek
şey TradingView'ın gönderdiği alert'lerdir, ve TradingView stop/TP dolumlarını **bildirmez** (yalnızca zorunlu
kapanışları: seans sonu, günlük zarar kesicisi, test aralığı sonu).

İkinci sorun enstrüman farkıdır (ADR-0000): alert'teki seviyeler TradingView grafiğinin fiyatlarıdır (ör. MES vadeli),
kullanıcı ise FTMO `US500.cash`'te kendi dolum fiyatına göre işlem yapar.

SPEC §8.1 ön kontrolü `gerçekleşen + açık risk + yeni risk ≤ günlük bütçe` der; Faz 1'de ilk iki terim ölçülemez.

## Seçenekler (bütçe satırı)

1. Bütçe satırını hiç göstermemek: kullanıcı günlük sınırı kafasında tutar.
2. `/aldim` · `/atladim` ile her sinyali elle işaretletmek: doğru ama her sinyalde iki dokunuş; unutulursa defter boş kalır.
3. **Temkinli sayım:** iletilen her giriş, prop günü boyunca tam riskiyle kaybetmiş gibi sayılır; kullanıcı yalnızca
   almadığı sinyali `/atladim` ile bildirir.

## Karar

**Bütçe defteri (seçenek 3).**

- İletilen her giriş sinyali riskiyle deftere yazılır ve prop günü (venue reset saatine göre) bitene kadar sayılır.
  `exit` ya da `cancel` alert'i kaydı **serbest bırakmaz**: zorunlu kapanışta sonuç −1R ile TP arasında herhangi bir
  şeydir, en kötüsü varsayılır.
- Kayıt yalnızca iki yolla serbest kalır: kullanıcının `/atladim`'ı, ya da mesajın kullanıcıya **kanıtlanabilir biçimde**
  ulaşmaması (ADR-0004). `/aldim` geri alır.
- `kullanılan + yeni risk > günlük bütçe` ise sinyal "[SİNYAL — ATLA]" olarak iletilir ve sayılmaz.
- `/atladim` (kimliksiz) yalnızca mesajı kullanıcıya **gönderilmiş** son girişi seçer ve yanıtında hangi girişi
  işaretlediğini yazar; henüz yolda olan yeni bir sinyal yanlışlıkla işaretlenemez.
- Haftalık tavan, ardışık zarar duraklatması ve eşzamanlı pozisyon sınırı Faz 1'de uygulanmaz: dolum bilgisi ister.

**Boyut satırı.**

- `lot = aşağı_yuvarla(risk / (stop puanı × 1 lotun 1 puanlık değeri))`, hiçbir zaman yukarı yuvarlanmaz; `min_lot`
  altında "ATLA" (SPEC §8.1). Venue profilinde `contract.*` boşken hata verilmez: mesaj en fazla riski ve formülü yazar.
- Lot yanında nominal büyüklük ve hesaba oranı yazılır ("≈ $102465 nominal, 1.0× hesap"): çok dar stop'un ürettiği
  anlamsız boyut gözle görülür. Otomatik bir eşik uydurulmadı (açık bulgu: asgari stop / azami kaldıraç kuralı).

**Mesafe uzayı.**

- Giriş mesajı seviyeleri puan mesafesi olarak verir ve SL/TP'nin **emirle birlikte** girilmesini söyler.
- Fiyat sapması koşulu TradingView fiyat aralığı olarak yazılır (kullanıcı FTMO fiyatını MES fiyatıyla karşılaştırmasın).
- **Stop güncellemeleri mesafe uzayında karşılaştırılır:** `ofset = stop − giriş` (giriş alert'inde sinyal barı
  kapanışı, stop güncellemesinde TradingView ortalama dolumu). TradingView'da mutlak seviye olarak "iyileşen" bir stop,
  dolum sinyal barı kapanışından farklıysa kullanıcının kendi giriş fiyatına göre **genişleme** olabilir. Örnek: giriş
  5123.25, stop 5110.75 (−12.50); TradingView 5125.25'ten dolar; trailing stop'u 5112.00'a çeker → kullanıcı için
  −13.25. Bu güncelleme iletilmez; "[UYARI] … mevcut stop'unu KORU" gönderilir (değişmez kural 2).
- Asgari adım (`trail_min_step_ticks` × venue tick boyutu) aynı uzayda uygulanır; sıra dışı gelen eski güncelleme
  yenisinin üzerine yazamaz.

**Hangi mesajlar her zaman gelir.** Duraklatma ve KILL yalnızca yeni girişleri keser; stop güncellemesi, çıkış ve iptal
mesajları risk azalttığı için her durumda iletilir. Kullanıcının atladığını bildirdiği ya da hiç iletilmemiş bir girişin
takip mesajları gönderilmez. Mesajı "iletim belirsiz" kalan bir girişin takip mesajları, kullanıcı pozisyonda olabileceği
için, bir notla iletilir.

**Kuralların tazeliği.** Venue kuralları 30 günden eskiyse `signal` modu SPEC §8.1 gereği çalışmaya devam eder; giriş
mesajlarının başına "[KURALLAR DOĞRULANMADI …]" satırı eklenir ve günde bir alarm gönderilir.

## Sonuçlar

- Defter gerçek riskten **yüksek** gösterir: kazanan işlemler de gün sonuna kadar sayılır. Varsayılan config ile
  (işlem başı %0,25, günlük bütçe %1) günde en çok dört giriş "uygula" olarak gelir; beşincisi "bütçe yetersiz" der.
  Bu bilinçlidir: Faz 1'in bütçe satırı bir üst sınırdır, muhasebe değildir.
- Kullanıcı almadığı sinyali bildirmezse sonraki sinyaller gereksiz yere "ATLA" olarak gelebilir. Tersi (bildirmeyi
  unutup fazla risk almak) mümkün değildir.
- `/atladim` o girişin stop/çıkış mesajlarını da keser; yanlış girişi işaretlemek bu yüzden pahalıdır. Yanıtın girişi
  adıyla söylemesi ve `/aldim` bunun içindir.
- Stop/TP dolumu bildirilmediği için bot bir pozisyonun kapandığını bilemez: her giriş mesajı "pozisyonun hâlâ açıksa
  seans sonunda kendin kapat" der. Yarım günlerde `exit` alert'ine güvenilmez (açık bulgu 18).
- Kurallar bayatken girişleri tamamen kesmek (değişmez kural 3'ün harfi) yerine uyarı satırı + günlük alarm seçildi
  (SPEC §8.1 `signal` modunu açık tutuyor). Bu, kullanıcı kararına açık bir noktadır; istenirse tek satırlık değişiklikle
  "bayat kurallarla giriş iletme" yapılır.
- Risk motoru (Faz 3) geldiğinde defterin yerini gerçek `realized_today + open_risk` alır; `suggest_size` aynen kullanılır.
