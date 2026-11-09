# Last of Life V3: teslim ölçütleri

Bu çalışma henüz tamamlanmadı. #71 veri güvenilirliğini iyileştirdi ancak ürünün temel gereksinimini karşılamadı: küçük ligler Banko/Sürpriz dışında kalıyordu.

- [ ] Gerçek güncel bültende ana lig olmasa da, geçerli fiyat ve olasılık bulunan küçük liglerden Banko ve Sürpriz alternatifleri oluşur.
- [ ] Tercih hedefleri tutmuyorsa en iyi mevcut geçerli kombinasyon açık `target_met=false` ile gösterilir; fiyat veya güven uydurulmaz.
- [ ] Maç açıldığında tek, belirgin önerilen seçim ve alternatifleri görünür; farklı veri kaynağı seçimi engellemek yerine anlaşılır biçimde açıklanır.
- [ ] Dün ve evvelsi gün hızlı tarih seçimleridir; aktif favori/canlı/yüksek güven filtresi geçmiş maçları gizleyemez.
- [ ] Gerçek bültendeki bugün, yarın, dün ve evvelsi gün API+tarayıcıda doğrulanır. Eksik kaynak günü ile yazılım hatası ayrılır.
- [ ] Last of Life V3 ismi, tutarlı yeni ana sayfa/maç/kupon tasarımı, mobil düzen ve eski kişisel kuponlar korunur.
- [ ] Yeni günlük seçim davranışı gerçek veri kopyasında eski sürümle aynı gün ve havuzda karşılaştırılır; teorik olasılık ile gerçekleşmiş isabet ayrılır.
- [ ] Regresyonlar ve bütünleşik akışlar geçer, incelenebilir PR hazırlanır. Kullanıcı açık onay vermeden merge yapılmaz.

İlk canlı bulgu: 9 Kasım 2026 incelemesinde 7 Kasım için 746, 8 Kasım için 671, 9 Kasım için 64 maç API'de mevcut. Geçmiş veri yok varsayımı yanlıştır; görünüm/filtre/önbellek/yükleme akışı gerçek tarayıcıda doğrulanacak.

İlk kök neden düzeltmeleri: favori filtresi tarih değişince sıfırlanır; geçmiş iki gün doğrudan seçilebilir; tarih URL'de tutulur. Geçerli gerçek fiyatlı maçta tercih edilen olasılık eşiği tutmadığında açık en-iyi-mevcut seçimi üretilir, bayat fiyat engeli korunur.
