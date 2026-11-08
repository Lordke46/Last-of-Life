const test = require("node:test");
const assert = require("node:assert/strict");
const S = require("../../static/system-calc.js");

const items = [{ odds: 2, p: 0.5 }, { odds: 3, p: 0.4 }, { odds: 1.5, p: 0.7 }, { odds: 2.5, p: 0.45 }];

test("kombinasyon sayısı ve sınır", () => {
  assert.equal(S.combinations(4, 2).length, 6);
  assert.equal(S.combinations(30, 15), null);
  assert.deepEqual(S.systemOptions(4).map((o) => o.label), ["2/4", "3/4"]);
  assert.ok(S.systemOptions(30).every((o) => o.count <= 500));
});

test("sistem özeti: oranlar, min/max, tutma ihtimali", () => {
  const s = S.summarizeSystem(items, 3, 80);
  assert.equal(s.count, 4);
  assert.equal(s.perCombo, 20);
  assert.equal(s.totalOddsMin, 7.5);
  const sum = 2 * 3 * 1.5 + 2 * 3 * 2.5 + 2 * 1.5 * 2.5 + 3 * 1.5 * 2.5;
  assert.equal(s.maxPayout, Math.round(20 * sum * 100) / 100 );
  assert.ok(Math.abs(s.hitProbability - S.atLeastK(items.map((i) => i.p), 3)) < 1e-12);
  assert.ok(s.hitProbability > s.allProbability);
  assert.equal(S.summarizeSystem(items, 3, null).maxPayout, null);
});

test("defter: kombine ve sistem kayıtları değerlendirilir", () => {
  const pk = (id, odds, res) => ({ id, market: "1", home: "A" + id, away: "B", odds, kickoff: "2099-01-01T10:00:00Z", res });
  const combo = S.sanitizeEntry({ stake: 100, kind: "combo", ts: "2026-10-01T10:00:00Z", picks: [pk("1", 2, "won"), pk("2", 1.5, "won")] });
  assert.deepEqual(S.evaluate(combo), { status: "won", payout: 300, odds: 3 });
  const lost = { ...combo, picks: [pk("1", 2, "won"), pk("2", 1.5, "lost")] };
  assert.equal(S.evaluate(lost).status, "lost");
  assert.equal(S.evaluate({ ...combo, picks: [pk("1", 2, "won"), pk("2", 1.5, null)] }).status, "pending");
  const sys = S.sanitizeEntry({ stake: 30, kind: "system", k: 2, ts: "2026-10-01T10:00:00Z", picks: [pk("1", 2, "won"), pk("2", 3, "won"), pk("3", 2, "lost")] });
  const r = S.evaluate(sys); // 3 kombinasyon x 10 TL; yalnız 1+2 tutar: 10*6
  assert.equal(r.status, "won"); assert.equal(r.payout, 60);
  const st = S.ledgerStats([combo, lost, sys]);
  assert.equal(st.stake, 230); assert.equal(st.payout, 360); assert.equal(st.net, 130);
  assert.equal(st.series.length, 3);
});

test("bilinmeyen pazar otomatik sonuçlanmaz", () => {
  const e = { stake: 10, kind: "combo", picks: [
    { id: "1", market: "İY 1", home: "A", away: "B", odds: 2, res: null },
    { id: "2", market: "Ü2.5", home: "C", away: "D", odds: 2, res: null }] };
  const out = S.settlePicks(e, () => ({ home_goals: 3, away_goals: 0 }), () => true);
  assert.equal(out.picks[0].res, null);
  assert.equal(out.picks[1].res, "won");
  assert.equal(S.evaluate(out).status, "pending");
});

test("sanitize: bozuk veri elenir; favoriler", () => {
  assert.deepEqual(S.sanitizeLedger("x"), []);
  assert.deepEqual(S.sanitizeLedger([{ stake: -1, picks: [] }, null]), []);
  assert.deepEqual(S.sanitizeFavs(["A", " A ", "", 5, "B"]), ["A", "B"]);
  assert.ok(S.isFavMatch(["A"], { home: "A", away: "Z" }));
  assert.equal(S.isFavMatch(["A"], { home: "X", away: "Z" }), false);
});

test('system probability rejects correlated selections and unknown probabilities',()=>{
 assert.equal(S.summarizeSystem([{id:'same',p:.7,odds:1.5},{id:'same',p:.6,odds:1.8},{id:'other',p:.6,odds:1.7}],2,100),null);
 assert.equal(S.summarizeSystem([{p:null,odds:1.8},{p:.6,odds:1.7}],2,100),null);
});
