# ADR-0010 — İşlem başı risk: canlıda ve karar koşularında tek kaynak `config/risk.yaml`

Durum: kabul edildi · 2026-11-19 · Karar: Oğuz (2026-11-19, Faz 2 teslim onayı, karar 5) · İlgili: SPEC §3.1, §6.4,
§6.6, §8.1, §9.1; değişmez kurallar 4 ve 10; ADR-0002, ADR-0003, ADR-0007, ADR-0008; CHANGELOG açık bulgu 82

## Bağlam

İşlem başı risk iki yerde, iki ayrı değerle duruyordu:

- **Pine script'leri:** risk girdisinin varsayılanı altı dosyada da %0,5 (Silver Bullet ve Mean Reversion'da E3 ile;
  MTF'de girdinin adı `riskPercent`, öbür beşinde `riskPct`). Python portları Pine girdi varsayılanlarını taşır
  (ADR-0008 madde 9), yani bir backtest %0,5 ile boyutlanıyordu.
- **`config/risk.yaml`:** `risk_per_trade_pct: 0.25` (kod tavanı 0,5; SPEC §8.1). Faz 1 mesajındaki lot önerisi
  (`src/propbot/risk/sizing.py`) ve Monte Carlo (`propbot montecarlo`) bunu kullanır; Go/No-Go G4 simülasyon riskinin
  bu değere eşit olmasını zaten istiyordu.

Sonuç (açık bulgu 82, kapı saldırısının üçüncü turu): karar koşuları %0,5 ile yapılıyor, o koşuların işlem listesi
Monte Carlo'da %0,25 riskle simüle ediliyordu. Bu ancak "R boyuttan bağımsızdır" doğruysa zararsızdır; değildir:

1. **Boyut tam birime aşağı yuvarlanır.** Yarı risk yarı boyut demek değildir, ve bazı script'lerin davranışı boyuta
   bağlıdır.
2. **Asgari boyutun altı atlanır.** Küçük riskte bazı işlemler hiç açılmaz.
3. **Script'lerin kendi yüzde kesicileri** başka sayıda kayıptan sonra tetiklenir (ör. %2 günlük kesici: tam −1R
   kayıplarla %0,5'te 4, %0,25'te 8 kayıp); hangi işlemlerin var olduğu değişir.

Kanıt, portların gerçek koduyla (`tests/unit/test_risk_rule.py`; **sentetik** 5 dk fikstür, yalnızca mekanizmayı
gösterir, hiçbir kenarı kanıtlamaz):

- `mtf_trend_cont`, 100 000 sermaye, aynı barlar: iki koşuda da aynı 20 giriş barı, ama ilk giriş %0,5'te **17**,
  %0,25'te **8** kontrat (8,5 aşağı yuvarlanır; 17'nin yarısı değil). Script yalnızca çift sayılı boyutta TP1'de
  yarıyı kapatır (SPEC §3.4.4): tek sayılı 17 bütün olarak stop'a gider (çıkış: `stop`), çift sayılı 8 önce hedefte
  yarıyı kapatır (çıkışlar: `target`, `stop`). Aynı sinyal, başka çıkışlar, başka R; en az beş girişin R'si ve çıkış
  dolumlarının sayısı farklı, toplam R farkı 0,5R'den büyük.
- `mr_bb_kc_rsi2`, 10 000 sermaye: %0,5'te **6**, %0,25'te **5** giriş; biri bir kontratın altında kaldığı için
  atlanır.

Yani %0,5 ile üretilmiş bir işlem listesi, %0,25 riskli hesabın yaşayacağı liste değildir.

## Seçenekler

Açık bulgu 82'nin sıraladığı iki yol:

- **A. `config/strategies.yaml`'da `risk_pct`'yi `risk.yaml` ile eşitlemek.** Pine'dan sapmadır: parite ancak
  TradingView girdisi de değiştirilirse geçerli kalır; aynı sayı iki dosyada durur ve yeniden ayrışabilir.
- **B. G4'te iki değerin eşitliğini istemek.** Uyuşmazlığı yakalar ama gidermez: karar koşusunu planlanan riskle
  üretmenin bir yolu olmadan G4 hiç geçemez.

Uygulamada ortaya çıkan üçüncü yol:

- **C. Tek kaynak.** Planlanan risk yalnızca `config/risk.yaml`'da durur; karar koşusu onu çalışma anında betiğin risk
  girdisine yazar. `config/strategies.yaml` risk girdisini hiç taşıyamaz. Parite, betiğin kendi değeriyle kalır.
  Go/No-Go zincirin her halkasında eşitliği denetler (B'nin denetimi dahil).

## Karar

Kullanıcı kararı (2026-11-19): **canlı çalışma ve portföy düzeyindeki değerlendirmelerde `config/risk.yaml`'daki
işlem başı risk (%0,25) geçerlidir; Pine'daki %0,5 yalnızca tekil strateji varsayımıdır.** Kararın kendisi bu
cümledir. Uygulama C'dir; aşağıdaki maddelerin ayrıntısı (`config/strategies.yaml`'da risk girdisinin yasağı,
`--pine-risk`, paritenin betiğin riskinde kalması, risk tabanı kuralı, işaretli liste, kapı denetimleri) o kararı
uygulamak için seçilen yoldur, ayrı kullanıcı kararı değildir:

1. **Tek kaynak `config/risk.yaml` → `risk_per_trade_pct`.** Kayıt defteri her portun risk girdisinin adını taşır
   (`StrategySpec.risk_param`: `risk_pct`, MTF'de `risk_percent`). `config/strategies.yaml` bu anahtarı taşıyamaz:
   değeri ne olursa olsun (Pine varsayılanı dahil) yükleme hatasıdır ve dosyayı okuyan her komut durur.
   **Risk tabanı da aynı kuralın parçasıdır:** planlanan risk hesabın başlangıç bakiyesinin yüzdesidir (SPEC §8.1),
   script'lerin `Risk base` girdisinin varsayılanı da odur (`Initial Capital`). Başka tabanda aynı yüzde başka bir
   risktir (sabit 200 000'in %0,25'i 100 000'lik hesabın %0,5'idir). `config/strategies.yaml` tabanı değiştirebilir
   (parite ve `--pine-risk` betiğin girdileriyle koşar), ama o zaman stratejinin karar koşusu yoktur: planlanan riskle
   koşu reddedilir (`propbot backtest` çıkış kodu 1, `propbot gonogo` 2), `propbot config check` bunu NOT olarak yazar.
   Kayıt defteri varsayılan tabanı `Initial Capital` olmayan bir portu kabul etmez.
2. **Hangi koşu hangi riskle:**

   | Koşu | İşlem başı risk | Etiket | Go/No-Go'daki yeri |
   |---|---|---|---|
   | `propbot backtest` (varsayılan) | planlanan risk (`risk.yaml`), betiğin risk girdisine yazılır | yalnızca kimlik | karar koşusu (öbür koşullar da tutuyorsa) |
   | `propbot backtest --pine-risk` | betiğin kendi değeri (Pine varsayılanı %0,5) | `-pinerisk` | karşılaştırma koşusu; karar vermez, eskisini gizlemez |
   | `propbot parity` | her zaman betiğin kendi değeri | parite artefaktı | G6, kendi beklenen parametre kümesiyle |
   | `propbot montecarlo` | `risk.yaml` (ya da `--risk-pct`) | — | G4: planlanan riske eşit olmalı |
   | Faz 1 sinyal mesajı (lot önerisi) | `risk.yaml` | — | — |

   Faz 3 kodu henüz yok; kullanıcı kararı canlı çalışmayı da kapsadığı için emir boyutu orada da SPEC §8.1 risk
   motorundan, aynı kaynaktan gelecektir.
3. **Sessiz düzeltme yok.** Planlanan riski betiğin girdisi kabul etmiyorsa (girdinin kendi alt sınırı: NY, Asian ve
   Master'da 0,1; öbürlerinde 0,01) koşu Türkçe bir hatayla durur; değer en yakın izinli değere çekilmez. Risk girdisi
   koşuya parametre değişikliği olarak da verilemez. Kod tavanının (0,5; SPEC §8.1) üstünde bir planlanan riski
   yalnızca config yükleyicisi değil, koşucu ve Go/No-Go raporlayıcısı da reddeder.
4. **Master:** planlanan risk iki motorun ortak `riskPct` girdisine bir kez yazılır; eşzamanlı risk tavanı
   (`maxConcurrentRiskPct`, 1,0) betiğin kendi girdisi olarak kalır ve özet bunu söyler.
5. **`--pine-risk`, planlanan risk betiğin riskine eşitken** (ve taban `Initial Capital` iken) risk bakımından
   karşılaştırılacak fark bırakmaz: koşu planlanan riskle yapılır, `-pinerisk` etiketi almaz ve ekrana bunu yazan bir
   NOT düşer (Go/No-Go etikete değil kayıtlı değere bakar). Başka bir seçenek (maliyetsiz, çarpan, `tv_ohlc`, başka
   OOS başlangıcı) koşuyu zaten karşılaştırma koşusu yapıyorsa NOT ona "karar koşusu" demez.
6. **Go/No-Go'nun denetledikleri** (`backtest/gonogo.py`; bilinmeyen hiçbir zaman GEÇTİ değildir):
   - Bir backtest'in **kayıtlı** risk girdisi planlanan riske eşit değilse o koşu karşılaştırma koşusudur
     (`--cost-factor` gibi bir "ya şöyle olsaydı"): karar vermez, daha eski bir karar koşusunu da gizlemez.
   - Kayıtlı risk okunamıyorsa (girdinin adı verilmemiş, alan yok ya da sayı değil) koşu doğrulanamaz: strateji
     DEĞERLENDİRİLEMEDİ, daha eski koşuya geçilmez.
   - Karar koşusunun beklenen parametreleri, koşucunun kayıt için kullandığı aynı fonksiyondan
     (`runner.effective_params`) planlanan riskle kurulur; ikisi ayrışamaz.
   - **G4 risk zinciri:** listenin arkasındaki her karar koşusu planlanan riskle boyutlanmış, liste o koşulardan
     (köken satırı, sayı, toplamlar, satır özetleri), simülasyon aynı riskte. Zincirin tamamı tutuyorsa kanıt cümlesi
     bunu tek yerde yazar; bir halka eksikse cümle yazılmaz ve G4 GEÇTİ olamaz.
   - **İşaretli işlem listesi:** betiğin kendi riskiyle yapılmış koşunun (`--pine-risk`) işlem listesi, satırlarının
     strateji kimliğinde ve köken satırında `-pinerisk` ekini taşır (`ny_orb_fvg-pinerisk`). G4 için işaret tek
     başına ret nedenidir (DEĞERLENDİRİLEMEDİ): hiçbir işlem yuvarlanıp atlanmadığında iki listenin satırları aynı
     çıkabilir ve satır özetleri onları ayıramaz.
   - **G6** parite raporunu **betiğin kendi riskiyle** kurulan ayrı bir beklenen kümeyle karşılaştırır; planlanan
     riskle yapılmış bir parite raporu ya da bu küme verilmemişse G6 DEĞERLENDİRİLEMEDİ. G6 karar kümesine hiç düşmez.
7. **`risk.yaml` değişirse** eski karar koşuları başka bir hesap için boyutlanmıştır: karşılaştırma koşusu olurlar;
   backtest, işlem listesi ve Monte Carlo yeniden üretilir (açık bulgu 81'deki kuralın aynısı).

## Sonuçlar

- **TradingView'ın betiğin %0,5'iyle gösterdiği sayılar ile Python karar koşularının %0,25'teki sayıları
  KARŞILAŞTIRILAMAZ.** Fark yalnızca ölçek değildir: boyut yuvarlaması, atlanan işlemler ve yüzde kesicileri başka
  işlemler üretir; üstüne karar koşusu kötümser dolum modeliyle yapılır ve R tanımı Pine tablosundan farklıdır
  (SPEC §6.4). Karşılaştırılabilir olan **parite koşusudur**: aynı barlar, `tv_ohlc`, betiğin kendi girdileri.
  `propbot backtest --fill-model tv_ohlc` `--pine-risk` olmadan koşulursa ve koşunun riski betiğinkinden farklıysa
  komut bunu bir NOT ile söyler.
- Parite (G6) portun betiği **betiğin girdilerinde** yeniden ürettiğini gösterir. Portun %0,25'te de betik gibi
  davrandığı ayrı bir TradingView dışa aktarımıyla kanıtlanmaz; aynı kod yolunun tek girdisi değişmiş hâline dayanır.
  %0,25 girdisiyle TradingView karşılaştırması yapan bir komut yoktur (`propbot parity` her zaman betiğin riskini
  kullanır); istenirse ayrı karar gerekir.
- 2026-11-19'dan önce betiğin %0,5'iyle yapılmış her backtest artık karşılaştırma koşusudur; onlardan üretilmiş işlem
  listesi ve Monte Carlo G4'ü geçemez. Yeniden üretilmeleri gerekir. Parite raporları etkilenmez (zaten betiğin
  riskindeydiler).
- Pine dosyaları değişmedi (Faz 0 kuralı: yalnızca B ve E maddeleri); betiklerin varsayılanı %0,5 kalır.
- Script'lerin kendi yüzde kesicileri (%2 günlük zarar vb.) %0,25'te daha çok kayıptan sonra tetiklenir; karar koşuları
  bu etkiyi içerir. Botun bağlayıcı günlük bütçesi yine SPEC §8.1'dir.
- Kod tavanı (0,5; SPEC §8.1, ADR-0003) ve değişmez kural 4 (kayıptan sonra risk artırma yok) aynen durur: planlanan
  risk tek sabit değerdir, koşu içinde değişmez.
- Bedeli: bir karar koşusunun parametre satırı artık "Pine varsayılanları (işlem başı risk dışında)" der; okuyan,
  farkın tek girdiden ibaret olduğunu özetin "İşlem başı risk" satırından görür.
