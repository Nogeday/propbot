# Altın dosyalar — Python motorunun kendine karşı regresyon sabitleri

> **SENTETİK VERİ. Bu dosyalar TradingView kanıtı DEĞİLDİR** (ADR-0007 madde 4, SPEC §6.6).
> Hiçbir stratejinin kârlı, doğru ya da Pine ile eşleşik olduğunu göstermez; Go/No-Go kapısında
> (SPEC §9) hiçbir kriterin kanıtı olarak kullanılamaz.

## Bunlar nedir

Her dosya, Python backtest motorunun bir koşusunun işlem listesidir:

- **Strateji:** `propbot.strategies.registry.STRATEGIES` içindeki her strateji, Pine varsayılan
  parametreleriyle (`config/strategies.yaml` geçersiz kılmaları UYGULANMAZ).
- **Veri:** depodaki iki sentetik fikstür, `tests/fixtures/ohlcv/synthetic_5m.csv` (20 seans) ve
  `tests/fixtures/ohlcv/synthetic_1m.csv` (3 seans). İkisi de `#propbot-synthetic` işaretlidir;
  gerçek piyasa değildir.
- **Dolum modeli:** `pessimistic` (varsayılan) ve `tv_ohlc` (TradingView aracı benzetimi).
- **Enstrüman ve maliyet:** `config/backtest.yaml` varsayılan enstrümanı (MES) ve onun maliyetleri;
  başlangıç sermayesi 100.000 (Pine `initial_capital`).

Dosya adı: `<strategy_id>__<fikstür adı>__<dolum modeli>.csv`. İlk satır
`# python-engine regression pin, NOT TradingView evidence:` işaretiyle başlar ve koşunun tamamını
(strateji, fikstür ve SHA-256'sı, dolum modeli, enstrüman, maliyetler, sermaye) yazar. Satır başına
bir çıkış dolumu vardır (kısmi çıkışlar ayrı satırdır); fiyatlar ve sayılar 6 ondalığa yuvarlanır.

## Ne işe yarar, ne işe yaramaz

- **Yarar:** motor ya da bir strateji portu değiştiğinde işlem listesinin değiştiğini görünür kılar.
  `tests/parity/test_golden.py` her koşuyu yeniden üretir ve dosyayla karşılaştırır.
- **Yaramaz:** Python'un Pine ile aynı işlemleri ürettiğini göstermez. Bu dosyalar Python'u yalnızca
  kendisiyle karşılaştırır; motorda bir hata varsa o hata da buraya sabitlenmiştir.
- **Gerçek parite** için TradingView'dan dışa aktarılmış bir "List of Trades" dosyası gerekir
  (`docs/tv-verification-checklist.md`); karşılaştırma `propbot.backtest.parity.compare_trades` ile
  ve gerçek veri üzerinde yapılır.

## Dosyalar

İşlem sayıları aşağıdaki tabloda; tabloyu yeniden üretme komutu yazar, elle düzenlemeyin. Bir test
tablonun dosyalarla aynı olduğunu denetler.

<!-- golden-table:start (support.golden yazar; elle düzenlemeyin) -->
| Dosya | Strateji | Fikstür | Dolum modeli | Satır (çıkış dolumu) |
|---|---|---|---|---:|
| `asian_sweep__synthetic_5m__pessimistic.csv` | `asian_sweep` | `synthetic_5m.csv` | `pessimistic` | 19 |
| `asian_sweep__synthetic_5m__tv_ohlc.csv` | `asian_sweep` | `synthetic_5m.csv` | `tv_ohlc` | 19 |
| `asian_sweep__synthetic_1m__pessimistic.csv` | `asian_sweep` | `synthetic_1m.csv` | `pessimistic` | 2 |
| `asian_sweep__synthetic_1m__tv_ohlc.csv` | `asian_sweep` | `synthetic_1m.csv` | `tv_ohlc` | 2 |
| `ict_silver_bullet__synthetic_5m__pessimistic.csv` | `ict_silver_bullet` | `synthetic_5m.csv` | `pessimistic` | 4 |
| `ict_silver_bullet__synthetic_5m__tv_ohlc.csv` | `ict_silver_bullet` | `synthetic_5m.csv` | `tv_ohlc` | 4 |
| `ict_silver_bullet__synthetic_1m__pessimistic.csv` | `ict_silver_bullet` | `synthetic_1m.csv` | `pessimistic` | 2 |
| `ict_silver_bullet__synthetic_1m__tv_ohlc.csv` | `ict_silver_bullet` | `synthetic_1m.csv` | `tv_ohlc` | 2 |
| `master_portfolio__synthetic_5m__pessimistic.csv` | `master_portfolio` | `synthetic_5m.csv` | `pessimistic` | 39 |
| `master_portfolio__synthetic_5m__tv_ohlc.csv` | `master_portfolio` | `synthetic_5m.csv` | `tv_ohlc` | 39 |
| `master_portfolio__synthetic_1m__pessimistic.csv` | `master_portfolio` | `synthetic_1m.csv` | `pessimistic` | 5 |
| `master_portfolio__synthetic_1m__tv_ohlc.csv` | `master_portfolio` | `synthetic_1m.csv` | `tv_ohlc` | 5 |
| `mr_bb_kc_rsi2__synthetic_5m__pessimistic.csv` | `mr_bb_kc_rsi2` | `synthetic_5m.csv` | `pessimistic` | 6 |
| `mr_bb_kc_rsi2__synthetic_5m__tv_ohlc.csv` | `mr_bb_kc_rsi2` | `synthetic_5m.csv` | `tv_ohlc` | 6 |
| `mr_bb_kc_rsi2__synthetic_1m__pessimistic.csv` | `mr_bb_kc_rsi2` | `synthetic_1m.csv` | `pessimistic` | 1 |
| `mr_bb_kc_rsi2__synthetic_1m__tv_ohlc.csv` | `mr_bb_kc_rsi2` | `synthetic_1m.csv` | `tv_ohlc` | 1 |
| `mtf_trend_cont__synthetic_5m__pessimistic.csv` | `mtf_trend_cont` | `synthetic_5m.csv` | `pessimistic` | 32 |
| `mtf_trend_cont__synthetic_5m__tv_ohlc.csv` | `mtf_trend_cont` | `synthetic_5m.csv` | `tv_ohlc` | 32 |
| `mtf_trend_cont__synthetic_1m__pessimistic.csv` | `mtf_trend_cont` | `synthetic_1m.csv` | `pessimistic` | 5 |
| `mtf_trend_cont__synthetic_1m__tv_ohlc.csv` | `mtf_trend_cont` | `synthetic_1m.csv` | `tv_ohlc` | 5 |
| `ny_orb_fvg__synthetic_5m__pessimistic.csv` | `ny_orb_fvg` | `synthetic_5m.csv` | `pessimistic` | 20 |
| `ny_orb_fvg__synthetic_5m__tv_ohlc.csv` | `ny_orb_fvg` | `synthetic_5m.csv` | `tv_ohlc` | 20 |
| `ny_orb_fvg__synthetic_1m__pessimistic.csv` | `ny_orb_fvg` | `synthetic_1m.csv` | `pessimistic` | 3 |
| `ny_orb_fvg__synthetic_1m__tv_ohlc.csv` | `ny_orb_fvg` | `synthetic_1m.csv` | `tv_ohlc` | 3 |
<!-- golden-table:end -->

## Bilinçli yeniden üretme

Normal bir test koşusu bu klasöre hiçbir şey yazmaz. Bir değişikliğin işlem listesini değiştirmesi
**amaçlanmışsa** (ör. Pine'a göre düzeltilmiş bir port), farkı inceledikten sonra depo kökünden:

```
PYTHONPATH=tests .venv/bin/python -m support.golden
```

Komut tüm dosyaları yeniden yazar, artık beklenmeyen dosyaları siler, tabloyu günceller ve her dosyanın
satır sayısını basar. Ardından `git diff tests/parity/golden/` ile değişen işlemleri tek tek inceleyin
ve commit mesajında nedenini yazın. Farkın nedeni açıklanamıyorsa yeniden üretmeyin; hatayı düzeltin.
