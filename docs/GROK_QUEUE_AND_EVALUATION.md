# Grok araştırma kuyruğu ve ölçüm — taslak

## Erişim ve araştırma kalitesi

Grok Intelligence görünümü varsayılan kapalıdır. Etkinleştirilse bile mevcut
uygulama oturumundan `admin` rolü backend'de zorunludur; normal kullanıcı 403,
oturumsuz kullanıcı 401 alır. Sunucu `RESEARCH_UI_OWNER` ile yalnız tek servis
kimliğinin kayıtlarını seçer. Rapor kimliğini bilmek yetki sağlamaz. Ayrı Bot API'si
mevcut kapsam/owner kontrolünü korur; tarayıcıya servis anahtarı gitmez. Yönetici
uygulama ayarında seçilen servis sahibinin tüm maç raporlarını okuyabilir; bu
model, her yöneticinin yalnız kendi raporları anlamına gelmez.

Pilotun 11 `confirmed_official` beyanı `reported` olarak saklandı; kaynak `kind`
alanları `unknown` idi. TFF ve federasyonların kendi resmî siteleri birincil
kaynak olabilir, fakat domain veya Bot etiketi bir bulgunun doğru yorumlandığını
ve haber tarihini/bağlamını kanıtlamaz. Kaynağın yetkili kuruma ait olduğu,
metnin belirli iddiayı açıkça desteklediği ve zamanının uygunluğu insan tarafından
ayrı değerlendirilmelidir. Redirect, benzer domain, yeniden yayın ve alıntı için
sahiplik ya da bağımsızlık varsayılmaz. Otomatik yükseltme yapılmaz; kaynak türü,
özgün statü/kategori ve anlaşmazlıklar korunur. Bu değerlendirme hiçbir olasılığı
veya kuponu değiştirmez.

## Manuel/dry-run kuyruğu

`app/research_planner.py` yalnız bellek içindeki **zaten hesaplanmış** ve öncelik
sırasında verilmiş aday bağlamlarını değerlendirir; motora, DB'ye veya API'ye
bağlanmaz ve iş oluşturmaz. Günlük sınır İstanbul gününe göre `full` için üçtür;
o gün oluşturulmuş başarısız/süresi dolmuş işler de sayılır. Aynı kanonik maç +
mod için önceki iş veya aynı listenin tekrarı dışlanır. `lineup_check` ayrı moddur;
öneri penceresi başlama öncesi son 90 dakika, deadline kickoff'tan beş dakika
öncedir. `full` önerisinde deadline kickoff'tan 30 dakika öncedir. Bu süreler
operasyonel öneridir; önce test ve kullanıcı onayı gerekir. Başlamış, ertelenmiş,
iptal edilmiş maç veya geçmiş deadline önerilmez. Eksik snapshot ve iki saati
geçen/bilinmeyen fiyat uyarılır; yeni tarih ya da değer uydurulmaz. Yakın tarihli
zaman damgası tek başına fiyatın güvenilir olduğunu kanıtlamaz; ayrıca provenance
kontrolü gerekir. Tam geçmişin sağlandığı açıkça belirtilmezse planlama reddedilir.

Üretim bağlantısı için operatör, matematiksel motorun **zaten yayımlanmış aday
havuzundan** maç kimliklerini okur; güvenilir fixture/event eşlemesi yapar ve
salt okunur bağlamları kuru denemeye verir. PostgreSQL migration taslağı, gözden
geçirilmiş sağlayıcı alias ve kanonik kimliği şart koşar; isim benzerliğiyle
birleştirme yapmaz. Operatör lig, ev/deplasman, kickoff ve sağlayıcı kimliklerini
kanıtla eşleştirir. Kimlik kaydı yoksa, kaynak değerleri uyuşmazsa veya eski
kaydedilmiş işler için identity backfill eksikse işlem 409 koduyla durur.

PostgreSQL insert tetikleyicisi işlem-kilitli kota ve aynı kanonik maç/mod için
tekrar koruması uygular. Aynı gün en fazla üç farklı full maç sayılır; tüm iş
statüleri, failed ve expired dahil hesaptadır. `lineup_check` ayrı değerlendirilir.
Kota İstanbul takvim gününde sunucu saatiyle hesaplanır; eşzamanlılık için
READ COMMITTED zorunludur. Değişik input_version veya idempotency anahtarı tekrar
kuralını atlayamaz. Aynı isteğin bire bir tekrarı idempotenttir. Tekrar araştırma
için kısıtlı identity-admin DB rolünün ayrı eklediği, gerekçe, sunucunun yazdığı
DB oturumu ve bitiş zamanı olan tek kullanımlık onay gereklidir; Bot rolü bunu
oluşturamaz. Bu tasarım, onay yetkisini bir HTTP endpoint'i olarak açmaz.

SQLite geliştirme deposu PostgreSQL tetikleyicilerini uygulamaz; uygulama SQLite üzerinde enqueue işlemini 503 ile kapatır. Geçiş migration'ları uygulanmadan veya tetikleyiciler etkin değilse PostgreSQL enqueue
503 ile çalışmaz; geçmiş işlerin otomatik isim benzerliğiyle eşleştirilmesi yasaktır.
Mevcut işlerin tamamı incelenip alias ile kanonik kimliğe güvenle bağlanmadan
ilgili mod için yeni iş reddedilir. Sunucu/DB yöneticisi rolü ve izinleri güven
sınırının içindedir. Üretim migration, rol grant'i, onay üretimi, scheduler ve
Grok Routine bu çalışmada yapılmadı.

## Üçlü kupon katkısını ölçme tasarımı

1. Önceden kaydedilmiş, maç başlamadan oluşturulmuş matematiksel aday evrenini,
   model sürümü, veri/kaynak zamanı, seçenek, olasılık, fiyat ve snapshot hash'iyle
   ayrı ve değiştirilemez temel kayıt olarak dondur. Eksik/eski fiyatlar ve kaynağı
   belirsiz olasılıklar açık işaretlenir; geçmişe dönük tahmin oluşturulmaz.
2. Ayrı append-only araştırma-değerlendirme kaydında rapor ID/sürümü, gözlem ve
   kayıt saatleri, değerlendiren kişi, risk/itiraz türü, gerekçe, kaynaklar ve
   `keep`/`avoid`/`uncertain`/`no_research` kararını koru. Başlama sonrası gelenler
   ön analiz olarak sayılmaz. Model ve geçmiş kupon satırları güncellenmez.
3. Aynı aday kümesi ve cutoff için kontrol (yalnız matematik) ile kör/önceden
   tanımlanmış araştırma değerlendirmesini eşleştir; tercihen aday/maç bazında
   rastgele ayrılmış veya ileriye dönük gölgede yürütülen deney kullan. Hangi
   araştırmaların seçildiğini, tamamlanmadığını veya başarısız olduğunu sakla;
   yalnız başarılı araştırmaları karşılaştırma (seçim yanlılığı).
4. Maç bittikten sonra ayrı, sürümlü sonuç/settlement kaydı ile riskli veya
   engellenen seçeneklerin de gerçekleşmelerini ve verilmeyen kararları ölç.
   Önceden belirlenmiş metrikler: tamamlanma ve zamanında olma oranı; seçim ve
   eleme sayısı; olay düzeyinde isabet ve kalibrasyon/Brier; üçlü kuponun ortak
   isabeti, gözlenen fiyatlarla varsayımsal getiri ve belirsizlik aralıkları.
   Korelasyon, küçük örneklem, oran değişimi, ertelenen maçlar ve çoklu seçimler
   hesaba katılmalı; varsayımsal getiri gerçek bahis sonucu diye gösterilmemeli.
5. Yeni tablo/ilişki, veri saklama, erişim politikası ve migration önerisi henüz
   uygulanmadı. Önce önceden kayıtlı kararlar, onay ve bağımsız değerlendirme
   olmadan Grok sonucu model veya kupon motoruna otomatik uygulanmaz.

## Salt okunur arayüz ve değerlendirme migration taslakları

`20261111000001_research_ui_readonly.sql`, NOLOGIN `lol_research_ui_reader`
rolü ve yalnız rapor görünümü view'ları hazırlar; Bot rolü genişletilmez. Web
arayüzü PostgreSQL için `RESEARCH_UI_DATABASE_URL` zorunlu ayrı bağlantı ve
read-only işlem kullanır; Bot bağlantısına geri düşmez. Sunucuda `admin` ve
owner filtresi korunur. Üretimde ayrı sınırlı login/izin ve secret verilmesi
ayrı onay gerektirir.

`20261111000002_research_evaluation_draft.sql` dondurulmuş aday, ayrı araştırma
kararı, kupon snapshot/seçimleri ve settlement revizyonları için taslak sağlar.
DB zamanı eklemede atanır; kanonik kickoff sonrası ekleme ve UPDATE/DELETE
tetikleyicide engellenir. Bu kontroller, istemci tarafından verilen model
snapshotının gerçekten geçmişte var olduğunu tek başına kanıtlamaz: imzalı/
güvenilir kaynak snapshotı ve sonuç/ödeme entegrasyonu ayrıca gereklidir.
Hiçbir uygulama bu tablolara bağlı değildir; production migration uygulanmadı.
