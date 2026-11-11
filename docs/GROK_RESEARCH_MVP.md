# Grok araştırma API — üretim dışı MVP pilot

## Skill uyumluluğu güncellemesi

`POST /api/research/jobs/{job_id}/skill-results`, tek maçlık `football-match-intel`
çıktısını sunucudaki iş kaydıyla bağlayıp doğrulanmış ResearchResult'a dönüştürür.
Mevcut `/results` korunur. `Idempotency-Key`, `X-Research-Claim` ve
`X-Skill-Version` başlıkları gereklidir. Gövde ve dönüştürülmüş sonuç 128 KiB
sınırındadır. `job_id`, `match_id`, `mode` ve `input_version` sunucudan alınır;
Skill bu alanları verirse eşleşmeleri kontrol edilir, uyuşmazlık 409'dur. Skill'in
`matches` listesi tam bir maç içermeli ve match_id atanmış kimlikle aynı olmalıdır.

Önemli sözleşme ayrımı: `GET /schema`, dönüştürülmüş `ResearchResult` ve
`POST /results` için şemadır; `POST /skill-results` istek gövdesinin şeması
değildir. `/skill-results` ham Skill JSON'u alır, sonra sonucu sunucuda dönüştürür.
ResearchResult gövdesindeki `researched_at`, `skill_name` ve `sources` gibi üst
seviye alanlar ham Skill sözleşmesinde yer almaz ve burada 422 ile reddedilir.

Örnek ham gövde (değerleri iş ve gerçek araştırmayla değiştirin; aşağıdakiler
örnektir, doğrulanmış bir haber değildir):

```json
{
  "schema_version": "1.0",
  "mode": "full",
  "research_time": "2026-11-10T12:00:00Z",
  "timezone": "UTC",
  "matches": [{
    "match_id": "iddaa:example",
    "findings": [{
      "id": "F1", "category": "injury", "claim": "Durum bilinmiyor",
      "sources": [], "status": "unverified", "checked_at": "2026-11-10T12:00:00Z"
    }],
    "model_prior": [], "form": {},
    "lineups": {"home": {"type": "not_found"}, "away": {"type": "not_found"}},
    "lineup_surprises": [], "limitations": ["Doğrulanmış kadro bulunamadı"]
  }]
}
```

`research_time` ve kaynaklı bulgu için `checked_at` veya kaynak başına
`accessed_at` saat dilimli tarih-saat olmalıdır. `research_time`, araştırmanın
gerçek zamanı olmalı; kaynaklı bulgu sayısı, kaynaklar ve durumlar gerçeğe uygun
olmalıdır. `matches[0].match_id` işten alınır; isteğe bağlı `mode`, `job_id` ve
`input_version` sunucu işiyle çelişemez. İsteğin diğer üst düzey alanları
`schema_version`, `research_time`, `timezone` ve `matches` ile sınırlıdır.
Sonuç ve kaynak sayıları sırasıyla 80 bulgu, 40 tekil kaynak ile sınırlıdır;
bulgu başına en çok 10 kaynak referansı vardır. Bilinmeyen yayın tarihi `null`
olabilir; tarih-saat uydurulmaz. Bilinmeyen veya kaynak bulunamayan bilgi
`unverified`/`not_found` ile sunulabilir. İleri tarihli kanıt, gizli bilgi ve
bilinmeyen alanlar reddedilir; bu hata bir kanıt doğrulaması değildir.

Yeni 422 yanıtlarında `detail.path` ve `detail.reason` bulunur. `$` ham JSON'u,
`$.matches[0]...` kaynak Skill alanını belirtir. `$.converted...` ise ham alana
her zaman bire bir eşlenemeyen dönüştürülmüş ResearchResult doğrulama konumudur.
Yanıtlar alan değerlerini, kimlik bilgilerini veya gövdeyi yansıtmaz; desteklenmeyen
anahtar adları da yanıtta açıklanmaz. JSON gövdeleri ve sırlar loglanmamalıdır.

Ham nesnenin `matches[0]` alanları: `match_id`, `competition`, `home_team`,
`away_team`, `kickoff`, `venue`, `fixture_verification`, `model_prior`, `findings`,
`form`, `lineups`, `lineup_surprises`, `key_developments`, `not_found`, `limitations`.
Her bulgu için izin verilen alanlar: `id`, `team`, `category`, `player`, `claim`,
`sources`, `independent_source_count`, `status`, `conflict_note`,
`qualitative_impact`, `checked_at`. Her kaynak URL metni veya `url`, `publisher`,
`title`, `tier`, `published_at`, `origin`, `kind`, `accessed_at` alanlarından
oluşan nesnedir. Desteklenmeyen ek anahtarlar reddedilir. Skill kategorileri
`injury`, `suspension`, `form`, `other` aynı adla; `absence`, `squad`, `coach`,
`tactics` → `other`; `probable_lineup`, `official_lineup` → `lineup`; `schedule`
→ `fixture_load` olarak saklanır. Değişen kategori `skill_details.original_category`
altında sürümlü raporda korunur; `absence` sakatlık sayılmaz ve kadro kategorisi
tek başına resmî kadro veya doğrulama kanıtı değildir. Geriye dönük `news`,
`lineup` ve `fixture_load` girdileri de kabul edilir. Kaynak `kind` değeri
`club`, `league`, `federation`, `media`, `other`, `unknown`
olabilir. Üstte açıklanan yedi Skill durumu dışındaki durumlar reddedilir.
Bozuk JSON, geçersiz biçim/tür/tarih, kanıt gereksinimi ihlali, çelişkili
tekilleştirilmiş kaynak metadata'sı, çok derin içerik ve sonuç boyut/sayı sınırları
422 üretir; iş, maç veya mod uyuşmazlığı 409, ham gövde 128 KiB sınırı 413'tür.

Durumlar: `confirmed_official` ve `confirmed_independent` → `confirmed`;
`single_source` → `reported`; `conflicting` → `conflicting`; `unverified`,
`not_found`, `outdated` → `unknown`. Orijinal durum, etki, bulgu kimliği ve kaynak
gözlemleri bulgu alt alanlarında korunur. Bağımsız doğrulama beyanı için en az iki
farklı kaynak ve bağımsız kaynak sayısı en az iki; çelişki için iki kaynak gerekir.
Resmî kaynak türü açıkça yoksa `confirmed_official` daha temkinli `reported` olarak
taşınır; orijinal durum ve nedeni korunur. Bu kontroller iddiaların, resmî kaynak
türünün veya kaynak bağımsızlığının bağımsız doğrulandığını göstermez.

Kaynaklar URL'ye göre tekilleştirilir; her bulgunun referansı ve kaynak kökeni
korunur. Kaynak erişimi için `accessed_at` veya ilgili bulgunun `checked_at` değeri
gereklidir; bunlar yoksa zaman uydurulmaz. `published_at` yalnızca tarih içeriyorsa
`published_date` alanında gün hassasiyetiyle saklanır, saatli alan null kalır.
Form/kadro/fixture içindeki ayrı URL'ler, erişim anı kanıtı bulunmadığı için kaynak
gözlemi diye üretilmez; ilgili `skill_details` altında, doğrulanmamış bağlam olarak
korunur. `model_prior` da `other` bulgusu altında yapılandırılmış veri olarak
korunur; model hesaplamalarını değiştirmez veya doğrulanmış kaynak sayılmaz.

`probable/predicted` kadro `predicted` olur. İki takım için resmî kadro beyanı,
ilgili kaynağa bağlı `lineup` bulgusu ve açık resmî kaynak türü varsa `official`
olarak taşınır; bu kaynak türü ve iddia bağımsız doğrulama değildir. Kanıt
bağlamı yetersizse `unknown` kalır ve orijinal beyan ayrıntıda korunur.
Ham JSON arşivlenmez. Bilinen gizli alan/başlık ve URL kimlik bilgisi kalıpları
reddedilir; serbest metindeki her olası sırrın otomatik saptanması garanti edilemez.
Bu nedenle Skill girdisine credential veya kullanıcıya özel gizli içerik konmamalı.

Uzun araştırma için `POST /api/research/jobs/{job_id}/renew` eklendi; `jobs:claim`
kapsamı ve geçerli `X-Research-Claim` gerekir. Yeni token eskisini derhal geçersiz
kılar, bitiş en fazla 20 dakika uzar ve iş deadline'ını geçmez. Süresi dolan token
yenilenmez; deadline geçmediyse iş yeniden claim edilebilir. Eşzamanlı yenilemede
yalnız biri kazanır. Bu işlem ücretli LLM API çağrısı veya otomasyon başlatmaz.
Kayıttan sonra aynı sonucu güvenle tekrar istemek mevcut token ile mümkündür;
yenileme öncesindeki token tekrar isteğinde de reddedilir.

### Claim yanıtı ve istemci kurtarma

Hem `/claim` hem `/renew` aynı yapıda yanıt döndürür; aşağıdaki değerler sahtedir:

```json
{
  "job": {
    "id": "fake-job", "match_id": "iddaa:example", "mode": "full",
    "status": "claimed", "deadline": "2026-11-12T18:00:00Z",
    "input_version": "example-only", "created_at": "2026-11-12T15:00:00Z",
    "report_id": null, "lease_expires_at": "2026-11-12T15:20:00Z"
  },
  "claim_token": "example-only-not-a-real-token"
}
```

`lease_expires_at` üst seviyede değil `job` içindedir. `report_id` ve
`lease_expires_at` iş görünümünde null olabilir; istemci eksik/bozuk opsiyonel
alanlara dayanıklı olmalıdır. `claim_token` ve iş kimliği güvenli işlem için
gereklidir. Başlangıç claim'i 20 dakikadır ve mevcut uygulamada lease zamanı iş
deadline'ını aşabilir; iş deadline'ı ayrıca dikkate alınır. `/renew` yeni token
verir, eskisini hemen geçersiz kılar ve yeni lease'i deadline ile sınırlar.

Örnek `scripts/research_pilot_client.py` import edilen bir yardımcıdır, kendi
başına otomasyon başlatmaz. Çağıran kod doğrulanmış HTTPS ve kapalı redirect
kullanan bir HTTP istemcisi ile secret kasasından belleğe aldığı API anahtarını
verir. HTTP istemcisinde header ve gövde loglaması kapalı olmalıdır. API anahtarı
ve claim token'ı dosyaya, komut argümanına veya çıktıya yazılmamalıdır.

```python
# httpx veya httpx2 bağımlılığı gerekir (httpx requirements-dev.txt içinde).
# api_key: secret kasasından yalnız belleğe alınmış değer
# job_id ve skill_output: aynı, süresi dolmamış atanmış işin gerçek bağlamı
from scripts.research_pilot_client import secure_http_client, ResearchPilotClient, PilotError, SubmissionUncertain
http = secure_http_client('https://staging.example.invalid')
pilot = ResearchPilotClient(http, api_key, job_id)
lease_info = pilot.claim()  # Süre bilgisi mevcut değilse None; token içermez.
try:
    result = pilot.submit(skill_output, 'ayni-istek-icin-sabit-kimlik', 'skill-surumu')
except SubmissionUncertain:
    # Aynı süreçte aynı token, gövde ve idempotency anahtarıyla güvenli tekrar.
    result = pilot.retry_submission()
except PilotError:
    # pilot nesnesini ve süreci açık tut; sır/gövde yazdırmadan incele.
    result = None
```

İsteğin cevabı kaybolursa sunucu sonucu kaydetmiş olabilir. Aynı süreçteki
`retry_submission()` aynı gövde ve idempotency anahtarıyla yeniden dener. Aynı
iş farklı içerikle gönderilemez. İşlem sona ermiş ve token da kaybolmuşsa önce
`GET /jobs` ile iş ve `report_id` kontrol edilir; rapor varsa tekrar oluşturulmaz.

Kaybolan claim token'ı yalnız özeti saklandığından sunucudan geri okunamaz. İş
henüz bitmemiş ve deadline geçmemişse istemci `GET /jobs` ile güncel lease
sonunu kontrol edip süre dolduktan sonra açık operatör kararıyla yeniden claim
alabilir; `reclaim_after_loss()` bu kontrolleri yapar. Yeni claim eski token
özetini değiştirir. Token süresi dolunca yeni rapor kaydı yapılamaz; mevcut
uygulama, daha önce kaydedilmiş *aynı* sonucun idempotent tekrarını token özeti
hâlâ eşleşiyorsa süresi geçse de döndürebilir. Yeni claim bu eşleşmeyi bozar.
Claim/renew yanıtı kaybolursa sunucu işlemi yapmış olabilir: otomatik tekrar
veya tokenın hâlâ geçerli olduğunu varsayma; sunucunun iş görünümünü kontrol et.
Deadline dolmuşsa yeniden claim 410 verir; eski araştırmanın zamanını değiştirme.

Bu özellik Grok/xAI/OpenAI istemcisi içermez, LLM API çağrısı yapmaz ve Grok üzerinde
rutin oluşturmaz. API varsayılan olarak kapalıdır. Mevcut öneri motoru, Poisson/Elo,
haber etkileri, kalibrasyon ve kupon seçimi değiştirilmedi.

Kod tabanı: main `bd876ce`, çalışma dalı `codex/grok-research-mvp`. Production migration,
production ortam değişikliği, deployment veya git push yapılmadı. Grok hesabında
gerçek Skill çalıştırılması ve güvenli secret aktarımı bu yerel doğrulamanın dışındadır.

## Dosyalar ve mimari

- `app/research_api.py`: yalnız yeni ad alanına uygulanan bearer, gövde sınırı, JSON ve no-store koruması.
- `app/research_service.py`: servis kapsamları, sahiplik, manuel kuyruk, lease, idempotency ve sürümler.
- `app/research_context.py`: SELECT-only gerçek maç/fiyat/haber/mevcut tahmin adaptörü.
- `app/research_models.py`: `football-match-intel` sonuç sözleşmesi.
- `app/research_store.py`: izole SQLite pilot şeması ve ayrı PostgreSQL bağlantısı.
- `app/research_admin.py`: yalnız açıkça belirtilen ayrı SQLite üzerinde manuel yönetim.
- `app/main.py`: yeni router kaydı. `app/auth.py`: yalnız `/api/research/` için mevcut
  kullanıcı çerezi yerine zorunlu servis doğrulaması. Araştırma anahtarı diğer API'lerde oturum değildir.
- `.env.example`: kapalı özellik ayarı; gerçek sır içermez.
- `supabase/migrations/20261109000000_ai_research.sql`: ayrı şema, görünümler ve NOLOGIN rolü.
- `tests/test_research_api.py`, `scripts/research_postgres_check.py`: davranış ve DB izinleri.
- `scripts/test_postgres.py`: araştırma kontrolünü mevcut geçici CI testine bağlar;
  eski şema onarımının dosyasını artık açık adıyla seçer.

Başlangıçta `db.init_db()` araştırma şemasını otomatik kurmaz. Yeni migration,
mevcut veri yenilemesinin `ensure_schema.py` yoluna eklenmedi.

## Endpoint'ler

Hepsi `RESEARCH_API_ENABLED=1` ve geçerli servis anahtarı gerektirir.
Hem başarı hem hata yanıtlarında tarayıcı/CDN `no-store` başlıkları vardır.

| Endpoint | Kapsam | Davranış |
|---|---|---|
| `GET /api/research/jobs?limit=25` | `jobs:read` | Yalnız kendi işleri; en fazla 100. Süresi geçenler `expired`. |
| `GET /api/research/matches/{match_id}/context?job_id=...` | `context:read` | Yalnız kendisine atanmış işin dondurulmuş girdi görüntüsü. |
| `POST /api/research/jobs/{job_id}/claim` | `jobs:claim` | 20 dakikalık üstlenme; eşzamanlı ikinci talep 409. |
| `POST /api/research/jobs/{job_id}/results` | `reports:write` | Şema/sahiplik/iş/maç/mod/sürüm/lease kontrolü; yalnız yeni sürüm ekler. |
| `GET /api/research/reports/{id}?revision=1` | `reports:read` | Yalnız kendi raporu; sürüm verilmezse en son sürüm. |
| `GET /api/research/schema` | `reports:write` | Gerçek doğrulayıcıdan üretilen JSON Schema. |

`results` başlıkları: `Content-Type: application/json`, `Idempotency-Key` ve claim
yanıtından alınan `X-Research-Claim`. Aynı iş + aynı anahtar + aynı normalize edilmiş
JSON tekrar gönderilirse aynı kayıt döner (`duplicate=true`). Değişik sonuç 409'dur.

Başka servisin iş/raporu 404, eksik/geçersiz/iptal edilmiş anahtar 401, eksik kapsam
403, hatalı JSON 422, büyük gövde 413, yanlış içerik türü 415, süre geçmiş claim
410, rate limit 429, kapalı/hazır olmayan depo 503 döner. Sonuç boyutu 128 KiB'dir.
Rate limit: servis kimliği başına 60/dakika, bağlantı adresi başına 180/dakika.
IP için istemcinin gönderdiği güvenilmeyen forwarded başlıkları kullanılmaz.

## Kimlik, sahiplik ve veritabanı sınırı

Servis anahtarı biçimi `key_id.secret`'tir. `secret` 32–256 karakter olmalı ve gerçek
kurulumda güvenli rastgele kaynaktan gelmelidir. MVP geliştirilirken gerçek anahtar
üretilmedi; testlerde yalnız geçici DB'ye kayıtlı deterministik test değerleri kullanılır.

DB yalnız tam anahtarın SHA-256 özetini saklar. Anahtarın son kullanma tarihi,
iptal bayrağı, servis kimliği ve kapsamları sunucudadır. Bot JSON ile sahip veya
doğrulama/onay durumu atayamaz. Yeni iş oluşturma yetkisi HTTP API'de yoktur.
Anahtarlar koda, komut satırı argümanına, Skill'e, rapora veya vault notlarına yazılmaz.

PostgreSQL için `RESEARCH_DATABASE_URL` zorunlu ayrı runtime bağlantısıdır. Ana
uygulamanın `DATABASE_URL` değeri tanımlıysa araştırma bu bağlantıya otomatik düşmez;
ayrı bağlantı yoksa 503 verir. Bu koruma, yanlışlıkla production bağlantısının
araştırma deposu olarak kullanılmasını önler.
Bağlantı rolünün lol_research_runtime üyeliği, superuser/CREATEDB/CREATEROLE/BYPASSRLS
olmaması ve public tablolarda yazma yetkisi bulunmaması da her bağlantıda kontrol
edilir. Ayrı değişken adı altında geniş yetkili DSN verilirse de 503 döner.

Migration `ai_research` şemasında `service_keys`, `jobs`, `reports`,
`report_revisions`, `rate_limits` oluşturur. `lol_research_runtime` NOLOGIN rolüdür;
parola veya login oluşturmaz. Mevcut tablolarda değişiklik yapmaz. Rol:

- Onaylı kaynak görünümlerini ve anahtar özetlerini okuyabilir.
- İş durumunu ve kendi katmandaki sayaçları güncelleyebilir, rapor/sürüm ekleyebilir.
- Anahtar oluşturamaz, mevcut matematiksel tablolara yazamaz.
- Eski rapor sürümlerini UPDATE/DELETE edemez, araştırma şemasında DDL yapamaz.

Sahiplik HTTP servisinde uygulanır; ayrı Bot'ların aynı bulut bilgisayarı kullanması
güvenlik sınırı değildir. DB runtime bağlantısı yalnız sunucuda kalır. SQLite pilotta
dosya rolü yoktur; dosya, yalnız güvenilir yerel uygulama sürecine verilir.

## Veri sözleşmesi ve sürümler

İş: `id`, `match_id`, `mode`, `status`, `deadline`, `input_version`, `created_at`.
`input_version`, dondurulan maç/fiyat/tahmin/haber bağlamının SHA-256 özetidir.
`context` mevcut `prediction_snapshots` kaydını okur; tahmin yoksa `null` döner.
Yeni tahmin üretmez, fiyat çekmez, eski öneri kaydı yazmaz. `model`, `market`,
`blend` ayrı kalır. Kaynak gözlem tarihleri korunur; eski fiyat güncel ilan edilmez.
`iddaa:<event_id>` ile model eşleşmesi olmayan bülten maçları da desteklenir.

Grok sonuç örneği (yer tutucuları alınan işten değiştirin):

```json
{
  "schema_version": "1.0",
  "job_id": "ALINAN-JOB-ID",
  "match_id": "ALINAN-MATCH-ID",
  "mode": "full",
  "input_version": "ISIN-64-KARAKTERLIK-SHA256-DEGERI",
  "skill_name": "football-match-intel",
  "skill_version": "KULLANILAN-SKILL-SURUMU",
  "researched_at": "ARASTIRMANIN-GERCEK-RFC3339-ZAMANI",
  "summary": null,
  "sources": [],
  "findings": [],
  "injury_assessment": {"summary": null, "verification_status": "unknown", "source_ids": []},
  "lineup_assessment": {"summary": null, "verification_status": "unknown", "source_ids": [], "lineup_status": "unknown"},
  "risks": [],
  "limitations": ["Kaynak bulunmadıysa açıkça belirtin"]
}
```

`sources`: `id`, HTTP(S) `url`, isteğe bağlı `publisher`, `title`, `published_at`,
zorunlu `accessed_at`, `kind` (`club/league/federation/media/other/unknown`).
`findings`: `category` (`news/injury/suspension/lineup/form/fixture_load/other`),
`statement`, isteğe bağlı `team/player/observed_at`, `verification_status`, `source_ids`.
Doğrulama durumları `reported/confirmed/conflicting/unknown`'dır. Kaynak kimlikleri
mevcut olmalı; unknown dışındaki iddialar kaynak gerektirir. Resmî kadro için
club/league/federation türünden kaynak ve confirmed durumu gerekir. Bunlar yapısal
kontroldür; haberin doğruluğunu bağımsız olarak kanıtlamaz. API kaynak URL'lerini
kendisi indirmez veya çalıştırmaz.

Kaynak tarihleri null olabilir; araştırma/erişim zamanları saat dilimi içermelidir.
İleri tarihli kanıt (5 dakika saat farkı toleransı dışında), bilinmeyen alanlar,
sahiplik/model uygulama talimatları, yanlış mod/maç/sürüm reddedilir.

Her iş tek sonuç alır. Rapor kökü servis + maç + mod için sabittir; yeni işin
sonucu revision 2, 3… olarak eklenir. Operatör aynı bağlam için bilinçli düzeltme
istiyorsa yeni `--request-id correction-1` verir; normal tekrarlı create-job aynı
işi döndürür. Kaynaklar/findings/değerlendirmeler/riskler değişmez JSON sürümünde
tutulur. Eski sürümler `?revision=N` ile okunur.

Geçerli lease ile gelen geç sonuç, girdi değiştiyse veya deadline/maç zamanı
geçtiyse `expired` arşiv olarak saklanır; güncel sayılmaz. Lease süresi bittiyse
409 döner; deadline uygunsa iş yeniden üstlenilebilir. Rapor okunurken güncel
maç/girdi tekrar kontrol edilir; `freshness_at_ingest` ayrıca korunur. Model
uygulaması her zaman `false`'tur.

## Yerel tek maç pilotu

Önce otomatik, tamamen izole entegrasyon testini çalıştırın:

```sh
python -m unittest tests.test_research_api.ResearchApiTest.test_end_to_end_unknown_fields_and_idempotent_result
python -m unittest tests.test_research_api
```

Bu test gerçek FastAPI handler'larını ve geçici SQLite şemasını kullanır; Grok'u
taklit eden HTTP istemcisi yalnız test sonucunu gönderir. LLM, abonelik veya üretim
bağlantısı yoktur. Test anahtarı dışarıya açılmış bir sunucuda kullanılmamalıdır.

Grok ile gerçek maç operatör pilotu (henüz çalıştırılmadı):

1. Mevcut **yerel** verinin, oturum/kullanıcı verileri ayıklanmış ayrı SQLite kopyasını
   hazırlayın; canlı DB adresini Bot'a vermeyin. Kaynak kopyanın güncelliğini doğrulayın.
2. `DATABASE_URL` ve `RESEARCH_DATABASE_URL` değerlerini bu terminalde boş tutun.
   `python -m app.research_admin --db /ABS/PATH/pilot.sqlite init-local` çalıştırın.
   Komut PostgreSQL'i ve bu çalışma kopyasının varsayılan `data/iddia.db` dosyasını reddeder.
3. Ayrı pilot servis sırrını güvenli yöntemle hazırlayın. Tam `key_id.secret`
   anahtarın SHA-256 özetini güvenilir yerel ortamda hesaplayın; düz anahtarı komut
   argümanına koymayın. Gerçek anahtar üretimi/aktarımı bu MVP çalışmasında yapılmadı.
4. Yalnız özeti kaydedin:

   ```sh
   python -m app.research_admin --db /ABS/PATH/pilot.sqlite register-key \
     --key-id grok_pilot --principal-id grok-football-intel \
     --token-sha256 SHA256_OZETI --expires-at GELECEK_RFC3339_ZAMANI
   ```

5. Yaklaşan bir gerçek maç için manuel iş oluşturun; deadline kickoff'tan sonra olamaz:

   ```sh
   python -m app.research_admin --db /ABS/PATH/pilot.sqlite create-job \
     --owner grok-football-intel --match-id MAC_ID --mode full \
     --deadline GELECEK_RFC3339_ZAMANI
   ```

6. Yalnız yerelde, otomatik veri yenileme kapalı çalıştırın. `LOCAL_PYTHON` mevcut
   sanal ortamınızın Python yoludur:

   ```sh
   DATABASE_URL='' RESEARCH_DATABASE_URL='' RESEARCH_API_ENABLED=1 \
   LOCAL_AUTO_UPDATE=0 LOCAL_DB_PATH=/ABS/PATH/pilot.sqlite \
   LOCAL_PYTHON=/ABS/PATH/venv/bin/python PORT=8014 npm start
   ```

7. Yetkili HTTP istemcisiyle jobs → claim → context akışını çalıştırın. Skill'e
   dönen işin `mode`, `match_id`, `input_version` ve kaynak tarihlerini aktarın.
   20 dakika içinde JSON'u results'a gönderin. Aynı gövdeyi aynı Idempotency-Key
   ile tekrar gönderince duplicate=true ve tek sürüm görülmeli. Report GET ile
   kaynakları, null/unknown durumlarını ve `applied_to_model=false` değerini kontrol edin.
8. `lineup_check` için ayrı manuel iş oluşturun. Gerekirse önceki full işinin
   `report_id` değeriyle kendi raporunu okuyarak karşılaştırma yapın.

Anahtar iptali: `python -m app.research_admin --db /ABS/PATH/pilot.sqlite revoke-key --key-id grok_pilot`.
Yerel CLI bu MVP'de PostgreSQL anahtar/iş yönetimini bilinçli olarak açmaz; staging
PostgreSQL kurulumu ve operatör erişimi ayrıca incelenmelidir.

## Grok bulut bilgisayarından HTTPS bağlantısı

`localhost:8014`, Grok'un bulut bilgisayarından Mac'inize erişmez. Dış erişim için
ayrıca onaylanmış, geçerli TLS sertifikalı **üretim dışı** HTTPS adresi gerekir.
Bu çalışmada deployment, tünel, DNS değişikliği veya Grok bağlantısı kurulmadı.

Grok'a aktarılacak yapılandırma:

- `LAST_OF_LIFE_RESEARCH_BASE_URL`: yalnız üretim dışı HTTPS kökü; sır değildir.
- `LAST_OF_LIFE_RESEARCH_TOKEN`: tam pilot servis anahtarı; güvenli secret kartı/Vault
  veya HTTP aracının eşdeğer güvenli bağlantı mekanizmasıyla saklanır.
- İzin verilen işlemler: jobs, claim, context, schema, results ve kendi report GET.
- Skill: `football-match-intel`; moda göre full/lineup_check. Mevcut Skill'in gerçek
  çıktısı bu sözleşmeye eşlenmeli; Skill sürümü raporda yer almalı.

Production DATABASE_URL, Supabase service_role, Vercel/GitHub yönetim token'ı,
Mac klasörü veya Obsidian vault erişimi gerekmez. Claim token yalnız kısa ömürlü iş
belirtecidir; rapora/Skill'e kalıcı kaydedilmemeli veya loglanmamalıdır.

Grok'un güvenli secret mekanizmasının özel HTTP başlığına anahtarı uygulayabilmesi,
Skill'in iki modda bu JSON'u üretmesi ve sonuç gönderiminin gerekli onaylarla
çalışması gerçek Bot üzerinde henüz test edilmedi. Bu doğrulamayı otomatik rutin
oluşturmadan tek manuel görevle yapın. Skill veya web sayfasındaki talimatlar API
yetkilerini genişletemez.

## Üretime geçiş için ayrıca onaylanacaklar

- Migration ve NOLOGIN rolü; gerçek Supabase proje/bağlantı hedefi ve view izinleri.
- Ayrı runtime login/bağlantı ve sır yönetimi; mevcut production DB parolasının kullanılmaması.
- Üretim servis kimliği/anahtarının oluşturulması, güvenli aktarımı, süre ve iptal planı.
- Kod incelemesi, git push/PR/main entegrasyonu ve Vercel dağıtımı.
- RESEARCH_API_ENABLED/RESEARCH_DATABASE_URL ortam ayarları ve HTTPS erişim politikası.
- Gerçek Grok Skill pilotunun başlatılması ve abonelik tüketim bütçesi.
- Otomatik iş seçimi, webhook veya zamanlanmış rutin: MVP kapsamı dışında, ayrı onay.
- Araştırmayı matematiksel modele, mevcut news tablosuna veya kuponlara uygulamak:
  mevcut MVP'de böyle bir yol yok; ayrı tasarım ve açık onay gerektirir.

## Yerel doğrulama kanıtı — 9 Kasım 2026

Yeni 24 API testi; yetkisiz/iptal/expired anahtarlar, scope, sahiplik, yanlış maç,
girdi sürümü, tekrar ve eşzamanlı sonuçlar, sürüm koruma, lease, eski araştırma,
hatalı JSON, kaynak referansları, body/rate limit ve mevcut tabloların/motorun
değişmemesini kapsar. Son tam yerel testte **494 Python ve 92 JavaScript testi geçti**.
`git diff --check` temizdir. Bunlar CI veya üretim doğrulaması değildir.

Geçici yerel PostgreSQL'de migration/SQL kontrolleri; ayrı NOLOGIN rolünden yetki
alan parolasız yerel test login'i ile altı endpoint, idempotency ve geniş yetkili
DSN'nin reddedilmesi kontrol edilir. Mevcut kaynaklara ve eski rapor sürümlerine
yazma/silme dahil 11 yasak işlem DB tarafından reddedilir. Bu login yalnız geçici,
loopback test sunucusundadır; production hesabı veya credential değildir.

Mevcut yerel gerçek veri kopyasının geçici kopyasında Chapelton Maroons FC –
Racing United FC (`iddaa:3282544`, 10 Kasım 01:05 TSİ) için 18 gerçek fiyat satırı
context'e ulaştı. Tahmin snapshot'ı bulunmadığı açıkça null döndü. Context → claim →
test sonucu → report akışları HTTP 200 verdi; eski maç/haber/oran/tahmin/meta/kupon
tabloları byte-değer karşılaştırmasında aynı kaldı. Test raporu bilinçli olarak
"Grok araştırması yapılmadı" sınırlaması taşıdı ve geçici DB test bitince silindi.
Bu, güncel bahis önerisi veya Grok Skill'inin çalıştığına dair kanıt değildir.

## Girişli kullanıcı için Grok Intelligence görünümü

`RESEARCH_UI_ENABLED=1` ve açıkça tanımlı `RESEARCH_UI_OWNER` ile, ayrı araştırma
veritabanı bağlantısı kullanılarak etkinleşir. `/api/grok-intelligence/{match_id}`
her durumda yönetici oturumu ister; `AUTH_REQUIRED=0` erişimi açmaz.
Yalnız sunucuda belirtilen araştırma sahibinin en fazla 30 sürümünü, salt okunur
ve `private, no-store` olarak döndürür. Yalnız uygulama yöneticileri seçilen sahibin raporlarını görebilir; kullanıcı bazlı rapor sahipliği değildir.
Kayıt veya konfigürasyon yoksa boş durum ya da güvenli hata gösterilir. Servis
anahtarı tarayıcıya gönderilmez. Canlıya geçmeden önce yönetici erişim politikası,
veri kaynağı rolü ve kimlik doğrulama ayarları ayrıca gözden geçirilmelidir.
Özgün bulgular, adaptörün sabit `Skill alanı:` etiketli bağlam kayıtlarından
ayrılır; ham Skill çıktısı saklanmadığından bu ayrım kayıttaki işaretlere dayanır.
Bilgi ve kaynak etiketleri tek başına bağımsız doğrulama değildir.

PostgreSQL arayüzü için ayrıca `RESEARCH_UI_DATABASE_URL` ve yalnız
`lol_research_ui_reader` rolü gereklidir; Bot veritabanı bağlantısına geri dönüş
olmaksızın hata verir. Yeni rol ve queue migration'ları yalnız inceleme ve izole
veritabanı testleri içindir. Kısıtlar ve geçiş kararları:
`docs/GROK_QUEUE_AND_EVALUATION.md`.
