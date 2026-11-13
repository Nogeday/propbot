# ADR-0003 — Config doğrulaması: tavan aşımı yükleme hatasıdır

Durum: kabul edildi · 2026-11-13 · Karar: Oğuz (plan onayı) · İlgili: SPEC §8.1, §8.3, değişmez kurallar 1, 8, 10;
denetim Y1, Y7–Y9

## Bağlam

`config/risk.yaml` ve `config/venues/*.yaml` botun risk sınırlarını taşır, ama 2026-11-13 denetimine kadar onları
okuyan ve doğrulayan kod yoktu (Y1). `risk.yaml` başlığı "tavanlar kodda da zorunludur; bu dosyayı düzenlemek onları
aşamaz" der; SPEC §8.1 ise günlük bütçeyi `min(daily_cap_pct, 0,5 × daily_loss_limit_pct)` olarak tanımlar. Tavanı aşan
bir değerle ne yapılacağı açık değildi. Ayrıca YAML'ın sessiz tuzakları var: tırnaksız `22:30` sayı olur (1350),
yinelenen anahtarda sonuncusu kazanır, `"0.25"` metindir.

Kod: `src/propbot/risk/` (`config.py`, `venue.py`, `rules.py`, `loader.py`, `errors.py`). Komut: `propbot config check`.

## Seçenekler

1. **Sessiz kırpma:** tavanı aşan değer tavana çekilir. Bot çalışır, ama dosyada yazan ile uygulanan farklıdır; yazım
   hatası fark edilmez.
2. **Uyarı + kırpma:** aynı risk, log satırı kaçırılabilir.
3. **Yükleme hatası:** tavanı aşan, tipi yanlış ya da tanınmayan her şey botu başlatmaz.

## Karar

Seçenek 3. Ayrıntılar:

- **Katı tipler** (`strict=True`): `true` ya da `"0.25"` sayı alanına girmez; `5.0` tam sayı alanına girmez. Tam sayı,
  ondalık alana girebilir (`40` → `40.0`). `NaN` ve sonsuz reddedilir.
- **Bilinmeyen ve eksik anahtar hatadır** (`extra="forbid"`, bütün anahtarlar zorunlu). Varsayılanlar kodda değil
  YAML'dadır. Boş bırakılabilen anahtarlar da dosyada **bulunmalıdır**; silinen satır fark edilir.
- **Yinelenen YAML anahtarı hatadır** (PyYAML varsayılanı sessizce sonuncuyu alır).
- **Kod tavanları:** `risk_per_trade_pct ≤ 0,5` · `max_positions_per_strategy = 1` (SPEC §8.1 "strateji başına 1") ·
  `self_request_budget_per_day ≤ 300` (değişmez kural 8).
- **Dosyalar arası kurallar:** `daily_cap_pct ≤ 0,5 × daily_loss_limit_pct` · `daily_loss_limit_pct < max_loss_pct` ·
  `self_request_budget_per_day ≤ firm_request_threshold_per_day` (sonuncusu SPEC'te yazmıyordu; eklendi).
- **Çift katman:** yükleme hatasına rağmen `daily_budget_pct()` yine `min(…)` kullanır; kontrolü atlayan bir çağrı da
  tavanı aşamaz.
- **`null` yalnızca iki tür alanda:** VERIFY (`rules_verified_at`, `min_trading_days`, `server_tz`) ve çalışma anı
  (`contract.*`). `unresolved_fields()` bunları raporlar. `profit_target_pct.phase2` tek fazlı programda `null` olabilir.
- **Enum'lar:** `platform` (`mt5`), `max_loss_type` (`static`, `trailing_eod`), `daily_loss_basis`
  (`max_balance_equity_at_reset`, `balance_at_reset`). Yeni değer = kod değişikliği.
- **Saat ve tarih:** `daily_reset.time` tırnaklı `"HH:MM"`; saat dilimleri `zoneinfo` ile çözülmeli; `rules_verified_at`
  yalnızca tarih (`YYYY-AA-GG`).
- **Tazelik:** `rules_freshness()` → `missing` · `stale` (> 30 gün) · `future` (bugünden > 1 gün ileri) · `fresh`.
  Yalnızca `fresh`, `signal`/`paper` dışındaki modların kilidini açar. İleri tarih kilidi açamaz ve `config check`'i
  düşürür; 1 günlük pay kullanıcının yerel tarihi ile UTC tarihi arasındaki farktır.
- **Bütün sorunlar tek seferde** ve Türkçe bildirilir; ilk hatada durulmaz.

## Sonuçlar

- Config'e anahtar eklemek artık şema değişikliğidir (model + test). Bilinçli: `cost_buffer`, spread koruması ve strateji
  önceliği (denetim Y6, Y10) risk motoruyla birlikte, ayrı kararla eklenecek.
- Sıkılaştırma her zaman serbesttir (daha küçük risk, daha küçük günlük tavan); gevşetme tavanlarda durur. Tavanın
  kendisini değiştirmek kod + test + ADR ister.
- Her kuralın geçmesi ve **kalması** gereken örnek dosyası var: `tests/fixtures/config/{valid,invalid}/`.
- Yeni bağımlılık: PyYAML (YAML okumak için) ve tip tanımları `types-PyYAML` (yalnızca geliştirme).
- Mod kilitlerinin kendisi (`auto` için beş koşul) bu kararın kapsamında değil; risk motoruyla gelecek.
