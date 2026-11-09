# Grok Research: izole staging pilot hazırlığı

Bu hazırlık `51508e1` tabanlıdır. Production PostgreSQL'e bağlanmaz; ayrı yerel
PostgreSQL kullanır. Gerçek servis sırrı oluşturma, public HTTPS tüneli açma ve
Grok'ta çalıştırma kullanıcı onayı olmadan yapılmaz.

## Seçim

Tek maçlık geçici pilot için Mac üzerinde yalnız `127.0.0.1` dinleyen ayrı PostgreSQL
cluster + yalnız loopback'te Research API + onaydan sonra Cloudflare Quick Tunnel.
Mac açık kalmalı; Quick Tunnel geçicidir ve adresi açılınca belirlenir. Bulut Bot
Mac'in `localhost` adresine doğrudan erişemez. Tünel API'yi internete açar: gerçek
pilot kimliği, erişim süresi, kullanılacak tünel yapılandırması ve kapatma adımı
önceden gözden geçirilmelidir. Tünel araçları bu makinede hazır bulunmadı; kurulum
ve harici tünel oluşturma bu hazırlıkta yapılmadı.

Alternatif: ayrı bir Supabase Free projesi. Yerel Mac bağımlılığı kalkar; ayrıca
proje, ağ ve kimlik yapılandırması gerekir. Hesabın ücretsiz proje kotası ve
faturalaması doğrulanmadı. Her iki alternatif de Grok abonelik kullanımı gerektirir.

## Hazır doğrulama

`scripts/test_postgres.py` yalnız localhost ve `lol_ci` adını kabul eder; yine de
**var olan bir yerel veritabanında çalıştırmayın**. Test, geçici ayrı PostgreSQL
cluster'ında çalıştırılmalıdır; uygulama migration'larını yalnız bu geçici veritabanına
uygular. `scripts/research_postgres_check.py` fixture maç ve yalnız test anahtarıyla
schema/jobs/context/claim/renew/skill-results/reports, tekrarlı gönderim ve DB
yetki retlerini kontrol eder. `TestClient` kullandığı için bu kontrol gerçek ağ,
TLS, tünel veya Grok Bot doğrulaması değildir.

## Operatör: gerçek pilot için uygulanacak işlemler

1. Yeni, yalnız yerel PostgreSQL cluster oluşturun. Production bağlantısı veya
   production kopyası kullanmayın. Veritabanını açık şekilde pilot olarak tanımlayın.
   PostgreSQL `listen_addresses=127.0.0.1`, ayrı port, ayrı veri dizini kullanın.
2. Tüm proje migration'larını **yalnız yeni pilot DB'ye** uygulayın; pilotun DB adı,
   host/portu ve process'i production'dan ayrı doğrulanmadan devam etmeyin. `ai_research`
   migration'ı mevcut tablo ve görünüm tanımlarına bağlıdır. Başka veritabanına
   otomatik migration uygulayan komutlar kullanmayın.
3. Migration'ın `lol_research_runtime` NOLOGIN rolüne ait ayrı login'i yalnız
   pilotta kurun. Runtime login superuser, CREATEDB, CREATEROLE, BYPASSRLS veya
   public tablolarına yazma yetkisi taşımamalıdır. Gerçek parola üretimi bu görevde
   yetkilendirilmedi. Bağlantı bilgisini ortam değişkeni/secret yöneticisinde tutun;
   konsola, loga, Git'e ve Vault notlarına yazmayın.
4. `app.research_admin` yalnız SQLite'ı destekler. Yerel PostgreSQL pilotu için
   `scripts/research_staging_admin.py`, yalnız `localhost/127.0.0.1:55439/lol_staging`
   hedefini kabul eder. Sunucunun gerçek adresini, portunu ve veritabanı adını da
   kontrol eder; `DATABASE_URL` veya `RESEARCH_DATABASE_URL` doluysa durur.
   Admin bağlantısı `STAGING_ADMIN_DSN` üzerinden gizli sağlanır, yazdırılmaz.
   Aracı yalnız oluşturduğunuz ayrı cluster'a bağlayın; production sunucusuna yerel
   port yönlendirmesi yapmayın. Runtime rolüne kimlik/iş oluşturma yetkisi vermeyin.

   Onaylanmış servis anahtarının **yalnız SHA-256 özetini** güvenli yerel ortamda
   hesaplayıp `register-key` ile kaydedin; plaintext değeri argüman olarak vermeyin:

   ```sh
   python scripts/research_staging_admin.py register-key --key-id pilot \
     --principal-id grok-pilot --token-sha256 OZET --expires-at RFC3339
   python scripts/research_staging_admin.py create-job --owner grok-pilot \
     --match-id ATANACAK_MAC_ID --mode full --deadline RFC3339
   ```

   İş iptali için değil, anahtarı devre dışı bırakmak için `revoke-key --key-id pilot`
   kullanılır. Bu adımlar gerçek secret hazırlanmasını gerektiriyorsa önce onay alınır.
5. Bu hazırlıkta daha önce mevcut olan yerel veri kopyasından yalnız bir yaklaşan
   İddaa olayı, 18 fiyat satırı ve ilgili sınırlı kaynak metadatası seçilerek pilot
   DB'ye kopyalandı; production'a bağlantı yapılmadı. Kullanıcı/oturum/kişisel
   kupon verisi taşınmadı. Kayıt eşleşmiş takım-model snapshot'ı içermiyor;
   snapshot null. Fiyat gözlemi hazırlık anında yaklaşık 6,2 saat eskiydi; güncel
   olarak sunulmamalı. Gerçek Bot denemesinde tazelik ve maç zamanı tekrar kontrol
   edilmeli. Düzenli/güncel veri aktarımı için tekrar kullanılabilir bir prosedür
   henüz yok; canlı veriye erişim ayrıca ele alınmalıdır.
6. Onaylı ortamda API `RESEARCH_API_ENABLED=1` ve ayrı kısıtlı
   `RESEARCH_DATABASE_URL` ile yalnız loopback'e bağlanır. `LOCAL_AUTO_UPDATE=0`
   kullanılmalı. Araştırma API kullanıcı çereziyle değil kendi kapsamlı servis
   kimliğiyle çalışır. Uygulamanın normal `DATABASE_URL` değerini production'a
   yönlendirmeyin: uygulama lifespan ve diğer endpoint'ler de onu kullanabilir.
   `AUTH_REQUIRED=1` diğer API'lerin kullanıcı oturumu gerektirmesini sağlar.
   Komutta gerçek URL yazmak yerine güvenli mekanizmadan alınan ortam değişkenini
   kullanın; API'yi sadece lokal arayüzde çalıştırın:

   ```sh
   DATABASE_URL='' LOCAL_AUTO_UPDATE=0 AUTH_REQUIRED=1 RESEARCH_API_ENABLED=1 \
   RESEARCH_DATABASE_URL="$SECURE_STAGING_RUNTIME_DSN" \
   LOCAL_DB_PATH=/ABS/PATH/staging-app.sqlite \
   /ABS/PATH/venv/bin/python -m uvicorn app.main:app \
     --host 127.0.0.1 --port 8014 --no-access-log
   ```

   Bu API pilotunda erişim logları kapalıdır. Gerçek servis anahtarını terminal
   argümanına yapıştırmayın.
7. Servis anahtarının özeti ve manuel iş yetkili operatörce oluşturulduktan sonra,
   Grok'a yalnız onaylı HTTPS kökü ve güvenli secret aktarım yöntemi verilir. Tünel
   adresi henüz mevcut değildir; bu belgede bir adres veya gerçek anahtar yoktur.
8. Tek işte schema → jobs → claim → context → renew → skill-results → reports
   yürütülür. Başka işe/maça erişim, eski lease, tekrarlı gönderim ve 128 KiB sınırı
   da kontrol edilir. İş bitince tünel durdurulur ve servis kimliği iptal edilir.

## Grok'a verilecek ilk talimat taslağı

> Bu görev yalnız benim onayladığım staging adresi ve sana güvenli biçimde sağlanan
> sınırlı araştırma kimliği ile tek maç içindir. Önce şemayı ve atanmış işi al;
> claim yap ve context'teki match_id, mode, input_version ve job_id'yi aynen koru.
> football-match-intel Skill'ini işin modunda çalıştır. Gerekirse süre bitmeden
> claim token'ını yenile. Tek maçlık çıktıyı skill-results'a gönder ve report'u
> oku. Sırları sohbete, dosyalara veya rapora yazma. Production, kod, tahmin,
> haber ve kupon tablolarını değiştirme; otomasyon kurma. Kaynak erişim saati veya
> kimlik bilgisi eksikse uydurma, durumu açıkça bildir.

HTTP istekleri: `Authorization: Bearer <güvenli servis anahtarı>`; sonuç için
`Content-Type: application/json`, `Idempotency-Key`, `X-Research-Claim` ve
`X-Skill-Version`; renew için `X-Research-Claim`. Değerler burada bilerek yoktur.
Grok'un güvenli secret'ı HTTP başlığına enjekte etmesi henüz test edilmedi.

## 9 Kasım 2026: tamamlanan yerel hazırlık

- Ayrı, yalnız loopback'te `55439` portunu dinleyen geçici PostgreSQL cluster'ında
  `lol_staging` ve ayrı test veritabanı oluşturuldu; migration'lar yalnız bu yerel
  DB'lere uygulandı. Production DB'ye bağlanılmadı. Cluster süreç ömrüne ve geçici
  dosya konumuna bağlıdır; Mac kapanırsa hizmete erişilemez.
- **Mevcut geçici test cluster'ı yerel `trust` kimlik doğrulaması kullanır.**
  Yalnız sahte test kimlikleri ve seçilmiş kamuya açık veriler içindir. Gerçek
  servis sırrı eklenmeden veya HTTPS tüneli açılmadan önce staging PostgreSQL'in
  yerel kimlik doğrulaması sıkılaştırılmalı, ayrı runtime/admin kimlik bilgileri
  güvenli biçimde oluşturulmalı ve test hesapları gözden geçirilmelidir. Bu gerçek
  sır oluşturma ve yapılandırma değişikliği ayrıca kullanıcı onayı gerektirir.
- Sahte maç ve test kimliğiyle operatör kimlik kaydı/iş oluşturma ve schema, jobs,
  claim, context, renew, skill-results, reports akışı TestClient üzerinden 200;
  aynı sonucu yeniden gönderme `duplicate=true`. Test kimliği iptal edildi.
- Aynı yedi uç nokta çalışan `127.0.0.1:8014` sunucusunda gerçek yerel HTTP ile de
  sahte kimlik/maç kullanılarak kontrol edildi; hepsi 200 ve tekrar `duplicate=true`.
  HTTP test kimliği de iptal edildi. Sunucu yalnız loopback dinler ve erişim logu kapalıdır.
- Yerel kamuya açık maç kopyasından seçilen bir maç için `grok-pilot` sahibine
  `full` işi hazırlandı. Yalnız iş hazırlamak için kullanılan, geçerli secret'ı
  bulunmayan placeholder kimlik iptal edildi; gerçek servis anahtarı yok. İşin son
  zamanı 10 Kasım 2026 01:44 TSİ'dir; pilot gecikirse yeniden iş açılmalı ve maçın
  başlamadığı doğrulanmalı. Gerçek kimlik oluşturulursa aynı principal kullanılmalı.
- 506 Python ve 92 JavaScript testi ile ayrı yerel PostgreSQL migration/yetki
  kontrolleri geçti. Yerel HTTP dahil bu testler HTTPS veya gerçek Grok testi değildir.
