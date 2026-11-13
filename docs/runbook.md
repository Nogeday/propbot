# Runbook — Faz 1 sinyal botu (TradingView → webhook → Telegram)

Bot **emir göndermez**: TradingView alert'lerini doğrular, boyut önerisi ekler ve tek bir Telegram sohbetine iletir.
Tasarım kararları: `docs/adr/0000`, `docs/adr/0004`. Spesifikasyon: `docs/SPEC.md` §5.

İçindekiler: 1 Ön koşullar · 2 Kurulum · 3 Sırlar ve config · 4 Servis · 5 Seçenek A (DuckDNS + Caddy) ·
6 Seçenek B (kendi bilgisayarın + tünel) · 7 İstemci IP doğrulaması · 8 TradingView alarmları · 9 Duman testi ·
10 Günlük işletim · 11 Sorun giderme · 12 Kabul testi (SPEC §5.7)

> **Doğrulama durumu.** Caddy, Tailscale ve Cloudflare ile ilgili maddeler 2026-11-13'te projelerin resmi GitHub
> depolarından (doküman kaynağı ve kaynak kod), `ufw` ve `chronyc` sözdizimi Ubuntu man sayfalarından okundu.
> **(VERIFY)** ile işaretli maddeler bu oturumdan erişilemeyen resmi sayfalara dayanır; uygulamadan önce o sayfadan
> kontrol et. TradingView'ın webhook kuralları (4 kaynak IP, yalnızca 80/443, 3 sn, mTLS yok) senin verdiğin
> değerlerdir ve `config/tv_alerts.yaml`'da durur; **her kurulumda** TradingView'ın "About webhooks" sayfasıyla karşılaştır.

## 1. Ön koşullar

- Ubuntu 22.04 ya da üstü, `git`, `curl`.
- TradingView hesabında webhook (ücretli plan) ve 2FA açık.
- Bir Telegram hesabı.
- İki dağıtım yolundan biri (ikisi de ücretsiz):
  - **Seçenek A:** internetten 80 ve 443 portlarına ulaşılabilen bir Ubuntu makine (ücretsiz katmanlı bir VPS ya da evdeki
    makine + modemde port yönlendirme) + ücretsiz DuckDNS alt alan adı + Caddy.
  - **Seçenek B:** kendi Ubuntu bilgisayarın, port açmadan: Tailscale Funnel (önerilen) ya da Cloudflare hızlı tünel
    (yalnızca deneme için).

| | A: DuckDNS + Caddy | B1: Tailscale Funnel | B2: Cloudflare hızlı tünel |
|---|---|---|---|
| Adres | `https://ALT-ALAN.duckdns.org` (kalıcı) | `https://MAKINE.TAILNET.ts.net` (kalıcı) | `https://RASTGELE.trycloudflare.com` (her başlatmada değişir) |
| Açık port | 80 (herkese), 443 (yalnızca TradingView) | yok | yok |
| IP süzme | ufw + Caddy + bot | yalnızca bot | yalnızca bot |
| Aynı anda iki yol | — | Funnel ve Cloudflare tüneli birlikte açık kalmasın; kullanmadığını kapat (`tailscale funnel reset`) | aynı |
| 10 günlük kabul testi için | uygun | uygun (bilgisayar hep açık olmalı) | **uygun değil** (adres değişince alarmlar boşa gider) |

## 2. Kurulum

Aşağıda depo `/opt/propbot` altında ve servis **kendi (root olmayan) kullanıcınla** çalışır varsayılır. Seçenek B'de
depo `~/Belgeler/propbot` gibi bir yerde kalabilir; o zaman `deploy/propbot.service` içindeki yolları ona göre yaz.

```bash
sudo install -d -o "$USER" -g "$USER" /opt/propbot
git clone https://github.com/Nogeday/propbot.git /opt/propbot
cd /opt/propbot
curl -LsSf https://astral.sh/uv/install.sh | sh                # uv yoksa (ya da: pipx install uv); sonra yeni terminal aç
uv sync --frozen --no-dev                                      # .venv/ oluşur; sürümler uv.lock'tan
mkdir -p data && chmod 700 data
uv run propbot config check
```

Saat eşitlemesi (tazelik kontrolü ve gecikme ölçümü sunucu saatine dayanır):

```bash
sudo apt install chrony
chronyc tracking        # "System time : 0.000… seconds slow/fast of NTP time" → fark 1 saniyenin çok altında olmalı
```

## 3. Sırlar ve config

### 3.1 Telegram botu
1. Telegram'da `@BotFather` → `/newbot` → bot adını ver → **token**'ı al.
2. Kendi botuna bir mesaj yaz (ör. `merhaba`); bot sana özel sohbette ulaşabilsin.
3. Sohbet kimliğini öğren (token kabuk geçmişine yazılmasın diye `read -rs` ile):
   ```bash
   read -rs TG && curl -s "https://api.telegram.org/bot${TG}/getUpdates" | python3 -m json.tool | grep -A3 '"chat"'; unset TG
   ```
   `"id": 123456789` satırındaki **pozitif** sayı senin sohbet kimliğin. Negatif sayı grup/kanal demektir; bot onları
   reddeder (değişmez kural 7: tek kişisel sohbet).

### 3.2 `.env`
```bash
cp .env.example .env && chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"      # TV_WEBHOOK_TOKEN için
nano .env
```

| Anahtar | Değer |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather'ın verdiği token |
| `TELEGRAM_ALLOWED_CHAT_ID` | 3.1'deki pozitif sayı |
| `TV_WEBHOOK_TOKEN` | üretilen rastgele değer (en az 32 karakter; harf, rakam, `-`, `_`). Aynısı Pine girdisine yazılır |
| `PROPBOT_PIN` | 6–12 haneli sayı (`/kill` ve `/devam` onayı) |

`.env` yalnızca sahibi tarafından okunabilir olmalı (`chmod 600`); değilse bot başlamaz. Dosya git'e girmez.
Broker ya da prop firması şifresi **hiçbir yere** yazılmaz (değişmez kural 6).

### 3.3 `config/`
- `config/tv_alerts.yaml` → `webhook.source_ips`: TradingView'ın resmi sayfasındaki dört adresle aynı mı? Değiştiyse
  dosyayı, Caddyfile'ı ve ufw kurallarını **birlikte** güncelle; `rules_verified_at` tarihini yaz.
- `config/tv_alerts.yaml` → `alerts`: kurduğun her TradingView alarmı için bir satır ve bitiş tarihi (8. bölüm).
- `config/venues/ftmo_2step_100k.yaml` → `contract.*`: MT5'te Piyasa Gözlemi → `US500.cash` üzerinde sağ tık →
  **Özellikler (Specification)** penceresinden oku ve yaz:

  | Profil alanı | MT5 penceresindeki satır |
  |---|---|
  | `tick_size` | Tick size (tick boyutu) |
  | `tick_value_per_lot` | Tick value (tick değeri, hesap para biriminde, 1 lot için) |
  | `min_lot` | Minimal volume (en küçük hacim) |
  | `lot_step` | Volume step (hacim adımı) |

  Bu alanlar boşken bot çalışır; mesajda lot yerine "boyut hesaplanamadı, risk ≈ $250" yazar.
- Her değişiklikten sonra: `uv run propbot config check`.

## 4. Servis (systemd)

```bash
sudo cp deploy/propbot.service /etc/systemd/system/propbot.service
sudo nano /etc/systemd/system/propbot.service      # User=<kullanıcın>; WorkingDirectory / ExecStart / ReadWritePaths yolları
sudo systemctl daemon-reload
sudo systemctl enable --now propbot
systemctl status propbot
journalctl -u propbot -f                           # JSON satırları; sırlar *** olarak maskelenir
curl -s http://127.0.0.1:8000/healthz              # {"status":"ok"}
```

- Bot yalnızca `127.0.0.1:8000`'i dinler; dışarıya TLS vekili (Caddy) ya da tünel açar.
- Arka plan görevlerinden biri ölürse süreç hata koduyla çıkar, systemd 5 sn sonra yeniden başlatır. Kuyruk ve Telegram
  kutusu `data/propbot.db`'de durur; kaldığı yerden devam eder. Süresi geçmiş giriş sinyalleri "BAYAT" ya da
  "İLETİLEMEDİ" olarak bildirilir, hiçbir zaman geçerli sinyal gibi iletilmez.
- Günlükler journald'dedir; boyut sınırı için `/etc/systemd/journald.conf` → `SystemMaxUse=200M`.
- **Yeniden başlatmayı izlenen pencerelerin dışında yap** (varsayılan pencere: Pzt–Cum 03:00–16:00 New York):
  süreç kapalıyken gelen alert kaybolur.

## 5. Seçenek A — DuckDNS + Caddy + ufw

1. **DuckDNS:** duckdns.org'da oturum aç, bir alt alan adı oluştur (`ALT-ALAN`), sayfadaki **token**'ı not et.
   Makinenin IP'si sabit değilse (ev bağlantısı) güncelleyiciyi kur:
   ```bash
   sudo install -m 600 /dev/null /etc/duckdns.env
   sudo nano /etc/duckdns.env                       # içine tek satır: DUCK_URL="https://www.duckdns.org/update?domains=ALT-ALAN&token=DUCKDNS-TOKEN&ip="
   echo '*/5 * * * * root . /etc/duckdns.env && curl -fsS "$DUCK_URL" >/dev/null' | sudo tee /etc/cron.d/duckdns
   ```
   (`ip=` boş bırakılınca DuckDNS isteğin geldiği adresi kullanır. Yanıt `OK` olmalı.) Ev bağlantısında modemden 80 ve
   443'ü bu makineye yönlendir.
2. **Güvenlik duvarı** (SSH'yi kilitlemeden önce anahtarla girebildiğini doğrula):
   ```bash
   sudo ufw default deny incoming
   sudo ufw default allow outgoing
   sudo ufw limit 22/tcp comment 'SSH'
   sudo ufw allow 80/tcp comment 'ACME HTTP challenge + redirect'
   for ip in 52.89.214.238 34.212.75.30 54.218.53.128 52.32.178.7; do
     sudo ufw allow proto tcp from "$ip" to any port 443 comment 'TradingView webhook'
   done
   sudo ufw enable && sudo ufw status verbose
   ```
   443 yalnızca TradingView'a açıktır. Sertifika bu yüzden HTTP challenge ile (port 80) alınır; 80'i kapatırsan sertifika
   yenilenemez. 80'i de kapatmak istersen DNS challenge gerekir: `caddy-dns/duckdns` eklentili özel bir Caddy derlemesi
   (`xcaddy`) ve Caddyfile'da `tls { dns duckdns {env.DUCKDNS_API_TOKEN} }`.
3. **Caddy:**
   ```bash
   sudo apt install --yes debian-keyring debian-archive-keyring apt-transport-https curl
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
   sudo apt update && sudo apt install caddy
   sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
   sudo nano /etc/caddy/Caddyfile                   # ALT-ALAN-ADIN.duckdns.org → kendi adın
   sudo systemctl reload caddy && journalctl -u caddy -n 30
   ```
   Caddy yalnızca TradingView adreslerinden gelen `POST /tv/webhook` isteğini bota geçirir, gerisini yanıtsız kapatır.
   İstemcinin gönderdiği `X-Forwarded-For` başlığını yok sayar ve gerçek bağlantı adresini yazar.
4. `config/tv_alerts.yaml` → `receiver` varsayılanları bu kurulum içindir (`trusted_proxies: 127.0.0.1, ::1`,
   `client_ip_header: X-Forwarded-For`). Webhook adresin: `https://ALT-ALAN.duckdns.org/tv/webhook`.

## 6. Seçenek B — kendi bilgisayarın, port açmadan

Bilgisayar izlenen pencereler boyunca açık ve uyanık kalmalı (uyku/askıya alma kapalı).

### 6.1 Tailscale Funnel (önerilen)
```bash
curl -fsSL https://tailscale.com/install.sh | sh        # (VERIFY: kurulum komutu için tailscale.com/download)
sudo tailscale up                                        # tarayıcıda oturum aç
sudo tailscale funnel --bg 8000                          # ilk seferde HTTPS ve Funnel'ı açman için bir bağlantı verir
tailscale funnel status                                  # https://MAKINE.TAILNET.ts.net → http://127.0.0.1:8000
```
- Adres kalıcıdır ve 443'ten yayınlanır; `--bg` yapılandırmayı yeniden başlatmalardan sonra da korur. Kapatmak için:
  `sudo tailscale funnel reset`.
- Tailscale, yerel bota ilettiği istekte `X-Forwarded-For` başlığını **kendisi** yazar (istemcinin gönderdiğinin üzerine)
  ve Funnel isteklerine `Tailscale-Funnel-Request: ?1` ekler (kaynak: `tailscale/tailscale` → `ipn/ipnlocal/serve.go`).
  `config/tv_alerts.yaml` → `receiver` varsayılanları değişmeden çalışır.
- Webhook adresin: `https://MAKINE.TAILNET.ts.net/tv/webhook`.

### 6.2 Cloudflare hızlı tünel (yalnızca deneme)
```bash
cloudflared tunnel --url http://localhost:8000           # (VERIFY: cloudflared kurulumu için Cloudflare'in indirme sayfası)
```
- Çıktıdaki `https://RASTGELE.trycloudflare.com` adresi **her başlatmada değişir**; TradingView alarmlarındaki webhook
  adresini her seferinde yeniden yazman gerekir. Hızlı tüneller deneme amaçlıdır, çalışma süresi garantisi yoktur
  (VERIFY: Cloudflare "Quick Tunnels" sayfası). Kalıcı adres, Cloudflare'e eklenmiş bir alan adı ister (ücretsiz değil).
- Cloudflare, bağlanan gerçek istemci adresini `X-Forwarded-For` başlığının **sonuna** ekler (ayrıca
  `CF-Connecting-IP` olarak da verir). Bot her zaman `X-Forwarded-For`'un en sağdaki adresine bakar; config'te
  değişiklik gerekmez. Başka bir başlık adı yazılamaz: desteklenen her vekil yalnızca bu başlığı kendisi yazar,
  diğerleri bir tünelden olduğu gibi geçip taklit edilebilir.
- Webhook adresin: `https://RASTGELE.trycloudflare.com/tv/webhook`.

## 7. İstemci IP doğrulaması nasıl çalışır

Bot her istekte şunu yapar (`propbot/signals/receiver.py`):

1. TCP bağlantısının karşı ucu `receiver.trusted_proxies` içindeyse (varsayılan: yalnızca `127.0.0.1` ve `::1`),
   `X-Forwarded-For` başlığı **zorunludur** ve virgülle ayrılmış listenin **en sağdaki** adresi istemcidir.
   Başlık yoksa ya da bozuksa istek reddedilir (403).
2. Karşı uç güvenilir vekil değilse başlık yok sayılır; istemci, bağlantının kendisidir.
3. İstemci adresi `webhook.source_ips` listesinde değilse 403.

Bu yüzden:
- Bot yalnızca `127.0.0.1`'i dinler; başlığı ancak aynı makinedeki vekil/tünel yazabilir. `--host` ile dış arayüze
  bağlanmak reddedilir.
- `trusted_proxies` listesini genişletme. Geniş bir aralık, başlığı herkesin uydurabilmesi demektir; config yükleyicisi
  256 adresten büyük ya da genel (public) aralıkları kabul etmez.
- uvicorn'un kendi `X-Forwarded-For` işlemesi kapalıdır (`proxy_headers=False`); adresi yalnızca botun kodu yorumlar.
- Seçenek B'de adresini bilen herkes tünelden bota ulaşabilir; koruma botun IP süzgeci + token'dır. IP süzgeci
  "TradingView'dan geldi"yi, token "benim alarmımdan geldi"yi kanıtlar; token'ı kimseyle paylaşma.
- Dört kaynak adres bütün TradingView kullanıcılarının ortak adresleridir. Bu yüzden hız sınırı (dakikada 30) yalnızca
  **token'ı doğru** isteklerde sayılır: adresini öğrenen başka bir TradingView kullanıcısı senin alert'lerini 429'a
  düşüremez. Token'sız/yanlış token'lı istekler reddedilir; bot bunları sayar ve en fazla **saatte bir** Telegram'a
  bildirir (token'ı değiştirdikten sonra bir alarmı eski token'la unutmuşsan böyle fark edersin).

Başlığın doğru geldiğini kontrol et: TradingView'dan ilk alert'ten sonra `journalctl -u propbot | grep webhook_`
satırlarında `"event": "webhook_accepted"` ve `client_ip` alanında dört TradingView adresinden biri görünmeli.
`webhook_rejected` + `"code": "ip_not_allowed"` görüyorsan `client_ip` hangi adresin okunduğunu söyler (boşsa vekil
başlığı hiç yazmamıştır).

## 8. TradingView alarmları

Her strateji için (ve heartbeat göstergesi için) **bir** alarm:

1. Script'in **Inputs → Webhook token** alanına `.env`'deki `TV_WEBHOOK_TOKEN` değerini yaz.
2. Alarm oluştur → Condition: strateji için **"alert() function calls only"**, `pine/heartbeat_indicator.pine` için
   **"Any alert() function call"**.
3. Notifications → **Webhook URL**: `https://…/tv/webhook`. Mesaj kutusu kullanılmaz.
4. Alarmın bitiş tarihini `config/tv_alerts.yaml` → `alerts` altına yaz; bot bitişten 7 gün önce hatırlatır:
   ```yaml
   alerts:
     - {name: "heartbeat MES1! 1m", expires_on: 2027-01-12}
     - {name: "master MES1! 1m", expires_on: 2027-01-12}
   ```
5. Girdi değiştirirsen alarmı yeniden kur (alarm, oluşturulduğu andaki girdilerle çalışır).

Heartbeat göstergesini stratejilerle **aynı sembolün** 1 dakikalık grafiğine ekle. Saat başı bir mesaj gönderir; izlenen
pencerede 75 dakika hiçbir alert gelmezse bot "TradingView alarm hattı sessiz" uyarısı yollar. Tatil ve erken kapanış
günlerinde bu uyarı beklenen bir şeydir.

## 9. Duman testi

Sunucunun kendisinde, vekili atlayarak (başlığı elle yazıyoruz; bu yalnızca `127.0.0.1`'den mümkündür):

```bash
read -rs TOKEN
NOW_MS=$(( $(date +%s) / 60 * 60000 - 60000 ))           # bir önceki dakikanın açılışı
curl -s -X POST http://127.0.0.1:8000/tv/webhook -H 'X-Forwarded-For: 52.89.214.238' \
  -d "{\"v\":1,\"token\":\"$TOKEN\",\"strategy_id\":\"heartbeat\",\"symbol\":\"TEST\",\"tf\":\"1\",\"bar_time\":$NOW_MS,\"action\":\"heartbeat\"}"
unset TOKEN
```

Beklenen: `{"status":"queued"}` (aynı komutu yinelersen `{"status":"duplicate"}`). Sonra Telegram'da `/saglik`:
"son heartbeat" birkaç saniye önce görünmeli. Yanlış token → `{"error":"unauthorized"}`; başlıksız →
`{"error":"forbidden"}`.

Uçtan uca test TradingView'dan yapılır: heartbeat alarmını kur, saat başını bekle, `/saglik`'a bak.

## 10. Günlük işletim

| Komut | Ne yapar |
|---|---|
| `/durum` | mod, duraklatma/KILL durumu, kuyruk, bugünkü sinyaller, son alert ve heartbeat |
| `/risk` | işlem başı risk, günlük bütçe (temkinli sayım), bugünkü girişler ve durumları |
| `/pnl` | Faz 1'de emir yok: iletilen giriş sayısı ve sayılan risk |
| `/atladim [id]` | almadığın girişin riskini bütçeden düşer **ve o girişin stop/çıkış mesajlarını keser** (id yoksa: sana ulaşmış son giriş). Yanıt hangi girişin işaretlendiğini söyler |
| `/aldim <id>` | `/atladim`'ı (ya da "iletilemedi" işaretini) geri alır |
| `/duraklat` · `/devam` | yeni giriş sinyallerini durdurur / sürdürür (stop, çıkış ve iptal mesajları her zaman gelir) |
| `/kill <PIN>` | yeni girişleri kalıcı keser; açık pozisyonu **sen** kapatırsın. Geri dönüş: `/devam <PIN>`. PIN içeren mesajı bot siler; silemezse uyarır |
| `/mod` | çalışan mod (`signal`); diğer modlar bu sürümde kilitli |
| `/saglik` | çalışma süresi, kuyruk, gecikme medyanı, reddedilen istek sayaçları, saat kontrolü, kural tazeliği |

- **Günlük bütçe temkinlidir:** iletilen her giriş, o prop günü boyunca kaybetmiş gibi sayılır (bot dolumu ve sonucu
  bilmez). Almadığın sinyali `/atladim` ile bildir; yoksa sonraki sinyaller "bütçe yetersiz — ATLA" diye gelir.
- **Mesajdaki seviyeler puan mesafesidir.** TradingView grafiği (ör. MES vadeli) ile FTMO `US500.cash` fiyatı aynı
  değildir; stop ve TP'yi **emirle birlikte**, kendi giriş fiyatına göre gir. Geçerlilik süresi dolduysa ya da
  TradingView'daki fiyat mesajdaki aralığın dışındaysa atla.
- **Stop güncellemeleri de mesafe olarak gelir** ("stop = kendi giriş fiyatın −3.50 puan"). Bot, stop'unu genişletecek
  bir güncellemeyi iletmez; "[UYARI] … mevcut stop'unu KORU" yazar (değişmez kural 2).
- **Stop ya da TP TradingView'da dolunca mesaj gelmez.** FTMO'daki pozisyonun hâlâ açıksa seans sonunda kendin kapat.
- **`/duraklat` ve `/kill` gecikmeli ulaşsa da uygulanır**; `/devam`, `/atladim`, `/aldim` 60 saniyeden eskiyse reddedilir.
- **Yarım gün / erken kapanış:** seans sonu `exit` alert'ine güvenme; kapanıştan önce elle düzleştir (açık bulgu 18).
- **Haftalık:** `journalctl -u propbot --since "7 days ago" | grep -c webhook_rejected`, `uv run propbot signals report`.
- **30 günde bir:** FTMO kurallarını resmi sayfadan yeniden doğrula (`rules_verified_at`).
- **Yedek:** `sqlite3 data/propbot.db ".backup 'data/yedek-$(date +%F).db'"` (bot çalışırken güvenlidir).
- **Güncelleme** (izlenen pencere dışında): `git pull && uv sync --frozen --no-dev && uv run propbot config check &&
  sudo systemctl restart propbot`.

## 11. Sorun giderme

| Belirti | Olası neden | Bakılacak yer |
|---|---|---|
| Bot başlamıyor: "chmod 600" | `.env` başkalarınca okunabilir | `chmod 600 .env` |
| Bot başlamıyor: config hatası | YAML'da tavan aşımı, bilinmeyen anahtar, yanlış tip | `uv run propbot config check` |
| TradingView alert'i geliyor, Telegram'a düşmüyor | bot duraklatılmış / KILL; bütçe dolu; sinyal bayat | `/durum`, `/risk` |
| Günlükte `ip_not_allowed` (403) | vekil başlığı yazmıyor; TradingView IP listesi değişmiş; ufw/Caddy listesi eski | 7. bölüm; `config/tv_alerts.yaml` |
| Telegram'da "kimliği doğrulanamadan reddedildi" · günlükte `bad_token` (401) | Pine girdisindeki token ≠ `.env`; alarm eski girdiyle kurulmuş; başkası adresini deniyor | her alarmın "Webhook token" girdisi; alarmı yeniden kur |
| Günlükte `bad_json` (400) | sembol adı düz `BORSA:SEMBOL` değil (grafikte uzatılmış seans / ayarlama açık) ve JSON bozuluyor | TradingView Alert Log'daki mesaj |
| Telegram'da "dakikada 30 istekten fazlasını gönderiyor" (429) | bir alarm döngüde ya da aynı alarm birkaç kez kurulmuş | TradingView Alerts paneli |
| "[SİNYAL — ATLA] Sunucu saati TradingView ile uyuşmuyor" | sunucu saati geride | `chronyc tracking`; `sudo systemctl restart chrony` |
| "[İLETİM BELİRSİZ]" | Telegram yanıtı gecikti; mesaj ulaşmış olabilir | sinyali gördüysen bir şey yapma; görmediysen `/atladim <id>` |
| "Sinyal işlenemedi" alarmı | o sinyalde yazılım hatası; kuyruk devam eder | `journalctl -u propbot \| grep signal_processing_failed`; Claude Code'a bildir |
| Telegram'da "Geçersiz alert gövdesi" | script eski sürüm; desteklenmeyen zaman dilimi (günlük+); sembol adında beklenmeyen karakter | mesajdaki alan adları |
| "TradingView alarm hattı sessiz" | alarm durmuş ya da süresi dolmuş; TradingView kesintisi; tatil | TradingView Alerts paneli |
| "[BAYAT SİNYAL]" sık geliyor | sunucu saati kaymış; ağ gecikmesi | `chronyc tracking`, `/saglik` gecikme medyanı |
| `/saglik` yanıt vermiyor | Telegram token/sohbet kimliği yanlış; ağ yok | `journalctl -u propbot -n 50` |
| Mesajda "boyut hesaplanamadı" | venue profilinde `contract.*` boş | 3.3 |

## 12. Kabul testi (SPEC §5.7)

1. Başlangıç: stratejiler TradingView'da derlenmiş (Faz 0 doğrulama listesi), alarmlar kurulu, heartbeat çalışıyor.
2. **10 işlem günü** boyunca servisi izlenen pencerelerde yeniden başlatma.
3. Her gün sonunda TradingView → Alerts → Log'u dışa aktar ve bot kaydıyla karşılaştır:
   ```bash
   uv run propbot signals report --since 2026-12-01 --csv reports/sinyaller.csv
   ```
   Ölçütler: kaçan sinyal 0, çift mesaj 0, uçtan uca medyan gecikme (bar kapanışı → Telegram) < 5 sn.
4. Sonuçları (rapor çıktısı + TradingView Alert Log sayıları) Claude Code oturumuna yapıştır.
