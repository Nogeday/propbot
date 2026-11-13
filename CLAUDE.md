# PropBot — Claude Code proje talimatları

Kullanıcı: Oğuz · Ubuntu · Python. Sohbet ve `docs/` Türkçe. **Kod, tanımlayıcılar ve kod yorumları İngilizce.**
Kullanıcıya dönük metinler (Telegram mesajları, CLI çıktısı) Türkçe.
Ayrıntılı spesifikasyon `docs/SPEC.md`'de — bir faza başlamadan önce o fazın bölümünü ve §8'i (risk) oku.

## Amaç
`pine/` altındaki TradingView Pine v5 stratejilerini, sermayeyi ve prop kurallarını koruyarak adım adım otomatikleştirmek.

| Faz | Çıktı | SPEC |
|---|---|---|
| 0 | Pine hata düzeltmeleri (§4.1 B-maddeleri) + kural/varsayılan hizalaması (§4.3 E-maddeleri) + TradingView doğrulama listesi | §4 |
| 1 | TradingView → webhook → Telegram sinyal botu (emir YOK) | §5 |
| 2 | Python strateji motoru + maliyetli backtest + parite + Monte Carlo prop simülatörü | §6 |
| 3 | MT5 köprüsü, `paper` ve `approve` modları (yalnızca demo hesap) | §7 |
| 4 | Ücretli sınav — yalnızca §9 Go/No-Go kapısı PASS ve kullanıcı kararıyla | §9 |

Faz 2 ön dilimi (2026-11-13 kullanıcı kararı): proje iskeleti, config şemaları ve Monte Carlo prop simülatörü Faz 0
kabulü ve Faz 1 beklenmeden yapıldı (SPEC §6 başındaki not). Faz sırası bunun dışında değişmedi.

Öncelik sırası: (1) sermaye ve prop kuralı koruma → (2) Pine ile parite → (3) gözlemlenebilirlik → (4) özellik/hız.

## Çalışma şekli
- Her faz: önce plan modu → dosya listesi, riskler, açık sorular → kullanıcı onayı → uygulama.
- Faz bitince SPEC'teki "Kabul" maddelerini tek tek kanıtla raporla. "Faz N'e geç" denmeden sonraki faza geçme.
- Belirsizlikte dur ve sor. API uç noktası, kütüphane davranışı, MQL5 fonksiyonu veya prop kuralı **uydurma**; resmi dokümana bak.
- Mimari kararları `docs/adr/NNNN-baslik.md` olarak yaz (bağlam, seçenekler, karar, sonuçlar).
- Küçük, odaklı commit'ler (conventional commits). Gereksiz bağımlılık ekleme; sürümler `uv.lock` ile sabit.
- Testi geçirmek için testi gevşetme; hatayı düzelt. Testsiz risk/strateji kodu yok.

## Pine kuralları
- `pine/` dosyalarının kaynağı kullanıcının TradingView'daki güncel script'leridir; düzeltilen dosya TradingView'a
  geri yapıştırılır. Her dosya tek parça ve doğrudan yapıştırılabilir kalır.
- Pine v5'te kal; v6'ya yükseltme (davranış farkları sonuçları değiştirir, önce/sonra karşılaştırmasını bozar).
- Faz 0'da yalnızca SPEC §4.1'deki B-maddeleri ve §4.3'teki E-maddeleri (2026-11-12 kullanıcı kararı) değişir; bunların
  dışında giriş/çıkış mantığı ve girdi varsayılanları aynı kalır.
  Başka sorun görürsen düzeltme, `CHANGELOG.md`'de "Açık bulgular" altında listele.
- Pine'ı derleyemez ve çalıştıramazsın. Kullanıcının TradingView'da yapacağı kontrolleri
  `docs/tv-verification-checklist.md`'ye yaz; derleme hatalarını ve test sonuçlarını kullanıcıdan iste.

## Değişmez kurallar — botun KODUNDA zorunlu kılınır ve testlerle korunur
1. Modlar: `signal` (varsayılan, emir yok) → `paper` → `approve` (her emir Telegram onayı) → `auto`.
   `auto` için hepsi şart: config `live.enabled: true` + CLI `--i-accept-live-risk` + Telegram PIN onayı
   + son Go/No-Go raporu PASS + venue profilinde `rules_verified_at` ≤ 30 gün.
2. SL ve TP brokerda native emir olarak girişle aynı istekte gider. Açık pozisyonun stop'u asla kaldırılmaz, asla genişletilmez.
3. Risk motoru her emirden önce çalışır ve veto eder. Belirsizlikte fail-closed: yeni giriş yok + Telegram alarmı
   (veri gecikmesi, köprü kopukluğu, pozisyon uyuşmazlığı, saat sapması > 2 sn, kural dosyası eskimiş).
4. Martingale, grid, ortalama düşürme, kayıptan sonra risk artırma: yasak.
5. Idempotency: (`strategy_id`, `symbol`, `bar_time`, `action`) en fazla bir emir üretir.
6. Sırlar yalnızca `.env`'de (gitignore, chmod 600); asla loglanmaz. Broker/prop şifresi Python'a hiç girmez (yalnızca MT5 terminalinde).
7. Sinyaller yalnızca whitelist'teki tek `chat_id`'ye gider. Kamuya açık kanal, başkasına sinyal, kopya işlem servisi yok.
8. İstek bütçesi ≤ 300 sunucu isteği/gün/hesap; trail güncellemesi bar başına en fazla 1 ve ≥ `trail_min_step_ticks`.
9. Zaman: depolama UTC; seans mantığı `America/New_York` (zoneinfo); prop günü venue profilindeki reset saatine göre.
10. Prop kural değerleri koda gömülmez; yalnızca `config/venues/*.yaml` (VERIFY alanlarıyla).

## Mekan (venue) kararları
- Kullanıcı Türkiye'de: Topstep uygun değil → Topstep adaptörü yazma.
- Birincil: FTMO · MT5 · `US500.cash` (terminalde doğrula). İkincil: Funding Pips — firmadan yazılı otomasyon onayı gelene kadar devre dışı.
- Kod mekan-bağımsız: `MarketDataFeed` ve `ExecutionAdapter` protokolleri.
- ADR-0000 (`docs/adr/0000-yurutme-sinyali-venue-verisinde.md`): Yürütme sinyali, işlem yapılan mekanın kendi verisinde
  hesaplanır. TradingView yalnızca Faz 1 sinyali ve araştırma içindir; webhook yürütme yolunda kullanılmaz.

## Teknoloji
Python 3.12 · uv · ruff · mypy --strict (`src/`) · pytest + hypothesis · pydantic v2 + pydantic-settings · PyYAML ·
FastAPI + uvicorn (Faz 1) · python-telegram-bot (async) · SQLite (WAL) · pandas/numpy · exchange_calendars ·
structlog · MQL5 (Faz 3, ince köprü EA). Dağıtım: Ubuntu · systemd · Caddy (TLS) · ufw · chrony.

## Dizin yapısı
```
pine/                 Pine kaynakları (Faz 0 sonrası strateji mantığının referansı)
src/propbot/
  core/               Bar, Signal, OrderIntent, Fill, Position, AccountState; zaman/takvim yardımcıları
  strategies/         saf, deterministik, bar-bazlı durum makineleri (I/O, saat, env okumaz)
  risk/               venue profilleri, boyutlandırma, günlük/haftalık bütçe, kill switch, mod kilitleri
  execution/          ExecutionAdapter protokolü, paper, mt5_bridge
  data/               CSV/Parquet içe aktarma, doğrulama, canlı besleme arayüzü
  signals/            Faz 1 webhook alıcısı + şema
  notify/             Telegram
  backtest/           motor, dolum ve maliyet modeli, IS/OOS, walk-forward, Monte Carlo, raporlar
  store/              SQLite erişimi
mql5/                 köprü EA (Faz 3)
config/               strategies.yaml, risk.yaml, tv_alerts.yaml, venues/*.yaml
deploy/               systemd birimi, Caddyfile (Faz 1; adımlar docs/runbook.md)
tests/                unit/, property/, parity/, integration/
docs/                 SPEC.md, adr/, runbook.md, tv-verification-checklist.md
```

## Komutlar (oluşturuldukça güncelle)
```
uv sync
uv run pytest -q
uv run ruff check . && uv run mypy src
uv run propbot config check
uv run propbot montecarlo --trades <trades.csv> --venue config/venues/ftmo_2step_100k.yaml --cost-r 0.05
uv run propbot serve --mode signal                      # Faz 1: webhook → Telegram, emir yok (docs/runbook.md)
uv run propbot signals report --since 2026-12-01        # Faz 1 kabul kanıtı: sayılar + gecikme
# Henüz yok (kendi fazında):
uv run propbot backtest --strategy ny_orb_fvg --data data/<file>.parquet --oos-start 2025-01-01
```

## Yapma
- `.env` içeriğini okuma, yazdırma, loglama; yalnızca `.env.example`'ı güncelle.
- TradingView arayüzünü/tarayıcısını otomatize etme; site kazıma yok (haber takvimi dahil).
- `data/`, `*.db`, `reports/` çıktıları ve `.env` commit edilmez.
- Faz kapsamı dışına taşma; "ileride lazım olur" diye kod yazma.
