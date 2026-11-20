# Monte Carlo prop simülatörü — kullanım

`propbot montecarlo`, bir stratejinin işlem listesini gün bazında yeniden örnekleyip prop sınavını binlerce kez oynar ve
şunu söyler: bu işlem dağılımı, bu risk ve bu firma kurallarıyla sınavı geçme ve kural ihlal etme olasılığı nedir?
Fiyat verisi gerekmez; yalnızca R cinsinden işlem listesi ve venue profili gerekir (SPEC §6.5).

**Ne değildir:** backtest motoru değildir, strateji üretmez; listedeki işlemler ne kadar gerçekçiyse sonuç o kadar
gerçekçidir. Go/No-Go hükmü vermez: G4'ü `propbot gonogo` bu komutun yazdığı artefakttan değerlendirir (SPEC §9.1,
`docs/backtest.md`). Modelleme kararları: `docs/adr/0002`.

## Hızlı başlangıç

```bash
uv run propbot config check
uv run propbot montecarlo --trades tests/fixtures/trades/synthetic_trades.csv \
    --venue config/venues/ftmo_2step_100k.yaml --cost-r 0.05
```

`tests/fixtures/trades/synthetic_trades.csv` **uydurma** bir listedir (`tests/support/synthetic.py` üretir); yalnızca
aracın çalıştığını gösterir. Komut raporu ekrana yazar ve ayrıca `reports/` altına bir `monte_carlo` artefaktı yazar
(`--no-artifact` ile yazmaz, `--reports-dir` ile başka klasör). Adında `synthetic` ya da `sentetik` geçen liste
sentetik işaretlenir. FTMO profilinde `min_trading_days` 2026-11-13'te doğrulandı (4), bu yüzden
`--assume-min-trading-days` gerekmez. Bayrak yalnızca bu alanı boş olan bir profille zorunludur ve raporda VARSAYIM
olarak yazılır.

## İşlem listesi (CSV)

Başlık satırı zorunludur. Ayırıcı virgül, ondalık ayırıcı **nokta**, kodlama UTF-8.

| Sütun | Zorunlu | Anlamı |
|---|---|---|
| `entry_time` | evet | Giriş zamanı, ISO 8601: `2026-01-05T14:36:00Z` ya da `2026-01-05T09:36:00-05:00` |
| `exit_time` | evet | Çıkış zamanı; girişten önce olamaz |
| `r` | evet | İşlemin sonucu, R cinsinden: `2.0` = riskin iki katı kazanç, `-1.0` = tam stop |
| `strategy_id` | hayır | Bilgi amaçlı (`ny_orb_fvg`, `asian_sweep`, …) |
| `mae_r` | hayır | İşlem sırasında görülen en kötü yüzen sonuç, R cinsinden; ≤ 0 ve ≤ `r`. Boş bırakılabilir |

```csv
entry_time,exit_time,r,strategy_id,mae_r
2026-01-05T14:36:00Z,2026-01-05T15:10:00Z,2.0,ny_orb_fvg,-0.4
2026-01-06T09:12:00Z,2026-01-06T10:02:00Z,-1.0,asian_sweep,
```

- Zaman damgasında UTC ofseti yoksa `--trades-tz America/New_York` gibi bir saat dilimi vermelisin; yoksa hata alırsın.
- Tanınmayan sütun, sayı olmayan `r`, girişten önce çıkış gibi her sorun satır numarasıyla bildirilir; liste ya tümüyle
  kabul edilir ya hiç.
- **R nasıl hesaplanır:** `r = işlemin net kâr/zararı ÷ o işlemde riske edilen tutar`. TradingView'da "Risk base =
  Initial Capital" ile nominal risk sabittir (ör. 100.000 × %0,5 = 500 $). Tam kontrata yuvarlama gerçek riski nominalin
  altına çeker; o zaman tam stop −1'den küçük çıkar (ör. −0,8). `mae_r` aynı tutara bölünmüş en kötü yüzen zarardır.
- **Liste hangi riskle üretildi:** R boyuttan bağımsız değildir (boyut tam birime yuvarlanır, asgari boyutun altı
  atlanır, script'lerin yüzde kesicileri başka sayıda kayıpta tetiklenir). Simülatör listeyi `--risk-pct` ile ölçekler
  ama listeyi **yeniden boyutlamaz**: Go/No-Go için liste, simülasyonla aynı riskte (`config/risk.yaml` →
  `risk_per_trade_pct`) boyutlanmış karar koşularından gelmelidir (2026-11-19 kullanıcı kararı, ADR-0010).
  `propbot backtest` varsayılanı budur. `--pine-risk` ile (betiğin %0,5'iyle, planlanan riskten farklıyken) üretilmiş
  liste satırlarının `strategy_id`'sinde ve köken satırında `-pinerisk` işaretini taşır (`ny_orb_fvg-pinerisk`);
  simülatör onu okur ve "Köken" satırında (artefakt notunda da) listenin planlanan hesabın işlemleri olmadığını, bu
  koşunun G4'ü GEÇTİ yapamayacağını yazar; G4 için işaret tek başına ret nedenidir (DEĞERLENDİRİLEMEDİ), satırlar
  karar koşusununkilerle birebir aynı olsa bile.
- Go/No-Go'da kullanılacak liste `propbot backtest --trades-out` ile üretilir (`docs/backtest.md` 4.5). TradingView
  "List of Trades" dışa aktarımını Monte Carlo listesine çeviren bir dönüştürücü yok (açık bulgu 26); o dışa aktarım
  yalnızca `propbot parity` ile kullanılır.
- **Hangi script sürümünden:** listeyi depodaki güncel `pine/` sürümünden (E12 dahil) ve maliyetler açıkken dışa aktar.
  E12 ile TP limitleri fiyat seviyeyi 1 tick geçince doluyor (`backtest_fill_limits_assumption = 1`); daha eski bir
  sürümden alınan liste "dokunma = dolum" sayar ve daha iyimserdir. Simülatör listenin hangi sürümden geldiğini bilemez.

### Köken satırı (`#propbot-trades`)

`propbot backtest`'in yazdığı işlem listesi, başlık satırından **önce** tek bir köken satırıyla başlar: liste hangi
koşudan, hangi veriden ve hangi stratejilerden geldi. Örnek (tek satır):

```text
#propbot-trades v=1 synthetic=false cost_free=false fill_model=pessimistic cost_factor=1.0 data_sha256=<64 onaltılık> data_file=us500_1m.csv strategy_ids=asian_sweep,ny_orb_fvg created_utc=2026-11-17T10:00:00Z
```

- Alanlar tam olarak bu sırayla ve tek boşlukla ayrılır; satırda başka alan, fazladan boşluk ya da sondaki boşluk
  olamaz. `synthetic`/`cost_free` yalnızca `true`/`false`; `fill_model` `pessimistic` ya da `tv_ohlc`; `cost_factor`
  Python'un kısa ondalık yazımı (`1.0`, `1.5`); `data_sha256` veri dosyasının sha256'sı (bellekte üretilen seride boş).
- `data_file` ve her strateji kimliği yüzde kodlamalıdır (UTF-8; harf, rakam ve `_ . - ~` dışındaki her karakter
  büyük harfli `%XX`): böylece boşluk, virgül, `=` ya da satır sonu içeren bir dosya adı satırı bozamaz. Strateji
  kimlikleri sıralı, tekrarsız ve virgülle ayrılır.
- Okuyucu satırı ayrıştırıp yeniden yazar ve birebir aynı olmasını ister: elle düzenlenmiş (sıra değişmiş, `1.00`,
  küçük harfli `%xx` …) ya da bozuk bir köken satırı **hatadır**, "köken yok" sayılmaz.
- Listedeki her işlemin `strategy_id`'si köken satırında adı geçen stratejilerden biri olmalıdır; tersine, köken
  satırında adı geçen her stratejinin listede **en az bir** işlemi olmalıdır. Satırda adı geçip tek işlemi olmayan
  strateji listeyi geçersiz kılar (satırlar silinmiş demektir); yazıcı da böyle bir liste yazmaz. Bu yüzden
  `backtest --trades-out`, bir bacağında tamamlanmış işlem olmayan stratejiyi listeye hiç almaz ve ekrana "NOT"
  satırı yazar.
- Yalnızca **ilk** satır köken satırı olabilir. `#` ile başlayan başka her satır eskisi gibi hatadır (ilk satırdaysa
  başlık sayılır ve zorunlu sütunlar eksik çıkar; daha aşağıdaysa geçersiz tarih satırıdır).

Köken satırı **olmayan** (elle hazırlanmış ya da başka araçtan gelen) liste simülatörde eskisi gibi çalışır, ama
Go/No-Go raporunda **G4'ü geçemez**: "işlem listesinin kaynağı bilinmiyor; listeyi propbot backtest üretmeli"
(DEĞERLENDİRİLEMEDİ). G4 ayrıca şunları ister (SPEC §9.1):
- liste sentetik değil (sentetikse KALDI); maliyetsiz değil; `pessimistic` dolum modeli ve 1,0 maliyet çarpanıyla
  üretilmiş;
- `strategy_ids` değerlendirilen strateji listesiyle (`gonogo --strategies`) aynı; `data_sha256` o stratejilerin karar
  koşularının verisiyle aynı ve her stratejinin bir karar koşusu var. Master birleşik kimliğiyle (`master_portfolio`)
  değerlendirilirken köken satırı ve satırlar yine bacak kimliklerini (`master_ny_orb_fvg`, `master_asian_sweep`)
  taşır; G4 bunları birleşik karar koşusunun bacak başına satır özetleriyle eşler (birleşik kimliğin kabulü
  2026-11-19 kullanıcı kararıdır; eşleme yolu uygulamanın seçimidir);
- **işlem sayısı:** Monte Carlo'nun simüle ettiği işlem sayısı, karar koşularının tamamlanmış işlem sayılarının
  toplamına eşit. Listeden satır silmek (ör. kaybedenleri) bu yüzden G4'ü geçemez; komut listenin tamamını simüle eder;
- **toplamlar:** artefakt listenin okunduğu hâliyle (`--cost-r` düşülmeden) R toplamını ve mae_r toplamını kaydeder;
  ikisi karar koşularının `stats_all.total_r` ve `mae_total_r` toplamlarına eşit olmalı. Satır sayısı aynı kalsa da R
  ya da mae_r değerleri yeniden yazılmış liste G4'ü geçemez. Tolerans yalnızca toplamaların kayan nokta yuvarlamasıdır:
  `propbot backtest` değerleri `repr()` ile (geri okunduğunda aynı sayı) yazar, CSV'nin kendi yuvarlaması yoktur;
- **işlem içi model:** yalnızca `--intratrade stop` (varsayılan, kötümser) G4'ü geçebilir; `closed` iyimserdir.
  Artefakt modeli kaydeder;
- **kurallar:** artefakt venue profilinin simülasyon anındaki `max_loss_type`, `daily_loss_basis` ve `daily_reset`
  (saat dilimi, saat) değerlerini de kaydeder; hepsi bugünkü profille aynı olmalı. Profil sonradan değiştiyse eski koşu
  DEĞERLENDİRİLEMEDİ olur. Simülatör yalnızca `static` toplam kaybı modeller (`trailing_eod` reddedilir); günlük kayıp
  bazının iki değeri tek modelle hesaplanır (ADR-0002 madde 7: stratejiler reset'te pozisyon taşımaz, bakiye =
  özsermaye) ve G4 kanıtı bunu yazar;
- sıra: liste en yeni karar koşusundan sonra, Monte Carlo koşusundan önce oluşturulmuş;
- koşu planlanan riskle (`risk.yaml` → `risk_per_trade_pct`), venue profilinin kurallarıyla, `--dd-risk-steps` ve
  `--assume-min-trading-days` olmadan, en az 5000 yol ve 100 işlemle yapılmış (bu iki alt sınır mühendislik seçimidir;
  kullanıcı 2026-11-19'da onayladı);
- **risk zinciri (2026-11-19, ADR-0010):** listenin arkasındaki her karar koşusu da planlanan riskle boyutlanmış
  (kayıtlı risk girdisi `risk.yaml` değerine eşit). Zincir tutuyorsa G4 kanıtı "risk zinciri: karar koşuları planlanan
  riskle … boyutlandırıldı, işlem listesi o koşulardan, simülasyon aynı riskte" cümlesini taşır; bir halka eksikse
  cümle yazılmaz ve G4 GEÇTİ olamaz. Köken satırında `-pinerisk` işaretli kimlik varsa (betiğin kendi riskiyle
  yapılmış koşunun listesi) bu tek başına ret nedenidir: satır karşılaştırmasının sonucu ne olursa olsun G4 GEÇTİ
  olamaz.

- tekrar üretilebilir koşu (2026-11-19): `--seed` varsayılan (`20261113`), `--paths` karar değeri (10 000 ya da
  config'teki daha sıkı `min_mc_paths`), `--max-days` varsayılan (1000), `--skip-prob` 0. Bunlar firma kuralı değildir
  ama sonucu değiştirir: 5000 yolda P(geçme)'nin standart hatası ≈ 0,7 puan, 40 tohumun en iyisi tipik olandan ≈ 1,2
  puan yukarıda; kısa ufuk ihlalleri "sonuçlanmadı"ya iter. Başka değerlerle koşmak serbesttir ama G4'ü geçemez;
- listenin satırları değiştirilmemiş: artefakt strateji başına satırların (giriş/çıkış zamanı, R, mae_r) sıralı sha256
  özetini kaydeder ve karar koşusunun kendi özetiyle aynı olmalı. Zamanı kaydırılmış bir satır sayıyı ve toplamları
  korur ama günlere dağılımı (günlük kayıp ihlalleri) değiştirir.

FTMO programında süre sınırı olup olmadığı resmi sayfadan doğrulanmalı (açık bulgu 78); varsayılan 1000 piyasa günü
bir mühendislik seçimidir. Listenin R değerlerinin maliyet sonrası olduğu (`--r-is-net`) G4'te elle teyit maddesidir. Köken satırı
listenin maliyetsiz bir koşudan geldiğini söylüyorsa rapor "MALİYETSİZ işlem listesi" der, "maliyet sonrası" ifadesini
kullanmaz ve `--r-is-net` yok sayılır (köken satırı bayraktan üstündür).

Bu alanları taşımayan eski `monte_carlo` artefaktları yine yüklenir ve panelde görünür; G4 onlar için "eski rapor"
cümlesiyle DEĞERLENDİRİLEMEDİ olur, asla GEÇTİ olmaz.

## Seçenekler

| Bayrak | Varsayılan | Anlamı |
|---|---|---|
| `--trades`, `--venue` | — | İşlem listesi ve venue profili (zorunlu) |
| `--risk-config` | `config/risk.yaml` | `risk_per_trade_pct` buradan okunur; çapraz kurallar denetlenir |
| `--risk-pct` | `risk.yaml` | İşlem başı risk, başlangıç bakiyesinin %'si. 0,5'i aşamaz (SPEC §8.1) |
| `--cost-r` | `0` | Her işlemden düşülen maliyet, R cinsinden |
| `--r-is-net` | kapalı | Listedeki R değerleri maliyet dahil; "maliyetsiz koşu" uyarısını kaldırır |
| `--skip-prob` | `0` | Her işlemin bağımsız olarak atlanma olasılığı (kaçan sinyal, veto) |
| `--assume-min-trading-days` | — | Profilde `min_trading_days` boşken kullanılacak varsayım |
| `--intratrade` | `stop` | `stop`: işlem stop'una kadar aleyhe gitmiş sayılır (kötümser). `closed`: yalnızca kapanışlar (iyimser) |
| `--dd-risk-steps` | — | Deneysel: `3:0.5,6:0.25` = bakiye %3 aşağıdayken risk ×0,5, %6 aşağıdayken ×0,25. Yalnızca küçültür |
| `--paths` | `10000` | Simüle edilen yol sayısı |
| `--seed` | `20261113` | Rastgele tohum; aynı tohum aynı raporu verir |
| `--max-days` | `1000` | Sınav ufku, piyasa günü. Dolunca yol "sonuçlanmadı" sayılır |
| `--dd-days` | `250` | Maks. düşüşün ölçüldüğü pencere, piyasa günü |
| `--trades-tz` | — | Ofsetsiz zaman damgalarının saat dilimi |
| `--reports-dir` | `reports` | `monte_carlo` artefaktının yazılacağı klasör |
| `--no-artifact` | kapalı | Artefakt yazma; rapor yalnızca ekrana |

Çıkış kodu: 0 başarılı · 1 geçersiz config ya da girdi · 2 kullanım hatası.

## Model (özet)

1. İşlemler venue'nun reset saat dilimine göre **prop günlerine** ayrılır. Havuz, ilk ve son işlem arasındaki bütün
   hafta içi günlerdir; işlem olmayan günler de havuzdadır.
2. Her yol, her piyasa günü için havuzdan yerine koyarak bir gün çeker ve o günün işlemlerini zaman sırasıyla oynar.
3. İşlemin kâr/zararı `r × risk %` kadardır; risk tabanı başlangıç bakiyesidir, bileşik değildir.
4. Her giriş ve çıkıştan sonra olası en düşük özsermaye hesaplanır: bakiye + açık işlemlerin en kötü yüzen sonucu.
   Günlük tabana (gün başı bakiye − günlük limit) ya da toplam tabana (−toplam limit) **değmek** ihlaldir.
5. Hedef yalnızca açık pozisyon yokken sayılır. Minimum işlem günü eksikse yol, kalan günleri risksiz tamamlar.
6. Sonraki faz ertesi gün başlangıç bakiyesiyle başlar.
7. Maks. düşüş ayrıca, kural durdurması olmadan sabit bir pencerede ölçülür.

## Raporu okumak

- **Girdi:** işlem ve gün sayıları, maliyet sonrası ortalama R, standart sapma, kazanma oranı ve beklentinin
  t-istatistiği. t < 2,0 ise rapor "örneklem zayıf" uyarısı verir (SPEC §9 G2).
- **Uyarılar:** kurallar doğrulanmamış ya da bayat · `min_trading_days` varsayım · maliyetsiz koşu · iyimser işlem içi
  model · reset'i aşan işlem · deneysel risk kademesi · zayıf örneklem. Uyarı varken sonuç ön bilgidir.
- **Sonuçlar:** faz 1'i geçme, iki fazı geçme, ihlal (faz × neden) ve ufukta sonuçlanmayan yolların oranı. "%95 GA"
  yalnızca simülasyon hatasıdır (yol sayısı arttıkça küçülür); listenin kendi belirsizliğini içermez.
- **Geçme süresi:** geçen yollar için piyasa günü (işlem olmayan günler dahil). Takvim haftasına çevirmek için 5'e böl.
- **Maks. düşüş:** medyan, %95 ve %99 dilimleri ve beklenen kuyruk kaybı (ES: en kötü %5 / %1 yolun ortalaması).
  Başlangıç bakiyesinin yüzdesidir ve sınav kuralları uygulanmadan ölçülür; toplam kayıp limitiyle karşılaştır.

Aynı liste üzerinde seçenekleri karşılaştırırken tohumu değiştirme: yollar aynı günleri çeker, fark yalnızca
değiştirdiğin seçenekten gelir.

## Sınırlar

- Günler birbirinden bağımsız çekilir; ardışık kötü günlerin birbirini izlemesi (rejim) listede olduğundan daha
  seyrek görünebilir.
- Botun kendi günlük bütçesi, haftalık tavanı, ardışık kayıp duraklaması ve düzleştirme eşiği modellenmez.
- Lot adımına yuvarlama, kaldıraç sınırı, spread'in gün içi değişimi ve haber kuralı modellenmez.
- Gün sonu trailing toplam kayıp (`max_loss_type: trailing_eod`) desteklenmez; komut açık hata verir.
- Reset'i aşan işlemler için günlük kayıp kuralı yaklaşıktır.
- Sonuç, listenin temsil ettiği dönem kadar geçerlidir: maliyetsiz, optimize edilmiş ya da kısa bir backtest'ten gelen
  liste iyimser olasılık üretir.
