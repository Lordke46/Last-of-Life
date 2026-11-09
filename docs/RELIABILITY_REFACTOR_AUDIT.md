# Güvenilirlik refaktörü çalışma denetimi

Görev: 9 Kasım 2026 tarihli kullanıcı metnindeki dokuz madde. Bu belge ara çalışma kaydıdır; tamamlanma raporu değildir.

## Doğrulanmış kök nedenler

- Bültenin `is_archived=not current` koşulu, başlamış ama hâlâ kaynakta bulunan maçları bültenden kaldırılmış maçlarla karıştırıyor. `match_state` ile maç aşaması ve fiyat durumu ayrılıyor.
- Küçük liglerde kaynak geçmişi yok; oranlardan bağımsız takım modeli üretilemiyor. Ayrı bir geçmiş sonuç/eşleşme veri hattı olmadan güven skoru eklemek yanıltıcı olur.
- Küçük lig kuponu eksiksiz 1X2 zorunluluğuyla bağımsız ve geçerli Alt/Üst veya KG çiftlerini eliyor.
- Küçük ligler ana Banko/Sürpriz havuzundan tasarım gereği çıkarılıyor; ayrı küçük lig kuponunda gerçek alternatif üretimi yok.
- Küçük lig adayında bütün fiyatların aynı anda güncel olması aranıyor; tek geçersiz/eksik fiyat gözlemi geçerli bir başka marketi de engelleyebiliyor. Güncellik seçilecek market üzerinde denetlenmeli.
- Bugün ana liglerde maç bulunmadığı için Banko/Sürpriz için modellenmiş havuz sıfır. Bu, tüm küçük liglerin kötü olması anlamına gelmiyor.

## İlk değişikliklerin doğrulaması

Maç aşaması için: gelecek, başlangıcı geçmiş/sonucu doğrulanmamış, bitmiş, ertelenmiş/iptal.
Fiyat için: güncel, doğrulanmamış/eski, bültenden kaldırılmış, geçmiş maç fiyatı, eksik.
Başlama saati geçmesi bitiş skoru icat etmez.
İlk odaklı regresyon koşusunda 33 test geçti; kapsam yaşam döngüsü, bülten günleri, küçük lig detayı ve açıklamalardır.

## Açık işler / tamamlanma kanıtı

1. Bugün/yarın/geçmiş ve detay/kupon/sonuç kullanıcı akışları: canlı taban ölçümü sürüyor; son sürümde yerel tarayıcı doğrulaması gerekiyor.
2. Kaynak eşleştirme, durum/skor/handikap: mevcut korumaların denetimi ve somut kaynak örnekleri gerekiyor.
3. Arşiv: paylaşılan durum sınıflaması eklendi; bütün detay yollarında ve UI'da tutarlılık doğrulanmalı.
4. Kapsam: mevcut geçmiş kaynakları ve genişletilebilir küçük ligler araştırılmalı; gerçek veriyle model desteği uygulanmalı.
5. Motor: #69 karşılaştırma kanıtı mevcut; bu görevde eski başarılı sürüm ve güncel kohort karşılaştırması tamamlanmalı.
6. Güven: piyasa/model ayrımı korunmalı; veri ve fiyat hatalarına özgü nedenler gösterilmeli.
7. Alternatifler: geçerli OU/KG pazarlarının korunması ve gerçek küçük lig alternatifleri geliştirmesi sürüyor.
8. Mimari: durum mantığı ayrılıyor; olasılık/prompt/kupon paritesi ve performans doğrulanmalı.
9. Ölçüm: aynı kohort, kapsama, isabet, olasılık kalitesi ve getiri raporu gerekiyor.

Kullanıcı verileri ve kişisel kuponlara dokunulmayacak. Test edilmiş PR hazırlanacak; açık onay olmadan merge yapılmayacak.

## İkinci aşama

- Küçük liglerde tam 1X2 önkoşulu kaldırıldı; seçilen pazarın tamamlanmış devig grubu ve gerçek fiyatı gerekiyor.
- Piyasa temelli küçük lig kuponunda 2–4 farklı maçtan alternatifler eklendi. Sağlayıcı minimum maç sayısı korunur; örneklem bazında aynı maç tekrarlanmaz ve alt/üst küme kuponlar elenir.
- Kombinasyon hesaplaması minimum maç şartı gruplarında sınırlı güçlü aday sınırıyla yapılır. 500 maçlık regresyon verisi hesaplamaya sınırlı aday girdiğini doğrular.
- Alternatifler arayüz ve paylaşımda ayrı ayrı erişilebilir; eski tek-kupon API alanı korunur. Bu kuponlar açıkça piyasa verili olarak adlandırılır.
- Canlı ESPN skor okuyucusu, eksik skorları varsayılan 0–0 yapıyordu. Eksik/bozuk skorlar artık bilinmiyor sayılır; sağlayıcının sayısal skor nesneleri desteklenir.
- İlgili Python testleri: 45 geçti. Alternatif kupon arayüzü: 10 JavaScript testi geçti. Nihai tüm testler henüz çalıştırılmadı.

Kapsam araştırması: ESPN esp.2 resmi scoreboard kaynağı 9 Kasım için iki maç veriyor. Bu maçlar mevcut uygulamada küçük lig olarak geçen İspanya ikinci lig karşılaşmalarıyla eşleştirilecek; doğrulanmamış organizasyon kimliğiyle otomatik model açılmayacak.

## Aynı küçük lig adaylarıyla politika karşılaştırması

9 Kasım canlı kuponundaki aynı dört aday üzerinde, mevcut kupon dört maç/3,95 oran/%12,78 piyasa çarpımı veriyor. Yeni seçici sağlayıcının minimum üç maç koşulunu koruyarak üç ayrı üç maçlı alternatif üretiyor; ilkinde 2,744 oran/%21,40 piyasa çarpımı var. Bu ölçüm gerçek sonuç isabeti değildir; aynı mevcut adayların farklı birleştirilmesidir. Tam veri [küçük kupon karşılaştırması](benchmarks/small-coupon-policy-2026-11-09.json).

## Yerel doğrulama ve kaynak sınırlamaları

- `npm start` Python sunucusunu başlatır. `LOCAL_DB_PATH=/tmp/...` ayrı SQLite dosyası seçer; `DATABASE_URL` doluysa PostgreSQL önceliği korunur. `LOCAL_PYTHON` kurulu test ortamını kullanabilir; `LOCAL_AUTO_UPDATE=0` yerel başlangıç güncellemesini kapatır.
- Ayrı `/tmp/lol-reliability-ui.db` üzerinde 4.524 izinli dışa aktarılmış maç ve altı gerçek küçük lig detayıyla bülten ve kupon sayfalarının tarayıcı DOM'u incelendi. Tüm bülten seçili, altı maç çizildi; kupon sayfasında ayrı piyasa verili alternatifler çizildi. Bu veri kopyası üretim değildir; eksik sağlayıcı minimum bahis adetleri gibi alanlar yüzünden bu gözlem gerçek kupon performansı kanıtı olarak kullanılamaz.
- Yeni boş havuz açıklaması “bu gün modellenmiş ana lig maçı yok” diyerek model kapsamını bildirir; bütün maçların uygunsuz olduğu izlenimini vermez.
- ESPN `esp.2` kaynak incelemesinde iddaa katalog kimliği `1951` doğrulandı. Ancak 9 Kasım maçlarında iki kaynak aynı takımları ters ev/deplasman sırasında verdi (ESPN Real Sociedad II–Sabadell / Eldense–Las Palmas; İddaa Sabadell–Real Sociedad B / Las Palmas–Eldense). Otomatik ters eşleştirme yapılmayacak; mevcut `match_event` taraf uyumu zorunluluğu korunacak.
- ESPN'in yönlendirdiği [kullanım koşulları](https://disneytermsofuse.com/english/) otomatik veri çıkarmayı kısıtlıyor; kamuya açık uç nokta yeniden yayım lisansı değildir. Yeni kapsam için [CC0 OpenFootball](https://github.com/openfootball/football.json) adapteri araştırılıyor. İddaa fiyatlarının bağımsız model doğrulaması olduğu iddia edilmeyecek.
- Son toplu test: 426 Python ve 83 JavaScript geçti; bundan sonra eklenen yerel başlatma/boş havuz metni için son bütünleşik kontrol tekrar yapılacak.

## Nihai gereksinim denetimi

Önceki “açık işler” listesi çalışma sırasındaki kayıttır. Aşağıdaki tablo son durum ve kanıtı belirtir.

| Görev | Son durum / kanıt |
|---|---|
| 1. Gerçek akış denetimi | Canlı üç gün × üç kategori ölçümü `benchmarks/reliability-live-baseline-2026-11-09.json`. Yerel tarayıcıda gün değişimi: 8 Kasım 43, 9 Kasım 40, 10 Kasım 7 maç; yerel kopyanın arşiv kapsamı üretimden farklıdır. Bulgaristan detayı ve prompt düğmesi çizildi; piyasa modeli kapsam açıklaması görünür. Ayrı küçük lig alternatifleri kupon sayfasında çizildi. |
| 2. Veri zinciri | Taraf eşleştirme `match_event` değişmedi: ters ev/deplasman eşleştirme yasak. `test_prediction_audit`, `test_iddaa`, `test_bulletin`, sonuç/slip testleri mevcut kaynak/handikap/sonuç korumalarını doğrular. Eklenen `test_live_scores` eksik skorun 0–0 sayılmasını engeller. |
| 3. Arşiv | `app/match_state.py` maç aşamasını ve fiyat durumunu ayırır; `test_match_state` ve `test_bulletin` gelecekte kaldırılmış, eski fiyatlı, başlamış, bitmiş ve iptal durumlarını kapsar. Liste fiyat metni ve detay açıklaması aynı sınıflamaya dayanır. |
| 4. Küçük lig kapsamı | `openfootball` CC0 adapteri, opt-in `licensed_history` importu ve bağımsız ön analiz entegre. Gerçek 453 sonuçla 10 Haziran 2025'te model mevcut; 9 Kasım 2026'da eskilik nedeniyle reddediliyor. `test_openfootball` ve `test_licensed_history`; ayrıntı `OPEN_HISTORY_SOURCE.md`. Bulgaristan için güncel lisanslı geçmiş bulunmadı; piyasa olasılığı bağımsız güven olarak sunulmaz. |
| 5. Motor kıyaslaması | #69'daki 1.165 maç / dört lig / gerçek Football MLE karşılaştırması `ONERI_MOTORU_KARSILASTIRMASI.md` ve benchmark JSON'larında duruyor. Bu görevde çekirdek model değiştirilmedi (`reliability-model-kernel.json`); yeni bir isabet artışı iddia edilmiyor. Kazananı kanıtsız değiştirmek yerine seçim kapsamı ve tutarlılık düzeltildi. |
| 6. Güven | Ana öneri/kupon/karşılaştırma kalibrasyon paritesi `test_recommendation_tools` ile doğrulanır. Küçük ligde model eşiği uygulanmıyor. Bağımsız lisanslı ön analiz doğrulanmamış statüde ayrı tutulur; seçim güvenini şişirmez ve promptta ayrıca bulunur. |
| 7. Kupon | Geçerli OU/KG çifti 1X2 olmadan korunur. Küçük liglerde farklı 2–4 maç kombinasyonları, minimum maç şartı, gerçek fiyat, güncellik, tekil maç ve alt/üst küme kontrolleri. 500 maçlık sınırlandırma testi ve `test_coupons`; aynı adaylarla %12,78→%21,40 yalnız tahmini piyasa çarpımıdır. |
| 8. Mimari / hız | Ortak yaşam döngüsü ve ayrı lisanslı veri adapteri; API içinde ağdan geçmiş indirme yok. Kombinasyonların aday cephesi minimum bahis adedine göre sınırlanır, yalnız kazananlar UI sözlüğüne çevrilir. Güncelleme arka planda olurken bülten kullanılabilir. `npm start` ile izole yerel DB desteklenir. |
| 9. Ölçüm | Aynı 64 gerçek fiyatlı kayıtta dengeli/yüksek olasılık tekrarı ve 1.165 maç motor karşılaştırması korunur; canlı isabet ile karşı-olgusal tekrar ayrılır. Küçük kupon karşılaştırması aynı dört adayladır. Kapsama sayıları, isabet, Brier/log-loss, belirsizlik ve getiri kaynakları önceki rapor/JSON'larda; bu refaktörün ileriye dönük başarı artışı henüz ölçülmedi. |

Son doğrulama: 437 Python + 84 JavaScript testi geçti. Yeni çalışma zamanı bağımlılığı ve şema migrasyonu yok. Yerel sunucu izole DB ile başlatıldı; üretim veritabanına ve kullanıcı/kişisel kupon kayıtlarına yazılmadı. Mevcut `REPO_INCELEME_RAPORU.md` dosyası değiştirilmedi veya commit edilmedi.

## Kalan veri sınırları

- Her küçük lig için güncel, yeniden kullanımı izinli geçmiş yok. Sistem bu durumda piyasa adayı verir; takım modeli güveni üretmez. Lisanslı veri importu hazırdır, fakat eski/eksik sezonu güncelmiş gibi kabul etmez.
- Küçük liglerin her maçında doğrulanmış sonuç kaynağı bulunmayabilir. Başlama saati geçmesi “bitti” veya 0–0 sonucu üretmez; sonuç bekleniyor açıklaması kullanılır.
- API verisi ve tarayıcı DOM akışları test edildi. Yerel temiz profilde 18 yaş bildirimi ayrıca görünür; kullanıcı adına yaş beyanı yapılmadı. Paylaş düğmesi seçenekleri test edildi, üçüncü kişilere paylaşım yapılmadı.
- Yeni bağımsız ön analiz deneysel kalır; bahis seçimlerinin kalibrasyon kanıtını devralmaz. Bu PR kapsamı güvenilirlik ve kapsama hatalarını düzeltir; dünyadaki en iyi tahmin sistemine ulaşıldığı iddiası içermez.
