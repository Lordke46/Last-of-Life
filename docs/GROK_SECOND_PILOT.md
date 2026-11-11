# Grok araştırma istemcisi — manuel staging pilotu

İstemci yalnız HTTPS Research API'yi kullanır; Mac dosyalarına veya PostgreSQL'e
bağlanmaz. `scripts/research_pilot_client.py` dosyasını Grok'un bulut ortamına
kullanıcının aktarabileceği güvenli bir yolla koyun; GitHub'a push gerektirmez.
Python 3 ve `httpx` veya `httpx2` gerekir. Skill'in dosya konumunu veya çalıştırma
komutunu tahmin etmeyin: Grok'tan ortamında `football-match-intel` Skill'inin ve
ürettiği JSON'un konumunu doğrulamasını isteyin. Skill'i yalnız kullanıcı istediğinde
bir kez çalıştırın; istemci Skill'i otomatik çalıştırmaz.

Ön koşullar: ayrı staging HTTPS adresi, henüz başlamamış bir maç için operatörün
oluşturduğu tek iş, doğru kapsamlı geçerli servis anahtarı ve HTTP header/gövde
loglamasının kapalı olması. Anahtarı güvenli secret kasasından aynı Python sürecine
alın; komut argümanına, URL'ye, çıktıya veya dosyaya koymayın. Aşağıdaki
`LOL_RESEARCH_API_KEY` örneği yalnız kasa gizli değeri bu isimle **ortam değişkeni
olarak gerçekten enjekte ediyorsa** çalışır; enjekte edilmiyorsa durun ve ortamın
güvenli secret okuma yolunu doğrulayın. Anahtarı yazdırmayın.

1. İstemciyi aktarın. Güvenli bir Python süreci açın ve aşağıdaki kurulumla iş
   listesinden operatörün hazırladığı işi seçin. `PILOT_API_URL` ve `PILOT_JOB_ID`
   yalnız staging URL'si ve iş kimliğidir; secret içermez.
2. `fetch_context` ile atanmış maçın kickoff ve bağlamını okuyun;
   `context_warnings()` çıktılarını dikkate alın. Oranların gözlem zamanı veya
   provenance güncel değilse güncel oran olarak kullanmayın. Snapshot yoksa
   matematiksel tahmin uydurmayın.
3. Gerçek Skill'i bu iş için bir kez çalıştırın. Skill'in yazdığı ham, tek maçlık
   JSON'un yolunu Grok ortamında doğrulayın. Kasa içeriğini, HTTP başlıklarını veya
   claim token'ı Skill JSON'una koymayın. Çıktı dosyası kullanıcıya özgü/gizli içerik
   taşımamalı ve üreticinin güvenlik politikasına göre korunmalıdır.
4. Aynı Python sürecinde çıktıyı `prepare_skill` ile okuyun. Bu adım JSON boyutunu,
   maç kimliğini, modu, araştırma zamanını, iş deadline'ını ve maç başlangıcını
   doğrular. Sonra claim alıp doğrudan gönderin. Claim veya renew token'ını asla
   yazdırmayın. Araştırma uzun sürüyorsa claim'i araştırma tamamlandıktan sonra
   almak token kaybı ve süre aşımını azaltır.

```python
import os
from scripts.research_pilot_client import (
    secure_http_client, ResearchPilotClient, PilotError,
    ValidationRejected, SubmissionUncertain,
)
api_key = os.environ.get('LOL_RESEARCH_API_KEY')
if not api_key:
    raise RuntimeError('Güvenli secret enjekte edilmedi; durun.')
http = secure_http_client(os.environ['PILOT_API_URL'])
jobs = ResearchPilotClient.list_jobs(http, api_key)
job_id = os.environ['PILOT_JOB_ID']
job = next((j for j in jobs if j.get('id') == job_id and j.get('status') == 'queued'), None)
if not job:
    raise RuntimeError('Atanmış açık pilot işi bulunamadı; durun.')
pilot = ResearchPilotClient(http, api_key, job_id)
context = pilot.fetch_context(job['match_id'])
for warning in pilot.context_warnings():
    print(warning)
# Beklerken Skill'i context ile bir kez çalıştırın; gerçek çıktı yolunu doğrulayın.
skill_path = input('Doğrulanmış Skill JSON dosya yolu: ')
with open(skill_path, encoding='utf-8') as stream:
    raw_skill = pilot.prepare_skill(stream)
pilot.claim()  # claim_token yalnızca pilot nesnesinin belleğinde kalır.
try:
    result = pilot.submit(raw_skill, 'second-pilot-result-v1', 'skill-version-verified')
except ValidationRejected as exc:
    # Bunlar izinli alan yolu ve genel neden; gerçek değer veya gövde içermez.
    print('422:', exc.path, exc.reason)
    result = None  # Otomatik tekrar yok; doğrulanmış hata düzeltilirse açıkça karar verin.
except SubmissionUncertain:
    # Hemen ve yalnız bir kez, aynı süreçte aynı gövde/idempotency anahtarıyla.
    try:
        result = pilot.retry_submission()
    except PilotError:
        result = None  # Süreci açık tutun; operatöre durumu bildirin.
except PilotError:
    result = None
if result and result.get('report_id'):
    report = pilot.report(result['report_id'])
    print('Rapor kaydı okundu; freshness:', result.get('freshness_status'))
```

Gerçek Skill sürümünü `skill-version-verified` yerine yazın. 422 için yalnız
`exc.path` ve `exc.reason` paylaşın. Hata açıkça düzeltilecekse aynı süreçte
`pilot.discard_rejected()` çağrısı sonrası düzeltilmiş gövdeyle yeni, sabit bir
idempotency anahtarı kullanılabilir. Ağ hatası veya 5xx sonrasında isteğin durumu
belirsizdir; gövdeyi değiştirmeyin. Belirsiz durumda süreç sona ererse `GET /jobs`
ile rapor ID'sini ve lease'i kontrol edin. Rapor varsa tekrar oluşturmayın; kayıp
token kurtarılamaz. Lease bittiyse deadline dolmamış iş için ancak açık operatör
kararıyla `reclaim_after_loss()` denenebilir. Deadline dolmuş iş tekrar açılmaz.

`secure_http_client` TLS doğrulamasını etkinleştirir, redirect ve ortam proxy'lerini
kapatır. İstemci `job.lease_expires_at` değerini okur ve `renew()` çağrısında token'ı
bellekte değiştirir. Aynı süreçteki hataları yakalayın ve süreç canlıyken düzeltin;
süreç kaybolduğunda claim token'ı geri alınamaz. Tünel ve anahtar süreleri ayrı
kontrol gerektirir. Bu rehber yeni iş, anahtar, tünel veya otomasyon oluşturmaz.
