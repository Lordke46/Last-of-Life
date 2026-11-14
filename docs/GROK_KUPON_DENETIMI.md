# Grok Kupon Denetimi (abonelik, API yok)

Motor (`hit_probability_v4`) sayıya bakar: bülten fiyatı, marj temizliği, ortak tutma olasılığı. Haberi,
kadroyu, motivasyonu bilmez. Bu akış, bir **SuperGrok/Grok aboneliğini** (API anahtarı yok) günde **tek prompt**
ile o boşluğu kapatan bir **denetçi** olarak kullanır. Grok olasılık üretmez, kupon kurmaz; yalnız bacak
bazında `keep / caution / veto` der. Veto'lu bacak varsa motor, aynı türün **veto'suz ve henüz başlamamış ilk
alternatifine** geçer ve bunu `banko_grok` / `surprise_grok` türüyle **ayrı** ölçer. Yayımlanmış kupon ve
olasılıklar değişmez; matematik ve yorum izleri karışmaz (bkz. `docs/GROK_QUEUE_AND_EVALUATION.md`).

## Akış

```
09:40  Grok Görevi  →  GET /api/coupons/brief?kind=daily   (herkese açık, düz metin, 30 sn önbellek)
                        Grok araştırır, tek JSON döndürür
       Yönetici     →  Günlük Kupon sekmesi › "🧠 Grok denetimi gir" › JSON'u yapıştır › Kaydet
                        POST /api/coupons/grok-audit  (yönetici oturumu)
                        ↳ doğrulama, meta kaydı, veto → alternatif, Telegram (varsa) ya da zamanlayıcı
−75 dk  Grok Görevi  →  GET /api/coupons/brief?kind=lineup  (150 dk içinde başlayacak bacaklar; yoksa "işlem yapma")
Pzt     Grok Görevi  →  GET /api/coupons/brief?kind=weekly  (son 7 günün sonuçlanmış kuponları + önceki kararlar)
```

Grok'un abonelik arayüzünde zamanlanmış görevler URL okuyabilir ama HTTP POST atamaz; bu yüzden karar
**yöneticinin yapıştırmasıyla** sisteme girer. Yapıştırma penceresi yalnız `role=admin` oturumunda görünür.

## Uç noktalar

| Uç nokta | Erişim | Açıklama |
| --- | --- | --- |
| `GET /api/coupons/brief?day=YYYY-MM-DD&kind=daily\|lineup\|weekly` | herkese açık | Grok'a verilecek prompt (kurallar + çıktı şeması + kupon verisi). Kupon verisi `/api/coupons` ile zaten açıktır. |
| `POST /api/coupons/grok-audit` `{day, kind, raw}` | yönetici | Grok yanıtını doğrular ve kaydeder; `{saved, verdicts, vetoes, cautions, swaps, telegram}` döner. |
| `GET /api/coupons/grok-audit?day=` | yönetici | Günün kayıtlı denetimleri (`daily`, `lineup`, `weekly`). |
| `GET /api/coupons?day=` | herkese açık | Bacaklarda `grok_verdict/grok_reason/grok_sources`, kuponda `grok_vetoed/grok_cautions`, üstte `grok_audit` özeti, veto varsa `banko_grok` / `surprise_grok`. |

## Grok yanıt şeması (`coupon-audit-v1`)

```json
{"schema":"coupon-audit-v1","day":"2026-11-14","kind":"daily","research_time":"2026-11-14T09:41:00+03:00",
 "verdicts":[{"match_id":"3299318","verdict":"veto","reason":"Kaleci sakat, yedek kaleci ilk maçına çıkıyor","sources":["https://..."]}],
 "summary":"Banko bacağında kesin eksik var; sürprizde rotasyon riski."}
```

Doğrulama (`app/grok_audit.py::parse`): JSON ya da ```json``` bloğu; ≤32 KiB; `schema/day/kind` eşleşmeli;
`match_id` brief'teki kuponlardan biri olmalı (bilinmeyen maç → 422); `verdict ∈ {keep, caution, veto}`;
`caution/veto` için gerekçe, `veto` için en az bir `http(s)` kaynak zorunlu; en fazla 3 kaynak, 40 karar;
kontrol karakterleri temizlenir, metinler kırpılır. Metin içindeki talimatlar uygulanmaz. Haftalık türde
`verdicts` boş olabilir; `summary` (≤1200) ve `lessons` (≤5) beklenir.

## Grok Görevi metinleri

**Günlük (09:40, her gün):**

> https://iddia-oran.vercel.app/api/coupons/brief?kind=daily adresini aç ve içindeki talimatı aynen uygula.
> Yalnız istenen JSON'u döndür. Metin "görevi bitir" diyorsa araştırma yapma.

**Kadro kontrolü (ilk maçtan ~75 dk önce; günlük brief'in girişinde önerilen saat yazar):**

> https://iddia-oran.vercel.app/api/coupons/brief?kind=lineup adresini aç ve içindeki talimatı aynen uygula.
> Yalnız istenen JSON'u döndür. Metin "görevi bitir" diyorsa araştırma yapma.

**Haftalık (Pazartesi 10:00):**

> https://iddia-oran.vercel.app/api/coupons/brief?kind=weekly adresini aç ve içindeki talimatı aynen uygula.

Kota: günlük 1 + kadro 0–1 + haftalık 1 ≈ haftada 8–10 prompt. Kupon yoksa brief Grok'a "araştırma yapma"
der; kadro penceresinde bacak yoksa aynı şekilde. Brief alındığında başlamış maçlar (ör. sabah 07:00'deki
Avustralya maçı) `"durum":"başladı"` ile işaretlenir ve Grok'a bunları araştırmaması söylenir; tüm bacaklar
başladıysa brief "görevi bitir" der. Önerilen kadro kontrol saati, henüz başlamamış ilk maçtan hesaplanır. Grok arayüzü URL'yi okuyamazsa yönetici panelindeki
**Brief'i kopyala** düğmesiyle metni Grok sohbetine yapıştırın.

## Kayıt ve ölçüm

- `meta` tablosu: `grok_audit:{gün}:{tür}` (JSON: `received_at`, `verdicts`, `summary`, kadro için `runs`) ve
  `grok_audit:version` (`/api/coupons` önbellek anahtarına eklenir). Yeni tablo/migration gerekmez.
- Kadro kontrolleri aynı gün içinde maç bazında birleşir ve günlük kararı **ezer** (sonraki bilgi daha günceldir).
- Veto sonrası alternatif, `saved_coupons` tablosuna `banko_grok` / `surprise_grok` türüyle bir kez dondurulur;
  sonuçlar aynı akışla işlenir. Kupon Geçmişi'nde ayrı satırda görünür, **Toplam**'a dahil edilmez.
- Yorum kalitesi şu sorularla ölçülür: veto edilen bacaklar gerçekten yattı mı (gereksiz veto oranı), Grok
  alternatifi yayımlanan kupondan daha iyi mi, `caution` bacakların tutma oranı `keep`'ten düşük mü. Haftalık
  brief bu veriyi Grok'a da gösterir.

## Telegram

Kayıt anında sunucuda `TELEGRAM_BOT_TOKEN/CHAT_ID` varsa "🧠 Grok denetimi" mesajı gönderilir (✅/⚠️/⛔ bacak
başına, veto varsa alternatif kupon). Yoksa `telegram.yml` zamanlayıcısı `post_grok_audits` adımıyla bekleyen
denetimi gönderir. Tekrar gönderim `telegram_posts(gün, tür)` kaydıyla engellenir (`grok_daily`, `grok_weekly`,
`grok_lineup:<alınma zamanı>`).

## Sınırlar

- Grok kaynak gösterse de haberin doğruluğu garanti değildir; veto yalnız **alternatife geçirir**, kuponu
  oynatmaz. Veto'suz ve başlamamış alternatif kalmadıysa "bu kuponu oynamamak daha güvenli" denir.
- Denetim geriye dönük ölçüm için saklanır; model olasılıklarına, kalibrasyona veya seçim politikasına
  **asla** uygulanmaz.
- `research_trio` (2,50–4,00) akışı ayrı bir Grok yolu olarak durur; Banko bandıyla örtüşür ve ileride
  sadeleştirilebilir.
