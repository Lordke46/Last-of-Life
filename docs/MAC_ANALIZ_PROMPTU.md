# Maça özel AI analiz promptu

Maç detayındaki **Bu maç için prompt hazırla** düğmesi, tek şablonu seçilen maçın verileriyle doldurur. Metin ekranda görüntülenir; **Promptu kopyala** ile başka bir AI aracına elle yapıştırılabilir. Uygulama AI servisine istek yapmaz, anahtar veya üyelik istemez.

Tek kaynak `static/match-prompt.js` içindeki `TEMPLATE` sabitidir; sürüm `match-review-v2`. Şablon bütün maçlarda aynıdır. Yalnız `{{MATCH_DATA}}` JSON değişkeni doldurulur. Şablonu geliştirmek için takım başına ayrı metin yazılmaz.

Prompt şunları kapsar:

- Maç/lig kimliği, ev-deplasman sırası, başlama saati ve Türkiye saati, üretim zamanı, oran güncelleme zamanı, arşiv/canlı veri durumu.
- Gerçek iddaa fiyatları ile modelin olasılıkları/adil/tahmini fiyatlarının ayrı gösterimi; varsa piyasa olasılıkları ve kayıtlı fiyat hareketi.
- Mevcut takım verileri: son form, puan durumu, Elo, Poisson gol beklentisi, dinlenme, ikili geçmiş, haberlerden çıkarılan eksikler ve haber kaynakları.
- Sistemin ana önerisi veya aday seçimi, model güveni, açıklaması ve varsa kalibrasyon bilgisi. Eksik değerler bilinmiyor olarak kalır; küçük liglerde takım modeli icat edilmez.

AI'dan bağımsız değerlendirme, mevcut öneriye gerekçeli katılım/itiraz, toplamı %100 olan 1X2 yüzdeleri ve en fazla üç seçenek istenir. Gerçek fiyat, adil fiyat, tutma olasılığı ve fiyat avantajı ayrılır. Varsayılan tercih 1,40–2,30 gerçek oran bandında yüksek tutma ihtimalidir. Öneriye körü körüne katılmak veya sırf farklı olmak için karşı çıkmak istenmez.

AI'ın web erişimi varsa güncel kadro, haber ve fiyatı kaynak/zaman vererek doğrulaması; yoksa bunu söylemesi istenir. Haber başlığı doğrulanmış ilk 11 değildir; model gol beklentisi ölçülmüş şut xG'si değildir. Başlamış/biten maçlarda geçmişe dönük tahmini önceden yapılmış gibi sunması engellenmeye çalışılır. Prompt bu ayrımları açıkça ister; dış AI'ın doğru yanıt vereceğini garanti etmez.

Takım adı ve haberler JSON veri alanında kalır, arayüze HTML olarak yerleştirilmez. Hesap, e-posta, token gibi ilgisiz alanlar kopyalanmaz. Haberler takım başına en fazla 8 başlıkla, pazarlar 120 satırla sınırlandırılır; pazar kapsamı metne yazılır. JSON gereksiz boşluklar ve yinelenen boş pazar alanları azaltılarak oluşturulur.

Kopyalama başarısızsa metin seçilir ve kullanıcıya elle kopyalama yolu gösterilir. Yeni maç açıldığında önceki maçın prompt alanı taşınmaz. Küçük liglerde ve veri eksikliği halinde aynı düğme/şablon kullanılır.
