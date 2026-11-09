const test = require("node:test");
const assert = require("node:assert/strict");
const Simple = require("../../static/simple.js");

test("plain Turkish labels", () => {
  assert.equal(Simple.headline("Ü2.5"), "2.5 Üst");
  assert.equal(Simple.title("Ü2.5"), "3 veya daha fazla gol");
  assert.equal(Simple.title("A2.5"), "En fazla 2 gol");
  assert.equal(Simple.title("KG Var"), "İki takım da gol atar");
});

test("server summary is rendered with 3-level badge and no jargon", () => {
  const html = Simple.pickBlock({ summary: { market: "2", headline: "MS 2", title: "Deplasman kazanır", odds: 2.1, level: 3, level_label: "Yüksek", reason: "Rakip zayıf." } });
  assert.match(html, /MS 2/);
  assert.match(html, /2\.10/);
  assert.match(html, /conf-3/);
  assert.doesNotMatch(html, /xG|EV|Kelly|Elo|lambda/i);
});

test("highest-probability view requires distinct backend-eligible iddaa pick", () => {
  const match = {pick:{market:"1"}, strongest_pick:{market:"Ü1.5",p:.74,odds:1.5,
    odds_source:"iddaa",policy:"highest_probability_v1"}};
  const html = Simple.strongestPickBlock(match);
  assert.match(html,/En yüksek olasılık/);
  assert.match(html,/Üst/);
  assert.match(html,/74%/);
  assert.match(html,/iddaa 1\.50/);
  assert.match(html,/Tahmini olasılık; garanti değil/);
  assert.equal(Simple.strongestPickBlock({...match,strongest_pick:{...match.strongest_pick,market:"1"}}),"");
  assert.equal(Simple.strongestPickBlock({...match,strongest_pick:{...match.strongest_pick,odds_source:"model"}}),"");
  assert.equal(Simple.strongestPickBlock({...match,strongest_pick:{...match.strongest_pick,policy:"other"}}),"");
});

test('league list puts high, medium and low confidence before limited-data rows', () => {
  const m = (id, level, score, kickoff) => ({id, confidence:score, kickoff,
    summary:{market:'1',level}});
  const rows=[m('low',1,.4,'2026-10-24T10:00Z'),m('medium',2,.6,'2026-10-24T11:00Z'),
    m('high',3,.8,'2026-10-26T18:00Z'),m('higher',3,.9,'2026-10-27T18:00Z'),m('unknown',0,null,'2026-10-24T09:00Z')];
  assert.deepEqual(Simple.sortMatches(rows).map(m=>m.id),['higher','high','medium','low','unknown']);
  assert.equal(rows[0].id,'low');
});

test('quick filters combine confidence, Istanbul day and live state', () => {
  const rows = [
    {id:'a',kickoff:'2026-10-23T22:00Z',summary:{level:3}},
    {id:'b',kickoff:'2026-10-24T18:00Z',summary:{level:2}},
    {id:'c',kickoff:'2026-10-24T22:00Z',summary:{level:3}},
  ];
  const now=Date.parse('2026-10-24T10:00Z');
  const live=new Map([['a',{state:'in'}],['b',{state:'post'}]]);
  assert.deepEqual(Simple.filterMatches(rows,{today:true},live,now).map(m=>m.id),['a','b']);
  assert.deepEqual(Simple.filterMatches(rows,{today:true,high:true,live:true},live,now).map(m=>m.id),['a']);
  assert.deepEqual(Simple.filterMatches(rows,{live:true},live,now).map(m=>m.id),['a']);
  assert.equal(Simple.filterMatches(rows,{},live,now).length,3);
});

test('double chance and signed handicaps use source-compatible labels',()=>{
 assert.equal(Simple.headline('12'),'ÇŞ 1–2');
 assert.equal(Simple.headline('1X'),'ÇŞ 1–0');
 assert.equal(Simple.headline('Handikap -1 1'),'HMS 0:1 1');
 assert.equal(Simple.headline('Handikap 2 X'),'HMS 2:0 0');
});

test('result-only summaries show the result label once and have no recommendation badge',()=>{
 const html=Simple.pickBlock({summary:{market:null,headline:'Bitti',title:'Ev 2–1 Dep',recommendation_label:'Bitti',level:0}});
 assert.equal((html.match(/Bitti/g)||[]).length,1);
 assert.doesNotMatch(html,/Net öneri yok/);
});
