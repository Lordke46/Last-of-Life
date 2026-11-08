import math
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import db
from app.main import coupons as coupons_endpoint, picks as picks_endpoint
from app.model import coupons as cp
from app.model.coupons import (_choices, _coupon, select_banko, select_banko_alternatives,
                               select_surprise, select_surprise_alternatives, select_small_league,
                               small_league_recommendation)


def pred(mid, markets, kickoff=None, league="eng.1"):
    return {"id": mid, "league": league, "kickoff": kickoff or (datetime.now(timezone.utc)+timedelta(days=1)).isoformat(), "home": f"H{mid}", "away": f"A{mid}",
            "data_n":20, "markets": {m: {"p": p, "suggested": s, "fair": round(1 / p, 2)} for m, (p, s) in markets.items()},
            # Test fixtures have synthetic *verified* calibration; production never invents it.
            "calibration_by_market": {m: {"p": p, "n": 100, "applied": True} for m, (p, _) in markets.items()}}


def entry(mid, markets, iddaa=None, **kw):
    p = pred(mid, markets, **kw)
    return p, _choices(p, iddaa if iddaa is not None else {m:s for m,(_,s) in markets.items()})


class OddsSourceTest(unittest.TestCase):
    def test_iddaa_odds_preferred_with_claude_fallback(self):
        p = pred(1, {"1": (0.6, 1.60), "X": (0.25, 4.0)})
        by_market = {c["market"]: c for c in _choices(p, {"1": 1.85})}
        self.assertEqual((by_market["1"]["odds"], by_market["1"]["odds_source"]), (1.85, "iddaa"))
        self.assertEqual(by_market["1"]["claude_odds"], 1.60)
        self.assertEqual((by_market["X"]["odds"], by_market["X"]["odds_source"]), (4.0, "claude"))
        self.assertIsNone(by_market["X"]["iddaa_odds"])

    def test_coupon_totals_and_source(self):
        picks = [{"odds": 2.0, "p": 0.8, "odds_source": "iddaa"}, {"odds": 1.5, "p": 0.75, "odds_source": "claude"}]
        c = _coupon("banko", picks, "n")
        self.assertEqual((c["total_odds"], c["total_probability"]), (3.0, 0.6))
        self.assertEqual((c["odds_source"], c["iddaa_picks"]), ("karışık", 1))
        self.assertEqual(_coupon("banko", picks[:1], "n")["odds_source"], "iddaa")
        self.assertIsNone(_coupon("banko", [], "n")["odds_source"])


class BankoTest(unittest.TestCase):
    def test_mid_price_high_confidence_and_one_pick_per_match(self):
        entries=[entry(1,{"1X":(.94,1.05),"1":(.72,1.70)}),entry(2,{"2":(.70,1.65)}),entry(3,{"1":(.65,1.8)})]
        picks=select_banko(entries)
        self.assertEqual(len(picks),2)
        self.assertEqual([p['id'] for p in picks],[1,2])
        self.assertTrue(all(1.4<=p['odds']<=2.3 and p['odds_source']=='iddaa' for p in picks))
        self.assertNotIn('1X',[p['market'] for p in picks])

    def test_hit_rate_policy_does_not_require_positive_expected_profit(self):
        entries = [entry(i, {"1": (.65, 1.40)}) for i in (1, 2, 3)]
        picks = select_banko(entries)
        self.assertEqual(len(picks), 2)
        self.assertTrue(all(p["value"] < 0 for p in picks))

    def test_banko_discloses_uncalibrated_estimates_without_blocking_them(self):
        entries = [entry(i, {"1": (.75, 1.65)}) for i in (1, 2, 3)]
        for prediction, _ in entries:
            prediction["calibration_by_market"] = {}
        entries = [(prediction, _choices(prediction, {"1": 1.65})) for prediction, _ in entries]
        picks = select_banko(entries)
        self.assertEqual(len(picks), 2)
        self.assertTrue(all(not p["calibration_applied"] for p in picks))
        coupon = _coupon("banko", picks, "n")
        self.assertEqual(coupon["calibration_status"], "limited")
        self.assertIn("0/2", coupon["calibration_note"])
        self.assertIn("henüz doğrulanmadı", coupon["calibration_note"])

    def test_stale_odds_are_not_counted_as_missing_quotes(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        entries = []
        for i in (1, 2):
            prediction = pred(i, {"1": (.7, 1.6)})
            prediction.update(iddaa_event_id=str(i), iddaa_updated_at=old, iddaa_odds={"1": 1.6})
            entries.append((prediction, _choices(prediction, prediction["iddaa_odds"])))
        report = {}
        self.assertEqual(cp._select_balanced(entries, "surprise", 1, report), [])
        self.assertEqual(report["stale_odds"], 2)
        self.assertEqual(report["no_market_quote"], 0)
        self.assertGreaterEqual(report["odds_age_minutes"], 180)

    def test_calibration_gap_is_disclosed_without_fabricating_calibration(self):
        self.assertEqual(cp.calibration.MIN_SAMPLES, 30)
        entries = []
        for i in (1, 2, 3):
            prediction = pred(i, {"1": (.7, 1.6)})
            prediction["calibration_by_market"] = {"1": {"p": 0.7, "n": 18, "applied": False}}
            entries.append((prediction, _choices(prediction, {"1": 1.6})))
        report = {}
        alternatives = cp._select_balanced(entries, "banko", 1, report)
        self.assertEqual(len(alternatives[0]), 2)
        self.assertTrue(all(not p["calibration_applied"] and p["calibration_n"] == 18 for p in alternatives[0]))
        self.assertEqual((report["calibration_best_n"], report["calibration_min_samples"]), (18, 30))
        self.assertGreater(report["calibration_insufficient"], 0)

    def test_banko_diagnostics_explain_missing_picks(self):
        entries = [entry(i, {"1": (.61, 1.40)}) for i in (1, 2)]
        report = {}
        self.assertEqual(cp._select_balanced(entries, "banko", 1, report), [])
        self.assertGreater(report["low_probability"], 0)
        self.assertEqual(report["selected_coupons"], 0)

    def test_probability_ranks_before_confidence_score_and_price(self):
        first = entry(1, {"1": (.80, 1.50)})
        first[0]["data_n"] = 12
        picks = select_banko([first, entry(2, {"1": (.71, 1.60)}), entry(3, {"1": (.69, 1.42)})])
        self.assertEqual({p["id"] for p in picks}, {1, 2})

    def test_applied_calibration_still_controls_the_probability_threshold(self):
        prediction = pred(1, {"1": (.75, 1.65)})
        prediction["calibration_by_market"]["1"].update(p=.60)
        entries = [(prediction, _choices(prediction, {"1": 1.65})), entry(2, {"1": (.75, 1.65)})]
        self.assertEqual(select_banko(entries), [])

    def test_raw_model_probability_is_not_replaced_by_calibrated_probability(self):
        prediction = pred(1, {"1": (.75, 1.65)})
        prediction["calibration_by_market"]["1"].update(p=.68)
        choice = _choices(prediction, {"1": 1.65})[0]
        view = cp._match_view(prediction, choice)
        self.assertEqual(view["p"], .68)
        self.assertEqual(view["model_probability"], .75)

    def test_unapplied_calibration_never_overrides_raw_probability(self):
        prediction = pred(1, {"1": (.60, 1.65)})
        prediction["calibration_by_market"]["1"].update(p=.90, applied=False)
        choice = _choices(prediction, {"1": 1.65})[0]
        self.assertEqual(choice["p"], .60)
        self.assertFalse(choice["calibration_applied"])

    def test_verified_missing_season_results_are_not_automatic_coupon_candidates(self):
        entries=[entry(i,{'1':(.75,1.6)}) for i in (1,2)]
        for p,_ in entries:p['data_integrity']={'history_complete':False}
        self.assertEqual(select_banko(entries),[])

    def test_sparse_data_and_fake_odds_are_not_banko_candidates(self):
        entries=[entry(i,{'1':(.8,1.6)}) for i in (1,2)]
        entries[0][0]['data_n']=3
        self.assertEqual(select_banko(entries),[])
        entries=[entry(i,{'1':(.8,1.6)},iddaa={}) for i in (1,2)]
        self.assertEqual(select_banko(entries),[])

    def test_two_matches_are_preferred_to_needless_extra_risk(self):
        picks=select_banko([entry(i,{'1':(.7,1.6)}) for i in (1,2,3)])
        self.assertEqual(len(picks),2)
        self.assertAlmostEqual(math.prod(p['p'] for p in picks),.49)

    def test_source_minimum_match_count_is_enforced(self):
        entries=[entry(i,{'1':(.72,1.6)}) for i in (1,2,3)]
        for _,choices in entries:
            for choice in choices: choice['min_matches']=3
        self.assertEqual(select_banko(entries[:2]),[])
        self.assertEqual(len(select_banko(entries)),3)

    def test_low_price_and_longshots_do_not_force_a_coupon(self):
        for odds in (1.08,3.5):
            self.assertEqual(select_banko([entry(i,{'1':(.8,odds)}) for i in (1,2)]),[])
        self.assertEqual(select_banko([]),[])
        self.assertEqual(select_banko([entry(1,{'1':(.8,1.6)})]),[])

    def test_experimental_markets_remain_manual_candidates(self):
        p,choices=entry(1,{'İY 1':(.8,1.6)})
        self.assertEqual(choices,[])


class SurpriseTest(unittest.TestCase):
    def test_bounded_odds_probability_and_unique_matches(self):
        picks=select_surprise([entry(i,{'1':(.5,2.2),'X':(.2,9)}) for i in range(1,6)])
        self.assertEqual(len(picks),3)
        self.assertEqual(len({p['id'] for p in picks}),3)
        self.assertTrue(8<=math.prod(p['odds'] for p in picks)<=25)
        self.assertTrue(all(p['market']=='1' for p in picks))

    def test_lottery_odds_and_synthetic_quotes_are_excluded(self):
        self.assertEqual(select_surprise([entry(i,{'X':(.15,9)}) for i in range(1,6)]),[])
        self.assertEqual(select_surprise([entry(i,{'1':(.5,2.2)},iddaa={}) for i in range(1,6)]),[])

    def test_alternate_market_can_reach_target_without_adding_a_match(self):
        picks=select_surprise([entry(i,{'1':(.65,1.9),'Ü2.5':(.6,2.3)}) for i in range(1,4)])
        self.assertEqual(len(picks),3)
        self.assertTrue(8<=math.prod(p['odds'] for p in picks)<=25)
        self.assertTrue(any(p['market']=='Ü2.5' for p in picks))


class CouponAlternativesTest(unittest.TestCase):
    def test_banko_alternatives_are_unique_and_span_main_leagues(self):
        entries = [entry(i, {"1": (0.82 - i * 0.01, 1.65)}, league=league)
                   for i, league in enumerate(("eng.1", "tur.1", "esp.1", "ger.1"), 1)]
        small = entry(9, {"1": (0.95, 1.5)}, league="small:country:league")
        small[0]["small_league"] = True
        alternatives = select_banko_alternatives(entries + [small])
        signatures = [frozenset(p["id"] for p in coupon) for coupon in alternatives]
        self.assertGreaterEqual(len(alternatives), 2)
        self.assertEqual(len(signatures), len(set(signatures)))
        for index, signature in enumerate(signatures):
            for other in signatures[index + 1:]:
                self.assertFalse(signature <= other or other <= signature)
        self.assertEqual({p["league"] for p in alternatives[0]}, {"eng.1", "tur.1"})
        self.assertTrue(all(p["id"] != 9 for coupon in alternatives for p in coupon))
        self.assertEqual(alternatives[0], select_banko(entries + [small]))

    def test_surprise_alternatives_are_unique_and_exclude_small_leagues(self):
        leagues = ("eng.1", "tur.1", "esp.1", "ger.1", "ita.1")
        entries = [entry(i, {"X": (0.50, 2.3)}, league=league) for i, league in enumerate(leagues, 1)]
        small = entry(9, {"X": (0.5, 5.0)}, league="small:country:league")
        small[0]["small_league"] = True
        alternatives = select_surprise_alternatives(entries + [small])
        signatures = [frozenset(p["id"] for p in coupon) for coupon in alternatives]
        self.assertEqual(len(alternatives), 3)
        self.assertEqual(len(signatures), len(set(signatures)))
        self.assertEqual({p["league"] for p in alternatives[0]}, {"eng.1", "tur.1", "esp.1"})
        self.assertTrue(all(p["id"] != 9 for coupon in alternatives for p in coupon))
        self.assertEqual(alternatives[0], select_surprise(entries + [small]))


class SmallLeagueCouponTest(unittest.TestCase):
    def test_small_leagues_are_excluded_from_existing_coupons(self):
        entries = [entry(1, {"1": (0.90, 1.3)}), entry(2, {"1": (0.90, 1.3)}),
                   entry(3, {"X": (0.3, 3.5)}), entry(4, {"X": (0.3, 3.5)}), entry(5, {"X": (0.3, 3.5)})]
        for p, _ in entries:
            p["small_league"] = True
        self.assertEqual(select_banko(entries), [])
        self.assertEqual(select_surprise(entries), [])

    def test_standard_selectors_exclude_small_flag(self):
        entries = [entry(i, {"1": (0.9, 1.3), "X": (0.3, 3.0)}) for i in range(1, 6)]
        for p, _ in entries:
            p["small_league"] = True
        self.assertEqual(select_banko(entries), [])
        self.assertEqual(select_surprise(entries), [])
        self.assertEqual(select_small_league(entries), [])  # model-only fixtures are not bulletin data

    def test_small_recommendation_uses_existing_probability_and_market_rules(self):
        p, choices = entry(1, {"1": (0.64, 1.55), "1X": (0.88, 1.12), "Ü2.5": (0.91, 1.1)},
                           {"1": 1.6}, league="small:iceland:premier-league")
        p["small_league"] = True
        self.assertIsNone(small_league_recommendation(p, choices))  # synthetic probabilities cannot enter small coupons

        p, choices = entry(2, {"1": (0.49, 1.9), "Ü2.5": (0.91, 1.1)},
                           league="small:iceland:premier-league")
        p["small_league"] = True
        self.assertIsNone(small_league_recommendation(p, choices))

    def test_standard_day_entries_filter_small_flag(self):
        now = datetime.now(timezone.utc) + timedelta(hours=3)
        kickoff = now.strftime("%Y-%m-%dT%H:%MZ")
        regular = {**pred(10, {"1": (0.8, 1.3)}, kickoff=kickoff), "iddaa_odds": {}}
        small = {**pred(11, {"1": (0.9, 1.2)}, kickoff=kickoff), "small_league": True, "iddaa_odds": {}}
        fake = type("M", (), {"upcoming_predictions": lambda self, d: [regular, small]})()
        with patch.object(cp, "LEAGUES", {"eng.1": {"name": "x"}}), \
             patch.object(cp.engine, "get_model", lambda l: fake), \
             patch.object(cp, "blend_predictions", lambda conn, xs: xs):
            entries = cp._day_entries(now.astimezone(cp.TZ).date())
        self.assertEqual([p["id"] for p, _ in entries], [10])

    def test_small_coupon_has_two_to_four_market_candidates_without_model_confidence(self):
        entries = []
        for i in range(1, 6):
            p = pred(i, {"1": (0.66, 1.5), "1X": (0.82, 1.2)}, league=f"small:country:league")
            p["small_league"] = True
            p["league_name"] = "Premier League"
            p["data_availability"] = {"market_probabilities": True}
            entries.append((p, _choices(p, {"1": 1.55, "X": 4.0, "2": 5.0})))
        with patch.object(cp, "LEAGUES", {}):
            picks = select_small_league(entries)
        self.assertEqual(len(picks), 4)
        self.assertTrue(all(p["small_league"] and p["confidence"] is None for p in picks))



class BuildCouponsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = patch.object(db, "DB_PATH", Path(self.tmp.name) / "t.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        db.init_db()

    def test_empty_day_and_no_iddaa_data(self):
        with patch.object(cp, "LEAGUES", {}):
            r = cp.build_coupons(date(2026, 1, 1))
        self.assertEqual((r["banko"]["picks"], r["surprise"]["picks"], r["matches"]), ([], [], 0))
        self.assertEqual((r["banko_alternatives"], r["surprise_alternatives"]), ([], []))

    def test_build_scans_main_leagues_together_and_keeps_legacy_coupon_fields(self):
        target = (datetime.now(cp.TZ) + timedelta(days=2)).date()
        kickoff = datetime.combine(target, datetime.min.time(), cp.TZ).replace(hour=18).isoformat()
        leagues = {f"league.{i}": {"name": f"Liga {i}"} for i in range(1, 6)}
        predictions = {league: [{**pred(i, {"1": (0.82 - i * 0.01, 1.65), "X": (0.50, 2.3)},
                                                 kickoff=kickoff, league=league), "iddaa_odds": {"1":1.65,"X":2.3}}]
                       for i, league in enumerate(leagues, 1)}
        fake_models = {league: type("M", (), {"upcoming_predictions": lambda self, days, p=items: p})()
                       for league, items in predictions.items()}
        with patch.object(cp, "LEAGUES", leagues), \
             patch.object(cp.engine, "get_model", lambda league: fake_models[league]), \
             patch.object(cp, "blend_predictions", lambda conn, items: items), \
             patch.object(cp.calibration, "calibrate", side_effect=lambda p, *args: {"p": p, "n": 100, "applied": True}):
            result = cp.build_coupons(target)

        self.assertEqual(result["matches"], 5)
        self.assertGreaterEqual(len(result["banko_alternatives"]), 2)
        self.assertGreaterEqual(len(result["surprise_alternatives"]), 2)
        self.assertEqual(result["banko"], result["banko_alternatives"][0])
        self.assertEqual(result["surprise"], result["surprise_alternatives"][0])
        self.assertTrue(all(c["type"] == "banko" for c in result["banko_alternatives"]))
        self.assertTrue(all(c["type"] == "surprise" for c in result["surprise_alternatives"]))
        self.assertEqual(result["small_leagues"]["picks"], [])

    def test_day_uses_istanbul_time_and_skips_started_matches(self):
        now = datetime.now(timezone.utc)
        late = (now + timedelta(days=2)).replace(hour=21, minute=30, second=0, microsecond=0)  # 00:30 TR ertesi gün
        started = now - timedelta(minutes=30)
        fmt = lambda t: t.strftime("%Y-%m-%dT%H:%MZ")
        fake = type("M", (), {"upcoming_predictions": lambda self, d: [
            pred(1, {"1": (0.8, 1.3)}, kickoff=fmt(late)), pred(2, {"1": (0.8, 1.3)}, kickoff=fmt(started))]})()
        with patch.object(cp, "LEAGUES", {"eng.1": {"name": "x"}}), patch.object(cp.engine, "get_model", lambda l: fake):
            same_utc_day = cp.build_coupons(late.date())
            istanbul_day = cp.build_coupons((late + timedelta(hours=3)).date())
            today = cp.build_coupons(started.astimezone(cp.TZ).date())
        self.assertEqual(same_utc_day["matches"], 0)
        self.assertEqual(istanbul_day["matches"], 1)
        self.assertEqual(today["matches"], 0)

    def test_endpoint_day_validation(self):
        with self.assertRaises(HTTPException) as ctx:
            coupons_endpoint(day="17-10-2026")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_small_picks_endpoint_reads_existing_event_and_odds_fields(self):
        kickoff = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        small_id = 'small-1" data-markup="<img src=x onerror=alert(1)>'
        with db.session() as conn:
            conn.execute(
                """INSERT INTO iddaa_events
                   (id, league, competition_id, kickoff, home, away, league_name, country, is_small, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (small_id, "small:iceland:premier-league", 123, kickoff, "North FC", "South FC",
                 "İzlanda · Premier League", "İzlanda", 1, kickoff))
            for market, odd in (("1", 1.3), ("X", 6.0), ("2", 9.0)):
                conn.execute("INSERT INTO iddaa_odds(event_id, market, odd) VALUES (?, ?, ?)",
                             (small_id, market, odd))

        result = picks_endpoint(category="small", days=7, min_conf=0.99)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], small_id)
        self.assertEqual(result[0]["country"], "İzlanda")
        self.assertEqual(result[0]["league_name"], "İzlanda · Premier League")
        self.assertEqual(result[0]["event_id"], small_id)
        self.assertEqual(result[0]["iddaa_event_id"], small_id)
        self.assertEqual(result[0]["competition_id"], 123)
        self.assertEqual(result[0]["pick"]["market"], "1")
        self.assertEqual(result[0]["pick"]["odds"], 1.3)
        self.assertEqual(result[0]["pick"]["odds_source"], "iddaa")
        self.assertEqual(result[0]["pick"]["probability_source"], "iddaa_market")
        self.assertIsNone(result[0]["pick"]["suggested"])
        self.assertEqual(result[0]["recommendation_source"], "iddaa_market")
        self.assertEqual(result[0]["recommendation_label"], "İddaa piyasa favorisi")
        self.assertIsNone(result[0]["confidence"])
        self.assertEqual(result[0]["confidence_label"], "Düşük · model güveni yok")
        self.assertFalse(result[0]["confidence_filter_applied"])
        self.assertTrue(result[0]["small_league"])


if __name__ == "__main__":
    unittest.main()
