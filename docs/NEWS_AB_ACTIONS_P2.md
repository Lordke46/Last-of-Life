# P2 — Haber etkisi A/B ve çalışma maliyeti

## A/B deneyi

`prediction_snapshots` ilk **maç öncesi** tahminde haberli ve habersiz
1X2 olasılıklarını aynı anda saklar. Sonuçlandığında
`/api/model-report` içindeki `prospective.news_ab.affected_only` bölümünde:

- `n`: sadece haberli ve habersiz tahminlerin farklı olduğu eşleşmiş maçlar,
- `delta_brier`: Brier(haberli) − Brier(habersiz), **negatif daha iyi**,
- `interval95`: maç başına eşleştirilmiş farkın yaklaşık %95 aralığı,
- `verdict`: `insufficient_data`, `inconclusive`, `news_improves` veya `news_hurts`.

100 farklılaşan maçtan önce yorum yapılmaz. 100 ve üstünde bile
belirsizlik aralığı sıfırı kapsamıyorsa bir yön söylenir.
Bu bir kalite ölçümüdür: modelin haber katkısını **otomatik açıp kapatmaz**.
Tarihsel haber arşivi olmayan eski maçlara haber etkisi uydurulmaz.

## RSS ve Actions maliyeti

Güncellemede her yaklaşan takımın haber RSS'ini yeniden istemek yerine:
- İlk 24 saat içinde maç varsa başarılı son sorgu **1 saat** geçerlidir.
- Maçı daha uzaktaysa başarılı son sorgu **6 saat** geçerlidir.
- Cache DB'de takım/lig anahtarıyla tutulur; başarısız HTTP sorguları
  başarılıymış gibi cache'e yazılmaz.
- Her çalışmada kaç takım tarandığı ve kaçının önbellekten atlandığı
  logda görünür.
- Telegram Action'ı günde yaklaşık 20 yerine 7 kez sorgu yapar
  (Türkiye 09:20, 12:20, 15:20, 18:20, 21:20, 00:20, 03:20).
  Mesajın tek sefer gönderilme koruması değişmedi.

## Güvenlik sınırı

RSS önbelleği haber doğruluğunu garanti etmez. Haberlerin puanını
kalibre etmek için ileriye dönük yeterli örnek birikmesi gerekir.
Sadece olumlu A/B sonuçları görüldü diye model otomatik değiştirilmez.
