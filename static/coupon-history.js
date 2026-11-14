// "Kupon Geçmişi" sekmesi: app.js'teki yardımcıları ($, esc, api, pct, fmtDay, fmtDateTime) kullanır.
const CH_STATUS = { won: ["Tuttu", "b-good"], lost: ["Yattı", "b-bad"], pending: ["Bekliyor", "b-warn"], void: ["İptal", "b-info"] };
const CH_KIND = { banko: "Banko", surprise: "Sürpriz", banko_grok: "Banko · Grok sonrası", surprise_grok: "Sürpriz · Grok sonrası" };
const CH_GROK_KINDS = ["banko_grok", "surprise_grok"];

const chBadge = (s) => `<span class="badge ${CH_STATUS[s][1]}">${CH_STATUS[s][0]}</span>`;
const chPct = (v, digits = 0) => (v == null ? "–" : (v * 100).toFixed(digits) + "%");
const chTL = (v) => (v < 0 ? "−" : v > 0 ? "+" : "") + Math.abs(v).toLocaleString("tr-TR", { maximumFractionDigits: 2 }) + " TL";
const chSign = (v) => (v > 0 ? "ch-win" : v < 0 ? "ch-loss" : "");

function chPickRow(p) {
  const score = p.home_goals == null ? "" : `<span class="muted">${p.home_goals}-${p.away_goals}</span>`;
  return `<tr><td class="l"><b>${esc(p.home)}</b> – ${esc(p.away)}</td><td>${esc(p.league_name || p.league)}</td><td>${fmtDateTime(p.kickoff)}</td><td><span class="badge b-info">${esc(p.market)}</span></td><td>${p.odds.toFixed(2)}</td><td>${score}</td><td>${chBadge(p.status)}</td></tr>`;
}

function chCoupon(kind, c) {
  if (!c) return `<div class="ch-coupon"><b>${CH_KIND[kind]}</b> <span class="muted">kayıt yok</span></div>`;
  const won = c.picks.filter((p) => p.status === "won").length;
  const profit = c.profit == null ? "" : ` · <span class="${chSign(c.profit)}">${chTL(c.profit)}</span>`;
  return `<details class="ch-coupon"><summary><b>${CH_KIND[kind]}</b> ${chBadge(c.status)} <span class="muted">${won}/${c.picks.length} seçim · oran ${c.total_odds}</span>${profit}</summary>
    ${c.settlement_warning ? `<p class="muted">${esc(c.settlement_warning)}</p>` : ""}
    <table><tr><th class="l">Maç</th><th>Lig</th><th>Saat</th><th>Seçim</th><th>Oran</th><th>Skor</th><th>Sonuç</th></tr>${c.picks.map(chPickRow).join("")}</table></details>`;
}

function chSummaryRow(label, s) {
  return `<tr><td class="l"><b>${label}</b></td><td>${s.won}/${s.won + s.lost}${s.pending ? ` <span class="muted">(+${s.pending} bekliyor)</span>` : ""}</td><td>${chPct(s.hit_rate)}</td>
    <td>${s.stake.toLocaleString("tr-TR")} TL</td><td>${s.returned.toLocaleString("tr-TR")} TL</td><td class="${chSign(s.profit)}"><b>${chTL(s.profit)}</b></td><td class="${chSign(s.profit)}"><b>${chPct(s.roi, 1)}</b></td></tr>`;
}

async function renderCouponHistory() {
  const el = $("#couponHistory");
  el.innerHTML = `<p class="muted">Yükleniyor…</p>`;
  const h = await api("/api/coupon-history?days=60");
  const { banko, surprise, total } = h.summary;
  // Grok veto'su sonrası geçilen alternatifler yalnız kayıt varsa gösterilir; "Toplam" yayımlanan kuponları sayar.
  const grokRows = CH_GROK_KINDS.filter((k) => h.summary[k]?.coupons).map((k) => chSummaryRow(CH_KIND[k], h.summary[k])).join("");
  const grokDay = (d) => CH_GROK_KINDS.filter((k) => d[k]).map((k) => chCoupon(k, d[k])).join("");
  if (!total.coupons) {
    el.innerHTML = `<div class="card muted">Henüz kayıtlı kupon yok. Günün Banko ve Sürpriz kuponu üretildiği haliyle kaydedilir; maçlar bitince sonuçlar otomatik işlenir.</div>`;
    return;
  }
  el.innerHTML = `
    <div class="ch-stats">
      <div class="card"><div class="muted">Banko tutma oranı</div><div class="ch-big">${chPct(banko.hit_rate)}</div><div class="muted">${banko.won}/${banko.won + banko.lost} kupon · seçim bazında ${chPct(banko.pick_hit_rate)} (${banko.picks_won}/${banko.picks_settled})</div></div>
      <div class="card"><div class="muted">Sürpriz tutan kupon</div><div class="ch-big">${surprise.won}</div><div class="muted">${surprise.won + surprise.lost} sonuçlanan kuponun ${surprise.won}'i · seçim bazında ${chPct(surprise.pick_hit_rate)}</div></div>
      <div class="card"><div class="muted">Her gün ${h.stake} TL yatırsaydın</div><div class="ch-big ${chSign(total.profit)}">${chTL(total.profit)}</div><div class="muted">Kâr oranı ${chPct(total.roi, 1)} · yatırılan ${total.stake.toLocaleString("tr-TR")} TL</div></div>
    </div>
    <div class="card"><h3>Her gün ${h.stake} TL yatırsaydın</h3>
      <table><tr><th class="l">Kupon</th><th>Tutan/Sonuçlanan</th><th>Tutma</th><th>Yatırılan</th><th>Dönen</th><th>Kâr/Zarar</th><th>Kâr oranı</th></tr>
      ${chSummaryRow("Banko", banko)}${chSummaryRow("Sürpriz", surprise)}${chSummaryRow("Toplam", total)}${grokRows}</table>
      <p class="muted">Her gün Banko ve Sürpriz kuponun her birine ${h.stake} TL, kaydedilen oranla; yalnız sonuçlanan kuponlar sayılır. Ertelenen/iptal maç oranı 1.00 kabul edilir.${grokRows ? " Grok satırları veto sonrası alternatifleri ayrı ölçer; Toplam'a dahil değildir." : ""}</p></div>
    <div class="card"><h3>Günlük liste</h3>${h.days.map((d) => `<div class="ch-day"><div class="row-between"><b>${fmtDay(d.date + "T12:00:00Z")}</b><span class="${chSign(d.profit)}">${chTL(d.profit)}</span></div>${chCoupon("banko", d.banko)}${chCoupon("surprise", d.surprise)}${grokDay(d)}</div>`).join("")}
      ${h.total_days > h.days.length ? `<p class="muted">Son ${h.days.length} gün gösteriliyor (toplam ${h.total_days}).</p>` : ""}</div>`;
}
