# PropBot — Başlangıç

Bu klasör Claude Code'un çalışma alanı. Claude Code yalnızca açtığın klasörün içindeki dosyaları görür ve düzenler;
klasördeki `CLAUDE.md`'yi her oturumda kendi talimatı olarak okur. "Klasörde çalıştırmak" = Claude Code'a bu klasörü göstermek.

## Durum (2026-11-19)
- **Faz 0** (Pine düzeltmeleri) ve **Faz 1** (TradingView → Telegram sinyal botu, emir yok) kodu bitti; TradingView
  derlemesi ve 10 günlük kabul koşusu saha testi olarak sürüyor.
- **Faz 2** kodu bitti (kullanıcı kararıyla erken, ADR-0007): Python motoru, altı stratejinin portu, maliyetli backtest,
  parite altyapısı, Monte Carlo, Go/No-Go raporu ve yerel web paneli. Depoda henüz **gerçek veri ve TradingView dışa
  aktarımı yok**; testler sentetik barlarla yapıldı ve sentetik sonuçlar hiçbir şey kanıtlamaz. Faz 2'nin kabulü senin
  vereceğin dosyaları bekliyor (`CHANGELOG.md` Faz 2 bölümü). Bot hâlâ **hiç emir göndermez** (Faz 3 ayrı karar ister).
- **Faz 2 teslimatı 2026-11-19'da onaylandı**; altı kararın ve yapılanların kaydı `CHANGELOG.md` → "Kullanıcı kararları
  (2026-11-19)". En önemlisi (ADR-0010): canlı çalışma ve portföy düzeyindeki değerlendirmeler (karar backtest'leri,
  Monte Carlo, Go/No-Go) `config/risk.yaml`'daki **%0,25** işlem başı riskle yapılır; Pine script'lerindeki %0,5
  yalnızca tekil strateji varsayımıdır ve yalnızca TradingView paritesinde (ve `backtest --pine-risk` karşılaştırma
  koşusunda) kullanılır. Onay kodun teslimidir: parite ve veri kalitesi hâlâ kanıtlanmadı.

Komutlar (ayrıntı: `docs/runbook.md` Faz 1 ve panel, `docs/backtest.md` Faz 2):
```bash
uv run propbot config check
uv run propbot serve --mode signal          # sinyal botu (sunucuda systemd ile; runbook)
uv run propbot dashboard                    # yerel panel: http://127.0.0.1:8787/
uv run propbot data check --data data/<dosya>.csv --bar-tz NY+7
uv run propbot backtest --strategy all --data data/<dosya>.csv
uv run propbot gonogo --strategies ny_orb_fvg,asian_sweep   # Faz 2'de her zaman NO_GO
```

## 1. Stratejileri `pine/` klasörüne koy
TradingView → Pine Editor → script'i aç → `Ctrl+A`, `Ctrl+C` → Ubuntu'da **Metin Düzenleyici** → `Ctrl+V` →
`Ctrl+S` → bu klasördeki `pine/` → ad örn. `master_portfolio.pine`. Her strateji için tekrarla.
TradingView'daki **güncel** hâlleri kullan. Ayrıntı: `pine/README.md`.

## 2. Claude Code'u bu klasörde aç

**Masaüstü uygulaması (terminal gerekmez):**
1. Claude uygulaması → kenar çubuğunun üstündeki **Code** sekmesi.
2. Mesaj kutusunun altındaki satırda ilk düğme (ortam) → **Local** grubundaki ilk seçenek.
3. Yanındaki klasör düğmesi → **Open folder…** → bu klasörü (`propbot`) seç.
4. Aynı satırdaki izin modu → **Plan**.

**Terminal (alternatif):**
```bash
curl -fsSL https://claude.ai/install.sh | bash   # yalnızca ilk kurulumda; sonra yeni terminal aç
cd ~/Belgeler/propbot
claude
```
İlk açılışta tarayıcıdan giriş yap ve klasöre güvenmeyi onayla. Plan modu: alt satırda "plan mode" görünene kadar `Shift+Tab`.

Code sekmesi için Pro veya üstü abonelik gerekir. Git yüklü değilse: `sudo apt install git`.

## 3. Bu promptu yapıştır (Faz 0)
```
CLAUDE.md ve docs/SPEC.md'yi oku. Görev: SADECE Faz 0 (SPEC §4) — pine/ altındaki stratejileri düzelt.
1. Klasör git deposu değilse git init yap; mevcut hâli "baseline: TradingView originals" olarak commit'le.
   pine/ altındaki dosyaları listele.
2. Plan: her dosyada §4.1'deki B-maddelerinin yerini satır numarasıyla göster (§4.1a ön analizini doğrula; zaten
   çözülmüş olanları belirt). Her düzeltme için önce/sonra kod parçası öner, açık soruları sor ve onayımı bekle.
3. Kurallar: Pine v5'te kal; B-maddeleri dışında mantık veya varsayılan değiştirme; başka sorun görürsen yalnızca listele.
4. Onaydan sonra: her dosyayı ayrı commit'le; CHANGELOG.md ve docs/tv-verification-checklist.md yaz.
5. Sonunda TradingView'da sırayla ne yapacağımı yaz ve sonuçlarımı iste. Faz 1'e geçme.
```

## 4. Planı onayla ve değişiklikleri izle
- Plan gelince oku. Uygunsa onayla ve izin modunu **Manual** yap: her değişikliği diff olarak görüp tek tek onaylarsın.
- Yön değiştirmek için düz Türkçe yaz. Durdurmak: durdur düğmesi (uygulama) veya `Esc` (terminal).
- Sonra kaldığın yerden devam: uygulamada kenar çubuğundaki oturum; terminalde aynı klasörde `claude --continue`.

## 5. TradingView'a geri dön
1. Düzeltilmiş `.pine` dosyasını aç → tümünü kopyala.
2. TradingView'da **yeni** bir script'e yapıştır → "… v2" adıyla kaydet. Orijinalin üzerine yazma.
3. `docs/tv-verification-checklist.md`'deki kontrolleri yap. Derleme hatalarını (satır numarasıyla) ve önce/sonra
   tablosunu aynı Claude Code oturumuna yapıştır.

## Sonraki fazlar
Faz 0 kabul edildikten sonra:
```
SPEC §5'e göre Faz 1'e geç. Önce plan, onayımdan sonra uygula, bitince §5.7 kabul maddelerini kanıtla.
```
Faz 2 için `§6` / `§6.7` (kod hazır; gerçek veriyle adımlar `docs/backtest.md`), Faz 3 için `§7` / `§7.5`.
