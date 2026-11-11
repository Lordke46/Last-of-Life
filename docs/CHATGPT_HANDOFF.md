# Last-of-Life — geliştirme devri

Durum: yerel Grok Research geliştirmeleri, `codex/grok-research-mvp` dalındaki `fcf4aa6` temeli üzerine kaydedildi. Bu dal üretime alınmış veya main'e birleştirilmiş değildir. Bu devir dosyasındaki kodları ve mevcut çalışma ağacını inceleyerek devam edin; staging ve production'a izinsiz müdahale etmeyin.

## Tamamlananlar

- Ayrı Research API, servis kimliği, iş kuyruğu, claim/renew, idempotent sonuç kaydı ve Grok Skill JSON adaptörü; mevcut sonuçlar versiyonlu saklanır.
- Yöneticiye özel Grok Intelligence API ve arayüzü; araştırma bulguları matematiksel olasılıklardan ayrı gösterilir.
- Kanonik maç kimliği, atomik günlük üç maç kotası, yeniden araştırma onayı ve salt okunur UI rolü için yerel PostgreSQL migration taslakları.
- Güvenilir ölçüm için append-only snapshot, kupon ve settlement migration taslağı. Geçmiş veriler doğrulanmış yeni tahminlere dönüştürülmez.
- Gerçek fiyatlı adaylardan 2,50–4,00 hedefli ayrı üçlü öneri; Grok risk iddiaları görünür, fakat model olasılıkları veya seçimler otomatik değiştirilmez.
- Mevcut zamanlayıcıya varsayılan olarak kapalı araştırma kuyruğu köprüsü eklendi. Grok Routine kurulmadı.

## Kanıt ve sınırlamalar

- Önceki yerel koşuda 544 Python testi, 80 alt test ve 93 JavaScript testi geçti. Bu devir sırasında uzun testler yeniden çalıştırılmadı. Yeni üçlü/kuyruk testleri pytest biçimindedir; mevcut GitHub iş akışındaki unittest keşfi bunları tek başına çalıştırmaz.
- Gerçek Grok pilotunda rapor kaydı ve UI daha önce staging üzerinde doğrulandı; son otomatik kuyruk ve üçlü bağlantısı gerçek Grok ile uçtan uca doğrulanmadı.
- Otomatik kuyruk için kanonik kimliklerin gözden geçirilmesi, eski işlerin güvenli backfill kararı, ayrı sınırlı kuyruk yazıcısı ve gerçek PostgreSQL entegrasyon testi eksik. Bot yetkilerini genişletmeyin.
- Grok Bot Routine ürün özelliği belgelenmiş olsa da hesapta Skill'in, secret erişiminin ve gözetimsiz çalışmanın uyumu doğrulanmalı. Eksik araştırmalar ve doğrulama etiketleri gerçek haber doğrulaması sayılmaz.
- Ölçüm taslağında güvenilir dış sonuç kanıtı, kesin settlement, gerçek bahis/ödeme verisi ve kapsamlı aday evreni henüz yok; gerçek getiri veya başarı iddia edilmiyor. Üçlü araması sınırlı bir aday havuzunu değerlendirir.
- Migration dosyaları inceleme taslağıdır; staging veya production'a uygulanmış varsayılmamalıdır. Özellikle eski evaluation taslağını üretime hazır kabul etmeyin.

## Sonraki adımlar

1. Değişiklikleri ve `docs/GROK_QUEUE_AND_EVALUATION.md`, `docs/GROK_MEASUREMENT_INTEGRITY.md`, `docs/GROK_SECOND_PILOT.md` belgelerini inceleyin; güvenlik ve kimlik geçişini kararlaştırın.
2. Kullanıcı onayıyla izole staging ortamında ayrı yazıcı ve okuyucu izinleriyle kuyruk, claim/renew, Skill sonucu ve UI akışını test edin. Gerçek sırları kaynak koda veya devir notuna koymayın.
3. Grok Routine ve Skill entegrasyonunu doğrulayın; deployment, production migration, main'e merge ve otomasyonun başlatılması için ayrıca onay alın.
