const test = require("node:test");
const assert = require("node:assert/strict");
const CouponView = require("../../static/coupon-view.js");

const pick = (overrides = {}) => ({
  home: "Ev <A>", away: "Dep B", league_name: "Ana Lig", kickoff: "2026-10-07T18:00:00Z",
  market: "1", odds: 1.8, claude_odds: 1.7, odds_source: "iddaa", p: 0.72,
  ...overrides,
});
const coupon = (type, overrides = {}) => ({
  type, picks: [pick()], total_odds: 1.8, total_probability: 0.72, odds_source: "iddaa",
  stake_note: "Bilgilendirme", ...overrides,
});

test("published daily coupon and live alternatives are visibly distinguished", () => {
  assert.match(CouponView.couponCard(coupon("banko", {publication: "published"}), "Banko", "b-good"), /Günün yayımlanan kuponu/);
  assert.match(CouponView.couponCard(coupon("banko", {publication: "live"}), "Banko", "b-good"), /Güncel alternatif/);
});

test("coupon card builds its rows and escapes match text", () => {
  const html = CouponView.couponCard(coupon("banko"), "Banko Kuponu 1", "b-good");
  assert.match(html, /&lt;A&gt;/);
  assert.match(html, /Ana Lig/);
  assert.match(html, /1\.8/);
  assert.match(html, /72%/);
  assert.doesNotMatch(html, /undefined|NaN/);
});

test("missing and partial coupon data renders a safe empty state", () => {
  assert.doesNotThrow(() => CouponView.couponCard(null, "Banko", "b-good"));
  const empty = CouponView.couponCard({ picks: [null, {}, { home: "Ev" }] }, "Banko", "b-good");
  assert.match(empty, /Takım bilgisi yok/);
  assert.match(empty, /Olasılık/);
  assert.doesNotMatch(empty, /undefined|NaN/);
  assert.match(CouponView.couponCard(null, "Banko", "b-good"), /uygun maç bulunamadı/);
  assert.match(CouponView.couponCard({ picks: [{}], total_odds: null, total_probability: null }, "Banko", "b-good"), /Toplam oran:<\/b> - · <b>Toplam tutma:<\/b> -/);
});

test("coupon groups display multiple alternatives under their type", () => {
  const html = CouponView.couponGroup({
    banko_alternatives: [coupon("banko"), coupon("banko", { picks: [pick({ home: "İkinci" })] })],
    surprise_alternatives: [coupon("surprise")],
  }, "banko", "b-good");
  assert.match(html, /Dengeli \(Banko\) Kupon Alternatifleri/);
  assert.match(html, /Dengeli \(Banko\) Kuponu 1/);
  assert.match(html, /Dengeli \(Banko\) Kuponu 2/);
  assert.doesNotMatch(html, /Sürpriz Kuponu/);
});

test("share controls identify each coupon alternative and stay hidden for empty coupons", () => {
  const html = CouponView.couponGroup({
    banko_alternatives: [coupon("banko"), coupon("banko")],
  }, "banko", "b-good", "2026-10-08");
  assert.match(html, /data-share-type="banko" data-share-index="0"/);
  assert.match(html, /data-share-type="banko" data-share-index="1"/);
  const empty = CouponView.couponCard({ picks: [] }, "Küçük Ligler", "b-warn", { kind: "small_leagues", index: 0 });
  assert.doesNotMatch(empty, /data-share-coupon/);
});

test("empty coupons explain stale prices and the calibration gap", () => {
  const stale = CouponView.couponGroup({
    current_surprise_alternatives: [],
    date: "2099-01-01",
    surprise_diagnostics: { stale_odds: 4, odds_age_minutes: 185 },
  }, "surprise", "b-warn");
  assert.match(stale, /185 dk önce/);
  assert.match(stale, /Canlı kupon kapalı/);
  assert.match(stale, /4 maç/);
  const banko = CouponView.emptyReason({
    banko_diagnostics: { calibration_insufficient: 6, calibration_best_n: 18, calibration_min_samples: 30 },
  }, "banko");
  assert.match(banko, /En dolu dilim 18\/30/);
  assert.match(banko, /en az 30/);
});

test("legacy single coupons and a missing response retain a safe rendering path", () => {
  assert.equal(CouponView.alternatives({ banko: coupon("banko") }, "banko").length, 1);
  assert.equal(CouponView.alternatives({ banko_alternatives: [], banko: coupon("banko") }, "banko").length, 1);
  const group = CouponView.couponGroup(undefined, "surprise", "b-warn");
  assert.match(group, /Sürpriz türünde uygun kupon bulunamadı/);
});

test('current alternatives take precedence while previously published coupons remain separate',()=>{
 const published=coupon('banko',{publication:'published'}),current=coupon('banko',{publication:'live'});
 const data={date:'2099-01-01',banko:published,banko_alternatives:[published],current_banko_alternatives:[current]};
 assert.deepEqual(CouponView.alternatives(data,'banko'),[current]);
 data.current_banko_alternatives=[];
 assert.deepEqual(CouponView.alternatives(data,'banko'),[]);
 data.date='2020-01-01';
 assert.deepEqual(CouponView.alternatives(data,'banko'),[published]);
});


test('limited calibration is visible and the new policy does not blame it for an empty coupon',()=>{
 const card=CouponView.couponCard(coupon('banko',{
  calibration_status:'limited', calibration_note:'0/2 seçim kalibre edildi; <model> tahmini.'
 }), 'Banko', 'b-good');
 assert.match(card,/Kalibrasyon verisi sınırlı/);
 assert.match(card,/&lt;model&gt;/);
 const reason=CouponView.emptyReason({banko_diagnostics:{selection_policy:'hit_rate_v3',eligible_matches:1,calibration_insufficient:3}},'banko');
 assert.match(reason,/1 aday var/);
 assert.doesNotMatch(reason,/en az 30/);
});

test('small league alternatives render and share as distinct market-only coupons', () => {
 const one={picks:[{home:'A',away:'B',market:'A2.5',p:.6,odds:1.5,odds_source:'iddaa'}],total_odds:1.5,total_probability:.6,stake_note:'Piyasa verisi; model güveni yok'};
 const two={...one,picks:[{...one.picks[0],home:'C'}]};
 const data={small_leagues:one,small_league_alternatives:[one,two]};
 assert.equal(CouponView.alternatives(data,'small_leagues').length,2);
 const html=CouponView.couponGroup(data,'small_leagues','b-info','2026-11-09');
 assert.match(html,/Piyasa verili küçük ligler/);
 assert.match(html,/data-share-index="1"/);
 assert.match(html,/model güveni yok/);
});
