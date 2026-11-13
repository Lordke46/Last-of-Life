# Veri güvenilirliği ve tahmin kalitesi denetimi — 8 Kasım 2026

## Kapsam ve mevcut mimari

Bu çalışma PR #67'nin sezon uzlaştırma / farklı kulvarlardan dinlenme düzeltmesinin üzerine gelir. Python/FastAPI, SQLite/PostgreSQL, mevcut zamanlayıcılar ve bağımlılıklar korunur. Yeni ücretli servis, LLM çağrısı, deployment veya migration gerekmez.

- **Fikstür/sonuç:** `scrapers/espn.py` günlük scoreboard; Süper Lig dışındaki desteklenen ESPN ligleri de aynı yolu kullanır. `scrapers/tff.py` Türkiye 1. Lig alternatifi. İsim benzerliği tek başına takım kimliği değildir.
- **Sezon uzlaştırma:** `scrapers/team_context.py`, ESPN sezon standings + takımın tüm resmi kulvarlardaki schedule verisini karşılaştırır. Tam güncellemede çalışır; hızlı güncelleme son kayıtları tazeler. Eski akış yalnız son yedi günü çektiğinden daha eski eksik maçları kendiliğinden bulamıyordu. Başarısız günlerde üç günlük atlama da #67'de kaldırıldı.
- **Takım modeli:** `model/ratings.py` zaman ağırlıklı Poisson güçleri ve Elo; `model/engine.py` log uzayında harmanlama, küçük form/haber/dinlenme etkileri; `model/predict.py` Dixon–Coles skor matrisi.
- **Gerçek fiyatlar:** `scrapers/iddaa.py` herkese açık tam bülten + bütçeli detay sorguları; `iddaa_events/iddaa_odds` son başarılı bülten, `odds_history` gözlenen değişimler, `odds_log` ileriye dönük maç öncesi fiyatlar. `bulletin_archive` geçmiş bültenleri güncel tekliften ayırır.
- **Piyasa harmanı:** `model/blend.py`, tam ve geçerli 1X2/Alt-Üst/KG gruplarında `(1/oran) / toplam(1/oran)`. 0,98–1,50 dışındaki toplamlar ve eksik gruplar reddedilir. Çifte şans 1X2'den türetilir. Gerçek fiyat ile adil/model fiyatı ayrıdır.
- **Seçim:** `balanced_v2` mevcut 1,40–2,30 bandında en yüksek olasılığı, mevcut olasılık eşiğiyle seçer. Banko ve sürpriz `hit_probability_v4` kullanır: toplam-oran tabanı üstünde en yüksek ortak olasılık, MBS'ye göre kupon boyutu. Deneysel marketler otomatik kupon havuzunda değildir; elle inceleme özelliği korunur.
- **Kayıt/değerlendirme:** `prediction_snapshots` ilk maç öncesi tahmini değişmez saklar. `calibration`, `snapshots`, `performance`, `report` sonuçları sonradan bu kayda eşler. Walk-forward yeniden hesaplamaları, gerçekten zamanında saklanmış tahminlerden ayrı tutulur.
- **Cache:** model veri sürümünü en fazla 15 saniyede kontrol eder; API süreç cache'i 30 saniyeye / kickoff'a sınırlıdır, bazı liste uçları CDN TTL kullanır. Hata durumu artık süreç cache sürümüne girer. İddaa HTTP cache kanıtı ayrıca kaydedilir; süreç/API cache zamanı fiyat değişim zamanı değildir.

## Doğrulanan hatalar ve düzeltmeler

1. **Kontrol zamanı fiyat değişim zamanı gibi görünüyordu.** `updated_at` her başarılı bültende değişiyordu; değişmeyen fiyat için bunun gerçekleşmesi normaldir. Ancak taşıma/cache kanıtı ve gözlenen değişim zamanı yoktu. Yeni provenance bunu ayırır. Başarısız güncellemede rollback sonrasında bile son denemenin başarısızlığı kaydedilir; korunmuş fiyatlar otomatik öneriye uygun sayılmaz.
2. **Geçmiş tek maç analizinde güncel güçler/tablo/haber kullanılabiliyordu.** Geçmiş kickoff veya açık geçmiş listesiyle tahmin, yalnız cutoff öncesi tamamlanmış resmi sonuçlarla güçleri/Elo'yu yeniden kurar. Güncel haberleri devre dışı bırakır; tablo/H2H de aynı cutoff'a bağlıdır. Bu geçmiş tahmin *gerçekten o gün yayımlanmış tahmin* diye etiketlenmez.
3. **Haber açıklama alanında değişken çakışması vardı.** `home_log/away_log` haber katkısı yerine ağırlıklı maç sayısını gösterebiliyordu. Hesabın kendisinden ayrı olan bu çıktı hatası düzeltildi; habersiz maçta katkı sıfırdır.
4. **Aynı takımın isim değişiklikleri güçleri bölebiliyordu.** Model yüklenirken aynı sağlayıcı takım ID'si, tek güncel görünen ad altında birleştirilir. Farklı kulvarların form/dinlenme eşleşmesi ID ile yapılır.
5. **Büyük model–piyasa farkı gösteriliyor ama seçimde sorgulanmıyordu.** Uygun tüm standart marketlerde fark, marj temizleme yöntemi/grubu ve toplam ters oran kaydedilir; harman ağırlığı sıfır olsa da kıyas korunur. Varsayılan inceleme eşiği `MODEL.disagreement_review_pp = 20`. Bu optimum olduğu kanıtlanmış bir eşik değildir. İlgili bant/market için kalibrasyon yoksa bu aday otomatik seçilmez; başka uygun aday varsa mevcut sıralamayla seçilir. Yoksa `decision.status=no_bet` döner.
6. **Kalibrasyon, değiştirilmiş gerçek kickoff'u kontrol etmiyordu.** Artık hem ilk kayıt kickoff'u hem güncel gerçek kickoff kontrol edilir; başlanmış maçtan sonraki kayıt eğitime giremez. Sonuç kullanılabilirliği için muhafazakâr gecikme vardır.
7. **Geçmiş fiyatlar zaman kanıtı olmadan backtest'e girebiliyordu.** `odds_log` fiyatı ancak geçerli `first_seen <= updated_at < kickoff` ile karşılaştırılır. Bugünkü fiyat geçmiş fiyat yerine konmaz.
8. **Açıkça farklı market kimlikleri aynı isimde hareket serisine bağlanabiliyordu.** Event/market türü, alt tür, line, market ID ve seçim ID/numarası korunur. Kimlik değişince eski kimliğin fiyatıyla düşüş hesabı yapılmaz. Sağlayıcı seçim ID vermiyorsa numara kullanılır, ID uydurulmaz.

## Veri ve form bütünlüğü

- Sezon karşılaştırması GP/GF/GA yanında, kaynak sağladığında W/D/L/GD'yi ve kaynak aritmetiğini kontrol eder. Puan farkı ayrıca kaydedilir: disiplin puanı silinmesi mümkün olduğundan skorlar tutarlıyken puan farkı otomatik eksik maç diye düzeltilmez.
- `data_coverage_score` saf örneklem kapsamıdır; eski `data_quality` uyumluluk için kapsam/tutarlılık birleşik skoru olarak kalır. Promptta eski değer `legacy_data_quality` adıyla ayrılır.
- Lig formu ve tüm kaydedilmiş resmi kulvarların son formu ayrı döner. Avrupa/kupa maçları dinlenme ve görünen son beşe dahildir. Kulvar güçleri için doğrulanmış form ağırlığı olmadığından lig modelinin form çarpanı lig formundan hesaplanır; Avrupa galibiyeti lig puanını veya lig gücünü yeniden yazmaz.
- Hazırlık/iptal/ertelenmiş/yarıda kalmış ve skoru eksik maçlar kullanılmaz. Sonuç kullanılabilirliği, saklanmış kesin bitiş zamanı olmadığı için kickoff + 3 saatle muhafazakâr biçimde sınırlandırılır.
- Dinlenme günleri **kickoff'tan kickoff'a** ölçülür. Kesin bitiş zamanı varmış gibi davranılmaz. Program kontrolü yoksa `coverage_verified=false` kalır.
- Kaynak sezonu, 24 saatlik güncellik ve kaynak kontrolünün analiz kickoff'undan önce olması kontrol edilir. Eksik olduğu doğrulanan sezon güveni düşürür ve otomatik kupona girmez. Doğrulanmamış veri doğrulanmış gibi etiketlenmez.

## Oran provenance sözleşmesi

Mevcut `iddaa_updated_at` API alanı korunur: son başarılı bülten kontrolü anlamındadır; fiyatın sağlayıcıda değiştiği zaman değildir. Ayrıntılar `quote_provenance` altında gelir. Liste yanıtı şişmesin diye market bazlı provenance maç detayında/promptta yer alır.

| Alan | Anlamı |
|---|---|
| `last_fetch_at` | En son bülten denemesinin zamanı; başarısız da olabilir |
| `last_successful_fetch_at` | En son başarılı, ayrıştırılabilir bülten kontrolü; fiyat değişimi iddiası değildir |
| `provider_price_updated_at` | Sağlayıcının gerçek fiyat değişim zamanı; mevcut yanıtta doğrulanmadığı için **null** |
| `quotes[market].quote_observed_at` | İlgili bülten/detay HTTP yanıtının alınma zamanı |
| `quotes[market].quote_source` | O fiyatın bülten veya event detay kaynak URL'si |
| `quotes[market].last_observed_change_at` | Bizim farklı fiyat durumunu ilk gözlediğimiz zaman; ilk kayıtta başlangıç gözlemidir, resmi açılış/değişim zamanı değildir |
| `quote_status` | `observed`, `cache_or_transport_unverified`, `fetch_failed`, `unverified` |
| `quotes[market].cache_status` | `shared_cache`, `unknown`, `network_no_cache_evidence` |
| `quotes[market].identity` | Aynı market/seçim karşılaştırması için kaynak kimlikleri ve line |

HTTP `Age`, `X-Cache`, `CF-Cache-Status` ile cache kanıtı aranır; istek `Cache-Control: no-cache` gönderir. `network_no_cache_evidence`, *cache kanıtı görülmedi* demektir; sağlayıcının gizli cache'i olmadığına dair garanti değildir. Cache kanıtlı veya taşıması doğrulanmamış yanıt otomatik seçim için kullanılmaz. Oran eksikse üretilmez. Sağlayıcı başarısızlığında eski fiyatlar görülebilir ancak yeni teklif olarak önerilmez. İlk yeni başarılı veri güncellemesine kadar eski satırlarda provenance doğrulanmamış kalır.

Bu bilgiler mevcut `meta` tablosunda event bazında, toplu yazılarak tutulur; maç detayı yalnız ilgili event'leri okur. Her API isteğinde bütün bültenin büyük metadata JSON'unu indirmek gerekmez.

## Karar, belirsizlik ve deneysel pazarlar

- Kapsam, güncellik, bütünlük, model/piyasa olasılığı, fiyat farkı, kalibrasyon ve sistem kararı ayrı alanlardır. `uncertainty.empirical_accuracy_claim=false` açıkça döner.
- Kalibrasyon için ilgili bantta mevcut 30 kayıt eşiği korunur. Yetersizse `insufficient_data`; yüksek güven etiketi en fazla Orta olur. Yüksek tutma olasılığı bu etiketten ayrı gösterilir.
- Büyük ayrışma otomatik fırsat değildir. Şüpheli aday dışarıda tutulur; standart aday seçimi için kalanlarda `balanced_v2` sıralaması korunur. Pozitif tahmini fiyat farkı ayrıca gösterilir; mevcut Banko politikasının pozitif EV zorunlu tutmaması keyfi biçimde değiştirilmez. Bu değer doğrulanmış avantaj değildir.
- Başlamış/bitmiş, iptal/ertelenmiş, fiyatı doğrulanmamış veya bilinen tutarsızlığı olan aday otomatik seçilmez. Standart politika için uygun aday yoksa `no_bet`; alternatif yalnız inceleme amacı taşır. Sürprizin mevcut farklı oran bandı, standart politika `no_bet` verdi diye topluca kapatılmaz; aynı market bazlı kalite kontrollerinden geçmek zorundadır.
- Handikap üç sonuçlu **Avrupa handikabı** olarak açık tanımlanır; tam maç skor matrisiyle toplamı 1 olur. Kesirli Asya çizgisi üç sonuçlu markete dönüştürülmez. İlk yarı / İY-MS / ilk yarı Alt-Üst deneysel tanımlarla, model varsayımlarıyla verilir. Tam sayı ilk yarı toplamında iade kütlesi ayrı tutulur, koşulsuz `1/p` adil oranı uydurulmaz. Korner/kart için model yüzdesi **null** kalır.

## Beşiktaş – Gençlerbirliği ve v2 karşılaştırması

Kaynak: [İddaa event 3279014](https://sportsbookv2.iddaa.com/sportsbook/event/3279014). 8 Kasım 2026'da alınmış, zaman damgalı küçük gerçek yanıt kesiti `tests/fixtures/besiktas_genclerbirligi_20261108_quotes.json` içindedir. Kaynakta -2 handikap üçlüsü gerçekten 2,00 / 3,85 / 2,24'tür; MS 2 marjsız karşılığı yaklaşık %37,01. Bu veriler sabit model tahmini olarak kullanılmaz; market eşleme ve matematik regresyonudur. [Resmi ürün kuralı](https://yardim.bilyoner.com/hc/tr/articles/14820679057937-Futbol-Bahisleri-Nelerdir): 0:2 HMS 2, deplasmanın kazanması/beraberliği/tek fark yenilgisinde kazanır. İşaret olasılığa uydurulmadı.

| Alan / davranış | v2 | Bu değişiklik |
|---|---|---|
| Sezon tablosu, Avrupa dinlenmesi | #67 ile uzlaştırma eklenmişti | W/D/L/GD/puan ayrımı, isim-ID birleştirme ve geçmiş cutoff sağlamlaştırıldı |
| Değişmeyen 53 fiyat sonrası yeni `odds_updated_at` | Neden değiştiği anlaşılamıyordu | Son kontrol değişebilir, son gözlenen fiyat değişimi aynı kalır; sağlayıcı zamanı null |
| Verilen örnek: model 0,7105 / piyasa 0,4828 / harman 0,5966 | Ayrışma gösterilebilir; seçimde kullanılabilirdi | Aynı girdilerin yüzdeleri elle değiştirilmez; fark **22,77 puan**, inceleme gerekir. Kalibrasyon yoksa bu aday otomatik önerilmez |
| Verilen örnek: -2 MS 2 model %64,10 | Deneysel olasılık görülebilirdi | Kaynak marjsız %37,01 ile yaklaşık **27,09 puan** fark açık; deneysel ve otomatik havuz dışında |
| Geçmiş analiz | Güncel güç/tablo karışabilirdi | Cutoff öncesi sonuçlarla yeniden kurulur, kesin point-in-time veri iddiası yok |
| Haber etkisi alanı | Maç sayısı yanlış yazılabiliyordu | Gerçek haber katkısı; haber yokken 0 |
| Güven | Kalibrasyon yokken Yüksek olabiliyordu | İlgili kalibrasyon yoksa en fazla Orta; olasılık ayrı kalır |
| Forward market raporu | Brier | Brier + Log Loss + olasılık dilimi/gerçekleşme + örneklem durumu |
| Ana öneri | Fiyat/ayrışma sorunu olsa da seçim kalabilirdi | Market bazlı veto; kalan uygunsa seçim, yoksa açık `no_bet` |

Kullanıcının v2 dosyasının tam tarihsel JSON'u bu görevde verilmedi. Bu nedenle örnek yüzdeleri baştan üretilmiş gerçek v3 tahmini gibi sunmuyoruz. Sayılar kullanıcı tarafından bildirilen örnektir; İddaa fiyat/market kesiti ayrıca doğrulanmıştır.

## Değişen dosyalar ve nedenleri

- `app/quote_provenance.py`, `app/scrapers/iddaa.py`, `app/updater.py`: taşıma/kontrol/başarısızlık/fiyat kimliği kaydı.
- `app/odds_history.py`, `app/odds_log.py`: aynı kimlikli hareket karşılaştırması ve geçerli maç öncesi zaman filtresi.
- `app/scrapers/team_context.py`, `app/model/ratings.py`, `app/model/engine.py`: sezon tutarlılığı, ID normalizasyonu, resmi form/dinlenme ve tarihsel cutoff.
- `app/model/blend.py`, `app/model/decision_quality.py`, `app/model/recommendation_tools.py`, `app/config.py`: marj/fark açıklaması, yapılandırılabilir inceleme eşiği ve market bazlı karar denetimi.
- `app/model/coupons.py`, `app/model/small_leagues.py`: gerçek ürün yollarına güncellik/ayrışma/başlangıç kontrolü ve kalibrasyon sınırlaması.
- `app/model/calibration.py`, `app/model/snapshots.py`: gerçek kickoff'a göre eğitim ve ileriye dönük market metrikleri.
- `app/main.py`, `app/bulletin_api.py`, `app/explain.py`, `app/ops.py`: detay/bülten yollarında provenance ve açık bahis önermeme açıklaması, hata cache invalidation ve gövde büyüklüğü koruması.
- `static/match-prompt.js`, `static/recommendation-tools.js`, `static/model-report.js`, `static/index.html`: v3 veri dışa aktarımı, inceleme uyarıları, kalibrasyon açıklaması ve cache sürümleri.
- `tests/test_prediction_audit.py`, gerçek kaynak fixture'ı, `tests/test_blend.py`, `tests/test_coupons.py`, `tests/test_reliability.py`, `tests/js/match_prompt.test.js`, `scripts/test_postgres.py`: regresyonlar; eski testlerde geçersiz `t` zamanı / geçmişte kalan fikstürler geçerli test zamanlarıyla düzeltildi.

## Testler ve ölçüm sınırları

Tam Python ve JavaScript testleri ile CI PostgreSQL kontrolünün nihai sonuçları PR açıklamasına kaydedilir. Yeni testler eksik/tutarsız sezon, Avrupa sonrası dinlenme, iptal/ertelenme, isim değişimi, değişmeyen fiyat, cache, başarısız deneme/rollback, market kimliği değişimi, eksik fiyat, 20+ puan ayrışma, handikap toplamı, yetersiz kalibrasyon, değişmiş kickoff, geçmişe veri sızıntısı, no_bet ve başlamış maçları kapsar.

Yerel `data/iddia.db` dosyası salt okunur incelendi: `matches`, `prediction_snapshots`, `odds_log` tabloları yok. Burada ölçülebilir yerel geçmiş örneklem bulunmadığından isabet/ROI artışı raporlanamaz. Üretim veritabanı bu çalışma kapsamında değiştirilmedi veya ileriye dönük kayıt varmış gibi doldurulmadı. Üretimde mevcut karne, yeni kod sonrası normal rapor işiyle hesaplanır.

**Açık sınırlar:** geçmiş sonuçların o tarihte sisteme hangi an girdiği ve kesin maç bitiş zamanları eski kayıtlarda yok; bunları yeniden oluşturamayız. Geçmiş resmi kadrolar/haber doğrulaması ve sağlayıcının gerçek fiyat değişim zamanı yok. Tam sezon uzlaştırma ESPN ID'li desteklenen liglerle sınırlı; TFF kimlikleri buna karıştırılmaz. Deneysel marketlerin ampirik başarı kanıtı yok. 20 puan / 30 kayıt eşikleri bilimsel optimum iddiası taşımaz. Yeni sürümün daha isabetli olduğu ancak yeterli, zamanında saklanmış örneklemle karşılaştırılarak gösterilebilir.

**Devreye alma:** PR merge edilince ilk tam veri güncellemesi sezon uzlaştırmasını; ilk başarılı İddaa güncellemesi kaynak provenance'ını doldurur. İlk güncelleme öncesi eski fiyatın yalnız kontrol tarihine bakarak doğrulandığı iddia edilmez. Mevcut arşivler ve değişmez ilk tahmin kayıtları yeniden yazılmaz.
