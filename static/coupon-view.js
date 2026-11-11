(() => {
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
  const numeric = (value) => value === null || value === undefined || value === ""
    ? null : Number.isFinite(Number(value)) ? Number(value) : null;
  const displayNumber = (value, digits = 2) => {
    const number = numeric(value);
    return number === null ? "-" : number.toFixed(digits);
  };
  const probability = (value) => {
    const number = numeric(value);
    return number === null ? "-" : `${Math.round(number * 100)}%`;
  };
  const pickTime = (kickoff) => {
    const time = kickoff ? new Date(kickoff) : null;
    return time && !Number.isNaN(time.getTime())
      ? time.toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" })
      : "-";
  };

  function odds(pick) {
    const amount = esc(displayNumber(pick.odds));
    return pick.odds_source === "iddaa"
      ? `<b class="iddaa-odd">${amount}</b><span class="src src-iddaa">iddaa</span>`
      : `<b class="iddaa-odd">${amount}</b><span class="src src-claude">tahmini</span>`;
  }

  function source(coupon, picks) {
    if (!picks.length) return "";
    if (coupon.odds_source === "iddaa") return `<span class="badge b-info">oranlar: iddaa</span>`;
    if (coupon.odds_source === "claude") return `<span class="badge b-warn">oranlar: tahmini</span>`;
    const iddaaPicks = numeric(coupon.iddaa_picks) ?? 0;
    return `<span class="badge b-info">oranlar: ${Math.trunc(iddaaPicks)}/${picks.length} iddaa, kalanı tahmini</span>`;
  }

  function couponCard(coupon, title, badgeClass, share = null) {
    coupon = coupon && typeof coupon === "object" ? coupon : {};
    const picks = Array.isArray(coupon.picks) ? coupon.picks.filter((pick) => pick && typeof pick === "object") : [];
    const rows = picks.map((pick) => `<tr>
      <td class="l" data-label="Maç"><b>${esc(pick.home || "Takım bilgisi yok")}</b> – ${esc(pick.away || "Takım bilgisi yok")}</td>
      <td data-label="Lig">${esc(pick.league_name || pick.league || "-")}</td>
      <td data-label="Saat">${esc(pickTime(pick.kickoff))}</td>
      <td data-label="Seçim"><span class="badge b-info">${esc({"1X":"ÇŞ 1–0","X2":"ÇŞ 0–2","12":"ÇŞ 1–2"}[pick.market] || pick.market || "-")}</span></td>
      <td data-label="Oran">${odds(pick)}</td>
      <td data-label="Olasılık">${probability(pick.p)}${pick.probability_source === "iddaa_market" ? "<small> · Piyasa</small>" : ""}</td>
    </tr>`).join("");
    const content = picks.length
      ? `<table><thead><tr><th class="l">Maç</th><th>Lig</th><th>Saat</th><th>Seçim</th><th>Oran</th><th>Olasılık</th></tr></thead><tbody>${rows}</tbody></table>`
      : `<p class="muted">Bu kupon için uygun maç bulunamadı.</p>`;
    const totals = picks.length
      ? `<div class="coupon-total"><b>Toplam oran:</b> ${esc(displayNumber(coupon.total_odds))} · <b>Toplam tahmini olasılık:</b> ${probability(coupon.total_probability)}${coupon.stake_note ? `<details class="coupon-method"><summary>Seçim yöntemi</summary><p class="muted">${esc(coupon.stake_note)}</p></details>` : ""}</div>`
      : "";
    const shareActions = picks.length && share
      ? `<div class="coupon-share-actions"><button type="button" class="btn" data-share-coupon data-share-type="${esc(share.kind)}" data-share-index="${share.index}">Paylaş</button><select aria-label="Görsel boyutu" data-share-size><option value="square">Kare 1080×1080</option><option value="story">Story 1080×1920</option></select></div>`
      : "";
    const calibrationNote = coupon.calibration_note
      ? `<p class="coupon-calibration"><span class="badge ${coupon.calibration_status === "limited" ? "b-warn" : "b-info"}">${coupon.calibration_status === "limited" ? "Kalibrasyon verisi sınırlı" : "Kalibrasyon uygulandı"}</span> ${esc(coupon.calibration_note)}</p>` : "";
    const publication = coupon.publication === "published"
      ? `<p class="muted">Günün yayımlanan kuponu. Geçmiş ve Telegram bu kuponu kullanır; oranlar yayımlandığı andaki değerlerdir. Başlamış maçlar yeniden oynanamaz.</p>`
      : coupon.publication === "live" ? `<p class="muted">Güncel alternatif · günün kayıtlı kuponundan farklı olabilir.</p>` : "";
    const tier = coupon.selection_tier || coupon.metadata?.selection_tier || share?.selectionTier;
    const targetMet = coupon.target_met ?? coupon.metadata?.target_met ?? share?.targetMet;
    const tierLabel = tier === "trio" ? "Üçlü hedefi sağlandı" : tier === "preferred" ? (coupon.type === "surprise" ? "Sürpriz hedefi sağlandı" : "Banko hedefi sağlandı") : tier === "banko" || tier === "balanced" ? "Banko tercihi · dengeli" : tier === "surprise" ? "Sürpriz tercihi" : "Günün en uygun mevcut alternatifi";
    const targetNote = targetMet === true
      ? "Tercih koşullarını karşıladı; sonuç garantisi değildir."
      : targetMet === false
        ? "Hedef koşullarına tam uyan kupon oluşmadı; en iyi mevcut alternatif gösteriliyor. Garanti değildir."
        : "Tercih, mevcut maç ve oran verilerine göre oluşturuldu; garanti değildir.";
    const selectionNote = picks.length
      ? `<p class="coupon-selection-note"><b>${esc(tierLabel)}.</b> ${esc(targetNote)}</p>` : "";
    const research = coupon.type === "trio" && picks.length ? `<div class="coupon-research"><p class="muted">Grok bulguları ayrı risk bilgisidir; seçim olasılıkları değişmez. Araştırma iddiaları ayrıca doğrulanmalıdır.</p>${picks.map(pick => `<div data-grok-coupon-match="${esc(pick.id)}"><b>${esc(pick.home)} – ${esc(pick.away)}</b></div>`).join("")}</div>` : "";
    return `<div class="card coupon-card"><div class="row-between"><h3>${esc(title)}</h3><span>${source(coupon, picks)} <span class="badge ${esc(badgeClass)}">${picks.length} maç</span></span></div>${selectionNote}${publication}${calibrationNote}${content}${totals}${research}${shareActions}</div>`;
  }

  function alternatives(data, type) {
    if (!data || typeof data !== "object") return [];
    if (Array.isArray(data[`current_${type}_alternatives`]) && (!data.date || data.date >= new Date().toLocaleDateString("sv-SE",{timeZone:"Europe/Istanbul"}))) return data[`current_${type}_alternatives`];
    const key = type === "small_leagues" ? "small_league_alternatives" : `${type}_alternatives`;
    const listed = Array.isArray(data[key])
      ? data[key].filter((coupon) => coupon && typeof coupon === "object" && Array.isArray(coupon.picks) && coupon.picks.length)
      : [];
    if (listed.length) return listed;
    return data[type] && typeof data[type] === "object" && Array.isArray(data[type].picks) && data[type].picks.length
      ? [data[type]] : [];
  }

  function emptyReason(data, type) {
    const label = type === "small_leagues" ? "Piyasa verili küçük ligler" : type === "surprise" ? "Sürpriz" : "Dengeli (Banko)";
    const diag = data && typeof data === "object" ? data[`${type}_diagnostics`] || {} : {};
    const lines = [];
    if (diag.stale_odds) {
      const age = Number(diag.odds_age_minutes);
      const when = Number.isFinite(age) ? `Oranlar ${Math.trunc(age)} dk önce alındı. ` : "";
      lines.push(`${when}Canlı kupon kapalı: iddaa fiyatı 2 saatten eski (${diag.stale_odds} maç).`);
    }
    if (type === "banko" && ["hit_rate_v3", "hit_rate_v4"].includes(diag.selection_policy) && !diag.eligible_matches && !diag.stale_odds) {
      lines.push(diag.selection_policy === "hit_rate_v4" ? "Henüz başlamamış maçlarda %70 olasılık, 1,15–2,30 gerçek oran ve yeterli takım verisi koşullarına uyan aday bulunamadı." : "Henüz başlamamış maçlarda %62 olasılık, 1,40–2,30 gerçek oran ve yeterli takım verisi koşullarına uyan aday bulunamadı.");
    } else if (type === "banko" && ["hit_rate_v3", "hit_rate_v4"].includes(diag.selection_policy) && diag.eligible_matches) {
      lines.push(`${diag.eligible_matches} aday var; farklı maç sayısı, toplam oran veya minimum bahis adedi koşullarında uygun kupon oluşmadı.`);
    } else if (type === "banko" && diag.calibration_insufficient) {
      const best = Number.isFinite(Number(diag.calibration_best_n)) ? Math.trunc(Number(diag.calibration_best_n)) : 0;
      const need = Number.isFinite(Number(diag.calibration_min_samples)) ? Math.trunc(Number(diag.calibration_min_samples)) : 30;
      lines.push(`Banko için aynı lig, pazar ve olasılık diliminde en az ${need} sonuçlanmış kayıt gerekir. En dolu dilim ${best}/${need}.`);
    }
    if (!lines.length && data && (data.selection_tier || data.target_met !== undefined)) {
      lines.push(data.target_met === false
        ? "Hedef koşullarına tam uyan kupon oluşmadı; seçilebilir maç ve oranlar içindeki en iyi alternatif gösteriliyor."
        : "Günün en uygun mevcut alternatifi bulunamadı.");
    }
    if (!lines.length) lines.push(`${label} için şu anda gösterilebilir alternatif yok.`);
    return lines.join(" ");
  }

  function couponGroup(data, type, badgeClass, date) {
    const label = type === "small_leagues" ? "Piyasa verili küçük lig alternatifleri" : type === "surprise" ? "Sürpriz tercihi" : "Banko tercihi · Dengeli";
    const coupons = alternatives(data, type);
    const content = coupons.length
      ? coupons.map((coupon, index) => couponCard(coupon, `${label} Kuponu ${index + 1}`, badgeClass, { kind: type, index, date, selectionTier: data?.selection_tier, targetMet: data?.target_met })).join("")
      : `<div class="card muted">${esc(emptyReason(data, type))}</div>`;
    return `<section class="coupon-group"><h3>${label} Kupon Alternatifleri</h3>${content}</section>`;
  }

  const api = { alternatives, couponCard, couponGroup, emptyReason };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.CouponView = api;
})();
