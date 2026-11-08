# Öneri araçlarından projeye uyarlanan yöntemler

Araştırma: 8 Kasım 2026. Amaç, gerçek iddaa fiyatları ve mevcut takım verileriyle seçimleri anlaşılır ve ölçülebilir yapmak. Üçüncü tarafların tahminleri kopyalanmaz; yeni ücretli API, üyelik veya otomatik mesaj eklenmez.

## İncelenen birincil kaynaklar

| Araç | Gözlenen yöntem | Bu projeye uyarlama |
| --- | --- | --- |
| [Forebet FAQ](https://www.forebet.com/index.php/en/faq) | Matematiksel olasılık, yüksek ihtimalli seçimler, ayrı fiyat/değer kavramı ve trendler | İsabet sıralaması ile fiyat avantajını ayırma; mevcut Poisson/Elo motorunu tekrar kurmama |
| [OddAlerts Value Bets rehberi](https://www.oddalerts.com/guides/tools/how-to-use-the-value-bets-tool-to-find-positive-ev-opportunities) | Olasılık, adil oran, gerçek fiyat, fiyat farkı ve modelin ligdeki geçmişi birlikte okunur | Maç detayında üç aday, model/piyasa ayrımı, fiyat ve geçmiş sonuç ölçümü |
| [OddAlerts oran hareketleri](https://www.oddalerts.com/dropping-odds) | İlk fiyat ile güncel fiyat karşılaştırılır | Bizim ilk kaydettiğimiz iddaa fiyatından değişim; gerçek açılış veya başarı kanıtı diye etiketlenmez |
| [Oddspedia Value Bets](https://oddspedia.com/valuebets) | Pazar ve oran aralığına göre fiyat/olasılık karşılaştırması | Mevcut 1,40–2,30 bandında gerçek fiyatlı standart pazarları karşılaştırma; çoklu bahis şirketi varmış gibi davranmama |
| [FootyMath yöntem ve test sayfası](https://footymath.com/how-it-works/) | Maç öncesi kayıtlar, zaman sırasına uygun testler, ayrı piyasa/başarı iddiaları | Aynı dondurulmuş seçimde piyasa karşılaştırması ve sonradan öğrenilen bilgiyi tahmine sızdırmama |

Bu sitelerin başarı iddiaları bağımsız doğrulama değildir. Kaynaklar özellik/yöntem örneğidir; kendi modelimizin başarı kanıtı olarak kullanılmaz.

## Entegrasyon

### Maç başına seçenek karşılaştırması

`/api/match/{id}` yanıtına `recommendation_tools` eklenir. Günlük bülten listesi büyütülmez; yeni dış istek yapılmaz. Ana ve küçük lig detaylarında çalışır.

- Yalnız mevcut gerçek fiyatı ve sonlu olasılığı olan standart tam maç pazarları karşılaştırılır.
- Varsayılan ilk üç sıra 1,40–2,30 bandında en yüksek tahmini tutma ihtimalidir. %56 öneri eşiği altı açıkça etiketlenir; üçüncü seçeneği doldurmak için sayı uydurulmaz.
- Adil fiyat `1/p`, başa baş ihtimali `1/oran`, tahmini fiyat farkı `p × oran − 1` olarak hesaplanır. Bunlar model tahminine bağlıdır.
- Fiyat avantajı sıralaması ayrıdır; Banko'ya pozitif değer engeli geri eklenmez.
- Model ve marjı temizlenmiş piyasa olasılığı arasında en az 15 yüzde puan varsa ayrışma açıklanır. Bu bir açıklama eşiğidir, kalibre edilmiş risk sınıfı değildir; olasılık veya güven bonusu verilmez.
- Fiyat hareketi ancak güncel fiyatla eşleşen kayıttan gösterilir. Küçük lig olasılığı piyasa kaynağı olarak kalır; takım modeli icat edilmez.
- İki saatten eski/doğrulanmamış oran veya başlamış maç karşılaştırmasında yeni ekleme düğmesi sunulmaz. Elde yetersiz takım geçmişi varsa belirtilir.

### Performans ölçümü

Mevcut ilk maç öncesi tahmin kayıtlarının değerlendirmesine şunlar eklenir:

1. Gerçek oran bandına göre seçim sayısı, isabet ve sabit birim ROI.
2. Aynı seçimler üzerinde harmanlı tahmin ve snapshot'ta saklanmış piyasa olasılığı için Brier karşılaştırması. Eksik piyasa olasılıkları örnekleme alınmaz; düşük skor daha iyidir. Küçük örneklem belirtilir.
3. İlk seçim fiyatı ile maçtan önceki son 60 dakika içinde gözlenmiş son iddaa fiyatının oran farkı. İlk kayıt sonrasındaki, fiili/snapshot başlama saatlerinin her ikisinden de önceki kayıtlar kullanılır. Eksik, çok erken veya maç başladıktan sonraki fiyat karşılaştırmaya girmez.

Son ölçü marj düzeltilmemiş, aynı şirkette gözlenen fiyat farkıdır. Kesin resmi kapanış, marjdan arındırılmış CLV veya kârlılık kanıtı olarak sunulmaz. Eksik fiyat geçmişi, ilgili seçimin isabet/ROI hesabını silmez. Şema ve çalışma takvimi değiştirilmez.

### Bağlı kupon seçimleri

Aynı maçın örneğin MS 1 ve 2,5 Üst seçimleri bağımsız değildir. Ortak olasılık modeli bulunmadığından, sonuç beklenirken bu seçimlerin yüzdeleri çarpılarak toplam şans veya beklenen getiri gösterilmez. Çelişen seçimler yine %0, doğrulanmış tamamlanmış seçimler yine kesin sonuç verir. Oran çarpımı resmi birleşik teklif olarak etiketlenmez. Sistem kuponu hesapları da bağlı veya olasılığı bilinmeyen seçimleri bağımsızmış gibi işlemez.

## Sonraki model geliştirmeleri için veri gereksinimi

Doğrulanmış ilk 11, oyuncu dakikaları, gerçek şut/xG ve çoklu bahis şirketi fiyatları mevcut veri setinde düzenli bulunmuyor. Bu nedenle bu alanlardan katsayı türetilmez veya eksik veri doldurulmaz. Poisson, piyasa ağırlığı, form ve haber katsayıları bu çalışmada başarı testi olmadan değiştirilmez. Yeni ölçümler, ileride değişiklikleri ayrı zaman aralıklarında sınamayı mümkün kılar.
