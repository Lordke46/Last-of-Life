# Üçlü kupon ölçümü — ileriye dönük, üretime hazır olmayan migration

`20261111000003_research_measurement_integrity.sql` ayrı
`research_measurement` şeması kurar. Önceki `research_evaluation` taslağına
veri yazmaz; eski tahmin veya araştırmaları güvenilir geçmişe dönüştürmez. Yeni
şema başka bir uygulama tarafından henüz kullanılmıyor ve hiçbir otomasyon
başlatılmıyor. SQL migration yalnız atılabilir PostgreSQL'de test edildi.
Önceki taslak şema varsa kayıtları silinmeden eski ve güvenilmez olarak işaretlenir;
PUBLIC, Bot ve UI rollerinin tablo izinleri kaldırılır. Şema sahibi/DB yöneticisi
ayrıcalıkları geri alınmaz; eski taslak üretim raporlamasında kullanılmamalıdır.

## Güven sınırı ve kaynak snapshot

Yeni tahmin yalnız mevcut backend'in `public.prediction_snapshots` tablosuna
**yeni INSERT** anında gözlemlenir. Tetikleyici yalnız açıkça
`lol_measure_model_writer` rolü verilen, superuser/CREATEDB/CREATEROLE/BYPASSRLS
olmayan DB oturumunu kabul eder; eski kayıtları taramaz. Maçın kanonik alias,
lig, takımlar, kickoff ve aktif durumu kontrol edilir. Gönderilen `captured_at`
çok eski/gelecekse ölçüm kaydı oluşturulmaz; gerçek ölçüm zamanı DB saatidir.
Sunucu, maç, kanonik ID, kickoff, ölçüm zamanı, model sürümü, JSON ve DB yazarı
zarfının SHA-256 özetini hesaplar. Rol sahipleri dahi ölçüm tablolarına doğrudan
yazamaz; güncelleme/silme/truncate reddedilir.

Bu garanti, **kaynağın doğruluğunu yetkili model writer'a emanet eder**. DB sahibi
ve güvenilir writer yanlış içerik yazarsa hash yalnız bu yanlış içeriği sabitler.
Mevcut app DB rolüne otomatik grant verilmez. Geçmiş tabloda kayıt zaten varsa
`ON CONFLICT DO NOTHING` yeni gözlem üretmez. Yakalama başarısızlığı ana tahmin
INSERT'ini bozmaz; bu da eksik gözlem için dış izleme gerektiği anlamına gelir.
Tetikleyicinin asıl motorun transaction'ına ek yükü ayrıca incelenmelidir.

Mevcut model snapshot'ındaki `model` değeri kaynak kodunda fallback içerebildiği
için bağımsız model olasılığı kanıtlanamıyor ve **NULL** kaydediliyor. Piyasa,
harmanlanmış olasılık ve model güveni ayrı tutuluyor; orijinal içerik de
kaybolmuyor. Bu aşamada bağımsız model kalibrasyonu hesaplanamaz.

## Fiyat ve kupon

Aday, yetkili operatörün kaynak kimlik ve mevcut market/selection anahtarını
vermesiyle sunucuda oluşturulur; fiyat/olasılık/hash dışarıdan alınmaz. Pozitif
ve 1'den büyük makul oran, doğrulanmış iddaa alias, aynı maç/pazarın mevcut
kaynağı ve timestamp'li, cache durumu doğrulanmış quote provenance gereklidir.
Saat dilimi içermeyen veya iki saatten eski gözlem ve eşleşmeyen güncel fiyat
reddedilir. Harici sağlayıcının gerçekten o anda işlem kabul etmesi bu kontrollerle
kanıtlanmaz. Fiyat yoksa aday saklanabilir fakat kuponda kullanılamaz.

A ve B yolları tek dondurulmuş aday havuzuna bağlıdır. A matematik yolu, B
araştırma değerlendirme yoludur. Araştırılmamış, başarısız, eksik ve reddedilen
adaylar ayrı kararlar olarak tutulur. `complete` durumu yalnız schema_valid'a
dayanmaz: kayıtlı maç/rapor eşleşmesi, kaynak ve özgün bulgu ile ayrıca yetkili
insan inceleme onayı gerekir. İnsan onayı kaynakların gerçekliğini otomatik
kanıtlamaz. Boş aday günleri ve iki yol için no-coupon açıkça kaydedilebilir.

Kupon başlığı ve tam üç farklı maç ayağı tek append-only satırda, tek transaction
ile dondurulur. Her ayakta immutable aday/model referansı, market, selection,
oran ve gözlem zamanı vardır; oranlar server tarafından çarpılır. Eksik,
yinelenen maçlı, stale veya source değişmiş kupon reddedilir. Satırdaki ayaklara
sonradan ekleme/değişiklik/silme yolu yoktur. Maç ve fiyat kaynakları freeze
süresince kilitlenir; normal commit'te kickoff ve fiyat zamanı yeniden denetlenir.
DB operatörleri ve SQL transaction sınırını kontrol eden yetkili backend güven
sınırındadır: `SET CONSTRAINTS ... IMMEDIATE` gibi işlemler ve transaction'ı
bekletme ayrı operatör politikası gerektirir. DB yöneticisi trigger'ları kapatabilir;
bu tasarım superuser'a karşı değişmezlik sağlamaz.

## Settlement ve metrik sınırları

Ayrı, superuser olmayan `lol_measure_result_writer` üyesi bir backend, kickoff
sonrası mevcut maç kaynağına yalnız `FT`/`Final`/`STATUS_FINAL`, tamamlanmış ve
negatif olmayan skor yazdığında sunucu zamanlı, append-only sonuç **gözlemi**
oluşur; değişen skor yeni revizyondur. Gözlem `unverified` etiketlidir: mevcut
maç tablosu kaynağın harici sağlayıcı URL'sini veya orijinal yayın zamanını
kanıtlamıyor. Bu yüzden `record_final_result` fail-closed: kazanmış kupon,
kesin settlement ve kesin gerçek getiri üretilmez. Güvenilir kaynak/kanıt,
iptal/erteleme ve düzeltme kuralları ayrı onaylı entegrasyon ister.

Toplam oran dondurulan üç fiyattan ayrı saklanır. İsabet oranı, gerçek net getiri
ve kalibrasyon raporu henüz uygulanmadı: gerçek bahis/stake/ödeme kanıtı ve
doğrulanmış settlement yok. A/B no-coupon, eksik araştırma ve ayrı outcome
kayıtları daha sonra her iki yolun kupon sayısını ayrı saymaya uygundur, ama
aday havuzunun her gün eksiksiz kaydedilmesi kendiliğinden garanti değildir.
Bu taslak henüz güvenilir gerçek getiri ölçümü sunduğu iddiasıyla açılmamalıdır.

## Geçiş ve onaylar

1. Mevcut staging için yedek ve geri dönüş, önceki dört maç kimliği ve legacy
   backfill kararları, queue guard ve UI role migration'ları ayrı incelenir.
2. V1 evaluation taslağının uygulanıp uygulanmayacağına karar verilir; V1'in
   tarih/hash/settlement garantileri yetersizdir ve V2'ye otomatik backfill yoktur.
3. Üretici, gözden geçiren ve sonuç yazanlar için ayrı kişisel/servis DB login,
   RLS/policy, least-privilege grant, secret yönetimi ve bağımsız kaynak denetimi
   tasarlanır; superuser bağlantıları trusted producer sayılmaz.
4. Güvenilir sonuç kaynağı ve doğrulanabilir gözlem kanıtı ile mevcut settlement
   kuralları uyumlu biçimde ayrı çalışma ve testle tamamlanır. Gerçek bahis,
   stake, currency ve cash payout kanıtı olmadan gerçek net getiri hesaplanmaz.
5. Aday evreni eksikliği, karşılaştırma cutoff'u, sample selection, roll-back,
   migration etkileri ve commit-time trust gözden geçirilip staging/production
   migration ve entegrasyon için kullanıcıdan ayrı onay alınır.
