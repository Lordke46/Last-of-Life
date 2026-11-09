# Last of Life V3: teslim ölçütleri

V3, #71 sonrasında kalan günlük seçim ve görünüm sorunlarını giderir. Küçük ligler artık ayrı bir ürün akışına zorlanmadan günlük Banko/Sürpriz havuzuna katılır.

- [x] Gerçek güncel bültende ana lig olmasa da, geçerli fiyat ve olasılık bulunan küçük liglerden Banko ve Sürpriz alternatifleri oluşur.
- [x] Tercih hedefleri tutmuyorsa en iyi mevcut geçerli kombinasyon açık `target_met=false` ile gösterilir; fiyat veya güven uydurulmaz.
- [x] Maç açıldığında tek, belirgin önerilen seçim ve alternatifleri görünür; farklı veri kaynağı seçimi engellemek yerine anlaşılır biçimde açıklanır.
- [x] Dün ve evvelsi gün hızlı tarih seçimleridir; aktif favori/canlı/yüksek güven filtresi geçmiş maçları gizleyemez.
- [x] Gerçek bültendeki bugün, yarın, dün ve evvelsi gün API+tarayıcıda doğrulanır. Eksik kaynak günü ile yazılım hatası ayrılır.
- [x] Last of Life V3 ismi, tutarlı yeni ana sayfa/maç/kupon tasarımı, mobil düzen ve eski kişisel kuponlar korunur.
- [x] Yeni günlük seçim davranışı gerçek veri kopyasında eski sürümle aynı gün ve havuzda karşılaştırılır; teorik olasılık ile gerçekleşmiş isabet ayrılır.
- [x] Regresyonlar ve bütünleşik akışlar geçer, incelenebilir PR hazırlanır. Kullanıcı açık onay vermeden merge yapılmaz.

İlk canlı bulgu: 9 Kasım 2026 incelemesinde 7 Kasım için 746, 8 Kasım için 671, 9 Kasım için 64 maç API'de mevcut. Geçmiş veri yok varsayımı yanlıştır; görünüm/filtre/önbellek/yükleme akışı gerçek tarayıcıda doğrulandı.

İlk kök neden düzeltmeleri: favori filtresi tarih değişince sıfırlanır; geçmiş iki gün doğrudan seçilebilir; tarih URL'de tutulur. Geçerli gerçek fiyatlı maçta tercih edilen olasılık eşiği tutmadığında açık en-iyi-mevcut seçimi üretilir, bayat fiyat engeli korunur.


## 9 Kasım yerel doğrulama

Gerçek bültenin izole SQLite kopyasıyla `npm start`, 8010 portunda çalıştırıldı. Üretim veritabanına test verisi yazılmadı. API ve tarayıcıda 7 Kasım 746, 8 Kasım 671, 9 Kasım 64 maç görüldü; yarın görünümü de boş kalmadı. Son gerçek fiyat yenilemesinden sonra 10 Kasım API sayısı 78 oldu (önceki kopya yalnız 7 maç içeriyordu). Bu değişim kaynak kapsamıdır.

Aynı ilk kopyada main sürümü 0 Banko / 0 Sürpriz üretirken V3 3 / 3 üretti; sonuç `benchmarks/v3-daily-coupon-comparison.json` dosyasında. Son gerçek yenileme sonrasında 30 adaydan yine 3 güncel Banko ve 3 güncel Sürpriz alternatifi oluştu. Bu, öneri bulunabilirliği karşılaştırmasıdır; geçmiş isabet veya kârlılık kanıtı değildir. Yayımlanan eski kuponlar korunur, güncel alternatifler ayrı hesaplanır.

390 px mobil görünümde belge genişliği 390 px; yatay sayfa taşması yok. Yeni kimlik, önerilen seçim ve veri kaynağı tarayıcı DOM'unda doğrulandı. Temiz profildeki yaş onayı kullanıcı adına verilmedi; bahis işlemi yapılmadı. Kişisel kupon depolaması değiştirilmedi.

## Kaynak güncelliği

GitHub saatlik hızlı işinin gerçek çalışmaları arasında saatlerce boşluk gözlendi. Üretimde bugün/gelecek bülten veya maç açılışı, son doğrulanmış fiyat 60 dakikayı aştığında kısa bütçeli gerçek kaynak yenilemesini dener. Paylaşılan kilit, beş dakika deneme aralığı ve başarısız işlemde geri alma vardır. Yerel SQLite önizlemede kendiliğinden ağ yenilemesi yapılmaz.

Son çekim başarısız olsa bile önceden doğrulanmış fiyatın özgün iki saatlik süresi dolmadıysa kaynak uyarısıyla kullanılabilir. Zaman damgası değiştirilmez; iki saati aşmış veya önbellek kanıtı taşıyan fiyat güncel sayılmaz. Katalog zaman aşımı bilinen lig adlarını silmez.

Takım geçmişi/güncelliği yetersiz ana liglerde, tam ve güncel fiyat grubu varsa açık piyasa olasılığına geçilir. Bu seçenek model güveni veya kalibrasyon kanıtı olarak gösterilmez. Dinlenme bağlamı açık geçmiş verilmediğinde tüm kulvarların maç öncesi kayıtlarını kapsar.

## Kontroller ve teslim

454 Python ve 87 JavaScript testi yerelde geçti. GitHub Tests çalışması 42635742249, Python 3.12 / Node 22 / Postgres 17 ile bütün test ve Postgres bütünleşme adımlarını başarıyla tamamladı. PR: https://github.com/Lordke46/Last-of-Life/pull/72. Merge yapılmadı.
