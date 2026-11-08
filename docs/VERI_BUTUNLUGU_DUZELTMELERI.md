# Maç verisi ve prompt doğruluğu denetimi

8 Kasım 2026 tarihli kullanıcı geri bildirimi, canlı uygulama yanıtları ve birincil kaynaklarla karşılaştırıldı. Bu çalışma bir başarı garantisi veya model katsayı optimizasyonu değildir.

## Doğrulanan sorunlar

- Uygulamadaki Beşiktaş lig tablosu 8 maç/16 puan, Gençlerbirliği tablosu 9 maç/11 puandı. ESPN'in aynı günkü sezon tablosunda 10 maç/19 puan ve 10 maç/11 puan vardı. Sıralama, gün içindeki diğer maçlarla değişebildiğinden zaman damgasıyla sunulur.
- Tam güncelleme son 7 günü yeniden tarıyor; daha eski eksik sezon maçlarını onarmıyordu. Başarılı son gün kontrolü, tüm sezonun eksiksiz olduğunu göstermiyordu.
- Form/dinlenme yalnız aynı ligdeki kayıtlara bakıyordu. Beşiktaş'ın 5 Kasım Celtic deplasmanı dahil edildiğinde 8 Kasım maçına dinlenme 7,1 yerine 2,9 gün oldu.
- `data_quality` yalnız ağırlıklı maç sayısının kapsama oranıydı. Tahmin doğruluğu ölçüsü olmadığı halde JSON'daki `1.0` bu şekilde yorumlanabiliyordu.

## Uygulanan değişiklikler

### Sezon uzlaştırması

ESPN kullanan yerel liglerin mevcut tam güncellemesine sezon puan tablosu ve takımın tüm kulvarlardaki sezon programı kontrolü eklenir. Yeni takvim veya ücretli API eklenmez. Takım kimliği ve sezon doğrulanır; mevcut sezon takımlarıyla sınırlı çalışır. Hızlı güncelleme bu ek istekleri çalıştırmaz.

Programlardaki eksik maçlar mevcut `matches` tablosuna kendi organizasyon anahtarlarıyla yazılır. Hazırlık maçları ve gelecekte oynanmış gibi gelen sonuçlar alınmaz. Takvim yanıtındaki nesne biçimli skorlar da doğrulanarak ayrıştırılır. Eksik bir sonraki yanıt, doğrulanmış final skoru silemez; açık iptal/erteleme düzeltmeleri korunur.

Sezon maçı sayısı, atılan ve yenilen goller kaynak tabloyla karşılaştırılır. Puan silme ve lig sıralaması kaynak tablosundan alınır. Bilinen eksik kayıtlar açıkça işaretlenir. Başarısız kaynak/program kontrolü başarı diye etiketlenmez. Metadata mevcut `meta` tablosundadır; migration yoktur.

Kaynak tablo tazeyse modelde kaynağı ve kontrol zamanı ile kullanılır. Hızlı güncellemenin sonradan eklediği yeni sonuçlar eski tabloyla geri alınmaz; bu durumda kaynakla doğrulama bekleniyor olarak gösterilir. Okuma sırasında karşılaştırma tek toplu sezon sorgusuyla yapılır.

### Form, dinlenme ve kimlik

Poisson/Elo lig güçleri ve lig puanları ilgili organizasyonun maçlarından hesaplanır. Form ve dinlenme, kayıtlı resmi kulvarlardaki tamamlanmış maçlardan alınır. Takımlar yalnız adla değil kimlikle eşlenir; aynı adlı başka bir kulüp yanlış dinlenme kaydı oluşturamaz. Form satırında organizasyon ve rakip kimliği taşınır.

Walk-forward testi açık geçmiş listesi verdiğinde bu liste dışındaki güncel kupa maçları kullanılmaz. Böylece sonradan öğrenilen sonuçlar geçmiş tahmine sızmaz. Kapsam, tüm dünya maçları doğrulanmış gibi sunulmaz: TFF kaynaklı takımlarda sağlayıcılar arası kimlik eşlemesi bulunmadığından aynı genişlikte program kontrolü henüz yoktur.

### Kalite ve model güveni

- `sample_coverage`: zaman ağırlıklı veri miktarı.
- `data_integrity`: mevcut yerel sezonun adet/skor tutarlılığı, kaynak zamanı, program kontrolü ve kaynağı.
- `data_quality`: örnek kapsamı ile tutarlılık/güncelliğin sınırladığı mühendislik skoru. Doğrulanmış tamlıkta üst sınır 1, bilinen eksikte 0,5, doğrulama yok/eskiyse 0,75. Bu katsayılar istatistiksel güven aralıkları değildir.
- `confidence_scope`: model olasılığı ve veri miktarına dayalı skor; gerçekleşmiş başarı kanıtı değildir.

Bilinen eksik sezon kaydı varsa yüksek model güveni en fazla Orta'ya düşer; harmanlama bu sınırlamayı kaldıramaz. Bu aday otomatik kupona alınmaz. Doğrulama yokluğu, bütün maçlara yapay düşük güven verilmesi için kullanılmaz. Yeni maç öncesi snapshot'larda bu alanlar da dondurulur; eski kayıtlara sonradan kanıt eklenmez.

### Handikap ve fiyat doğrulaması

Doğrudan iddaa etkinlik yanıtında `t=2, st=100, sov=-2`, sonuç 1 fiyatı 2,00; beraberlik 3,85; sonuç 2 fiyatı 2,24 olarak gözlendi. Bu fiyat farkı tek başına aktarım hatası değildir. `-2 / 1`, ev sahibinin skorundan iki gol düşüldükten sonra galibiyet, yani en az üç farkla kazanmasıdır.

Handikap artık standart MS ile aynı tam maç skor matrisinden hesaplanır. Deneysel statüsü, açık sonuçlandırma kuralı, model yüzdesi ve tam gerçek fiyat grubundan marjı temizlenmiş piyasa yüzdesi ayrı taşınır. Fiyatın ham başa baş eşiği modelin gerçekleşme ihtimaline eşit sayılmaz. Büyük ayrışma gizlenmez; modelin haklı olduğu iddia edilmez. Otomatik kupon kapsamı genişletilmez.

Prompt v2 kapsam skorunu isabet oranı diye aktarmayı bırakır. Kaynak iddaa etkinlik URL'si, haberin yalnız başlık çıkarımı olduğu ve deneysel handikap tanımı bağlama eklenir. Farklı tarihli fiyatların değişebileceği, kaynağın aynı anı göstermeyebileceği belirtilir.

## Doğrulama

Ayrı yerel SQLite üzerinde canlı ESPN sezon tablosu ve Beşiktaş/Gençlerbirliği programlarıyla uzlaştırma çalıştırıldı. Lig sayıları 10/10, puanlar 19/11; Beşiktaş'ın son beş form kaydında Avrupa maçları ve 2,9 gün dinlenme doğrulandı. Üretim veritabanına bu denemede yazılmadı. Kod canlıya alındığında ilk tam veri güncellemesi mevcut sezon boşluklarını dolduracaktır.

## Birincil kaynaklar

- [TFF maç detayı](https://www.tff.org/Default.aspx?macId=317881&pageId=29)
- [Beşiktaş resmi maç duyurusu](https://www.bjk.com.tr/tr/haber/95546/genclerbirligi-maci-biletleri-hakkinda-bilgilendirme.html)
- [ESPN 2026 Süper Lig tablosu](https://site.web.api.espn.com/apis/v2/sports/soccer/tur.1/standings?season=2026)
- [ESPN Beşiktaş sezon programı](https://site.api.espn.com/apis/site/v2/sports/soccer/all/teams/1895/schedule?season=2026)
- [İddaa etkinlik fiyatları](https://sportsbookv2.iddaa.com/sportsbook/event/3279014)
- [Bilyoner resmi futbol pazar kuralları](https://yardim.bilyoner.com/hc/tr/articles/14820679057937-Futbol-Bahisleri-Nelerdir)
