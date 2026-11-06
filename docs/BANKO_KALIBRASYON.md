# Banko motoru: seçim, kalibrasyon ve haber karşılaştırması

Bu sürüm banko için **gerçek iddaa oranını**, yeterli maç verisini,
en az 30 eski ve maç öncesi yayımlanmış **aynı lig/pazar/olasılık dilimi**
örneğini ve en az %3 **tahmini beklenen değer** avantajını şart koşar.
Bu eşikler ihtiyatlı mühendislik korumalarıdır; istatistiksel olarak
optimum oldukları iddia edilmez. Veri yoksa banko **üretilmez**.

## Olasılık nereden geliyor?

- Elo, Poisson, form, haber ve dinlenme ile ham model olasılığı hesaplanır.
- Uygun oran grubundaki iddaa fiyatlarından marj temizlenir.
- Mevcut ayar %50 model + %50 piyasa olasılığıdır.
- Sonuçlanmış maçların **başlama saatinden önce** yakalanmış ilk tahminleri,
  lig + pazar + yüzde 10 olasılık diliminde gruplanır.
- En az 30 kanıt olduğunda gerçekleşen–tahmin edilen sapmasına göre, 60
  örneklik önsel ile yumuşatılıp **en fazla 8 yüzde puan** düzeltme uygulanır.
- Yüzde istatistiksel bir tahmindir, garanti değildir. Veri yetersizliğinde
  kalibrasyon uygulanmaz; böyle bir seçim bankoya alınmaz.

## Banko seçimi

- Gerçek iddaa fiyatı 1.40–2.30, kalibre edilmiş olasılık en az %62.
- Her iki takımın en düşük zaman ağırlıklı maç verisi en az 12.
- En az 30 eski kayıtla kalibrasyon kanıtı, fiyat × olasılık − 1 ≥ %3.
- Aynı maç iki kez alınmaz; 2–3 maçlık, kombinasyon olasılığı en az %25 kupon.
- Kuponun toplam oranı 1.90–8.00; uygun kombinasyon yoksa banko üretilmez.
- Pozitif **tahmini** değer, gerçek kârlılık veya bahis tavsiyesi garantisi değildir.
  Kalibrasyon örneği küçük olduğundan bu koşullar gelecekteki isabeti kanıtlamaz.

## Ölçüm ve haber A/B

`python -m app.model.report --force` geleceğe yönelik ilk yayımlanan
prediction snapshot'larından `banko_calibration` ve `prospective.news_ab`
raporlarını üretir. Haberli ve habersiz tahminler aynı maçta, **maç
öncesinde** birlikte kaydedilir; skor belli olduktan sonra 1X2 Brier,
log-loss ve isabet hesaplanır. Eski snapshot'larda habersiz tahmin
saklanmadığından karşılaştırma için yeni veri birikmesi gerekir.
Tekrarlı kupon kombinasyonlarının tam olasılığı bağımsızlık varsayımıyla
çarpılır; ilişkili sonuçlar için ayrı model kurulmadığı henüz çözülmemiş
bir sınırlamadır.

## Telegram ve tanılama

Telegram banko seçiminde mevcutsa model/piyasa yüzdelerini, kalibrasyon
örnek sayısını, veri miktarını ve tahmini değer avantajını gösterir.
Dondurulmuş eski kuponlarda bu alanlar yoksa sonradan uydurulmaz.
Banko yoksa Telegram bunu açıklar; Actions logunda
`Banko aday elemesi` satırında adayların niçin elendiği raporlanır.

**Çalışma sınırı:** Canlı veritabanında yeterli bitmiş pre-match snapshot
yoksa banka/Telegram'a 'banko' çıkmaması beklenen ve güvenli davranıştır.
