# Monte Carlo prop simülatörü — kullanım

`propbot montecarlo`, bir stratejinin işlem listesini gün bazında yeniden örnekleyip prop sınavını binlerce kez oynar ve
şunu söyler: bu işlem dağılımı, bu risk ve bu firma kurallarıyla sınavı geçme ve kural ihlal etme olasılığı nedir?
Fiyat verisi gerekmez; yalnızca R cinsinden işlem listesi ve venue profili gerekir (SPEC §6.5).

**Ne değildir:** backtest motoru değildir, strateji üretmez; listedeki işlemler ne kadar gerçekçiyse sonuç o kadar
gerçekçidir. Go/No-Go hükmü vermez (SPEC §9 G4 kararı senindir). Modelleme kararları: `docs/adr/0002`.

## Hızlı başlangıç

```bash
uv run propbot config check
uv run propbot montecarlo --trades tests/fixtures/trades/synthetic_trades.csv \
    --venue config/venues/ftmo_2step_100k.yaml --cost-r 0.05 --assume-min-trading-days 4
```

`tests/fixtures/trades/synthetic_trades.csv` **uydurma** bir listedir (`tests/support/synthetic.py` üretir); yalnızca
aracın çalıştığını gösterir. `--assume-min-trading-days`, profilde `min_trading_days` boş olduğu sürece zorunludur ve
raporda VARSAYIM olarak yazılır; değeri firmanın resmi sayfasından doğrulayıp profile yazınca bayrağa gerek kalmaz.

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
- TradingView "List of Trades" dışa aktarımını doğrudan okuyan dönüştürücü henüz yok (örnek dosya bekleniyor).

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
