// Sade gösterim yardımcıları: tek ana öneri, düz Türkçe etiket, 3 seviyeli güven rozeti.
// Sunucu `summary` alanını (app/explain.py) üretir; yoksa (küçük ligler, eski önbellek) buradan türetilir.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Simple = api;
})(globalThis, function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const LEVELS = { "Yüksek": 3, "Orta": 2, "Düşük": 1 };

  function confidenceLevel(match) {
    return Math.max(0, Math.min(3, Number(info(match).level) || 0));
  }
  function sortMatches(matches) {
    const score = (m) => typeof m.confidence === "number" && Number.isFinite(m.confidence) ? m.confidence : -1;
    return [...matches].sort((a, b) => confidenceLevel(b) - confidenceLevel(a)
      || score(b) - score(a) || Date.parse(a.kickoff) - Date.parse(b.kickoff)
      || String(a.id).localeCompare(String(b.id)));
  }

  function filterMatches(matches, filters = {}, live = new Map(), now = Date.now()) {
    const day = (value) => new Date(value).toLocaleDateString("sv-SE", {timeZone: "Europe/Istanbul"});
    return matches.filter((m) => (!filters.high || confidenceLevel(m) === 3)
      && (!filters.today || day(m.kickoff) === day(now))
      && (!filters.live || live.get(String(m.id))?.state === "in"));
  }

  function headline(market) {
    if (["1X", "X2", "12"].includes(market)) return {"1X":"ÇŞ 1–0","X2":"ÇŞ 0–2","12":"ÇŞ 1–2"}[market];
    if (["1", "X", "2"].includes(market)) return "MS " + (market === "X" ? "0" : market);
    const handicap=/^Handikap (-?\d+(?:\.\d+)?) ([1X2])$/.exec(market || "");
    if(handicap) {const n=Number(handicap[1]);return `HMS ${n < 0 ? "0:"+Math.abs(n) : n+":0"} ${handicap[2] === "X" ? "0" : handicap[2]}`;}
    const m = /^([ÜA])(\d+(?:\.\d+)?)$/.exec(market || "");
    if (m) return `${m[2]} ${m[1] === "Ü" ? "Üst" : "Alt"}`;
    return market || "";
  }

  function title(market) {
    const fixed = {
      "1": "Ev sahibi kazanır", "X": "Beraberlik", "2": "Deplasman kazanır",
      "1X": "Ev sahibi kaybetmez", "X2": "Deplasman kaybetmez", "12": "Beraberlik olmaz",
      "KG Var": "İki takım da gol atar", "KG Yok": "En az bir takım gol atamaz",
      "Ev 0.5Ü": "Ev sahibi gol atar", "Dep 0.5Ü": "Deplasman gol atar",
    };
    if (fixed[market]) return fixed[market];
    const m = /^([ÜA])(\d+(?:\.\d+)?)$/.exec(market || "");
    if (m) return m[1] === "Ü" ? `${Math.floor(Number(m[2])) + 1} veya daha fazla gol` : `En fazla ${Math.floor(Number(m[2]))} gol`;
    return market || "";
  }

  /** Bir maç/öneri nesnesinden gösterilecek tek öneri. */
  function info(m) {
    if (m.summary) {
      const market = m.summary.market;
      const marketPick = m.pick?.probability_source === "iddaa_market" || !!m.small_league;
      const p = Number.isFinite(m.pick?.p) ? m.pick.p : Number.isFinite(m.markets?.[market]?.p) ? m.markets[market].p : null;
      return { ...m.summary, p, odds: m.summary.odds ?? m.pick?.odds ?? m.iddaa_odds?.[market] ?? null,
        selectionTier: m.pick?.selection_tier, targetMet: m.pick?.target_met,
        pSource: p == null ? "" : marketPick ? "market" : "model" };
    }
    const pick = m.pick || m.alternative;
    if (!pick) return { market: null, headline: "Veri bekleniyor", title: "Seçim hesabı için yeterli oran grubu veya model verisi yok", odds: null, level: 0, level_label: "", reason: "" };
    const small = !!m.small_league;
    return {
      market: pick.market, headline: headline(pick.market), title: title(pick.market), p: pick.p ?? null,
      odds: pick.odds ?? m.iddaa_odds?.[pick.market] ?? null,
      level: small ? 0 : LEVELS[m.confidence_label] || 1, level_label: small ? "" : m.confidence_label || "",
      reason: small ? "Bu seçenek, marjı temizlenmiş iddaa piyasa olasılığına dayanır; takım modeli güveni değildir." : "",
      recommendation_label: small ? "Piyasa adayı" : "",
      selectionTier: pick.selection_tier, targetMet: pick.target_met, pSource: small ? "market" : "model",
    };
  }

  function levelBadge(level, label) {
    if (!level) return `<span class="conf conf-0">${esc(label || "Sınırlı veri")}</span>`;
    const text = { 3: "Yüksek güven", 2: "Orta güven", 1: "Düşük güven" }[level];
    return `<span class="conf conf-${level}" title="Model olasılığı ve veri miktarından türetilir; gerçekleşmiş isabet kanıtı değildir." aria-label="${esc(label || text)}"><span class="conf-bars" aria-hidden="true"><i></i><i></i><i></i></span>${text}</span>`;
  }

  function oddsBox(odds) {
    return `<div class="pick-odds"><small>Gerçek iddaa oranı</small><b>${odds == null ? "–" : Number(odds).toFixed(2)}</b></div>`;
  }

  /** Büyük öneri bloğu: [MS 2] Deplasman kazanır · güven · oran. */
  function pickBlock(m) {
    const s = info(m);
    const targetNote = s.targetMet === false || s.selectionTier === "best_available"
      ? `<p class="v3-target-note">Hedef eşiği karşılanmadı; bu, mevcut verideki en uygun adaydır.</p>` : "";
    const pSource = s.pSource === "market" ? "Piyasa olasılığı" : s.p != null ? "Model tahmini" : "";
    return `<div class="v3-pick">${s.market ? `<div class="hero-label">Önerilen seçim</div>` : ""}<div class="pick-main">
      <span class="pick-chip${s.market ? "" : " none"}">${esc(s.headline)}</span>
      <div class="pick-text"><b>${esc(s.title)}</b>${s.market ? `<span class="v3-probability">${s.p == null ? "Olasılık yok" : `${Math.round(s.p * 100)}% tahmini olasılık`}</span>${pSource ? `<small class="v3-p-source">${pSource}</small>` : ""}${levelBadge(s.level, s.level_label)}` : ""}</div>
      ${s.market ? oddsBox(s.odds) : ""}
    </div>${s.reason ? `<p class="pick-why">${esc(s.reason)}</p>` : ""}${targetNote}${s.market ? '<p class="v3-caveat">Tahmini olasılık; garanti değildir.</p>' : ""}</div>`;
  }

  function strongestPickBlock(m) {
    const pick = m?.strongest_pick, primary = m?.pick || m?.alternative;
    if (!pick || pick.policy !== "highest_probability_v1" || !pick.market || pick.market === primary?.market
        || !Number.isFinite(pick.p) || !Number.isFinite(pick.odds) || pick.odds_source !== "iddaa") return "";
    return `<div class="pick-why"><b>En yüksek olasılık:</b> ${esc(headline(pick.market))} · ${(pick.p * 100).toFixed(0)}% · iddaa ${pick.odds.toFixed(2)}<br><span class="muted">Tahmini olasılık; garanti değil. Ana öneri dengeli seçimdir.</span></div>`;
  }

  return { headline, title, info, levelBadge, oddsBox, pickBlock, strongestPickBlock, confidenceLevel, sortMatches, filterMatches };
});
