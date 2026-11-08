/* Sistem kuponu, kupon defteri ve favori takım hesapları: DOM'a/ağa dokunmayan saf fonksiyonlar (tests/js/system_calc.test.js). */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.SysCalc = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const MAX_COMBOS = 500;
  const MAX_LEDGER = 300;
  const round2 = (x) => Math.round(x * 100) / 100;
  const num = (x) => (typeof x === "number" && Number.isFinite(x) ? x : null);

  function binom(n, k) {
    if (k < 0 || k > n) return 0;
    let r = 1;
    for (let i = 1; i <= k; i++) r = (r * (n - k + i)) / i;
    return Math.round(r);
  }
  /** n seçimden k'lı kombinasyonlar (indeks dizileri). Sayı sınırı aşılırsa null. */
  function combinations(n, k, limit = MAX_COMBOS) {
    if (!Number.isInteger(n) || !Number.isInteger(k) || k < 1 || k > n || binom(n, k) > limit) return null;
    const out = [], cur = [];
    (function rec(start) {
      if (cur.length === k) { out.push(cur.slice()); return; }
      for (let i = start; i <= n - (k - cur.length); i++) { cur.push(i); rec(i + 1); cur.pop(); }
    })(0);
    return out;
  }
  /** Seçilebilir sistem tipleri: 2/3, 2/4, 3/4, 3/5, 4/5 ... (k < n ve kombinasyon sayısı sınır içinde). */
  function systemOptions(n) {
    const out = [];
    for (let k = 2; k < n; k++) if (binom(n, k) <= MAX_COMBOS) out.push({ k, n, count: binom(n, k), label: `${k}/${n}` });
    return out;
  }
  /** En az k seçimin tutma olasılığı (bağımsız varsayım; Poisson-binom DP). */
  function atLeastK(ps, k) {
    let dist = [1];
    for (const p of ps) {
      const nx = new Array(dist.length + 1).fill(0);
      dist.forEach((v, i) => { nx[i] += v * (1 - p); nx[i + 1] += v * p; });
      dist = nx;
    }
    return dist.slice(k).reduce((a, b) => a + b, 0);
  }

  /** items: [{odds, p}], k: kaç seçim tutmalı, stake: toplam yatırım (TL, null olabilir). */
  function summarizeSystem(items, k, stake) {
    const ids = items.filter(i => i.id !== undefined).map(i => String(i.id));
    if(new Set(ids).size !== ids.length || items.some(i => num(i.p) === null || i.p <= 0 || i.p > 1)) return null;
    const combos = combinations(items.length, k);
    if (!combos) return null;
    const rows = combos.map((idx) => ({
      idx, odds: round2(idx.reduce((a, i) => a * items[i].odds, 1)), p: idx.reduce((a, i) => a * items[i].p, 1),
    }));
    const st = stake !== null && stake !== undefined && Number.isFinite(stake) && stake >= 0 ? stake : null;
    const per = st === null ? null : st / rows.length;
    const out = {
      count: rows.length, rows, stake: st, perCombo: per === null ? null : round2(per),
      totalOddsMin: Math.min(...rows.map((r) => r.odds)), totalOddsMax: round2(rows.reduce((a, r) => a + r.odds, 0)),
      hitProbability: atLeastK(items.map((i) => i.p), k), // en az bir kombinasyon tutar
      allProbability: items.reduce((a, i) => a * i.p, 1),
      minPayout: null, maxPayout: null, expectedPayout: null, expectedValue: null,
    };
    if (per !== null) {
      out.minPayout = round2(per * out.totalOddsMin);   // en az bir kombinasyon tutarsa asgari getiri
      out.maxPayout = round2(per * out.totalOddsMax);   // hepsi tutarsa
      out.expectedPayout = round2(rows.reduce((a, r) => a + per * r.odds * r.p, 0));
      out.expectedValue = round2(out.expectedPayout - st);
    }
    return out;
  }

  // ---------- kupon defteri ----------
  /** pick: {id, market, home, away, odds, kickoff, res: null|"won"|"lost"}; kind: "combo" | "system". */
  function sanitizePick(p) {
    if (!p || typeof p !== "object" || typeof p.id !== "string" || typeof p.market !== "string") return null;
    if (typeof p.home !== "string" || typeof p.away !== "string" || num(p.odds) === null || p.odds < 1) return null;
    return { id: p.id, market: p.market, home: p.home, away: p.away, odds: p.odds,
      kickoff: Number.isFinite(Date.parse(p.kickoff)) ? p.kickoff : null, res: p.res === "won" || p.res === "lost" ? p.res : null };
  }
  function sanitizeEntry(e) {
    if (!e || typeof e !== "object" || num(e.stake) === null || e.stake <= 0) return null;
    const picks = (Array.isArray(e.picks) ? e.picks : []).map(sanitizePick).filter(Boolean).slice(0, 30);
    if (picks.length < 1) return null;
    const kind = e.kind === "system" ? "system" : "combo";
    let k = picks.length;
    if (kind === "system") { k = Number.isInteger(e.k) ? e.k : 0; if (!combinations(picks.length, k)) return null; }
    return { uid: typeof e.uid === "string" && e.uid ? e.uid : String(Date.parse(e.ts) || Date.now()) + Math.random().toString(36).slice(2, 6),
      ts: Number.isFinite(Date.parse(e.ts)) ? e.ts : new Date().toISOString(), stake: e.stake, kind, k, picks };
  }
  function sanitizeLedger(raw) {
    const list = Array.isArray(raw) ? raw : raw && Array.isArray(raw.entries) ? raw.entries : [];
    const seen = new Set(), out = [];
    for (const e of list) {
      const s = sanitizeEntry(e);
      if (s && !seen.has(s.uid)) { seen.add(s.uid); out.push(s); }
      if (out.length >= MAX_LEDGER) break;
    }
    return out;
  }

  /** Bir defter kaydının durumu ve getirisi. status: "pending" | "won" | "lost". payout yalnız kesinleşmiş kısmı sayar. */
  function evaluate(entry) {
    const { picks, kind } = entry;
    if (kind === "combo") {
      const lost = picks.some((p) => p.res === "lost");
      const done = picks.every((p) => p.res === "won");
      const odds = round2(picks.reduce((a, p) => a * p.odds, 1));
      return { status: lost ? "lost" : done ? "won" : "pending", payout: done ? round2(entry.stake * odds) : 0, odds };
    }
    const combos = combinations(picks.length, entry.k) || [];
    const per = entry.stake / (combos.length || 1);
    let payout = 0, pending = 0, wonCombos = 0;
    for (const idx of combos) {
      const rs = idx.map((i) => picks[i].res);
      if (rs.includes("lost")) continue;
      if (rs.every((r) => r === "won")) { payout += per * idx.reduce((a, i) => a * picks[i].odds, 1); wonCombos++; } else pending++;
    }
    payout = round2(payout);
    const status = pending > 0 ? "pending" : payout > 0 ? "won" : "lost";
    return { status, payout, wonCombos, combos: combos.length };
  }

  /** Sonuçlanmış maçlara göre pick.res alanlarını doldurur. finished: Map|id/ad'a göre bulucu -> {home_goals, away_goals}. */
  const SETTLEABLE = /^(1|X|2|1X|12|X2|KG Var|KG Yok|[ÜA]\d+(\.\d+)?)$/;
  const isSettleable = (market) => SETTLEABLE.test(market);
  function settlePicks(entry, find, judge) {
    let changed = false;
    const picks = entry.picks.map((p) => {
      if (p.res || !isSettleable(p.market)) return p; // bilinmeyen pazar elle işaretlenir
      const m = find(p);
      if (!m) return p;
      changed = true;
      return { ...p, res: judge(p.market, m.home_goals, m.away_goals) ? "won" : "lost" };
    });
    return changed ? { ...entry, picks } : entry;
  }

  function ledgerStats(entries) {
    const rows = entries.map((e) => ({ e, r: evaluate(e) }));
    const settled = rows.filter((x) => x.r.status !== "pending");
    const stake = round2(settled.reduce((a, x) => a + x.e.stake, 0));
    const payout = round2(settled.reduce((a, x) => a + x.r.payout, 0));
    const won = settled.filter((x) => x.r.status === "won").length;
    const sorted = [...settled].sort((a, b) => Date.parse(a.e.ts) - Date.parse(b.e.ts));
    let cum = 0;
    const series = sorted.map((x) => { cum += x.r.payout - x.e.stake; return round2(cum); });
    return {
      total: entries.length, pending: rows.length - settled.length, settled: settled.length, won, lost: settled.length - won,
      pendingStake: round2(rows.filter((x) => x.r.status === "pending").reduce((a, x) => a + x.e.stake, 0)),
      stake, payout, net: round2(payout - stake), roi: stake > 0 ? (payout - stake) / stake : null, series,
    };
  }

  // ---------- favori takımlar ----------
  const MAX_FAVS = 100;
  function sanitizeFavs(raw) {
    const list = Array.isArray(raw) ? raw : [];
    return [...new Set(list.filter((x) => typeof x === "string" && x.trim()).map((x) => x.trim()))].slice(0, MAX_FAVS);
  }
  const isFavMatch = (favs, m) => !!m && (favs.includes(m.home) || favs.includes(m.away));

  return { MAX_COMBOS, binom, combinations, systemOptions, atLeastK, summarizeSystem, sanitizeEntry, sanitizeLedger,
    evaluate, isSettleable, settlePicks, ledgerStats, sanitizeFavs, isFavMatch };
});
