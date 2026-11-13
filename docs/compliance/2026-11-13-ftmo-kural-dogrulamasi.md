# FTMO 2-Step kural doğrulaması — 2026-11-13

**Kapsam:** `config/venues/ftmo_2step_100k.yaml` (SPEC §8.3, Go/No-Go G7).
**Yapan:** Claude Code, kullanıcının 2026-11-13 tarihli yetki devriyle ("kullanıcı doğrulaması" şartı için).
**Yöntem:** FTMO'nun herkese açık resmi sayfaları 2026-11-13'te birer kez indirildi ve metinleri okundu. Aşağıdaki
ifadeler özettir, alıntı değildir; bağlayıcı olan sayfanın kendisidir.
**Geçerlilik:** `rules_verified_at: 2026-11-13` → 30 gün (2026-12-13'e kadar; CLAUDE.md değişmez kural 1).

## 1. Profil alanları

| Alan | Değer | Resmi kaynak ne diyor | Kaynak |
|---|---|---|---|
| `profit_target_pct` | 10 / 5 | Hedef başlangıç sermayesinin yüzdesidir: Challenge %10, Verification %5. Bakiye hedefi aştığında ve **tüm pozisyonlar kapalıyken** sağlanır. Fonlu hesapta hedef yok. | [1] |
| `daily_loss_limit_pct` | 5.0 | Günlük kayıp tutarı başlangıç sermayesinin %5'i. | [1] |
| `daily_reset` | `Europe/Prague`, `"00:00"` | Limit her gün 00:00 CE(S)T'de yeniden hesaplanır; Ekim 2026 duyuruları bunu açıkça "Prag saati" olarak adlandırıyor. | [1] [7] |
| `daily_loss_basis` | `balance_at_reset` | Limit = 00:00 CE(S)T'de kaydedilen **bakiye** − başlangıcın %5'i (ilk gün başlangıç sermayesi). İhlal özsermayeyle ölçülür: bakiye + açık pozisyon K/Z ± swap − komisyon limitin altına inerse. | [1] |
| `max_loss_pct`, `max_loss_type` | 10.0, `static` | Limit = başlangıç sermayesi − %10; sabittir, özsermayeyle ölçülür. | [1] |
| `min_trading_days` | 4 | En az 4 işlem günü. İşlem günü: 00:00:00–23:59:59 CE(S)T arasında en az bir pozisyonun **açıldığı** gün (kapanış sayılmaz). İki fazda da geçerli; fonlu hesapta yok. | [1] |
| `news_rule` | `funded_standard`, 2 dk | Seçili haberlerin 2 dk öncesi ve 2 dk sonrasında emir **yürütme** yasak; SL, TP ve bekleyen emir dolumu dahil. Swing tipinde ve Challenge/Verification'da uygulanmaz. | [3] |
| `automation_allowed` | `true` | Algoritmik işlem ve EA serbest; yasak uygulamalara girmemek kaydıyla. | [4] |
| `firm_request_threshold_per_day` | 2000 | EA'nın günde 2.000'den fazla sunucu isteği üretmesi "hiperaktif" sayılır ve yasaktır. | [2] |
| `symbol` | `US500.cash` | Ad resmi sitede bu biçimde geçiyor. Terminalde (Market Watch) ayrıca doğrulanacak. | [3] |
| `server_tz` | `null` | MT4/MT5 sunucu saati "GMT+2 +DST". Duyurulara göre geçiş **ABD** yaz saati takvimini izliyor (2026'da 2 Kasım'a kadar GMT+3, sonra GMT+2) → New York + 7 saat. Tek bir IANA dilimi bunu karşılamadığı için alan boş kaldı. | [5] [7] |
| `initial_balance`, `account_currency` | 100000, USD | Kural değil, hesap seçimi. Sayfadaki örnekler 100.000 $ hesap üzerinden. | [1] |
| `contract.*` | `null` | Çalışma anında terminalden okunur (SPEC §8.3); doğrulama konusu değil. | — |

`US500.cash`'i etkileyen kısıtlı ABD haberleri [3]: Federal Funds Rate & Statement · Non-Farm Employment Change ·
Unemployment Rate & Wages · Advance GDP q/q · FOMC Meeting Minutes · CPI y/y. Güncel liste FTMO'nun ekonomik takviminde
"Restricted event" notuyla işaretlenir.

## 2. Profilde alanı olmayan kurallar

Bunlar resmi sayfalarda var ama venue şemasında karşılığı yok; risk motoru ve strateji ayarları yazılırken karar ister
(CHANGELOG açık bulgu 28–30).

1. **"Gap trading" yasağı — bütün aşamalarda** [2]: (i) ilgili piyasayı etkileyebilecek büyük haber, makro olay ya da
   şirket raporu planlanmışken, **veya** (ii) ilgili piyasanın en az 2 saat kapalı kalacağı bir kapanıştan 2 saat ya da
   daha az önce pozisyon açmak. Sonuç: cuma günü haftalık kapanıştan (ve tatil öncesi erken kapanıştan) önceki son
   2 saatte yeni giriş olmamalı. `US500.cash` seans saatleri terminalden okunmalı.
2. **Haber penceresinde stop/hedef dolumu da ihlaldir — fonlu Standard hesapta** [3]: pencereden 2 dakikadan fazla önce
   açılan pozisyon taşınabilir, ama SL ya da TP pencerede dolarsa kural ihlal edilmiş sayılabilir. Fonlu aşamada
   kısıtlı haberden önce düzleştirmek ya da Swing tipi seçmek gerekir.
3. **2 saatten uzun piyasa aralarında pozisyon taşıma yasağı — fonlu Standard hesapta** [6]: ara başlamadan kapat.
   Challenge/Verification ve Swing hariç.
4. **Platform sınırları** [4]: aynı anda 200 emir, günde 2.000 pozisyon; sunucu mesajı (emir ve emir değişikliği) kabulü
   sınırlı.
5. **Diğer yasaklar** [2]: hizmet hatalarının (fiyat gösterimi, gecikme) istismarı; hesaplar arası karşıt pozisyon;
   manipülasyon ya da haksız avantaj sağlayabilecek yazılım, yapay zekâ ve çok yüksek hızlı araçlar; hesabı üçüncü kişiye
   kullandırmak. Risk yönetimi kuralları: diğer işlemlere göre belirgin biçimde büyük pozisyon, aynı fikirde biriken risk.
6. **Hesap tipi** [5] [8]: Standard kaldıraç 1:100'e kadar; Swing 1:30, yalnızca 2-Step'te ve sipariş sırasında
   seçilir. Standard → Swing değişimi yapılamaz.
7. **1-Step (bilgi)** [1] [10]: hedef %10, günlük kayıp %3, toplam %10 gün sonu trailing; 12 Ekim 2026'dan sonraki
   siparişlerde "Best Day" yerine "Consistency" (en iyi gün toplam kârın %50'sini aşamaz). 2-Step'i etkilemez.
8. **Uygunluk** [9]: Türkiye, ikamete dayalı kısıtlı ülkeler listesinde yok (2026-11-13).

## 3. Sınırlar

- Yalnızca herkese açık sayfalar okundu. Hesap satın alındığında Client Area'daki hesap koşulları ve kabul edilen
  sözleşme metniyle karşılaştır; fark varsa hesabınki geçerlidir.
- Şartlar ve Koşullar [11] yasak uygulamalar ve işlem hedefleri için web sitesine gönderme yapıyor (madde 7.3 ve
  tanımlar); yani yukarıdaki sayfalar bağlayıcı listedir ve FTMO bunları güncelleyebilir.
- Aynı belgenin 9.4.6 maddesi siteden **otomatik araçlarla** bilgi görüntülemeyi ve toplamayı yasaklıyor. Bu doğrulamada
  sayfalar birer kez indirildi; hesap açıldıktan sonra bu yöntem kullanılmamalı. Yenilemeler tarayıcıda elle okunarak
  yapılır; bot FTMO sayfalarını hiçbir zaman otomatik çekmez (CLAUDE.md: site kazıma yok).
- `server_tz` ve `symbol` terminalde doğrulanacak (SPEC §6.1: bilinen bir 09:30 NY mumuyla karşılaştır).

## 4. Yenileme (30 günde bir)

1. [1]–[6] sayfalarını tarayıcıda aç ve bölüm 1'deki tabloyla karşılaştır; "Trading Updates" sayfasında son 30 günün
   duyurularına bak.
2. Fark yoksa `rules_verified_at` tarihini güncelle ve bu klasöre yeni tarihli kısa bir kayıt ekle.
3. Fark varsa profili düzelt, `uv run propbot config check` çalıştır, Monte Carlo'yu yeni kurallarla yeniden koş.

## Kaynaklar

- [1] https://ftmo.com/en/trading-objectives/
- [2] https://ftmo.com/en/forbidden-trading-practices/
- [3] https://ftmo.com/en/faq/can-i-trade-news/
- [4] https://ftmo.com/en/faq/which-instruments-can-i-trade-and-what-strategies-am-i-allowed-to-use/
- [5] https://ftmo.com/en/faq/what-are-the-account-specifications/
- [6] https://ftmo.com/en/faq/can-i-hold-positions-overnight-or-over-the-weekend/
- [7] https://ftmo.com/en/blog/trading-updates/trading-update-22-oct-2026/ ·
  https://ftmo.com/en/blog/trading-updates/trading-update-29-oct-2026/ ·
  https://ftmo.com/en/blog/trading-updates/trading-update-12-nov-2026/
- [8] https://ftmo.com/en/faq/what-is-the-swing-account-type-and-how-does-it-work/
- [9] https://ftmo.com/en/faq/who-can-join-ftmo/
- [10] https://ftmo.com/en/blog/trading-updates/trading-update-8-oct-2026/
- [11] FTMO Challenge Terms and Conditions, 12 Ekim 2026:
  https://cdn.ftmo.com/docs/terms-and-conditions/b1d80f4da76c4bd4bcd42a5807727e34
