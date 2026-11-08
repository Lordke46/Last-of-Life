/* Sistem kuponu (Kuponum sekmesinde), favori takımlar ve kupon defteri. Hesaplar: system-calc.js.
 * Her şey bu tarayıcının localStorage'ında durur (giriş yok); depo hataları sessizce yutulur. */
window.Extras = (() => {
  "use strict";
  const S = window.SysCalc;
  const FAV_KEY = "iddiaoran.favs.v1", LEDGER_KEY = "iddiaoran.ledger.v1";
  const money = (n) => n.toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " ₺";
  const signed = (n) => (n > 0 ? "+" : "") + money(n);
  const read = (key) => { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } };
  const write = (key, v) => { try { localStorage.setItem(key, JSON.stringify(v)); } catch { /* özel mod / dolu depo */ } };

  let favs = S.sanitizeFavs(read(FAV_KEY));
  let ledger = S.sanitizeLedger(read(LEDGER_KEY));
  let sysK = 0, sysOpen = false, onlyFavs = false, lastUpcoming = [], notice = "";

  // ---------- favoriler ----------
  const isFav = (t) => favs.includes(t);
  function star(team) {
    return `<button type="button" class="fav-star${isFav(team) ? " on" : ""}" data-fav-team="${esc(team)}" aria-pressed="${isFav(team)}" aria-label="${esc(team)} favori" title="Favori takım">${isFav(team) ? "★" : "☆"}</button>`;
  }
  function toggleFav(team) {
    favs = isFav(team) ? favs.filter((t) => t !== team) : S.sanitizeFavs([...favs, team]);
    write(FAV_KEY, favs);
    document.querySelectorAll(".fav-star").forEach((b) => {
      const on = isFav(b.dataset.favTeam);
      b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on)); b.textContent = on ? "★" : "☆";
    });
    applyFilter(); renderFavToday();
  }
  function applyFilter() {
    document.querySelectorAll("#matches .match[data-home]").forEach((c) => {
      c.classList.toggle("hidden", onlyFavs && !S.isFavMatch(favs, { home: c.dataset.home, away: c.dataset.away }));
    });
    const bar = document.querySelector("#favFilter");
    if (bar) { bar.classList.toggle("active", onlyFavs); bar.setAttribute("aria-pressed", String(onlyFavs)); }
  }
  function filterButton() {
    return `<div class="fav-bar"><button type="button" id="favFilter" class="chip${onlyFavs ? " active" : ""}" aria-pressed="${onlyFavs}">★ Favorilerim <span class="muted">${favs.length}</span></button></div>`;
  }
  /** renderMatches() sonrası: yüklenen maç listesini önbelleğe alır, Bugün kartını doldurur. */
  function afterMatches(list) { lastUpcoming = list || []; applyFilter(); renderFavToday(); }
  function renderFavToday() {
    const el = document.querySelector("#favToday");
    if (!el) return;
    const mine = lastUpcoming.filter((m) => S.isFavMatch(favs, m)).slice(0, 4);
    el.innerHTML = `<h3>★ Favorilerim</h3>` + (!favs.length ? Ui.empty("Favori takım yok", "Maç kartındaki ☆ ile takım ekleyin.")
      : mine.length ? `<ul class="mini">${mine.map((m) => `<li class="clickable" data-id="${esc(m.id)}"><span>${esc(m.home)} – ${esc(m.away)}</span><span class="muted">${esc(fmtDateTime(m.kickoff))}</span></li>`).join("")}</ul>`
      : Ui.empty("Yakında favori maçı yok"));
  }

  // ---------- defter ----------
  const saveLedger = () => write(LEDGER_KEY, ledger);
  function addEntry(kind, rows, stake, k) {
    const e = S.sanitizeEntry({ stake, kind, k, ts: new Date().toISOString(),
      picks: rows.map((r) => ({ id: r.id, market: r.market, home: r.home, away: r.away, odds: r.odds, kickoff: r.kickoff, res: null })) });
    if (!e) { notice = "Kayıt için tutar ve geçerli seçim gerekli."; return false; }
    ledger = [e, ...ledger].slice(0, 300); saveLedger(); notice = "Kupon deftere kaydedildi."; return true;
  }
  /** Canlı/biten maç verisiyle seçimleri otomatik sonuçlandırır. */
  function settle() {
    if (typeof state === "undefined" || !state.live || !state.live.size || !ledger.some((e) => e.picks.some((p) => !p.res))) return false;
    const done = [...state.live.values()].filter((m) => m.state === "post");
    const find = (p) => done.find((m) => String(m.id) === p.id) || done.find((m) => m.home === p.home && m.away === p.away);
    let any = false;
    ledger = ledger.map((e) => { const n = S.settlePicks(e, find, selectionWins); if (n !== e) any = true; return n; });
    if (any) saveLedger();
    return any;
  }
  function onLive() {
    if (settle() && state.tab === "ledger") renderLedger();
  }
  function lineChart(series) {
    if (series.length < 2) return `<p class="muted small">Grafik için en az 2 sonuçlanmış kupon gerekir.</p>`;
    const W = 320, H = 120, pad = 8, pts = [0, ...series];
    const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1;
    const x = (i) => pad + (i * (W - 2 * pad)) / (pts.length - 1), y = (v) => H - pad - ((v - lo) * (H - 2 * pad)) / span;
    const d = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
    const last = pts[pts.length - 1];
    return `<svg class="ledger-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Kümülatif net kâr grafiği"><line x1="${pad}" x2="${W - pad}" y1="${y(0)}" y2="${y(0)}" class="zero"/><path d="${d}" class="${last >= 0 ? "up" : "down"}" fill="none"/></svg>`;
  }
  const STATUS = { won: ["tuttu", "b-good"], lost: ["yattı", "b-bad"], pending: ["bekliyor", "b-warn"] };
  function entryHtml(e) {
    const r = S.evaluate(e), [label, cls] = STATUS[r.status];
    const sys = e.kind === "system" ? `Sistem ${e.k}/${e.picks.length}` : "Kombine";
    const picks = e.picks.map((p, i) => `<li><span>${esc(p.home)} – ${esc(p.away)} <span class="badge b-info">${esc(p.market)}</span> <b class="iddaa-odd">${p.odds.toFixed(2)}</b></span>
      <span>${p.res ? `<span class="badge ${p.res === "won" ? "b-good" : "b-bad"}">${p.res === "won" ? "tuttu" : "yattı"}</span>` : `<button type="button" class="btn-mini" data-led-set="${esc(e.uid)}:${i}:won">Tuttu</button><button type="button" class="btn-mini" data-led-set="${esc(e.uid)}:${i}:lost">Yattı</button>`}</span></li>`).join("");
    return `<div class="card ledger-entry"><div class="row-between"><b>${sys} · ${money(e.stake)}</b><span><span class="badge ${cls}">${label}</span> <button type="button" class="slip-remove" data-led-del="${esc(e.uid)}" aria-label="Kaydı sil">×</button></span></div>
      <div class="muted small">${esc(fmtDateTime(e.ts))}${r.status === "pending" ? "" : ` · getiri ${money(r.payout)}`}</div><ul class="mini">${picks}</ul></div>`;
  }
  function renderLedger() {
    const el = document.querySelector("#ledger");
    if (!el) return;
    settle();
    const s = S.ledgerStats(ledger);
    el.innerHTML = `<div class="card"><div class="row-between"><h3>Kupon defteri</h3><span class="badge b-info">${s.total} kayıt · ${s.pending} bekliyor</span></div>
      <div class="slip-stats"><div><small>Toplam yatırım</small><b>${money(s.stake)}</b></div><div><small>Kazanç</small><b>${money(s.payout)}</b></div>
      <div><small>Net</small><b style="color:var(--${s.net >= 0 ? "good" : "bad"})">${signed(s.net)}</b></div>
      <div><small>Kâr oranı</small><b>${s.roi === null ? "–" : (s.roi > 0 ? "+" : "") + (s.roi * 100).toFixed(1) + "%"}</b></div>
      <div><small>Tuttu / Yattı</small><b>${s.won} / ${s.lost}</b></div></div>${lineChart(s.series)}
      <div class="slip-actions"><button type="button" class="btn" id="ledExport">Dışa aktar (JSON)</button><button type="button" class="btn" id="ledImport">İçe aktar</button>
      <input type="file" id="ledFile" accept="application/json,.json" class="hidden"></div>
      <p class="muted slip-note">Sonuçlar canlı takip verisinden otomatik işlenir; bulunamayanları elle işaretleyebilirsiniz. Yalnız sonuçlanan kuponlar toplama girer. ${esc(notice)}</p></div>
      ${ledger.length ? ledger.map(entryHtml).join("") : `<div class="card muted">Defter boş. Kuponum sekmesinden bir kuponu tutarla kaydedin.</div>`}`;
  }
  function exportLedger() {
    const blob = new Blob([JSON.stringify({ version: 1, entries: ledger }, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "kupon-defteri.json"; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  async function importLedger(file) {
    try {
      const incoming = S.sanitizeLedger(JSON.parse(await file.text()));
      const have = new Set(ledger.map((e) => e.uid));
      const fresh = incoming.filter((e) => !have.has(e.uid));
      ledger = [...ledger, ...fresh].slice(0, 300); saveLedger();
      notice = `${fresh.length} kayıt içe aktarıldı.`;
    } catch { notice = "Dosya okunamadı (geçerli bir defter JSON'u değil)."; }
    renderLedger();
  }

  // ---------- sistem kuponu (Kuponum sekmesi) ----------
  function systemHtml(rows, stake) {
    const opts = S.systemOptions(rows.length);
    if (rows.length < 3) return `<div class="card slip-card"><h3>Sistem kuponu</h3><p class="muted">Sistem için en az 3 seçim ekleyin (örn. 2/3).</p></div>`;
    if (!opts.length) return `<div class="card slip-card"><h3>Sistem kuponu</h3><p class="muted">Bu seçim sayısında kombinasyon sınırı (${S.MAX_COMBOS}) aşılıyor; seçimleri azaltın.</p></div>`;
    if (!opts.some((o) => o.k === sysK)) sysK = opts[0].k;
    const items = rows.map((r) => ({ id: r.id, odds: r.odds, p: r.p }));
    const s = S.summarizeSystem(items, sysK, stake);
    const combos = s.rows.slice(0, 50).map((c) => `<tr><td class="l">${c.idx.map((i) => i + 1).join(" + ")}</td><td><b class="iddaa-odd">${c.odds.toFixed(2)}</b></td><td>${(c.p * 100).toFixed(c.p < 0.1 ? 1 : 0)}%</td></tr>`).join("");
    return `<div class="card slip-card"><div class="row-between"><h3>Sistem kuponu</h3><label>Tip <select id="sysK">${opts.map((o) => `<option value="${o.k}"${o.k === sysK ? " selected" : ""}>${o.label} (${o.count} komb.)</option>`).join("")}</select></label></div>
      <div class="slip-stats"><div><small>Kombinasyon</small><b>${s.count}</b></div><div><small>Toplam yatırım</small><b>${s.stake === null ? "–" : money(s.stake)}</b></div>
      <div><small>Kombinasyon başı</small><b>${s.perCombo === null ? "–" : money(s.perCombo)}</b></div>
      <div><small>Min. kazanç (≥1 tutarsa)</small><b>${s.minPayout === null ? "–" : money(s.minPayout)}</b></div>
      <div><small>Max. kazanç (hepsi)</small><b>${s.maxPayout === null ? "–" : money(s.maxPayout)}</b></div>
      <div><small>En az ${sysK} seçim tutma şansı</small><b>${(s.hitProbability * 100).toFixed(s.hitProbability < 0.1 ? 1 : 0)}%</b></div>
</div>
      <details><summary class="muted small">Kombinasyon oranları${s.count > 50 ? " (ilk 50)" : ""}</summary><table><tr><th class="l">Seçimler (sıra no)</th><th>Oran</th><th>Şans</th></tr>${combos}</table></details>
      <div class="slip-actions"><button type="button" class="btn" data-led-add="system">Sistemi deftere kaydet</button><button type="button" class="btn" data-led-add="combo">Kombine olarak kaydet</button></div>
      <p class="muted slip-note">Hesap seçimlerin bağımsız olduğunu varsayar. ${stake === null ? "Tutar girerseniz kazançlar görünür. " : ""}${esc(notice)}</p></div>`;
  }
  /** Slip.renderView() sonunda çağrılır: kupon özetinin altına sistem bölümünü ekler. */
  function slipExtras(el, summary, stake) {
    let box = el.querySelector("#slipSystem");
    if (!box) { box = document.createElement("div"); box.id = "slipSystem"; el.appendChild(box); }
    const rows = summary.rows.filter((r) => r.odds);
    box._ctx = null;
    if(summary.correlated) {box.innerHTML='<p class="muted">Aynı maçtan bağlı seçimler var; sistem kuponu için her maçtan tek seçim kullanın.</p>';return;}
    if(rows.some((r)=>r.p === null)) {box.innerHTML='<p class="muted">Olasılığı bilinmeyen seçim var; sistem tutma olasılığı hesaplanmadı.</p>';return;}
    box.innerHTML = `<details class="detail"${sysOpen ? " open" : ""}><summary>Sistem kuponu (isteğe bağlı)</summary><div class="detail-body">${systemHtml(rows, stake)}</div></details>`;
    box.querySelector("details").addEventListener("toggle", (e) => { sysOpen = e.target.open; });
    box._ctx = { rows, stake };
  }

  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-fav-team], #favFilter, [data-led-add], [data-led-set], [data-led-del], #ledExport, #ledImport");
    if (!t) return;
    e.stopPropagation(); e.preventDefault();
    if (t.dataset.favTeam !== undefined) toggleFav(t.dataset.favTeam);
    else if (t.id === "favFilter") { onlyFavs = !onlyFavs; applyFilter(); }
    else if (t.id === "ledExport") exportLedger();
    else if (t.id === "ledImport") document.querySelector("#ledFile").click();
    else if (t.dataset.ledDel) { ledger = ledger.filter((x) => x.uid !== t.dataset.ledDel); saveLedger(); renderLedger(); }
    else if (t.dataset.ledSet) {
      const [uid, i, res] = t.dataset.ledSet.split(":");
      ledger = ledger.map((x) => x.uid === uid ? { ...x, picks: x.picks.map((p, j) => j === Number(i) ? { ...p, res } : p) } : x);
      saveLedger(); renderLedger();
    } else if (t.dataset.ledAdd) {
      const ctx = document.querySelector("#slipSystem")?._ctx;
      if (ctx && addEntry(t.dataset.ledAdd, ctx.rows, ctx.stake, sysK)) { window.Slip?.render(); }
      else if (ctx) { notice = "Kayıt için önce yatırılacak tutarı girin."; window.Slip?.render(); }
    }
  }, true);
  document.addEventListener("change", (e) => {
    if (e.target.id === "sysK") { sysK = Number(e.target.value); window.Slip?.render(); }
    else if (e.target.id === "ledFile" && e.target.files[0]) importLedger(e.target.files[0]);
  });

  return { star, filterButton, afterMatches, renderFavToday, renderLedger, slipExtras, onLive, isFav };
})();
