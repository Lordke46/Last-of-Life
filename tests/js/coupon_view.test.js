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
  assert.match(CouponView.couponCard({ picks: [{}], total_odds: null, total_probability: null }, "Banko", "b-good"), /Toplam oran:<\/b> - · <b>Toplam tahmini olasılık:<\/b> -/);
});

test("coupon groups display multiple alternatives under their type", () => {
  const html = CouponView.couponGroup({
    banko_alternatives: [coupon("banko"), coupon("banko", { picks: [pick({ home: "İkinci" })] })],
    surprise_alternatives: [coupon("surprise")],
  }, "banko", "b-good");
  assert.match(html, /Banko tercihi Kupon Alternatifleri/);
  assert.match(html, /Banko tercihi Kuponu 1/);
  assert.match(html, /Banko tercihi Kuponu 2/);
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
  assert.match(group, /Sürpriz için şu anda gösterilebilir alternatif yok/);
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
 const v4=CouponView.emptyReason({banko_diagnostics:{selection_policy:'hit_probability_v4',eligible_matches:0}},'banko');
 assert.match(v4,/%45 olasılık, 1,10–2,00/);
 const surprise=CouponView.emptyReason({surprise_diagnostics:{selection_policy:'hit_probability_v4',eligible_matches:0}},'surprise');
 assert.match(surprise,/%33 olasılık, 1,40–3,50/);
});

test('small league alternatives render and share as distinct market-only coupons', () => {
 const one={picks:[{home:'A',away:'B',market:'A2.5',p:.6,odds:1.5,odds_source:'iddaa'}],total_odds:1.5,total_probability:.6,stake_note:'Piyasa verisi; model güveni yok'};
 const two={...one,picks:[{...one.picks[0],home:'C'}]};
 const data={small_leagues:one,small_league_alternatives:[one,two]};
 assert.equal(CouponView.alternatives(data,'small_leagues').length,2);
 const html=CouponView.couponGroup(data,'small_leagues','b-info','2026-11-09');
 assert.match(html,/Piyasa verili küçük lig alternatifleri/);
 assert.match(html,/data-share-index="1"/);
 assert.match(html,/model güveni yok/);
});

test('zero main-league matches does not hide valid small-league alternatives in the daily flow',()=>{
 const small=coupon('banko',{selection_tier:'best_available',target_met:false,
  picks:[pick({home:'Alt Lig A',away:'Alt Lig B',league_name:'Küçük Lig'})]});
 const html=CouponView.couponGroup({matches:0,banko_alternatives:[small]},'banko','b-good');
 assert.match(html,/Alt Lig A/);
 assert.match(html,/en iyi mevcut alternatif gösteriliyor/);
 assert.match(html,/Garanti değildir/);
 assert.doesNotMatch(html,/bağımsız takım modeliyle değerlendirilmiş/);
});

test('target status distinguishes a candidate from a target-qualified coupon',()=>{
 const data={selection_tier:'best_available',target_met:false,banko_alternatives:[coupon('banko')]};
 assert.match(CouponView.couponGroup(data,'banko','b-good'),/Hedef koşullarına tam uyan kupon oluşmadı/);
 data.target_met=true;
 assert.match(CouponView.couponGroup(data,'banko','b-good'),/Tercih koşullarını karşıladı; sonuç garantisi değildir/);
 });

test("grok verdicts render as badges with escaped reason and only http(s) sources are linked", () => {
  const vetoed = pick({ grok_verdict: "veto", grok_reason: "Kaleci <sakat>", grok_sources: ["https://example.com/h"] });
  const html = CouponView.couponCard(coupon("banko", { publication: "published", picks: [vetoed], grok_vetoed: 1 }), "Banko", "b-good");
  assert.match(html, /badge b-bad[^>]*>Grok: veto/);
  assert.match(html, /Kaleci &lt;sakat&gt;/);
  assert.match(html, /href="https:\/\/example\.com\/h"/);
  assert.match(html, /Grok veto<\/span> 1 bacak güvenilir kaynakla veto edildi/);
  assert.match(CouponView.grokBadge({ grok_verdict: "caution", grok_reason: "Rotasyon" }), /b-warn[^>]*>Grok: dikkat/);
  assert.match(CouponView.grokBadge({ grok_verdict: "keep" }), /b-good[^>]*>Grok: tut/);
  assert.equal(CouponView.grokBadge({}), "");
  assert.doesNotMatch(CouponView.grokBadge({ grok_verdict: "veto", grok_sources: ["javascript:alert(1)"] }), /href/);
});

test("grok audit card summarises verdicts, swaps and renders the replacement coupon", () => {
  assert.equal(CouponView.grokAuditCard({ grok_audit: null }), "");
  assert.equal(CouponView.grokAuditCard(null), "");
  const data = {
    grok_audit: { received_at: "2026-11-14T06:00:00+00:00", kinds: ["daily", "lineup"], summary: "Özet <x>", vetoes: 1, cautions: 2,
      swaps: [{ kind: "banko", vetoed: ["Ev A - Dep B"], coupon_id: "2026-11-14:banko_grok:v1" }, { kind: "surprise", vetoed: ["C - D"], coupon_id: null }] },
    banko_grok: coupon("banko_grok", { publication: "grok", grok_note: "Grok veto'su: Ev A - Dep B" }),
    surprise_grok: null,
  };
  const html = CouponView.grokAuditCard(data);
  assert.match(html, /Grok denetimi/);
  assert.match(html, /günlük \+ kadro/);
  assert.match(html, /Özet &lt;x&gt;/);
  assert.match(html, /1 veto<\/span> <span class="badge b-warn">2 dikkat/);
  assert.match(html, /<b>Banko<\/b>: Ev A - Dep B veto edildi → veto'suz alternatif aşağıda/);
  assert.match(html, /<b>Sürpriz<\/b>: C - D veto edildi; başlamamış, veto'suz alternatif kalmadı/);
  assert.match(html, /Banko · Grok sonrası alternatif/);
  assert.match(html, /Grok veto'su sonrası geçilen veto'suz alternatif/);
  assert.doesNotMatch(html, /Sürpriz · Grok sonrası alternatif/);
  assert.doesNotMatch(html, /undefined|NaN/);
});
