# İddia Oran Öneri

Ücretli/anahtarlı spor API'si kullanmadan, herkese açık sayfalardan veri toplayıp maç sonucu
olasılıkları ve önerilen oranlar üreten lokal web uygulaması.

## Çalıştırma

```sh
cd ~/Desktop/IddiaOran
./run.sh
```

Sonra tarayıcıda: http://127.0.0.1:8000

`DATABASE_URL` tanımlı değilse yerel SQLite (`data/iddia.db`) kullanılır; bu modda ilk açılışta veri yoksa
(veya son güncelleme 6 saatten eskiyse) arka planda tam güncelleme başlar. İlk tam çekim 11 lig için birkaç
dakika sürer, sonrakiler sadece son günleri çeker.

Komut satırından (ağır) tam güncelleme:

```sh
.venv/bin/python -m app.updater              # tüm ligler + haberler + iddaa oranları
.venv/bin/python -m app.updater --league tur.1 --no-news
```

## Ortak veritabanı (Supabase) ve güncelleme akışı

Herkese açık (Vercel) kurulumda veri ortak Supabase Postgres'tedir; biri güncelleyince herkes aynı veriyi ve
"X dk önce güncellendi" bilgisini görür.

- `DATABASE_URL`: Supabase **transaction pooler** adresi (`...pooler.supabase.com:6543`). Ortam değişkeni
  (Vercel, GitHub secret) veya repo dışı `.env` (bkz. `.env.example`, gitignore'da) olarak verilir.
  Tanımlıysa uygulama Postgres, değilse SQLite kullanır. Testler `.env`'yi yok sayıp her zaman SQLite kullanır.
- Şema: `supabase/migrations/` (`supabase db push --db-url <session-pooler-url>`). Bir kerelik yerel veri
  aktarımı: `python scripts/import_sqlite_to_postgres.py --sqlite data/iddia.db`.
- `meta` tablosu: `last_updated_at` (son başarılı güncelleme), `update_lock` (kilidin bitiş zamanı; zaman aşımlı).
- **Verileri güncelle** (`POST /api/update`): istek içinde senkron, hızlı artımlı güncelleme (iddaa oranları +
  ESPN son 3 gün). Arka plan thread'i yoktur; kilit eşzamanlı çalışmayı engeller, son güncellemeden sonra
  10 dk bekleme vardır (`status`: `updated` | `partial` | `cooldown` | `busy` | `error`). Yönetici oturumu gerekir.
  Durum: `GET /api/update/status`; `sources` alanında lig/iddaa/haber bazında son deneme, son başarı ve hata bulunur.
  Yalnız istenen kaynakların hepsi başarılıysa `last_updated_at` ilerler; boş ama başarılı kaynak yanıtı hata sayılmaz.
  Kısmi güncellemede modellerin önbelleği de yenilenir; CLI kısmi başarısızlıkta sıfır olmayan çıkış kodu verir.
- Ağır tam güncelleme + haberler: `.github/workflows/update.yml` günde dört kez (`47 5,11,17,23 * * *`, 05:47 UTC sabah kuponundan önce); hızlı iş `quick.yml` saatte bir, dakika 17 (`workflow_dispatch` ile elle de);
  `DATABASE_URL` GitHub secret'ından okunur. Sağlık kontrolü saatliktir; oran veya güncelleme 75 dakikayı geçerse hızlı işi kendisi tetikler.

## Veri kaynakları (anahtar gerekmez)

| Veri | Kaynak |
|---|---|
| Maç sonuçları ve fikstür | ESPN fikstür sayfaları (sayfaya gömülü JSON okunur) |
| Güncel bahis oranları | iddaa.com maç öncesi bülteni (sitenin herkese açık JSON/XHR uç noktası) |
| Puan durumu, form, ev/deplasman performansı, H2H, dinlenme süresi | Yukarıdaki sonuçlardan hesaplanır |
| Haberler, sakatlık/ceza sinyali | Google Haberler RSS (takım başına, son 7 gün) |

Ligler: Süper Lig, Premier League, La Liga, Serie A, Bundesliga, Ligue 1, TFF 1. Lig, Eredivisie, Liga Portugal,
UEFA Şampiyonlar Ligi, UEFA Avrupa Ligi (`app/config.py` → `LEAGUES`).

### TFF 1. Lig ve Avrupa kupaları

- **TFF 1. Lig (`tur.2`)**: ESPN bu lig için sluğu listeliyor ama hiç maç/takım döndürmüyor. Veri resmi TFF
  sitesinden (`app/scrapers/tff.py`, `hafta=N` sayfaları; TSİ → UTC) okunur. Önceki sezon çekilmediği için
  sezon başında veri azdır (güven puanı bunu düşük gösterir). Kulüp adları `CLUB_NAMES` tablosundadır; yeni
  sezonda yükselen takımlar genel temizlemeyle adlandırılır, gerekirse tabloya eklenir.
- **Avrupa kupaları (`uefa.champions`, `uefa.europa`)**: `app/model/european.py`. Ligler arası güç =
  takımın kendi lig Elo'su + lig ofseti; ofsetler UEFA sırasına dayanan yaklaşık önsellerle başlar ve
  kupadaki ligler arası maçlardan MAP ile güncellenir; ardından kupa sonuçları takım başına Elo düzeltmesi
  ekler. Ligi bizde olmayan takımlar (Celtic, Club Brugge…) "diğer" grubunda 1500 civarından başlar.
  Haberler takımın kendi liginden alınır; kupalar için ayrıca haber çekilmez ve geriye dönük test yapılmaz.
- Tam geçmiş çekimi yalnız `python -m app.updater` / GitHub Actions cron'unda çalışır; web isteği
  (`/api/update`) yalnız hızlı güncellemeyi çalıştırır ve lig çekimleri için 120 sn sınırı vardır.

## iddaa oranlarıyla harmanlama

`app/model/blend.py`: iddaa.com oranından marj temizlenir (1X2 üçlü, Alt/Üst 1.5/2.5/3.5 ve KG ikili gruplar:
`1/oran`, grup toplamına bölünür) ve model olasılığıyla `p = (1-w)·p_model + w·p_piyasa` harmanlanır;
`w = MODEL["market_weight"]` (`app/config.py`, varsayılan 0,75; 0 = yalnız model). Varsayılan ağırlık
piyasaya yakındır çünkü kayıtlı maç öncesi snapshot'larda yalnız piyasa Brier'i (0,496) modelden (0,547)
daha iyidir; `adaptive_market` lig/pazar kanıtı biriktikçe bu ağırlığı aşağı/yukarı oynatabilir. Oran
yoksa (veya grup eksik/tutarsızsa) model olasılığı aynen kalır. Çifte şans harmanlı 1X2'den türetilir; öneri, güven, "Claude oran" ve günlük
kuponlar harmanlı olasılığı kullanır. Yanıtlarda her pazarda `model_p` / `market_p`, tahminde `blend` alanı vardır.

**Backtest karşılaştırması:** geçmiş iddaa oranı tutulmadığı için (`iddaa_odds` her senkronda yeniden yazılır)
oranlar bundan sonra `odds_log` tablosuna kaydedilir (maç başına pazar başı ilk ve maçtan önceki son oran;
her oran senkronunda otomatik). Model sekmesindeki test, kayıtlı maçlarda model / harmanlı / yalnız piyasa
olasılıklarını Brier, log-loss ve isabet ile karşılaştırır; ayrıca `market_weight` için ağırlık taraması verir.
Kayıt biriktikçe (birkaç yüz sonuçlanmış maç) ağırlık bu taramaya göre ayarlanmalıdır.
Şema: `supabase/migrations/20261006110000_odds_log.sql` (deploy'dan önce uygulanmalı; uygulanmazsa oran
senkronu bozulmaz, kayıt atlanır ve `meta.odds_log_error` doldurulur).

## Model

1. **Poisson güçleri**: zaman ağırlıklı (yarı ömür 200 gün), ev/deplasman ayrı hücum-savunma katsayıları.
2. **Elo**: gol farkı ağırlıklı, ev avantajlı, sezon arasında ortalamaya çekilir. Poisson ile harmanlanır.
3. **Form**: son 5 maç ağırlıklı puan ortalaması.
4. **Haber skoru**: başlıklardan sözlük tabanlı duygu skoru + sakatlık/ceza haberi cezası.
5. **Dinlenme**: 4 günden az dinlenen takıma küçük ceza.
6. **Dixon-Coles** düzeltmeli skor matrisi → 1X2, çifte şans, Alt/Üst 1.5/2.5/3.5, KG Var/Yok, skor.

Adil oran = 1/olasılık. Arayüzde iddaa.com oranı ana değer, modelin önerilen oranı ise **Claude oran**
etiketiyle gösterilir. Maç detayında iddaa oranı otomatik doldurulur ve model olasılığına göre beklenen
değer (EV) ile ¼ Kelly hesaplanır; isterseniz alanı değiştirerek başka bir oranı deneyebilirsiniz.

**Günlük Kupon** sekmesi seçilen günün (Türkiye saati, bültendeki tüm ligler, başlamamış maçlar) kuponlarını
kurar. Seçim politikası `hit_probability_v4` (`app/model/coupons.py`, `TIERS`): her kupon türü için bir
toplam-oran tabanı vardır ve bu tabanın üstünde **ortak tutma olasılığı en yüksek** kombinasyon seçilir.
iddaa marjı her bacakta yeniden ödendiği için daha az maç daima daha olasıdır; bu yüzden MBS 1–2 maçlar
(tek/iki maç oynanabilen Milli takım, alt lig vb.) havuzda korunur ve bültenin MBS kuralı kupon boyutuna
uygulanır (kupon en az bacaklarının en yüksek MBS'si kadar maç içerir).
- **Banko**: 1–3 maç, bacak 1,10–2,00 ve ≥%45, toplam oran 1,80–4,00, ortak olasılık ≥%25.
- **Sürpriz**: 2–4 maç, bacak 1,40–3,50 ve ≥%33, toplam oran 4,50–15, ortak olasılık ≥%7 (yüksek oran ama
  piyango değil).
- Her tür için 3 alternatif üretilir; alternatifler önce **ortak maç içermeyecek** şekilde seçilir.
- Hedef bandı tutmazsa kupon boş kalmaz: en yakın uygun kombinasyon `target_met=false` /
  `selection_tier=available_fallback` ile verilir, Telegram mesajında “hedef bandı dışı” yazar.

Beklenen getiri yine negatiftir (iddaa marjı ≈%20/pazar: tek maçta ≈−%14, üç maçta ≈−%42); motor tutma
olasılığını azamileştirir, kâr vaat etmez.

Kuponda oran olarak eşleşen **iddaa** oranı kullanılır; o pazarda iddaa oranı yoksa **Claude oran**'a düşülür.
Her satırda ve kupon başlığında hangisinin kullanıldığı yazar. Şartı sağlayan seçim yoksa kupon boş döner.
API: `/api/coupons?day=YYYY-MM-DD`. Testler: `.venv/bin/python -m unittest discover -s tests -t .`

**Grok denetimi** (isteğe bağlı, abonelik yeterli, API yok): günde tek prompt ile Grok günün kuponlarını haber,
kadro ve motivasyon açısından denetler; bacak bazında `tut / dikkat / veto` der. Grok olasılık üretmez;
veto'lu bacak varsa motor aynı türün veto'suz, henüz başlamamış ilk alternatifine geçer ve bunu `banko_grok` /
`surprise_grok` türüyle ayrı ölçer. Brief `GET /api/coupons/brief?kind=daily|lineup|weekly` (herkese açık),
karar yönetici panelinden `POST /api/coupons/grok-audit` ile girilir. Kurulum ve şema: `docs/GROK_KUPON_DENETIMI.md`.

**Grok Bot araştırma pilotu** (isteğe bağlı, Grok Bot aboneliği yeterli): günlük Actions köprüsü günün trio
adaylarını `ai_research` kuyruğuna yazar ve Grok Bot rutinini webhook'la uyandırır; Bot kendi bulut bilgisayarından
Research API ile claim → araştırma → rapor döngüsünü insan dokunmadan tamamlar. Kurulum: `docs/GROK_BOT_PILOT.md`.

**Kupon Geçmişi** sekmesi: günün Banko ve Sürpriz kuponu (gün + tür başına bir kez, ilk üretildiği haliyle) `saved_coupons` /
`saved_coupon_picks` tablolarına dondurulur — bugünün kuponu `/api/coupons` ile ilk üretildiğinde veya tam güncellemede
(cron). Maç sonuçları gelince (hızlı ve tam güncelleme akışında) her seçim ve kupon `Tuttu` / `Yattı` / `Bekliyor` (`İptal`:
ertelenen/iptal maç, oran 1.00) işaretlenir; tek seçim yattıysa kupon hemen yatmıştır. Sekmede banko tutma oranı, sürpriz
tutma sayısı, "her gün her kupona 100 TL yatırsaydın" kâr/zarar + ROI (yalnız sonuçlanan kuponlar, kayıtlı oranla) ve günlük
liste vardır. API: `GET /api/coupon-history?days=60`. Kod: `app/model/coupon_history.py`.

Web ve Telegram günün ilk kaydedilen Banko/Sürpriz kuponunu ortak kayıttan okur. Her kaydın
`coupon_id` / `published_at` bilgisi vardır; sonradan hesaplanan alternatifler arayüzde ayrı etiketlenir.
Otomatik kuponlarda yalnız tam maç skoruyla sonuçlandırılabilen pazarlar kullanılır; ilk yarı ve handikap
pazarları otomatik seçilmez. Geçmiş ve Telegram aynı sonuç/iptal kurallarını kullanır: ertelenmiş/iptal
durumu bir gün sonra, sonuç alınamayan maç yedi gün sonra iptal sayılır. Eski desteklenmeyen seçimler
otomatik iade sayılmaz; geçmişte elle doğrulama uyarısıyla görünür.

**Değerli Oranlar** sekmesi yaklaşan (başlamamış) maçlarda modelin olasılığını iddaa.com oranıyla karşılaştırır;
EV = olasılık × oran − 1 > 0 olan seçimleri EV'ye göre sıralar (pazar, seçim, model %, iddaa oranı, örtük %,
EV %, maç/lig/saat). Min. olasılık ve min. EV filtreleri vardır; iddaa oranı olmayan pazar/maç listeye girmez,
liste boşsa nedeni mesajla açıklanır. API: `/api/value-bets?days=7&min_prob=0.3&min_ev=0.05` (oran ve EV 0-1 aralığında).

**Kuponum** sekmesi kendi kuponunuzu kurar: maç kartlarındaki **＋ Kupona ekle** düğmesi modelin önerisini ekler;
maç detayındaki her pazar (1X2, çifte şans, Alt/Üst 1.5-2.5-3.5, KG) için ayrı düğme vardır. Kuponda toplam oran
(iddaa oranı varsa o, yoksa Claude oran), model tutma ihtimali (seçim olasılıklarının çarpımı), EV ve
yatırılan tutardan olası kazanç gösterilir; aynı maçtan birden fazla seçim ya da birlikte tutamayacak seçimler
(örn. 1 ve X) uyarılır. Kupon sunucuya gitmez, tarayıcının `localStorage`'ında (`iddiaoran.slip.v1`) durur; sekme
açılınca oranlar `/api/match/{id}` ile yenilenir. Hesaplar `static/slip-calc.js` (saf JS, `node --test tests/js/*.test.js`;
`unittest discover` de node varsa bunu çalıştırır), arayüz `static/slip.js` + `static/slip.css`.

**Model** sekmesindeki geriye dönük test her maçı sadece öncesindeki verilerle tahmin edip doğruluğu ölçer.
Parametreler `app/config.py` içindeki `MODEL` sözlüğündedir.

## Telegram kanalı (günlük kupon + sonuç)

`app/telegram_bot.py` Telegram Bot API'sini (kütüphanesiz, HTTP) kullanır. Türkiye saatiyle 09:00-15:00
arasında o günün **banko + sürpriz** kuponunu kanala gönderir; kuponun tüm maçları bitince sonucu
(✅/❌, skorlar) kupon mesajına yanıt olarak yazar; kaydedilmiş Grok denetimi varsa "🧠 Grok denetimi" mesajını da
gönderir. Aynı gün/tür ikinci kez gönderilmez: `telegram_posts`
tablosundaki (gün, tür) kaydı önce alınır, gönderim başarısız olursa bırakılır ve sonraki çalıştırmada
tekrar denenir. `TELEGRAM_BOT_TOKEN` veya `TELEGRAM_CHAT_ID` tanımlı değilse hiçbir şey yapmadan çıkar.

Kurulum (bir kez):
1. Telegram'da **@BotFather** → `/newbot` ile bot oluşturun, verdiği **token**'ı kopyalayın.
2. Bir **kanal** oluşturun (herkese açık ise `@kanal_adi`, özel ise sayısal kimlik `-100…` kullanılır).
3. Botu kanala **yönetici** olarak ekleyin ("Mesaj gönder" yetkisi yeterli).
4. Kanal kimliği: herkese açık kanalda `@kanal_adi`; özel kanalda kanala bir mesaj atıp
   `https://api.telegram.org/bot<TOKEN>/getUpdates` yanıtındaki `chat.id` değerini (`-100…`) alın.
5. GitHub → repo → Settings → Secrets and variables → Actions: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   ekleyin (`DATABASE_URL` zaten var).
6. Supabase'e `supabase/migrations/20261006000001_telegram_posts.sql` migration'ını uygulayın
   (yerel SQLite'ta tablo otomatik oluşur).
7. İlk deneme: Actions → "Telegram kupon" → *Run workflow* (`force` işaretli).

Workflow (`.github/workflows/telegram.yml`) her saatin 20. dakikasında çalışır; zamanlanmış workflow'lar
yalnızca varsayılan dalda (main) tetiklenir. Yerelde: `python -m app.telegram_bot --dry-run` mesajı göndermeden
ekrana yazar; `--force` sabah penceresini, `--day YYYY-MM-DD` günü değiştirir.
## PWA (ana ekrana ekle)

Site yüklenebilir bir PWA'dır (Android/Chrome "Uygulamayı yükle", iOS Safari "Ana Ekrana Ekle").

- `app/pwa.py`: `/manifest.webmanifest` (`application/manifest+json`) ve `/sw.js` (kökten, kapsam `/`; `no-cache`).
  Vercel'de tüm yollar FastAPI'ye gittiği için başlıklar burada ayarlanır.
- `app/sw.js` (service worker şablonu): giriş zorunlu değilse `/` + `static/` önbelleğe alınır (cache-first); önbellek adı `static/`
  içeriğinin özetinden türetilir, yani statik dosya değişince otomatik yeni sürüm kurulur ve "Yeni sürüm hazır" bandı çıkar.
  Yalnız açıkça `Cache-Control: public` olan `/api` GET yanıtları network-first saklanır: ağ yok/yavaş (8 sn)/5xx
  ise kayıtlı yanıt gösterilir. Auth/güncelleme yolları, `private`/`no-store` yanıtlar ve giriş zorunlu kurulumun
  API'leri saklanmaz. Giriş/çıkış/şifre sıfırlama sonrasında API önbelleği temizlenir; eski `api-v1` önbelleği
  yeni worker etkinleşince silinir. ESPN takım logoları cache-first.
- `static/pwa.js`: kayıt + çevrimdışı/güncelleme bandı. `static/icons/`: 192/512 + maskable + apple-touch-icon;
  `python scripts/make_pwa_icons.py` ile yeniden üretilir (saf Python).
- Yerelde de çalışır (`localhost` güvenli bağlam sayılır). Sorun olursa tarayıcıda DevTools → Application → Service Workers.
**Oran hareketi**: her iddaa çekiminde değişen oranlar `iddaa_odds_history` tablosuna zaman damgalı satır olarak
eklenir (oran aynıysa yazılmaz; güncel oran yine `iddaa_odds`'tadır). İlk kayıt "açılış", son kayıt güncel
orandır; açılış iddaa.com'un gerçek açılış oranı değil, bizim ilk gördüğümüz orandır. Oran hücrelerinde
`2.10 → 1.85 ↓`, maç detayında pazar başına geçmiş, Maçlar sekmesinde açılışa göre %10+ düşenlerin
**Oran düşüşü** listesi görünür. Bitmiş maçların geçmişi (bültenden çıkmış ve son maç saati 30 günden eski)
her çekimde temizlenir. API: `/api/odds/history/{match_id}`, `/api/odds/drops?min_pct=10&league=tur.1`;
`/api/upcoming`, `/api/picks` ve `/api/match/{id}` yanıtlarında `iddaa_movement` (yalnız değişen pazarlar).
Yeni tablo için `supabase/migrations/20261006000000_iddaa_odds_history.sql` deploy'dan önce uygulanmalıdır.

## Dosyalar

```
app/config.py          ayarlar, ligler, model parametreleri
app/scrapers/espn.py   sonuç & fikstür toplayıcı
app/scrapers/tff.py    TFF 1. Lig toplayıcı (ESPN'de verisi yok)
app/model/european.py  Avrupa kupaları için ligler arası güç modeli
app/scrapers/iddaa.py  iddaa.com bülten oranları + ESPN maç eşleştirmesi
app/odds_history.py    oran geçmişi: snapshot, hareket, düşüş listesi, temizlik (+ api_odds_history.py)
app/scrapers/news.py   Google Haberler RSS toplayıcı
app/sentiment.py       TR/EN başlık duygu + sakatlık analizi
app/model/ratings.py   Elo, Poisson güçleri, form, puan durumu
app/model/predict.py   skor matrisi, pazarlar, oranlar
app/model/engine.py    tüm parametreleri birleştiren motor + geriye dönük test
app/main.py            FastAPI uç noktaları
app/telegram_bot.py    Telegram kanalına günlük kupon + sonuç paylaşımı
app/grok_audit.py      Grok kupon denetimi: brief, karar doğrulama, veto → alternatif (docs/GROK_KUPON_DENETIMI.md)
app/research_auto.py   günlük araştırma kuyruğu köprüsü + Grok Bot webhook tetikleyicisi (docs/GROK_BOT_PILOT.md)
static/                web arayüzü
data/iddia.db          yerel SQLite veritabanı (DATABASE_URL yoksa; otomatik oluşur)
supabase/migrations/   Postgres şeması
scripts/               bir kerelik SQLite -> Postgres aktarımı
.github/workflows/     test CI + günde dört tam / saatlik hızlı güncelleme + Telegram paylaşımı
```

## Notlar

- Kaynak sitelerin HTML yapısı değişirse toplayıcı hata verebilir; hata arayüzde gösterilir.
- iddaa.com yalnızca o anda maç öncesi bülteninde yayımlanan oranları döndürür. Uzak tarihli veya henüz
  oran açılmamış ESPN fikstürlerinde iddaa oranı `-` görünür; Claude oranı gösterilmeye devam eder.
- Tahminler istatistiksel tahmindir, kesinlik içermez. Sorumlu oynayın.

## Otomatik testler

Push ve pull request'lerde `.github/workflows/tests.yml`, Python 3.12 ve Node 22 üzerinde gerçek
veritabanı secret'ı olmadan SQLite testlerini ve JS paketini çalıştırır. Yerelde:

```sh
python -m unittest discover -s tests -t .
node --test tests/js/*.test.js
```

GitHub işi adımlar başlamadan ödeme/harcama limiti nedeniyle reddedilirse, hesap sahibinin
**Settings → Billing & plans** bölümünü düzeltmesi gerekir; workflow dosyasını değiştirmek bu engeli çözmez.

## Performans, izleme ve gizlilik

- **Önbellek:** okuma uç noktaları süreç içinde `last_updated_at`/`iddaa_last_update` sürümüne göre önbelleğe alınır; ayrıca `Cache-Control: s-maxage + stale-while-revalidate` ile Vercel CDN'de tutulur.
- **/api/update koruması:** IP başına dakikada 5 istek (`rate_limits` tablosu, migration `20261008120000_rate_limits.sql`) + 10 dk cooldown.
- **/api/health:** DB erişimi, son güncelleme ve iddaa çekimi yaşı. Veritabanı kapalıysa 503; veri eskimesi `ok: false` ile 200 döner ve önbelleğe alınmaz. Eşik 75 dakikadır (saatlik hızlı iş). İddaa bayatlığı, çekim başarılıysa işin bitiş saatine bakar; oran damgası haber bitmeden yazıldığı için aynı eşik erken düşmez. GitHub Actions `health.yml` saatte bir durum kodunu ve gövdeyi okur: taşıma hatası, veritabanı hatası ve beklenmeyen yanıt işi düşürür, yalnız veri eskimesi uyarıdır ve bu uyarıda kuyrukta güncelleme yoksa hızlı işi tetikler. Sunucu hataları JSON yapılandırılmış log olarak yazılır.
- **Gizlilik:** Vercel Web Analytics ve Speed Insights çerezsiz çalışır, kişisel veri veya IP saklamaz; yalnızca anonim sayfa görüntüleme ve performans metrikleri toplanır. Script etiketleri (`/_vercel/insights/script.js`, `/_vercel/speed-insights/script.js`) şu an kaldırıldı; Vercel panelinden Analytics ve Speed Insights etkinleştirilince `static/index.html`'e eklenecek.

## Güvenilirlik ve ileriye dönük ölçüm

- Güncelleme kilidi işlem token'ıyla alınır. Yalnız sahibi bırakabilir; her veri commit'inde sahiplik ve
  süre yeniden kontrol edilip uzatılır. Zaman aşımına uğrayan lig işçisinin geç kalan yazıları geri alınır.
- Süreç önbelleği en çok 30 saniye ve listedeki ilk maç başlangıcına kadar geçerlidir. Zamana duyarlı
  öneri uçlarında CDN eski yanıt sunmaz. Günün dondurulmuş kuponu geçmiş kaydı olarak korunur.
- Küçük lig kuponları tam/geçerli 1X2 bülteni ve gerçek pazar oranı ister. Varsayılan gol modeli yoktur;
  başlamış maçlar ve ertesi günün gece yarısı listeye alınmaz.
- Canlı skorlar sınırlı paralellik ve 7 saniyelik toplam bütçeyle çekilir. Lig hatasında son iyi veri ve
  gerçek son başarı zamanı korunur; arayüz güncel olmayan skoru belirtir.
- `POST /api/backtest` yönetici isteğini DB kuyruğuna kaydeder. `backtest.yml` saatte bir
  kuyruğu kontrol eder; boş kuyrukta 15 dakikada bir runner açılmaz. İş kimliği, hata/başarı ve süre sınırı DB'dedir. `GET /api/backtest` durumunu okur.
  Yerelde işleyici: `python -m app.backtest_jobs`. Geçmiş testte eş zamanlı/son 3 saatte başlamış maçlar
  eğitim verisinden çıkarılır; gerçek sonuçların bilindiği ana ilişkin sınırlamalar nedeniyle bu hâlâ geriye dönük testtir.
- `prediction_snapshots` ilk yayımlanan maç öncesi tahmini değiştirmeden saklar: model kod/parametre
  sürümü, veri sürümü, haber etkisi, saf/harmanlı/piyasa olasılıkları ve o andaki oranlar. Karne, gerçekten
  yayımlanmış ve sonuçlanmış aynı maç örnekleminde üç varyantın metriklerini/kalibrasyonunu ayrıca gösterir.
  Veriler bundan sonra birikir; bu özellik anında doğruluk veya kârlılık kanıtı üretmez. İlk yarı/handikap
  pazarları deneysel etiketlenir ve otomatik kuponlara girmez.
- SMTP yokken şifre sıfırlama token'ı/e-posta adresi loglanmaz. Gönderim için SMTP yapılandırılmalıdır.
- Runtime bağımlılıkları `requirements.txt`, test bağımlılıkları `requirements-dev.txt` içinde tüm alt
  bağımlılıklarıyla sabittir. Güncelleme kaynakları `.in` dosyalarıdır; Python 3.12 için `uv pip compile
  --universal` ile yeniden çözümlenir. Test kurulumu: `pip install -r requirements-dev.txt`.

### Veritabanı geçişi

Haber migration'ı benzersiz `20261014000001` sürümüne taşındı. `20261024000000_reconcile_and_predictions.sql`,
eski çakışmada auth veya haber şemasından hangisi eksik kalmış olursa olsun idempotent olarak tamamlar ve
tahmin tablosunu ekler. Eski migration kayıtlarını silmeyin/değiştirmeyin.

`schema.yml`, main'e migration değişikliği gelince `python scripts/ensure_schema.py` ile yalnız bu ileri
onarımı uygular; Supabase migration geçmişini değiştirmez. Güncelleme işlerinin ön adımı da aynı onarımı
kontrol eder. Normal Supabase `db push` sonradan çalıştırılırsa bu idempotent SQL tekrar güvenle uygulanır.
CI, geçici PostgreSQL 17 üzerinde temiz kurulum + eksik eski şema + tekrar uygulama senaryolarını test eder.
