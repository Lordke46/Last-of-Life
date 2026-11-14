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

  const GROK_VERDICT = { keep: ["Grok: tut", "b-good"], caution: ["Grok: dikkat", "b-warn"], veto: ["Grok: veto", "b-bad"] };

  const safeUrl = (value) => { try { const u = new URL(String(value)); return u.protocol === "https:" || u.protocol === "http:" ? u.href : ""; } catch (_) { return ""; } };

  function grokBadge(pick) {
    const verdict = pick && GROK_VERDICT[pick.grok_verdict];
    if (!verdict) return "";
    const reason = pick.grok_reason ? ` title="${esc(pick.grok_reason)}"` : "";
    const url = Array.isArray(pick.grok_sources) ? safeUrl(pick.grok_sources[0]) : "";
    const source = url ? ` <a class="grok-source" href="${esc(url)}" target="_blank" rel="noopener nofollow">kaynak</a>` : "";
    return `<div class="grok-verdict"><span class="badge ${verdict[1]}"${reason}>${verdict[0]}</span>${pick.grok_reason ? ` <small class="muted">${esc(pick.grok_reason)}</small>` : ""}${source}</div>`;
  }

  function couponCard(coupon, title, badgeClass, share = null) {
    coupon = coupon && typeof coupon === "object" ? coupon : {};
    const picks = Array.isArray(coupon.picks) ? coupon.picks.filter((pick) => pick && typeof pick === "object") : [];
    const rows = picks.map((pick) => `<tr>
      <td class="l" data-label="Maç"><b>${esc(pick.home || "Takım bilgisi yok")}</b> – ${esc(pick.away || "Takım bilgisi yok")}${grokBadge(pick)}</td>
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
      : coupon.publication === "live" ? `<p class="muted">Güncel alternatif · günün kayıtlı kuponundan farklı olabilir.</p>`
      : coupon.publication === "grok" ? `<p class="muted">Grok veto'su sonrası geçilen veto'suz alternatif; geçmişte ayrı tür olarak ölçülür.</p>` : "";
    const grokWarning = Number(coupon.grok_vetoed) > 0
      ? `<p class="coupon-grok-warning"><span class="badge b-bad">Grok veto</span> ${esc(coupon.grok_note || `${coupon.grok_vetoed} bacak güvenilir kaynakla veto edildi; aşağıda alternatif gösterilir.`)}</p>`
      : coupon.grok_note ? `<p class="coupon-grok-warning"><span class="badge b-info">Grok</span> ${esc(coupon.grok_note)}</p>` : "";
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
    return `<div class="card coupon-card"><div class="row-between"><h3>${esc(title)}</h3><span>${source(coupon, picks)} <span class="badge ${esc(badgeClass)}">${picks.length} maç</span></span></div>${selectionNote}${publication}${grokWarning}${calibrationNote}${content}${totals}${research}${shareActions}</div>`;
  }

  function grokAuditCard(data) {
    const audit = data && typeof data === "object" ? data.grok_audit : null;
    if (!audit || typeof audit !== "object") return "";
    const when = audit.received_at ? new Date(audit.received_at) : null;
    const stamp = when && !Number.isNaN(when.getTime())
      ? when.toLocaleString("tr-TR", { timeZone: "Europe/Istanbul", hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" }) : "";
    const kinds = (Array.isArray(audit.kinds) ? audit.kinds : []).map((k) => ({ daily: "günlük", lineup: "kadro", weekly: "haftalık" }[k] || k)).join(" + ");
    const counts = `<span class="badge b-bad">${Number(audit.vetoes) || 0} veto</span> <span class="badge b-warn">${Number(audit.cautions) || 0} dikkat</span>`;
    const swaps = (Array.isArray(audit.swaps) ? audit.swaps : []).map((swap) => {
      const label = swap.kind === "surprise" ? "Sürpriz" : "Banko";
      const vetoed = (Array.isArray(swap.vetoed) ? swap.vetoed : []).map(esc).join(", ");
      return swap.coupon_id
        ? `<li><b>${label}</b>: ${vetoed} veto edildi → veto'suz alternatif aşağıda.</li>`
        : `<li><b>${label}</b>: ${vetoed} veto edildi; başlamamış, veto'suz alternatif kalmadı. Bu kuponu oynamamak daha güvenli.</li>`;
    }).join("");
    const replaced = ["banko", "surprise"].map((kind) => data[`${kind}_grok`] && Array.isArray(data[`${kind}_grok`].picks) && data[`${kind}_grok`].picks.length
      ? couponCard(data[`${kind}_grok`], kind === "banko" ? "Banko · Grok sonrası alternatif" : "Sürpriz · Grok sonrası alternatif", "b-info") : "").join("");
    return `<section class="coupon-group grok-audit"><div class="card"><div class="row-between"><h3>🧠 Grok denetimi</h3><span>${counts}</span></div>
      <p class="muted">${esc([kinds, stamp].filter(Boolean).join(" · "))}</p>
      ${audit.summary ? `<p>${esc(audit.summary)}</p>` : ""}
      ${swaps ? `<ul class="grok-swaps">${swaps}</ul>` : ""}
      <p class="muted small">Grok yorumu olasılıkları değiştirmez; yalnız kaynaklı olumsuz haberle bacak eler. Garanti değildir.</p></div>${replaced}</section>`;
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
    const label = type === "small_leagues" ? "Piyasa verili küçük ligler" : type === "surprise" ? "Sürpriz" : "Banko";
    const diag = data && typeof data === "object" ? data[`${type}_diagnostics`] || {} : {};
    const lines = [];
    if (diag.stale_odds) {
      const age = Number(diag.odds_age_minutes);
      const when = Number.isFinite(age) ? `Oranlar ${Math.trunc(age)} dk önce alındı. ` : "";
      lines.push(`${when}Canlı kupon kapalı: iddaa fiyatı 2 saatten eski (${diag.stale_odds} maç).`);
    }
    const policies = ["hit_rate_v3", "hit_rate_v4", "hit_probability_v4"];
    if (type !== "small_leagues" && policies.includes(diag.selection_policy) && !diag.eligible_matches && !diag.stale_odds) {
      const bands = {
        hit_rate_v3: "%62 olasılık, 1,40–2,30 gerçek oran",
        hit_rate_v4: "%70 olasılık, 1,15–2,30 gerçek oran",
        hit_probability_v4: type === "surprise" ? "%33 olasılık, 1,40–3,50 gerçek oran" : "%45 olasılık, 1,10–2,00 gerçek oran",
      };
      lines.push(`Henüz başlamamış maçlarda ${bands[diag.selection_policy]} ve yeterli takım verisi koşullarına uyan aday bulunamadı.`);
    } else if (type !== "small_leagues" && policies.includes(diag.selection_policy) && diag.eligible_matches) {
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
    const label = type === "small_leagues" ? "Piyasa verili küçük lig alternatifleri" : type === "surprise" ? "Sürpriz tercihi" : "Banko tercihi";
    const coupons = alternatives(data, type);
    const content = coupons.length
      ? coupons.map((coupon, index) => couponCard(coupon, `${label} Kuponu ${index + 1}`, badgeClass, { kind: type, index, date, selectionTier: data?.selection_tier, targetMet: data?.target_met })).join("")
      : `<div class="card muted">${esc(emptyReason(data, type))}</div>`;
    return `<section class="coupon-group"><h3>${label} Kupon Alternatifleri</h3>${content}</section>`;
  }

  const api = { alternatives, couponCard, couponGroup, emptyReason, grokAuditCard, grokBadge };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") window.CouponView = api;
})();
