// Çalıştırma: node --test tests/js/slip_calc.test.js  (unittest ile de çalışır: tests/test_slip_calc.py)
const test = require("node:test");
const assert = require("node:assert/strict");
const C = require("../../static/slip-calc.js");

const KICKOFF = "2099-01-01T18:00:00Z";
const match = (id, home, away, markets, iddaa = {}) => ({
  id, home, away, kickoff: KICKOFF, league_name: "Süper Lig", iddaa_odds: iddaa,
  markets: Object.fromEntries(Object.entries(markets).map(([k, [p, suggested]]) => [k, { p, suggested }])),
});
const pick = (m, market) => C.fromMatch(m, market);
const A = match("1", "Ev A", "Dep A", { "1": [0.5, 1.9], "X": [0.25, 3.8], "Ü2.5": [0.55, 1.7], "A1.5": [0.2, 4.9], "KG Var": [0.6, 1.6], "1X": [0.75, 1.3] }, { "1": 2.0 });
const B = match("2", "Ev B", "Dep B", { "2": [0.4, 2.4], "A2.5": [0.45, 2.1] });

test("oran önceliği: iddaa varsa iddaa, yoksa Claude", () => {
  assert.deepEqual(C.effectiveOdds(pick(A, "1")), { odds: 2, source: "iddaa" });
  assert.deepEqual(C.effectiveOdds(pick(A, "X")), { odds: 3.8, source: "claude" });
  assert.equal(C.effectiveOdds({ iddaaOdds: 1.0, claudeOdds: null }), null);
  assert.deepEqual(C.effectiveOdds({ iddaaOdds: 1.0, claudeOdds: 1.5 }), { odds: 1.5, source: "claude" });
});

test("fromMatch: tüm pazar grupları eklenebilir, geçersiz pazar/olasılık reddedilir", () => {
  assert.equal(C.fromMatch(A, "Ev 0.5Ü"), null);
  assert.equal(C.fromMatch(A, "2"), null); // maçta bu pazar yok
  assert.equal(C.fromMatch(match("9", "x", "y", { "1": [0, null] }), "1"), null);
  const sel = pick(A, "Ü2.5");
  assert.deepEqual([sel.id, sel.market, sel.p, sel.iddaaOdds, sel.claudeOdds, sel.leagueName], ["1", "Ü2.5", 0.55, null, 1.7, "Süper Lig"]);
  assert.equal(C.fromMatch({ ...A, league_name: undefined }, "1", "Premier League").leagueName, "Premier League");
});

test("toplam oran, tutma olasılığı, EV ve kazanç", () => {
  const items = [pick(A, "1"), pick(B, "2")]; // 2.0 (iddaa) x 2.4 (Claude)
  const s = C.summarize(items, 50, Date.now());
  assert.equal(s.totalOdds, 4.8);
  assert.ok(Math.abs(s.totalProbability - 0.5 * 0.4) < 1e-12);
  assert.ok(Math.abs(s.ev - (0.2 * 4.8 - 1)) < 1e-12);
  assert.equal(s.payout, 240);
  assert.equal(s.profit, 190);
  assert.equal(s.expectedValue, -2); // 50 * (0.96 - 1)
  assert.equal(s.source, "karışık");
  assert.deepEqual([s.iddaaCount, s.claudeCount, s.sameMatch.length], [1, 1, 0]);
});

test("kaynak özeti ve boş kupon", () => {
  assert.equal(C.summarize([pick(A, "1")], 10, 0).source, "iddaa");
  assert.equal(C.summarize([pick(B, "2")], 10, 0).source, "claude");
  const empty = C.summarize([], 10, 0);
  assert.deepEqual([empty.count, empty.totalOdds, empty.totalProbability, empty.ev, empty.payout, empty.source], [0, 0, 0, null, null, null]);
});

test("tutar yoksa para alanları null", () => {
  const s = C.summarize([pick(A, "1")], null, 0);
  assert.deepEqual([s.payout, s.profit, s.expectedValue, s.stake], [null, null, null, null]);
  assert.equal(C.summarize([pick(A, "1")], 0, 0).payout, 0);
});

test("aynı maçtan iki seçim uyarılır; olasılıkça çelişenler 0 sayılır", () => {
  const ok = C.summarize([pick(A, "1"), pick(A, "Ü2.5")], 10, 0);
  assert.equal(ok.sameMatch.length, 1);
  assert.deepEqual(ok.sameMatch[0].markets, ["1", "Ü2.5"]);
  assert.equal(ok.sameMatch[0].impossible, false);
  assert.equal(ok.totalProbability, null); // Aynı maçın pazarları bağımsız değildir.
  assert.equal(ok.ev, null);
  assert.equal(ok.expectedValue, null);
  assert.equal(ok.correlated, true);

  const bad = C.summarize([pick(A, "1"), pick(A, "X"), pick(B, "2")], 10, 0);
  assert.equal(bad.impossible, true);
  assert.equal(bad.totalProbability, 0);
  assert.ok(Math.abs(bad.naiveProbability - 0.5 * 0.25 * 0.4) < 1e-12);
  assert.ok(Math.abs(bad.ev + 1) < 1e-12);
});

test("çelişki kontrolü", () => {
  assert.equal(C.conflicts(["1", "X"]), true);
  assert.equal(C.conflicts(["1", "1X"]), false);
  assert.equal(C.conflicts(["1X", "X2"]), false); // beraberlik ikisini de tutar
  assert.equal(C.conflicts(["Ü2.5", "A2.5"]), true);
  assert.equal(C.conflicts(["Ü1.5", "A2.5"]), false); // tam 2 gol
  assert.equal(C.conflicts(["Ü2.5", "A1.5"]), true);
  assert.equal(C.conflicts(["KG Var", "KG Yok"]), true);
  assert.equal(C.conflicts(["KG Var", "A1.5"]), true); // KG Var en az 2 gol demek
  assert.equal(C.conflicts(["X", "KG Yok"]), false); // 0-0
});

test("başlamış maç işaretlenir", () => {
  const past = { ...pick(A, "1"), kickoff: "2020-01-01T00:00:00Z" };
  const s = C.summarize([past, pick(B, "2")], 10, Date.now());
  assert.equal(s.startedCount, 1);
  assert.deepEqual(s.rows.map((r) => r.started), [true, false]);
});

test("ekle / çıkar / tekrar / limit", () => {
  let r = C.addSelection([], pick(A, "1"));
  assert.equal(r.status, "added");
  assert.equal(C.addSelection(r.items, pick(A, "1")).status, "exists");
  assert.equal(C.addSelection(r.items, pick(A, "X")).items.length, 2); // aynı maç, farklı pazar
  assert.equal(C.addSelection(r.items, { id: "1" }).status, "invalid");
  assert.equal(C.hasSelection(r.items, 1, "1"), true);
  assert.equal(C.hasSelection(r.items, "1", "X"), false);
  assert.deepEqual(C.removeSelection(r.items, "1|1"), []);

  const full = Array.from({ length: C.MAX_ITEMS }, (_, i) => ({ ...pick(A, "1"), id: "m" + i }));
  assert.equal(C.addSelection(full, pick(B, "2")).status, "full");
});

test("sanitize: bozuk localStorage verisi güvenle temizlenir", () => {
  assert.deepEqual(C.sanitize(null), { items: [], stake: C.DEFAULT_STAKE });
  assert.deepEqual(C.sanitize("x"), { items: [], stake: C.DEFAULT_STAKE });
  const good = pick(A, "1");
  const s = C.sanitize({ items: [good, good, { ...good, id: "z", p: 5 }, { id: "q" }, null, { ...good, market: "Ev 0.5Ü" }], stake: 25 });
  assert.equal(s.items.length, 1);
  assert.equal(s.stake, 25);
  assert.equal(C.sanitize({ items: [], stake: -3 }).stake, C.DEFAULT_STAKE);
  assert.equal(C.sanitize({ items: [], stake: null }).stake, null);
  assert.equal(C.sanitize(JSON.parse(JSON.stringify({ items: [good] }))).items[0].market, "1"); // JSON gidiş-dönüş
});

test("parseStake: virgül ve geçersiz girdiler", () => {
  assert.equal(C.parseStake("12,5"), 12.5);
  assert.equal(C.parseStake(" 100 "), 100);
  assert.equal(C.parseStake(""), null);
  assert.equal(C.parseStake("abc"), null);
  assert.equal(C.parseStake("-5"), null);
  assert.equal(C.parseStake(undefined), null);
});

test("refreshSelection: taze veriyle oran/olasılık güncellenir, uymayan veri eskisini korur", () => {
  const old = pick(A, "1");
  const fresh = match("1", "Ev A", "Dep A", { "1": [0.55, 1.7] }, { "1": 2.1 });
  const next = C.refreshSelection(old, fresh);
  assert.deepEqual([next.p, next.iddaaOdds, next.claudeOdds], [0.55, 2.1, 1.7]);
  assert.equal(C.refreshSelection(old, { id: "1", markets: {} }), old);
});

test('finished coupon pick retains score through storage, missing live feed and odds refresh', () => {
  const final = C.withResult(pick(A,'1'),{id:'1',state:'post',home_goals:2,away_goals:1,updated_at:'2026-10-24T18:00Z'});
  const restored=C.sanitize(JSON.parse(JSON.stringify({items:[final],stake:100}))).items[0];
  const missingFeed=C.withResult(restored,undefined);
  const staleFeed=C.withResult(missingFeed,{id:'1',state:'in',home_goals:1,away_goals:1});
  const refreshed=C.refreshSelection(staleFeed,A);
  assert.deepEqual(C.resultView(refreshed,Date.now()),{label:'Bitti',score:'2–1',result:'Tuttu',completed:true});
  const totals=C.summarize([refreshed],100,Date.now());
  assert.equal(totals.startedCount,0);
  assert.equal(totals.finishedCount,1);
});

test('0–0 draw is a final score and invalid result cannot fabricate a finish', () => {
  const final=C.withResult(pick(A,'X'),{state:'post',home_goals:0,away_goals:0});
  assert.equal(C.resultView(final,Date.now()).result,'Tuttu');
  assert.equal(C.resultView(C.withResult(pick(A,'1'),{state:'post',home_goals:null,away_goals:0}),Date.now()).completed,false);
  assert.equal(C.withResult(final,{id:'other',state:'post',home_goals:3,away_goals:0}),final);
});

test('elapsed kickoff without a verified result is pending, not an invented finished score', () => {
  const sel={...pick(A,'1'),kickoff:'2020-01-01T00:00Z'};
  assert.equal(C.resultView(sel,Date.now()).label,'Sonuç bekleniyor');
  assert.equal(C.resultView(sel,Date.now()).score,'');
});

test('coupon overall outcome settles on verified finals and counts remaining distinct matches', () => {
  const a=pick(A,'1'), b=pick(B,'2');
  const final=(item,h,a)=>C.withResult(item,{state:'post',home_goals:h,away_goals:a});
  let s=C.summarize([final(a,2,0),b],100,Date.now());
  assert.deepEqual([s.status,s.wonCount,s.lostCount,s.pendingCount,s.remainingMatches],['pending',1,0,1,1]);
  s=C.summarize([final(a,2,0),final(b,0,1)],100,Date.now());
  assert.deepEqual([s.status,s.totalProbability,s.remainingMatches],['won',1,0]);
  s=C.summarize([final(a,0,1),b],100,Date.now());
  assert.deepEqual([s.status,s.lostCount,s.totalProbability,s.remainingMatches],['lost',1,0,1]);
  s=C.summarize([a,pick(A,'1X'),b],100,Date.now());
  assert.equal(s.pendingCount,3);
  assert.equal(s.remainingMatches,2);
  assert.equal(C.summarize([],100,Date.now()).status,'empty');
});

test('experimental picks require real quotes, persist and never settle a first-half bet from full score', () => {
  const m={...A,extra_markets:{'İY 1':{p:.65,suggested:1.5},'Handikap -1 2':{p:.6,suggested:1.6}},iddaa_odds:{'İY 1':1.7,'Handikap -1 2':1.8}};
  const half=C.fromMatch(m,'İY 1');
  assert.ok(half);
  assert.equal(C.fromMatch({...m,iddaa_odds:{}},'İY 1'),null);
  const final=C.withResult(half,{state:'post',home_goals:2,away_goals:0});
  assert.equal(C.selectionOutcome(final),null);
  assert.equal(C.summarize([final],10,Date.now()).status,'pending');
  assert.equal(C.resultView(final,Date.now()).result,'Sonuç doğrulanmalı');
  const confirmed=C.sanitize({items:[{...final,manualStatus:'won'}]}).items[0];
  assert.equal(C.summarize([confirmed],10,Date.now()).status,'won');
  const handicap=C.withResult(C.fromMatch(m,'Handikap -1 2'),{state:'post',home_goals:1,away_goals:1});
  assert.equal(C.selectionOutcome(handicap),true); // signed SOV -1 gives away a 0:1 advantage
  assert.equal(C.conflicts(['İY 1','2']),false);
});

test('quoted corner/card markets can be added without inventing a probability or a final outcome',()=>{
  const m={...A,extra_markets:{'Korner A9.5':{p:null,suggested:null}},iddaa_odds:{'Korner A9.5':1.85}};
  const pick=C.fromMatch(m,'Korner A9.5');
  assert.ok(pick);
  const s=C.summarize([pick],100,Date.now());
  assert.equal(s.totalOdds,1.85);
  assert.equal(s.totalProbability,null);
  assert.equal(s.expectedValue,null);
  const final=C.withResult(pick,{state:'post',home_goals:3,away_goals:1});
  assert.equal(C.summarize([final],100,Date.now()).status,'pending');
});

test('minimum match count comes from the source market and counts distinct matches',()=>{
 const m={...A,iddaa_min_matches:{'1':3}};
 const item=C.fromMatch(m,'1');
 assert.equal(item.minMatches,3);
 const stored=C.sanitize({items:[item]}).items[0];
 assert.equal(stored.minMatches,3);
 const s=C.summarize([stored,pick(A,'1X')],100,Date.now());
 assert.equal(s.uniqueMatches,1);
 assert.equal(s.minMatches,3);
});

test('a removed real quote is retained as unavailable rather than replaced by a simulated price',()=>{
 const old=pick(A,'1'),updated=C.refreshSelection(old,{...A,iddaa_odds:{}});
 assert.equal(updated.iddaaOdds,2);
 assert.equal(updated.quoteUnavailable,true);
 assert.equal(C.effectiveOdds(updated).source,'iddaa');
});

test('archived or fixture-only odds cannot be added as current bookmaker selections',()=>{
 assert.equal(C.fromMatch({...A,odds_current:false},'1'),null);
});
