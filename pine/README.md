# pine/

TradingView'daki **güncel** strateji script'leri (`.pine`). Kaynak her zaman TradingView'dır; Faz 0'dan sonra strateji
mantığının referansı bu dosyalardır (`docs/SPEC.md` §3) ve düzeltilen dosya TradingView'a geri yapıştırılır.

| Dosya | Strateji | Grafik | `strategy_id` (alert) | SPEC |
|---|---|---|---|---|
| `master_prop_firm_portfolio.pine` | Master: S1 NY ORB + S2 Asian Sweep, ortak günlük risk kalkanı | 1m | S1 `ny_orb_fvg` · S2 `asian_sweep` | §3.4.1 |
| `ny_open_fvg_strategy.pine` | NY Open ORB + FVG/IFVG | 1m | `ny_orb_fvg` | §3.2 |
| `asian_sweep_reversal_strategy.pine` | Asian Range Sweep & Reversal | 1m | `asian_sweep` | §3.3 |
| `ict_silver_bullet_strategy.pine` | ICT Silver Bullet (Sweep → MSS → FVG, limit giriş) | 1m / 5m | `ict_silver_bullet` | §3.4.2 |
| `mean_reversion_bb_kc_rsi2_strategy.pine` | BB/KC + RSI(2) mean reversion | 5m–15m | `mr_bb_kc_rsi2` | §3.4.3 |
| `mtf_trend_continuation_strategy.pine` | MTF trend continuation (5m / 1H) | 5m | `mtf_trend_cont` | §3.4.4 |

- Hepsi Pine v5, tek parça ve doğrudan yapıştırılabilir. v6'ya yükseltme yok.
- Varsayılanlar `CME_MINI:MES1!` test profilidir: komisyon kontrat başı $0,50 (taraf başı), slipaj 1 tick, başlangıç
  sermayesi 100.000, marjin %0. Başka enstrümanda Properties'ten değiştir (her dosyanın başlığındaki COSTS notu).
- Asian Sweep ve Master'ın S2 motoru ~23 saat işlem gören enstrüman ister (MES/ES, US500 CFD); SPY/SPX'te çalışmaz.
- TradingView'dan güncelleme: Pine Editor → script'i aç → `Ctrl+A`, `Ctrl+C` → Metin Düzenleyici → `Ctrl+V` → aynı adla
  bu klasöre kaydet.
- TradingView'da yapılacak kontroller: `docs/tv-verification-checklist.md`. Değişiklik geçmişi: `CHANGELOG.md`.
