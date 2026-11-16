# ADR-0006 — Giriş süzgeci: kabul edilen TradingView sembolleri ve asgari stop mesafesi

Durum: kabul edildi · 2026-11-16 · Karar: Oğuz (2026-11-16 kararları 46 ve 33) · İlgili: SPEC §5.2, §8.1, §8.3;
değişmez kurallar 3, 4, 10; ADR-0000, ADR-0005; CHANGELOG açık bulgular 16, 33, 46

## Bağlam

Faz 1'de sinyal TradingView grafiğinde hesaplanır, işlem başka bir enstrümanda (FTMO `US500.cash`) elle açılır
(ADR-0000). Bot bu yüzden seviyeleri **mesafe** olarak iletir (ADR-0005): "giriş anındaki fiyata göre stop −12,50 puan".
Bu, iki varsayıma dayanır ve ikisi de kodda denetlenmiyordu:

1. **Grafiğin puanı venue sembolünün puanıyla aynı ölçektedir.** Alıcı `symbol` alanının yalnızca biçimine bakıyordu.
   Strateji yanlışlıkla başka bir grafiğe (ör. `CME_MINI:NQ1!`, bir hisse, `SPY`) eklenip alarmlanırsa sinyal iletilir;
   mesafeler başka bir enstrümanın ölçeğindedir ve `US500.cash`'e uygulanınca anlamsız bir işlem olur. Lot o mesafeye
   göre hesaplandığı için işlem başı risk tavanı aşılmaz, ama stop ya gülünç derecede yakın ya da uzak düşer.
2. **Stop mesafesi işlem yapılabilir büyüklüktedir.** Çok dar stop çok büyük lot önerir (0,25 puan → 100.000 USD
   hesapta, 1 lot = puan başına 1 USD varsayımıyla, 1000 lot). Mesaj nominal büyüklüğü ve hesaba oranını gösteriyordu,
   ama otomatik "ATLA" yoktu. Böyle bir
   emirde spread ve kayma tek başına stop mesafesini aşar: gerçek kayıp hesaplanan riskin katı olur, ve notional
   hesabın kaldıraç sınırını aşabilir (açık bulgu 16).

Ayrıca `CME_MINI:MES1!` ücretsiz TradingView hesabında gecikmeli gelebilir (runbook §1); kullanıcının aynı ölçekte,
gerçek zamanlı başka semboller kullanabilmesi gerekir.

## Seçenekler

**Sembol denetimi**
- A. Denetim yok, başlıkta sembol yazar (önceki durum). Yanlış grafik fark edilmeyebilir.
- B. Alıcıda reddet (400). TradingView hata yanıtını yeniden denemez ve kullanıcıya göstermez; yanlış grafik sessizce
  susar, kullanıcı nedenini göremez.
- C. **Kabul et, kaydet, girişi "ATLA" olarak bildir.** Kullanıcı nedeni ve TradingView'ın gönderdiği yazımı görür.
- Liste nerede dursun: `config/tv_alerts.yaml` (alarm hattının dosyası) ya da **venue profili**. Ölçek venue
  sembolüne göre tanımlıdır; ikinci bir venue (başka sembol adı, belki başka ölçek) kendi listesini ister.
- Eşleşme: birebir, ya da önek/büyük-küçük harf hoşgörülü. Hoşgörülü eşleşme, aynı adı taşıyan ama başka ölçekte
  fiyatlanan bir beslemeyi kabul edebilir; hangi sağlayıcının hangi ölçeği kullandığı buradan doğrulanamaz.

**Asgari stop**
- A. Yalnızca göster (önceki durum).
- B. **Puan cinsinden asgari stop** (`min_stop_points`). Basit, sembol ölçeğine bağlı, sözleşme değerleri bilinmeden
  de çalışır.
- C. Azami kaldıraç / notional sınırı. Sözleşme değerleri (`contract.*`) ve firmanın kaldıraç kuralı gerekir; ikisi de
  profilde henüz boş / doğrulanmamış. Faz 3 risk motorunun işidir.
- D. Spread'in katı olarak dinamik eşik. Faz 1'de bot venue fiyatını ve spread'ini görmez.

## Karar

Venue profiline zorunlu bir `signal` bölümü eklendi (firmanın kuralı değil, botun kendi süzgeci):

```yaml
signal:
  tv_symbols: ["CME_MINI:MES1!", "CME_MINI:ES1!", "US500.cash", "OANDA:SPX500USD",
               "FOREXCOM:SPXUSD", "PEPPERSTONE:US500", "CAPITALCOM:US500"]
  min_stop_points: 1.5
```

- **`tv_symbols`** (karar 46, seçenek C + venue profili + birebir eşleşme): puan ölçeği `symbol` ile aynı olan
  TradingView sembolleri. Liste kullanıcının 2026-11-16'da verdiği yedi semboldür; her birinin ölçeği kullanıcının
  beyanıdır (bu ortamdan TradingView'a erişilemiyor) ve kullanılmadan önce bir fiyat karşılaştırmasıyla doğrulanır
  (doğrulama listesi H). En az 1, en çok 32 sembol; yinelenen ya da alıcı şemasının kabul etmeyeceği biçimde bir sembol
  yükleme hatasıdır.
  - Listede olmayan sembolün **girişi**: `skip_symbol`, "[SİNYAL — ATLA] Sembol kabul listesinde değil (…)"; bütçede
    sayılmaz. Karar sırası: saat uyuşmazlığı → KILL → duraklatma → **sembol** → bayat → … (duraklatılmış bot sessiz
    kalır; sembol hatası bayatlıktan önce söylenir, çünkü kalıcı bir kurulum hatasıdır).
  - **Takip alert'leri** (stop güncelleme, çıkış, iptal): iletilmiş bir girişe bağlanıyorsa listeye bakılmaz — liste
    işlem açıkken değişmiş olabilir ve bu mesajlar risk azaltır. Girişi `skip_symbol` ile atlanmışsa zaten "ilgili işlem
    yok" olarak düşer. Hiçbir girişe bağlanamıyorsa **ve** sembol listede değilse iletilmez (`skip_symbol`) ve 10
    dakikada en çok bir alarm üretir: sahipsiz bir stop güncellemesi başka ölçekte bir fiyat taşırdı.
  - Heartbeat sembole bakılmadan sayılır (yalnızca alarm hattını kanıtlar).
- **`min_stop_points`** (karar 33, seçenek B): giriş ile stop arası bundan küçükse `skip_stop_too_tight`,
  "[SİNYAL — ATLA] Stop çok dar (… puan; alt sınır … puan)". Eşitlik kabul edilir; karşılaştırma tam ondalıktır
  (`Decimal`), ikili kayan nokta bir tick'lik sınırı yanlış yöne düşürebilir. Değer kullanıcının kararıdır: US500 / MES
  ölçeğinde 1,5 puan. Kural `contract.*` boşken de geçerlidir. Stop güncellemelerine uygulanmaz (açık işlemde stop'u
  sıkılaştırmak her zaman iletilir). Stop'u girişle **aynı fiyata** yazılmış bir girişi alıcı şeması daha önce
  reddeder (422, "Geçersiz alert gövdesi (alanlar: stop)" alarmı; kaydedilmez): Pine fiyatları tick'e yuvarlayarak
  yazdığı için yarım tick'ten dar bir stop da buraya düşer ve "stop çok dar" sayımına girmez. Bu şema kuralı
  değiştirilmedi (ayrı bir karar ister).
- İki yeni sonuç `signals.outcome` denetimine girdiği için **veritabanı şema sürümü** arttı (bekleme sonucuyla
  birlikte bugün 3; ADR-0004); eski sürümle oluşturulmuş dosya açılmaz
  (ADR-0004: göç yok). Kabul koşusu başlamadığı için göç yazılmadı.
- `propbot config check` ve `serve` afişi listeyi ve eşiği gösterir.

## Sonuçlar

- Yanlış grafiğe kurulan alarm ilk girişte görünür olur; kullanıcı mesajdaki yazımı listeye ekleyerek ya da alarmı
  doğru grafiğe taşıyarak düzeltir. Listeyi değiştirmek servis yeniden başlatması ister.
- Birebir eşleşme katıdır: TradingView bir sembolü listedekinden farklı yazarsa (önek, büyük/küçük harf) girişler
  "ATLA" olur. Özellikle `US500.cash`'in TradingView'da nasıl yazıldığı doğrulanmadı (liste kullanıcının yazdığı
  biçimi taşır). İlk gerçek alert bunu gösterir; açık bulgu olarak kayıtlıdır.
- Liste ölçeği **doğrulamaz**, yalnızca kullanıcının doğruladığını söylediği sembolleri tanır. Bir sağlayıcı sembolünü
  farklı ölçekte fiyatlıyorsa bot bunu göremez.
- Asgari stop bir **alt** sınırdır; azami kaldıraç / marjin denetimi değildir. Örnek (varsayım: 1 lot = puan başına
  1 USD; gerçek değer MT5'ten okunacak): 1,5 puanlık stop 100.000 USD hesapta %0,25 riskle hâlâ ~166 lot önerir, yani
  endeks 5000 iken hesabın ~8 katı nominal. Mesaj nominali ve oranı göstermeye devam eder; kaldıraç sınırı Faz 3 risk
  motoruna kalır (açık bulgu 16).
- Dar stop'lu kurulumlar (özellikle Silver Bullet ve küçük FVG'ler) sinyal olarak düşer; kabul koşusunda
  `propbot signals report` çıktısındaki "atla: stop çok dar" sayısı eşiğin ne kadar kestiğini gösterir.
- Değerler koda gömülmedi (değişmez kural 10'un ruhu): ikisi de `config/venues/*.yaml`'da; `min_stop_points`'i
  değiştirmek bir risk kararıdır ve gerekçesi bu dizine yazılır.
