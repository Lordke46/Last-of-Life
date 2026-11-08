# Arayüz tasarımı

Ana tema Forest Green. 60/30/10 renk dağılımı, ekranın içeriğine göre değişen bir tasarım hedefidir: yeşil ana kabuk ve maç bilgileri, bej okuma/seçim yüzeyleri, mor etkileşimler. Piksel bazında sabit oran uygulanmaz.

| Rol | Renk | Kullanım |
| --- | --- | --- |
| Ana, %60 | Forest Green `#1F3D2B` | Sayfa, menü, maç bilgisi, seçim etiketleri |
| İkincil, %30 | Sand Beige `#E8DCC4` | Öneri alanı, seçili gün ve kapsam |
| Vurgu, %10 | Electric Purple `#8B5CF6` | Kupona ekleme, birincil düğmeler, rozetler; odakta açık tonu |

Sistem yazı tipleri, çizgi ikonlar ve mevcut bileşenler kullanılır. Yeni font, ikon paketi veya çalışma zamanı bağımlılığı yoktur. Varsayılan yeşil tema; kullanıcının daha önce kaydettiği açık/koyu tercih korunur. Açık tema bej bir alternatiftir.

Masaüstünde yan menü ve yatay maç kartı, telefonda alt menü ve dikey kart kullanılır. Kartta takım ve mevcut oranlar yeşil alanda, öneri ve kupona ekleme bej alanda gösterilir. Ayrıntıdaki tam açıklama korunur. Veri kapsamı açılır bir açıklamaya taşınır; maç/lig sayısı ve güncelleme zamanı görünür kalır.

İlk açılış mevcut davranış gereği ana liglerdir; artık bunun seçili olduğu açıkça gösterilir. Tüm bülten ve diğer ligler ayrı kapsam seçenekleridir. Gün ve lig filtrelerinin çalışma mantığı, öneri hesapları ve kupon kuralları korunur.

## Yerel araçlar

- `petergyang/no-ai-slop`, `~/.codex/skills/no-ai-slop` altına kuruldu. Ekran metinleri doğrudan ve kısa tutuldu.
- `virgiliojr94/book-to-skill` (`master`), `~/.codex/skills/book-to-skill` altına kuruldu. Kaynak kitap/doküman sağlanmadığı için dönüşüm çalıştırılmadı.

Bunlar geliştirme skill'leridir; uygulamanın tarayıcı paketine eklenmez. Sonraki Codex turunda kullanılabilirler.
