/* Kupon oluşturucu arayüzü ("Kuponum" sekmesi + maç kartlarındaki "Kupona ekle" düğmeleri).
 * Kupon yalnız bu tarayıcının localStorage'ında durur (giriş yok). Hesaplar: slip-calc.js.
 * app.js'ten kullanılan yüzey: Slip.button(m, market) ve Slip.render(). Bu dosya app.js'ten ÖNCE yüklenir. */
(function () {
  "use strict";
  const C = window.SlipCalc;
  if (!C || typeof C.isSupported !== "function") {
    // Eski HTML ile yeni betik karışırsa sürümlü URL yeni uygulama kabuğunu getirir.
    window.Slip={button:()=>"",render:async()=>{},updateLive:()=>{}};
    const url=new URL(window.location.href);
    if(url.searchParams.get("app_version") !== "bulletin-v4") {
      url.searchParams.set("app_version","bulletin-v4");window.location.replace(url.href);
    }
    return;
  }
  const STORE_KEY = "iddiaoran.slip.v1";
  const REFRESH_AFTER_MS = 30 * 1000;

  const q = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (p, digits = 0) => p === null ? "Hesaplanmadı" : (p * 100).toFixed(digits) + "%";
  const money = (n) => n.toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " ₺";
  const fmtDateTime = (iso) => new Date(iso).toLocaleString("tr-TR", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  let data = load();
  const matchCache = new Map(); // kartlarda gösterilen maç verisi (id -> maç); düğme tıklanınca seçim buradan kurulur
  const missing = new Set();    // güncelleme sırasında listede bulunamayan maç id'leri (oynanmış olabilir)
  let lastRefresh = 0, refreshing = false, toastTimer = 0;

  function load() {
    try { return C.sanitize(JSON.parse(localStorage.getItem(STORE_KEY))); } catch { return C.sanitize(null); }
  }
  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(data)); } catch { /* özel mod / dolu depo: kupon bu oturumla sınırlı kalır */ }
  }
  function leagueName(m) {
    if (m.league_name) return m.league_name;
    const list = typeof state !== "undefined" && state.leagues ? state.leagues : [];
    return (list.find((l) => l.id === m.league) || {}).name || "";
  }

  // ---------- düğmeler (maç kartı + maç detayı) ----------
  function btnInner(on) { return on ? "✓ Kuponda" : "＋ Kupona ekle"; }
  function button(m, market) {
    const selection=m && C.isSupported(market) ? C.fromMatch(m,market) : null;
    if (!selection || selection.iddaaOdds === null) return ""; // Yeni seçim yalnız gerçek bülten fiyatıyla eklenir.
    matchCache.set(String(m.id), m);
    const on = C.hasSelection(data.items, m.id, market);
    return `<button type="button" class="slip-add${on ? " on" : ""}" data-slip-id="${esc(m.id)}" data-slip-market="${esc(market)}" aria-pressed="${on}" title="${esc(m.home)} – ${esc(m.away)}: ${esc(market)} seçimini kupona ekle / çıkar">${btnInner(on)}</button>`;
  }
  function syncButtons() {
    document.querySelectorAll(".slip-add").forEach((b) => {
      const on = C.hasSelection(data.items, b.dataset.slipId, b.dataset.slipMarket);
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", String(on));
      b.textContent = btnInner(on);
    });
    const badge = q("#slipCount");
    if (badge) { badge.textContent = data.items.length; badge.classList.toggle("hidden", !data.items.length); }
  }

  function toast(msg) {
    let el = q("#slipToast");
    if (!el) { el = document.createElement("div"); el.id = "slipToast"; el.className = "slip-toast"; el.setAttribute("role", "status"); document.body.appendChild(el); }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3000);
  }

  // ---------- durum değişiklikleri ----------
  const slipVisible = () => { const t = q("#tab-slip"); return !!t && !t.classList.contains("hidden"); };
  function changed() {
    save();
    syncButtons();
    if (slipVisible()) renderView();
  }
  function toggle(id, market) {
    if (C.hasSelection(data.items, id, market)) { data.items = C.removeSelection(data.items, id + "|" + market); return changed(); }
    const m = matchCache.get(String(id));
    const sel = m && C.fromMatch(m, market, leagueName(m));
    if (!sel) return toast("Bu seçim için oran bulunamadı.");
    const r = C.addSelection(data.items, sel);
    if (r.status === "full") return toast(`Kuponda en fazla ${C.MAX_ITEMS} seçim olabilir.`);
    data.items = r.items;
    changed();
  }

  async function fetchMatch(id) {
    try {
      const r = await fetch("/api/match/" + encodeURIComponent(id), {cache: "no-store"});
      if (r.status === 404) return { gone: true };
      return r.ok ? { m: await r.json() } : null;
    } catch { return null; } // ağ hatası: eldeki oranlar kalır
  }
  async function fetchResults(ids) {
    const query = new URLSearchParams();
    ids.forEach((id) => query.append("ids", id));
    try {
      const response = await fetch("/api/results?" + query, {cache: "no-store"});
      return response.ok ? (await response.json()).matches : null;
    } catch (_) { return null; }
  }
  function updateLive() {
    if (typeof state === "undefined" || !state.live) return;
    data.items = data.items.map((sel) => C.withResult(sel, state.live.get(sel.id)));
    save();
    if (slipVisible()) renderView();
  }
  async function refresh() {
    const ids = [...new Set(data.items.map((i) => i.id))];
    if (!ids.length || refreshing) return;
    refreshing = true;
    if (slipVisible()) renderView();
    try {
      const [results, details] = await Promise.all([
        fetchResults(ids),
        Promise.all(ids.map((id) => {
          const sel = data.items.find((s) => s.id === id);
          return sel && Date.parse(sel.kickoff) > Date.now() ? fetchMatch(id) : null;
        })),
      ]);
      const fresh = new Map(ids.map((id, i) => [id, details[i]]));
      for (const [id, r] of fresh) {
        if (r?.gone && results?.[id]?.state !== "post") missing.add(id);
        else missing.delete(id);
      }
      data.items = data.items.map((sel) => {
        const detail = fresh.get(sel.id);
        const next = detail?.m ? C.refreshSelection(sel, detail.m) : sel;
        return C.withResult(next, results?.[sel.id]);
      });
      lastRefresh = Date.now();
      updateLive();
    } finally {
      refreshing = false;
      changed();
    }
  }

  // ---------- Kuponum sekmesi ----------
  function marketBadge(market) {
    const group = C.GROUPS.find((g) => g.markets.includes(market));
    return `<span class="badge b-info" title="${esc(group ? group.label : "")}">${esc(window.Simple?.headline(market) || market)}</span>`;
  }
  function oddsCell(r) {
    return `<b class="iddaa-odd">${r.odds.toFixed(2)}</b>` + (r.source === "iddaa"
      ? `<span class="src src-iddaa">${r.quoteUnavailable ? "son alınan iddaa" : "iddaa"}</span>`
      : `<span class="src src-claude">tahmini</span>`);
  }
  function rowHtml(r, dupIds) {
    const flags = [];
    if (dupIds.has(r.id)) flags.push(`<span class="badge b-warn" title="Aynı maçtan birden fazla seçim">aynı maç</span>`);
    const view = C.resultView(r, Date.now());
    if(C.isExperimental(r.market)) flags.push(`<span class="badge b-warn">Deneysel${r.manualStatus ? " · kullanıcı sonucu" : ""}</span>`);
    const manual = view.completed && C.isExperimental(r.market) && !r.market.startsWith("Handikap ")
      ? `<label class="small">Doğruladığınız seçim sonucu <select data-slip-result="${esc(r.key)}"><option value="">Bekliyor</option><option value="won" ${r.manualStatus === "won" ? "selected" : ""}>Tuttu</option><option value="lost" ${r.manualStatus === "lost" ? "selected" : ""}>Yattı</option></select></label>` : "";
    flags.push(`<span class="badge ${view.completed ? "b-info" : "b-warn"}">${view.label}</span>`);
    if (missing.has(r.id) && !view.completed) flags.push(`<span class="badge b-warn">Sonuç doğrulanamadı</span>`);
    return `<tr class="${dupIds.has(r.id) ? "slip-dup" : ""}" data-slip-row data-live-match="${esc(r.id)}" data-home="${esc(r.home)}" data-away="${esc(r.away)}" data-market="${esc(r.market)}">
      <td class="l"><b>${esc(r.home)}</b> – ${esc(r.away)}<span class="claude-odd">${esc(r.leagueName)}</span></td>
      <td>${fmtDateTime(r.kickoff)} <span class="live-score">${view.score ? `<span class="badge b-info">${view.score}</span>` : ""}</span></td><td>${marketBadge(r.market)} <span class="live-status">${view.result ? `<span class="badge ${view.result === "Yattı" ? "b-bad" : "b-good"}">${view.result}</span>` : ""}</span> ${flags.join(" ")}${manual}</td>
      <td>${oddsCell(r)}</td><td>${pct(r.p)}</td>
      <td><button type="button" class="slip-remove" data-slip-remove="${esc(r.key)}" aria-label="${esc(r.home)} – ${esc(r.away)} ${esc(r.market)} seçimini çıkar">×</button></td></tr>`;
  }
  function warningsHtml(s) {
    const out = [];
    if(s.rows.some((r)=>r.quoteUnavailable)) out.push(`<div class="slip-warn">Bazı seçimlerin oranı artık bültende yok; son alınan oran korunuyor, güncel fiyat olarak kullanılmamalı.</div>`);
    if(s.uniqueMatches < s.minMatches) out.push(`<div class="slip-warn bad">Bu seçimlerin MBS kuralı ${s.minMatches} farklı maç gerektiriyor; kuponda ${s.uniqueMatches} farklı maç var.</div>`);
    for (const g of s.sameMatch) {
      const name = `<b>${esc(g.home)} – ${esc(g.away)}</b>`, mk = g.markets.map(esc).join(" + ");
      out.push(g.impossible
        ? `<div class="slip-warn bad">⚠ ${name}: ${mk} aynı anda gerçekleşemez; bu kupon kazanamaz (tutma ihtimali %0 sayıldı).</div>`
        : `<div class="slip-warn">⚠ ${name} maçından ${g.markets.length} seçim var (${mk}). Aynı maçın seçimleri birbirine bağlıdır; ortak olasılık modeli olmadığı için toplam tutma şansı ve beklenen değer hesaplanmaz. Çarpılan oranlar resmi birleşik teklif değildir.</div>`);
    }
    if (s.rows.some((r)=>C.isExperimental(r.market))) out.push(`<div class="slip-warn">Deneysel seçimler kalibre edilmemiştir. İlk yarı, İY/MS, korner ve kart sonuçları tam maç skorundan çıkarılmaz; maç bittikten sonra doğruladığınız sonucu seçebilirsiniz.</div>`);
    if (s.finishedCount) out.push(`<div class="slip-warn info">${s.finishedCount} seçimin maçı tamamlandı; son skorlar yukarıda gösteriliyor.</div>`);
    if (s.startedCount) out.push(`<div class="slip-warn bad">⚠ ${s.startedCount} seçimin maçının başlama saati geçti; sonuçlar kontrol ediliyor. Bu maçlara yeni seçim yapılamaz.</div>`);
    if (s.claudeCount) out.push(`<div class="slip-warn info">ℹ ${s.claudeCount} seçimde iddaa oranı yok, tahmini oran kullanıldı. Bu gerçek bahis oranı değildir; kazanç buna göre tahmindir.</div>`);
    return out.join("");
  }
  function resultHtml(s) {
    if (s.stake === null) return `<p class="muted">Olası kazancı görmek için yatırılacak tutarı girin.</p>`;
    return `<div class="slip-result">
      <div><small>${s.status === "won" ? "Hesaplanan kazanç" : s.status === "lost" ? "Kazanç" : "Olası kazanç"}</small><b class="slip-win">${money(s.status === "lost" ? 0 : s.payout)}</b></div>
      <div><small>${s.status === "pending" ? "Net kâr (tutarsa)" : "Hesaplanan net sonuç"}</small><b>${money(s.status === "lost" ? -s.stake : s.profit)}</b></div>
</div>`;
  }
  function summaryHtml(s) {
    return `<p role="status"><b>${{won: "Kazandı", lost: "Kaybetti", pending: "Bekliyor", empty: "Boş"}[s.status]}</b> · Tutan: ${s.wonCount} · Yatan: ${s.lostCount} · Bekleyen: ${s.pendingCount} seçim · Kalan: ${s.remainingMatches} maç</p><div class="slip-stats">
      <div><small>${s.correlated ? "Oran çarpımı (resmi teklif değil)" : "Toplam oran"}</small><b>${s.totalOdds.toFixed(2)}</b></div>
      <div><small>${s.status === "pending" ? "İlk tahmini tutma şansı" : "Sonuç"}</small><b>${pct(s.totalProbability, s.totalProbability < 0.1 ? 1 : 0)}</b></div>
</div>`;
  }
  function sourceBadge(s) {
    if (s.source === "iddaa") return `<span class="badge b-info">oranlar: iddaa</span>`;
    if (s.source === "claude") return `<span class="badge b-warn">oranlar: tahmini</span>`;
    return `<span class="badge b-info">oranlar: ${s.iddaaCount}/${s.count} iddaa, kalanı tahmini</span>`;
  }

  function renderView() {
    const el = q("#slip");
    if (!el) return;
    const stakeInput = q("#slipStake");
    const focus = stakeInput && document.activeElement === stakeInput ? stakeInput.selectionStart : null;
    const s = C.summarize(data.items, data.stake, Date.now());
    if (!s.count) {
      el.innerHTML = `<div class="card muted">Kuponunuz boş. Maç kartlarındaki <b>＋ Kupona ekle</b> düğmesiyle (ya da maç detayında istediğiniz pazarı seçerek: 1X2, çifte şans, Alt/Üst, KG) seçim ekleyin.</div>`;
      return;
    }
    const dupIds = new Set(s.sameMatch.map((g) => g.id));
    el.innerHTML = `<div class="card slip-card">
      <div class="row-between"><h3>Kuponum</h3><span>${sourceBadge(s)} <span class="badge b-good">${s.count} seçim</span></span></div>
      ${warningsHtml(s)}
      <table><tr><th class="l">Maç</th><th>Saat</th><th>Seçim</th><th>Oran</th><th>Şans</th><th></th></tr>${s.rows.map((r) => rowHtml(r, dupIds)).join("")}</table>
      ${summaryHtml(s)}
      <div class="slip-stake"><label>Yatırılacak tutar (₺) <input id="slipStake" type="text" inputmode="decimal" autocomplete="off" value="${data.stake === null ? "" : esc(String(data.stake).replace(".", ","))}" placeholder="örn. 100"></label></div>
      <div id="slipResult">${resultHtml(s)}</div>
      <div class="slip-actions">
        <button type="button" class="btn" id="slipShare">Paylaş</button>
        <select id="slipShareSize" aria-label="Görsel boyutu"><option value="square">Kare 1080×1080</option><option value="story">Story 1080×1920</option></select>
        <button type="button" class="btn" id="slipRefresh" ${refreshing ? "disabled" : ""}>${refreshing ? "Yenileniyor…" : "Skorları ve oranları yenile"}</button>
        <button type="button" class="btn btn-ghost" id="slipClear">Kuponu temizle</button>
      </div>
      <p class="muted slip-note">Farklı maçların bilinen olasılıkları bağımsızlık varsayımıyla birleştirilir. Aynı maçtan birden fazla seçimde veya olasılığı bilinmeyen seçimlerde toplam tutma şansı gösterilmez. Kupon bu tarayıcıda saklanır; oranlar zamanla değişebilir. Tahmindir, garanti değildir.</p>
    </div>`;
    window.Extras?.slipExtras(el, s, data.stake);
    if (focus !== null) { const inp = q("#slipStake"); inp.focus(); inp.setSelectionRange(focus, focus); }
  }

  /** app.js'in render() döngüsünden çağrılır (state.tab === "slip"). */
  async function render() {
    q("#leagueChips")?.classList.add("hidden");
    syncButtons();
    renderView();
    if (data.items.length && Date.now() - lastRefresh > REFRESH_AFTER_MS) await refresh();
  }

  // ---------- olaylar ----------
  // Yakalama aşamasında: kart içindeki düğme tıklaması maç detayını (app.js'teki [data-id] dinleyicisi) açmasın.
  document.addEventListener("click", (e) => {
    const t = e.target.closest(".slip-add, .slip-remove, #slipClear, #slipRefresh, #slipShare");
    if (!t) return;
    e.stopPropagation();
    if (t.classList.contains("slip-add")) toggle(t.dataset.slipId, t.dataset.slipMarket);
    else if (t.classList.contains("slip-remove")) { data.items = C.removeSelection(data.items, t.dataset.slipRemove); changed(); }
    else if (t.id === "slipClear") { data.items = []; missing.clear(); changed(); }
    else if (t.id === "slipRefresh") refresh();
    else if (t.id === "slipShare") { const s = C.summarize(data.items, data.stake, Date.now()); window.CouponShare?.share({ title: "Kuponum", date: new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Istanbul" }), picks: s.rows.map((r) => ({ home: r.home, away: r.away, market: r.market, odds: r.odds, kickoff: r.kickoff })), totalOdds: s.totalOdds }, q("#slipShareSize").value); }
  }, true);

  document.addEventListener("change", (event)=>{
    const key=event.target.dataset.slipResult;
    if(!key) return;
    data.items=data.items.map((item)=>C.keyOf(item) === key ? {...item,manualStatus:["won","lost"].includes(event.target.value) ? event.target.value : null} : item);
    changed();
  });

  document.addEventListener("input", (e) => {
    if (e.target.id !== "slipStake") return;
    data.stake = C.parseStake(e.target.value);
    save();
    const sm = C.summarize(data.items, data.stake, Date.now());
    q("#slipResult").innerHTML = resultHtml(sm);
    const view = q("#slip"); if (view) window.Extras?.slipExtras(view, sm, data.stake);
  });

  // Başka sekmede/pencerede kupon değişirse burada da güncelle
  window.addEventListener("storage", (e) => {
    if (e.key !== STORE_KEY) return;
    data = load();
    syncButtons();
    if (slipVisible()) renderView();
  });

  document.addEventListener("DOMContentLoaded", syncButtons);
  setInterval(() => {
    if (!document.hidden && slipVisible() && Date.now() - lastRefresh >= REFRESH_AFTER_MS) refresh();
  }, REFRESH_AFTER_MS);
  window.Slip = { button, render, updateLive };
})();
