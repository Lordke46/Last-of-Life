# Banko motoru: isabet önceliği ve kalibrasyon

Banko, uygulamadaki dengeli kupon kategorisidir. `hit_rate_v3` politikası, gerçek oran bandı içinde tahmini tutma olasılığını önce seçer. Kalibrasyonun bulunmaması kuponu kapatmaz; kanıt eksikliği açıkça gösterilir. Pozitif tahmini beklenen değer bir eleme şartı değildir. İsabet oranı ile kârlılık farklı ölçülerdir.

## Neden kupon çıkmıyordu?

Önceki politika, her lig + pazar + %10 olasılık diliminde en az 30 sonuçlanmış maç öncesi kayıt ve en az %3 tahmini beklenen değer arıyordu. 8 Kasım 2026 canlı yanıtında 26 maçtan diğer koşulları geçen 3 aday kalibrasyon şartına takılıyordu. Performans raporundaki 564 dilimin hiçbirinde 30 örnek bulunmuyordu. Böylece soğuk başlangıçta kategori tamamen kilitleniyordu.

Bu eşikler kullanıcının isabet önceliğiyle uyumlu değildi. Yeni politika kalibrasyon hesabını korur; örnek yetersizliğini, tahmin üretmeyi engelleyen bir koşul olarak kullanmaz.

## Olasılık ve seçim

1. Takım modeli; Poisson, Elo, form, haber ve dinlenme verilerini kullanır.
2. Tam ve geçerli oran grubu varsa, marjı temizlenen piyasa olasılığıyla %25 model / %75 piyasa harmanı uygulanır (`MODEL["market_weight"]`; lig/pazar kanıtı varsa `adaptive_market` ağırlığı değiştirir).
3. Maç başlamadan önce kaydedilmiş ilk tahminlerden aynı lig/pazar/dilim için en az 30 sonuçlanmış kayıt varsa kalibrasyon uygulanır. Düzeltme 60 örneklik önselle yumuşatılır ve en fazla 8 yüzde puandır.
4. Örnek azsa ham harmanlı tahmin korunur. `calibration_applied=false`, gerçek `calibration_n` ve `calibration_status=limited` taşınır. Bu seçim kalibre edilmiş gibi sunulmaz.
5. Banko adayında gerçek fiyat 1,10–2,00, kullanılacak olasılık en az %45 ve her iki takımın en düşük zaman ağırlıklı maç sayısı en az 12 olmalıdır. Uygulanan kalibrasyon olasılığı %45 altına düşürürse aday elenir.
6. Başlamış maçlar, eski/eksik gerçek oranlar, yetersiz takım verisi ve otomatik sonuçlandırılamayan pazarlar dışarıda kalır. Gerçek kaynağın oranı son 2 saat içinde güncellenmiş olmalıdır.
7. Aynı maçtan tek seçimle 1–3 farklı maç birleştirilir. Kaynağın minimum bahis adedi (MBS) korunur: kupon, bacaklarının en yüksek MBS'si kadar maç içerir; tek maç yalnız MBS 1 bültenlerde mümkündür. Toplam oran 1,80–4,00 ve tahmini ortak olasılık en az %25 olmalıdır.
8. Toplam-oran tabanını geçen kombinasyonlar arasında ortak olasılığı en yüksek olan seçilir; eşitlikte daha az maç, sonra daha yüksek toplam oran. Aday havuzu olasılığa ve verimliliğe (−ln p / ln oran) göre kurulur, MBS ≤ 2 maçlar havuzda tutulur. Alternatifler önce ortak maç içermeyecek şekilde seçilir. Taban tutmazsa tabana en yakın kupon `target_met=false` ile verilir.

`value = p × odds − 1` bilgi alanı olarak hesaplanır; pozitif olmasına göre kupon kapatılmaz. Sürpriz ve küçük lig kuponlarının eşikleri bu değişiklikle değiştirilmez.

## Kanıtın gösterimi ve geçmiş

Banko kartı, sınırlı kalibrasyon rozetini ve kaç seçimin kalibre edildiğini gösterir. Telegram metin üreticisi de kalibrasyon uygulanmayan seçimleri açıkça etiketler; bu değişiklik otomatik mesaj göndermez.

Yeni yayımlanan kuponun politika, kalibrasyon ve model/piyasa açıklamaları mevcut `meta` tablosuna seçimlerle aynı işlemde dondurulur. Sonraki tahminler geçmiş kuponun kanıt durumunu değiştirmez. Eski kuponlarda bu bilgiler bulunmuyorsa sonradan uydurulmaz; kaydedilmiş harmanlı yüzde ham model yüzdesi diye etiketlenmez. Şema değişikliği yoktur.

## Doğrulama ve sınırlar

Aynı 29 yaklaşan maça ait canlı tam tahmin örneğinde eski seçici 0, yeni seçici 3 alternatif üretti. Toplam oranlar 2,04–2,16; tahmini ortak olasılıklar yaklaşık %40; tümü kalibrasyon verisi sınırlı olarak işaretlendi. Bu bir kullanılabilirlik karşılaştırmasıdır, gelecekteki isabetin iyileştiğine dair geriye dönük başarı testi değildir.

Kupon olasılığı farklı maçların olasılıklarının bağımsızlık varsayımıyla çarpımıdır. Bütün yüzdeler tahmindir; gerçekleşmiş güvenilirlik, maç öncesi değişmez kayıtlar ve sonuçlarla ölçülmeye devam eder. `python -m app.model.report --force`, `banko_calibration` ve `prospective.news_ab` raporlarını üretir. Veri azlığında kalibrasyon/haber etkisi için başarı iddiası yapılmaz.
