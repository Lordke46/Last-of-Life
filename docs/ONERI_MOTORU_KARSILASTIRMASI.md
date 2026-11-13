# Öneri motoru: karşılaştırma ve seçici harmanlama

9 Kasım 2026. Bu çalışma doğrulanmış bir isabet artışı veya piyasadaki bütün sistemlere üstünlük iddiası değildir.

## Canlı karne ne söylüyordu?

`https://lastoflife.vercel.app/api/model-report` yanıtında:

- Geçmiş sonuçlardan yeniden oluşturulan 1X2 testi: 2.410 maç, %50,87 isabet.
- Aynı 100 ilk maç öncesi kayıtta: model %60; harman %62; piyasa %62.
- Bu 100 maçın Brier skoru: model 0,5468; harman 0,5182; piyasa 0,4963. Düşük daha iyi.
- Gerçek İddaa fiyatıyla ilk yayımlanan 65 seçim: 45 doğru (%69,23). %95 isabet aralığı %57,2–79,1; örnek küçük.
- Banko: iki sonuçlanmış kuponun ikisi kaybetmiş; tekil seçim isabeti %75. Kupon ve tek seçim başarısı aynı ölçüm değil.

Bu rakamlar farklı örneklemler ve model sürümleri içeriyor. Birbirlerinin yerine okunamaz. Karne artık gerçek öneri performansını önce gösterir; eski 1X2 testi ayrı adlandırılır.

## Kaynaklardan alınan yaklaşım

- [Penaltyblog](https://github.com/martineastwood/penaltyblog): rakip etkisini birlikte öğrenen gol modeli, zaman ağırlığı ve düşük skor düzeltmesi.
- [Football MLE](https://github.com/jpmouracodex/football-mle): öğrenilmiş takım hücum/savunması ve kronolojik değerlendirme. Referansın gerçek Python uygulaması da ayrı yerel ortamda çalıştırıldı; commit `51494de7912e3b7135ed9491f07c937b9466ea44`.
- [Value Betting Scanner](https://github.com/zakariae-boui/value-betting-scanner): piyasa olasılığını ölçülebilir karşılaştırma tabanı olarak kullanma. Bizde bağımsız Pinnacle akışı yok; İddaa verisini Pinnacle verisi gibi göstermiyoruz.
- [OpenFootball](https://github.com/openfootball/football.json): CC0 sonuç verisiyle bağımsız sezon deneyi. Bahis oranı içermez.

Üretime başka deponun kodu kopyalanmadı; yeni çalışma zamanı bağımlılığı eklenmedi. Bizim öğrenilmiş adayımız düzenlileştirilmiş Poisson parametrelerini, ardından bu parametrelere koşullu rho değerini öğrenir. Referans Football MLE ise ortak Dixon–Coles maksimum olabilirlik optimizasyonu yapar; aynı algoritma oldukları iddia edilmiyor.

## Bağımsız sezon karşılaştırması

2024/25 eğitimi, sonraki 2025/26 sezonunda ilerleyen zamanla yeniden eğitim. Her tahmin yalnız daha önce tamamlanmış maçları görür. Aynı gün sonuçları eğitimden çıkarılır. Hiperparametreler daha önceki 2023/24→2024/25 geliştirme denemelerinden sonra sabitlendi.

| Lig | Test maçı | Mevcut 1X2 | Öğrenilmiş aday 1X2 | Seçici harman 1X2 | Football MLE 1X2 |
|---|---:|---:|---:|---:|---:|
| Premier League | 380 | %48,68 | %46,84 | %48,68 | %46,58 |
| Bundesliga | 306 | %54,90 | %52,94 | %54,90 | %53,27 |
| La Liga | 380 | %51,05 | %51,32 | %51,05 | %50,79 |
| Süper Lig, kısmi veri | 99 | %49,49 | %51,52 | %49,49 | %51,52 |

Toplam 1.165 maç. Türkiye kaynağı yalnız 3 Kasım 2025'e kadar 99 sonuç içerdiği için tam sezon diye sunulmaz. La Liga eğitim sezonunda 370 sonuç vardır. Yeni yükselen/yetersiz geçmişli takımlarda hem bizim adayımız hem dış referans mevcut motora döner; karşılaştırma bu fallback dahil yapılmıştır.

Tek başına isabet, olasılık kalitesini ölçmez. Çok pazarlı Brier sonuçlarında da evrensel üstünlük yok: Bundesliga'da seçici harman 0,205070, mevcut motor 0,204766; bu örnekte harman biraz kötü. La Liga'da öğrenilmiş aday 0,205175, mevcut motor 0,205607; küçük bir iyileşme. Bu nedenle bütün liglerde zorla yeni modele geçilmedi.

Tam sonuçlar ve veri SHA-256 değerleri: [JSON](benchmarks/ensemble-2026-11-09.json). Oransız en olası seçim isabetleri JSON'da ayrıca bulunur; bunlar oynanabilir İddaa önerisi isabeti veya ROI değildir.

Tekrar çalıştırma:

```sh
python scripts/benchmark_ensemble.py \
  --train /path/en.1-2024-25.json --test /path/en.1-2025-26.json \
  --football-mle-root /path/football-mle --output /tmp/comparison.json
```

Dış referans için yalnız araştırma ortamında numpy, scipy ve pandas gerekir. Uygulamanın gereksinimleri değişmez.

## Üretim davranışı

1. **Lig bazında seçici harman:** son 160 uygun tarihsel maçta mevcut motor, %50 harman ve öğrenilmiş gol modeli karşılaştırılır. En az 80 maç, sekiz farklı hafta bloğu, en az 0,002 Brier iyileşmesi ve sıfırın altında belirsizlik üst sınırı gerekir. İki aday için muhafazakâr eşik kullanılır. Kanıt yoksa mevcut motor kalır.
2. **Hesaplama cron'da:** seçim Model Karnesi işi sırasında yapılır ve meta tablosunda saklanır. API isteğinde geçmiş test çalışmaz. Kod imzası uyuşmayan veya 14 günden eski seçim kullanılmaz. Takım geçmişi yetersizse/optimizasyon yakınsamazsa o maç mevcut modele döner; kullanılan gerçek ağırlık kaydedilir.
3. **Pazar bazında piyasa ağırlığı:** ilk maç öncesi snapshot'lardan 0 / 0,25 / 0,50 / 0,75 / 1 piyasa ağırlıkları karşılaştırılır. İlk %65 seçim, sonraki bölüm doğrulama içindir; en az 120 kayıt, 60 eğitim, 40 doğrulama ve sekiz doğrulama haftası gerekir. Kanıt yetersizse `MODEL["market_weight"]` (0,75) korunur. Lig verisi yetmezse açıkça belirtilen ligler arası havuz kullanılır. Yeni öğrenilmiş motor eski motorun piyasa-ağırlığı kanıtını devralmaz.
4. **Kalibrasyon:** lig/pazar/olasılık diliminde 30 kayıt; yetersizse aynı pazar ve dilimde en az 100 ligler arası kayıt ve daha güçlü yumuşatma. Kaynak açıkça `league` veya `pooled_leagues` olarak gösterilir. Öğrenilmiş modelin kayıtları ayrı tutulur. Ana öneri ve kupon aynı düzeltilmiş olasılıkla sıralanır; ham yüzde korunur.
5. **İki farklı seçim:** 1,40–2,30 bandındaki dengeli öneri kalır. Maç detayında ayrıca 1,15–2,30 bandındaki en yüksek olasılıklı gerçek fiyat gösterilir. Farklı bir seçenek yoksa tekrar edilmez. Bayat fiyat, başlamış maç, tutarsız veri ve deneysel pazar engelleri korunur. Kalibrasyon yoksa “yüksek güven” uydurulmaz.
6. **Banko ve Sürpriz (`hit_probability_v4`):** her tür için toplam-oran tabanı vardır (Banko 1,80, Sürpriz 4,50); tabanın üstünde ortak tutma olasılığı en yüksek kombinasyon seçilir. Banko bacakları 1,10–2,00 ve ≥%45, 1–3 maç; Sürpriz bacakları 1,40–3,50 ve ≥%33, 2–4 maç, toplam 4,50–15. Daha az maç olasılığı yükselttiği için MBS 1–2 maçlar havuzda tutulur; bültenin MBS kuralı kupon boyutuna uygulanır. Üç alternatif ortak maç içermeyecek şekilde kurulur; taban tutmazsa en yakın kupon `target_met=false` ile verilir. Toplam şans bağımsız maç varsayımıyla hesaplanır, garanti değildir; iddaa marjı (≈%20/pazar) nedeniyle beklenen getiri negatiftir.
7. **İzlenebilirlik:** seçilen model/ağırlık, kalibrasyon kaynağı ve en yüksek olasılık seçeneği ilk snapshot'a eklenir; yeni seçenek kendi performans tablosunda ölçülür. Geçmiş seçimler değiştirilmez. Prompt aynı bilgileri taşır.
8. **Tutarlılık:** ayrı piyasa ağırlıkları gol eşiklerinin mantıksal sırasını bozarsa toplam gol pazarları tutarlı model dağılımına döner. İptal/ertelenmiş ve sonuç kullanılabilirlik süresini geçmemiş maçlar performans hesabına alınmaz.

## Sınırlar

- Çok pazarlı geçmiş model seçimi; tarihsel haber, kadro ve diğer müsabaka veri giriş zamanlarını tam yeniden oynatamaz. İleriye dönük snapshot ölçümü asıl kanıttır.
- Ligler arası kalibrasyon bir maç/lig için doğruluk garantisi değildir; örnek büyüklüğü ve kapsamı gösterilir.
- Maçın sonunda bir sonucun gerçekleşmesi, o sonucun maçtan önce yüksek olasılıkla tahmin edilebilir olduğu anlamına gelmez.
- 1.165 maç deneyi yeni öneri politikasının gerçek fiyatlarla getirisini doğrulamaz. Gerçek oranlı değerlendirme ayrı yapılmalıdır.
- İlk gün yeni motor için yeterli kanıt bulunmayabilir. Sistem bu durumda etiketleri şişirmek yerine mevcut modeli kullanır ve yeni kayıt biriktirir.

## Salt okunur üretim denetimi

Backtest worker'ın isteğe bağlı `audit_export=true` çalışması şema güncellemesini ve iş kuyruğunu atlar. PostgreSQL oturumu `SET TRANSACTION READ ONLY` ile açılır. Yalnız herkese açık maç sonuçları ve izinli tahmin alanları üç gün saklanan artifact'a yazılır; kullanıcı, kişisel kupon, oturum veya bağlantı bilgisi dışa aktarılmaz. Canlı veritabanında değişiklik yapmaz.

## Gerçek üretim kayıtlarında seçim tekrarı

Salt okunur denetim başarıyla çalıştı: 4.524 maç, 226 ilk tahmin kaydı. Sonuçlanmış ve kayıtlı fiyatı olan 101 maç değerlendirilebildi. Bu, saklanmış yüzdelerden farklı seçim yapılmış olsaydı ne olacağını ölçer; yeni gol modelinin doğrulaması değildir. Oran güncelliği ve tüm üretim engelleri bu dışa aktarımda yeniden oynatılamaz.

| Aynı 64 maç | Doğru | İsabet | Ortalama gerçek fiyat | Birim başına getiri |
|---|---:|---:|---:|---:|
| Dengeli seçim | 44 | %68,75 | 1,4947 | +%2,67 |
| En yüksek olasılık | 50 | %78,12 | 1,2214 | −%4,38 |

Tüm uygun 101 maçta yüksek olasılık seçeneği 75 kez tuttu (%74,26), getiri −%10,21 oldu. Bu sonuç yüksek tutma ihtimalinin uygun fiyat anlamına gelmediğini gösterir. Küçük veri seti ve belirsizlik aralıkları nedeniyle başarı artışı kesinleşmiş değildir. Dengeli ana öneriyi koruyup yüksek olasılık seçeneğini ayrı sunma kararının gerekçesi budur.

Kalibrasyon için yalnız orijinal kayıt zamanından önce bilinebilen sonuçlar kullanıldı. Kayıt anında yeterli geçmiş bulunmadığında kalibrasyon uygulanmadı. Gerçekte yayımlanmış 65 seçimin sonuçları ayrıca korunur; ortak karşılaştırmada 64 maç vardır.

Tekrar çalıştırma: `python scripts/replay_recorded_picks.py --input /path/model-audit.json --output /tmp/replay.json`.

[Ham toplu sonuçlar](benchmarks/recorded-selection-2026-11-09.json). Kullanıcı veya kişisel kupon verisi içermez.
