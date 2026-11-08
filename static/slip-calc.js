/* Kupon oluşturucu hesapları: DOM'a ve ağa dokunmayan saf fonksiyonlar (tests/js/slip_calc.test.js).
 * Oran önceliği backend kuponlarıyla aynıdır: iddaa oranı varsa o, yoksa Claude oranı. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.SlipCalc = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const MIN_ODDS = 1.01;
  const MAX_ITEMS = 30;
  const DEFAULT_STAKE = 100;

  const GROUPS = [
    { label: "1X2", markets: ["1", "X", "2"] },
    { label: "Çifte şans", markets: ["1X", "12", "X2"] },
    { label: "Alt/Üst", markets: ["Ü1.5", "A1.5", "Ü2.5", "A2.5", "Ü3.5", "A3.5"] },
    { label: "KG", markets: ["KG Var", "KG Yok"] },
  ];
  const MARKETS = GROUPS.flatMap((g) => g.markets);

  // Her pazarın (ev, deplasman) gol sayılarına göre tutup tutmadığı; çelişki kontrolü için.
  const RULES = {
    "1": (h, a) => h > a, "X": (h, a) => h === a, "2": (h, a) => h < a,
    "1X": (h, a) => h >= a, "12": (h, a) => h !== a, "X2": (h, a) => h <= a,
    "KG Var": (h, a) => h > 0 && a > 0, "KG Yok": (h, a) => h === 0 || a === 0,
  };
  for (const line of [1.5, 2.5, 3.5]) {
    RULES["Ü" + line] = (h, a) => h + a > line;
    RULES["A" + line] = (h, a) => h + a < line;
  }
  const isExperimental = (market) => /^(İY [1X2]|İY [AÜ](?:0|1|2|3)\.5|İY\/MS [1X2]\/[1X2]|Handikap -?\d+(?:\.\d+)? [1X2]|(?:Korner|Kart) [AÜ]\d+\.5)$/.test(market || "");
  const isSupported = (market) => MARKETS.includes(market) || isExperimental(market);
  function selectionOutcome(sel) {
    const result=normalizeResult(sel.result);
    if(result?.state !== "post") return null;
    if(RULES[sel.market]) return RULES[sel.market](result.home_goals,result.away_goals);
    const handicap=/^Handikap (-?\d+(?:\.\d+)?) ([1X2])$/.exec(sel.market);
    if(handicap) {
      const h=result.home_goals+Number(handicap[1]), a=result.away_goals;
      return handicap[2] === (h>a ? "1" : h<a ? "2" : "X");
    }
    // Tam maç skoru ilk yarı/İY-MS seçimini sonuçlandırmak için yeterli değildir.
    return sel.manualStatus === "won" ? true : sel.manualStatus === "lost" ? false : null;
  }
  const MAX_GOALS = 5; // en yüksek çizgi 3.5; 0..5 gol her kombinasyonu denemeye yeter

  const round2 = (x) => Math.round(x * 100) / 100;
  const num = (x) => (typeof x === "number" && Number.isFinite(x) ? x : null);
  const keyOf = (sel) => sel.id + "|" + sel.market;

  /** iddaa oranı varsa o, yoksa Claude oranı; ikisi de yoksa null. */
  function effectiveOdds(sel) {
    const iddaa = num(sel.iddaaOdds), claude = num(sel.claudeOdds);
    if (iddaa !== null && iddaa >= MIN_ODDS) return { odds: round2(iddaa), source: "iddaa" };
    if (claude !== null && claude >= MIN_ODDS) return { odds: round2(claude), source: "claude" };
    return null;
  }

  function isValid(sel) {
    return !!sel && typeof sel === "object" && typeof sel.id === "string" && sel.id !== ""
      && isSupported(sel.market) && typeof sel.home === "string" && typeof sel.away === "string"
      && Number.isFinite(Date.parse(sel.kickoff))
      && ((num(sel.p) !== null && sel.p > 0 && sel.p <= 1) || (sel.p === null && num(sel.iddaaOdds) >= MIN_ODDS))
      && effectiveOdds(sel) !== null && (!isExperimental(sel.market) || num(sel.iddaaOdds) >= MIN_ODDS);
  }

  /** /api/match ya da /api/upcoming yanıtındaki bir maç + pazardan kupon seçimi; olmuyorsa null. */
  function fromMatch(m, market, leagueName) {
    if(m?.odds_current === false) return null;
    const mk = m && (m.markets?.[market] || m.extra_markets?.[market]);
    if (!mk || !isSupported(market)) return null;
    const sel = {
      id: String(m.id), market, leagueName: m.league_name || leagueName || "",
      home: m.home, away: m.away, kickoff: m.kickoff, p: num(mk.p),
      iddaaOdds: num((m.iddaa_odds || {})[market]), claudeOdds: num(mk.suggested),
      minMatches: Math.max(1,Math.min(30,Number(m.iddaa_min_matches?.[market]) || 1)),
    };
    return isValid(sel) ? sel : null;
  }

  /** Saklı seçimi taze maç verisiyle günceller (olasılık + oranlar); veri uymuyorsa eskisini korur. */
  function refreshSelection(sel, m) {
    const fresh = fromMatch(m, sel.market, sel.leagueName);
    if (m.iddaa_odds && sel.iddaaOdds >= MIN_ODDS && !(num(m.iddaa_odds[sel.market]) >= MIN_ODDS)) {
      return {...sel, p:fresh?.p ?? sel.p, quoteUnavailable:true};
    }
    return fresh ? { ...sel, ...fresh, result: sel.result, manualStatus: sel.manualStatus, quoteUnavailable:false } : sel;
  }

  function normalizeResult(value) {
    if (!value || !["pre", "in", "post", "unknown"].includes(value.state)) return null;
    const goal = (n) => Number.isInteger(n) && n >= 0 && n <= 999;
    const scored = value.state === "in" || value.state === "post";
    if (scored && (!goal(value.home_goals) || !goal(value.away_goals))) return null;
    return { state: value.state, home_goals: scored ? value.home_goals : null,
      away_goals: scored ? value.away_goals : null, minute: String(value.minute || "").slice(0, 40),
      updated_at: Number.isFinite(Date.parse(value.updated_at)) ? value.updated_at : null };
  }
  function withResult(sel, value) {
    if (value?.id != null && String(value.id) !== sel.id) return sel;
    const next = normalizeResult(value), old = normalizeResult(sel.result);
    if (!next || (old?.state === "post" && next.state !== "post") || (old?.state === "in" && ["unknown", "pre"].includes(next.state))) return sel;
    if (old?.updated_at && next.updated_at && Date.parse(next.updated_at) < Date.parse(old.updated_at)) return sel;
    return { ...sel, result: next };
  }
  function resultView(sel, now) {
    const r = normalizeResult(sel.result);
    if (r?.state === "post") {
      const outcome=selectionOutcome(sel);
      return {label:"Bitti",score:`${r.home_goals}–${r.away_goals}`,result:outcome === null ? "Sonuç doğrulanmalı" : outcome ? "Tuttu" : "Yattı", completed:true};
    }
    if (r?.state === "in") {
      const stale = !r.updated_at || now - Date.parse(r.updated_at) > 120000;
      return { label: stale ? "Son alınan skor" : "Canlı", score: `${r.home_goals}–${r.away_goals}`,
        result: stale ? "" : RULES[sel.market]?.(r.home_goals, r.away_goals) ? "Tutuyor" : "Bekliyor", completed: false };
    }
    return { label: Date.parse(sel.kickoff) <= now ? "Sonuç bekleniyor" : "Başlamadı", score: "", result: "", completed: false };
  }

  /** localStorage'dan okunan ham veriyi güvenli forma çevirir. */
  function sanitize(raw) {
    const out = { items: [], stake: DEFAULT_STAKE };
    if (!raw || typeof raw !== "object") return out;
    const seen = new Set();
    for (const sel of Array.isArray(raw.items) ? raw.items : []) {
      if (!isValid(sel) || seen.has(keyOf(sel)) || out.items.length >= MAX_ITEMS) continue;
      seen.add(keyOf(sel));
      out.items.push({
        id: sel.id, market: sel.market, leagueName: typeof sel.leagueName === "string" ? sel.leagueName : "",
        home: sel.home, away: sel.away, kickoff: sel.kickoff, p: sel.p,
        iddaaOdds: num(sel.iddaaOdds), claudeOdds: num(sel.claudeOdds),
        minMatches: Math.max(1,Math.min(30,Number(sel.minMatches) || 1)),
        result: normalizeResult(sel.result),
        manualStatus: ["won","lost"].includes(sel.manualStatus) ? sel.manualStatus : null,
        quoteUnavailable: sel.quoteUnavailable === true,
      });
    }
    if ("stake" in raw) out.stake = raw.stake === null ? null : (num(raw.stake) !== null && raw.stake >= 0 ? raw.stake : DEFAULT_STAKE);
    return out;
  }

  /** "12,5" / "12.5" -> 12.5; boş ya da geçersiz/negatifse null. */
  function parseStake(text) {
    const s = String(text ?? "").trim().replace(",", ".");
    if (s === "") return null;
    const n = Number(s);
    return Number.isFinite(n) && n >= 0 ? n : null;
  }

  function addSelection(items, sel) {
    if (!isValid(sel)) return { items, status: "invalid" };
    if (items.some((i) => keyOf(i) === keyOf(sel))) return { items, status: "exists" };
    if (items.length >= MAX_ITEMS) return { items, status: "full" };
    return { items: [...items, sel], status: "added" };
  }
  const removeSelection = (items, key) => items.filter((i) => keyOf(i) !== key);
  const hasSelection = (items, id, market) => items.some((i) => i.id === String(id) && i.market === market);

  /** Aynı maçtaki pazarlar hiçbir skorda birlikte tutamıyorsa (örn. 1 ve X, Ü2.5 ve A1.5) true. */
  function conflicts(markets) {
    if(markets.some((market)=>!RULES[market])) return false; // Farklı periyotları tam skorla çelişkili sayma.
    for (let h = 0; h <= MAX_GOALS; h++)
      for (let a = 0; a <= MAX_GOALS; a++)
        if (markets.every((mk) => RULES[mk](h, a))) return false;
    return true;
  }

  /** Kuponun tüm sayıları. `now` = ms (başlamış maç tespiti), `stake` = TL (null olabilir). */
  function summarize(items, stake, now) {
    const rows = items.map((sel) => {
      const o = effectiveOdds(sel);
      const completed = sel.result?.state === "post" && normalizeResult(sel.result) !== null;
      return { ...sel, key: keyOf(sel), odds: o ? o.odds : null, source: o ? o.source : null,
               completed, started: !completed && Date.parse(sel.kickoff) <= now };
    });

    const byMatch = new Map();
    for (const r of rows) byMatch.set(r.id, [...(byMatch.get(r.id) || []), r]);
    const sameMatch = [...byMatch.values()].filter((g) => g.length > 1).map((g) => ({
      id: g[0].id, home: g[0].home, away: g[0].away, markets: g.map((r) => r.market),
      impossible: conflicts(g.map((r) => r.market)),
    }));

    const count = rows.length;
    const impossible = sameMatch.some((g) => g.impossible);
    const naiveProbability = !count ? 0 : rows.every((r)=>num(r.p) !== null) ? rows.reduce((acc,r)=>acc*r.p,1) : null;
    const totalOdds = count ? round2(rows.reduce((acc, r) => acc * (r.odds ?? 1), 1)) : 0;
    const wonCount = rows.filter((r) => selectionOutcome(r) === true).length;
    const lostCount = rows.filter((r) => selectionOutcome(r) === false).length;
    const pendingCount = count - wonCount - lostCount;
    const remainingMatches = new Set(rows.filter((r) => !r.completed).map((r) => r.id)).size;
    const status = !count ? "empty" : lostCount || impossible ? "lost" : pendingCount ? "pending" : "won";
    // İlk tahmini, canlı skorla yeniden hesaplanmış olasılık gibi göstermeyelim.
    const correlated = sameMatch.length > 0;
    const totalProbability = status === "lost" ? 0 : status === "won" ? 1 : correlated ? null : naiveProbability;
    const ev = count && totalProbability !== null ? totalProbability * totalOdds - 1 : null;
    const iddaaCount = rows.filter((r) => r.source === "iddaa").length;

    const money = stake !== null && stake !== undefined && Number.isFinite(stake) && stake >= 0 && count > 0;
    return {
      rows, count, sameMatch, impossible, iddaaCount, minMatches: Math.max(1,...rows.map((r)=>r.minMatches || 1)), uniqueMatches: byMatch.size, wonCount, lostCount, pendingCount, remainingMatches, status,
      claudeCount: rows.filter((r) => r.source === "claude").length,
      startedCount: rows.filter((r) => r.started).length,
      finishedCount: rows.filter((r) => r.completed).length,
      source: !count ? null : iddaaCount === count ? "iddaa" : iddaaCount === 0 ? "claude" : "karışık",
      totalOdds, totalProbability, naiveProbability, correlated, ev,
      stake: money ? stake : null,
      payout: money ? round2(stake * totalOdds) : null,
      profit: money ? round2(stake * totalOdds - stake) : null,
      expectedValue: money && ev !== null ? round2(stake * ev) : null, // yatırılan tutarın beklenen net getirisi (TL)
    };
  }

  return {
    MIN_ODDS, MAX_ITEMS, DEFAULT_STAKE, GROUPS, MARKETS, isSupported, isExperimental, selectionOutcome,
    keyOf, effectiveOdds, isValid, fromMatch, refreshSelection, sanitize, parseStake,
    addSelection, removeSelection, hasSelection, conflicts, summarize, normalizeResult, withResult, resultView,
  };
});
