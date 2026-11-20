# Parite ayrıştırıcısı için örnek dosyalar

Bu klasördeki `sample_*` dosyalarının hepsi **elle yazılmıştır**. TradingView'dan alınmış dışa aktarımlar
değildir ve **TradingView kanıtı değildir** (ADR-0007, madde 4). Yalnızca
`propbot.backtest.parity.load_tradingview_trades` ayrıştırıcısının test girdileridir: ayırıcılar (virgül,
noktalı virgül, sekme), ondalık virgül, BOM, sütun adı varyantları, azalan sıra, açık işlem, iki biçimde
kısmi çıkış, bozuk satırlar, eksik sütun, yön çelişkisi ve ABD yaz saati geçişleri.

Sütun adları ve satır düzeni hatırlanan "List of Trades" biçimine göre yazıldı; gerçek biçim bu ortamdan
doğrulanamadı (VERIFY). Gerçek bir dışa aktarım geldiğinde bu dosyalar değil, o dosya parite kanıtıdır.
Adı `sample_` ile başlayan bir dosyayla üretilen parite raporu otomatik olarak "SENTETİK VERİ" uyarısı taşır.
