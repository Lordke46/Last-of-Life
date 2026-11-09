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
