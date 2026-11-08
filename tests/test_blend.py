import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import db, odds_log
from app.model import blend, engine
from app.model import predict as P
from app.scrapers import iddaa

ODDS_1X2 = {"1": 2.0, "X": 3.5, "2": 4.0}  # Σ1/oran = 0.5 + 0.2857 + 0.25 = 1.0357
ODDS_ALL = {**ODDS_1X2, "Ü2.5": 1.85, "A2.5": 1.85, "KG Var": 1.7, "KG Yok": 2.0, "1X": 1.3, "X2": 1.6}


def model_markets(p1=0.5, px=0.25, p2=0.25, over=0.55, btts=0.5):
    mk = {"1": P.odds(p1), "X": P.odds(px), "2": P.odds(p2),
          "1X": P.odds(p1 + px), "12": P.odds(p1 + p2), "X2": P.odds(px + p2),
          "Ü1.5": P.odds(0.8), "A1.5": P.odds(0.2), "Ü2.5": P.odds(over), "A2.5": P.odds(1 - over),
          "Ü3.5": P.odds(0.3), "A3.5": P.odds(0.7), "KG Var": P.odds(btts), "KG Yok": P.odds(1 - btts),
          "Ev 0.5Ü": P.odds(0.7), "Dep 0.5Ü": P.odds(0.6)}
    return mk


def prediction(**kw):
    mk = model_markets(**kw)
    return {"id": "m1", "markets": mk, "pick": P.best_pick(mk), "data_quality": 1.0,
            "data_n": 20, **P.confidence(P.best_pick(mk)["p"], 20)}


class DevigTest(unittest.TestCase):
    def test_margin_removed_and_sums_to_one(self):
        probs = blend.devig([2.0, 3.5, 4.0])
        self.assertAlmostEqual(sum(probs), 1.0)
        self.assertAlmostEqual(probs[0], 0.5 / (0.5 + 1 / 3.5 + 0.25))
        self.assertGreater(probs[0], probs[1])

    def test_fair_two_way_unchanged(self):
        self.assertEqual([round(p, 4) for p in blend.devig([2.0, 2.0])], [0.5, 0.5])

    def test_invalid_odds_rejected(self):
        self.assertIsNone(blend.devig([2.0, None, 3.0]))
        self.assertIsNone(blend.devig([1.0, 3.0]))
        self.assertIsNone(blend.devig([1.2, 1.2, 1.2]))  # Σ = 2.5, marj makul değil
        self.assertIsNone(blend.devig([5.0, 5.0, 5.0]))  # Σ = 0.6 < 1: eksik pazar gibi
        self.assertIsNone(blend.devig([]))

    def test_groups_need_complete_sets(self):
        groups = blend.implied_groups({"1": 2.0, "X": 3.5, "Ü2.5": 1.8, "KG Var": 1.7, "KG Yok": 2.0})
        self.assertEqual(set(groups), {"btts"})
        self.assertEqual(set(blend.implied_groups(ODDS_ALL)), {"1x2", "ou2.5", "btts"})


class BlendMarketsTest(unittest.TestCase):
    def test_weight_endpoints_and_midpoint(self):
        mk = model_markets()
        q = blend.devig([2.0, 3.5, 4.0])
        zero, groups0 = blend.blend_markets(mk, ODDS_1X2, 0.0)
        self.assertIs(zero, mk)
        self.assertEqual(groups0, [])
        one, _ = blend.blend_markets(mk, ODDS_1X2, 1.0)
        self.assertAlmostEqual(one["1"]["p"], q[0], places=3)
        half, groups = blend.blend_markets(mk, ODDS_1X2, 0.5)
        self.assertEqual(groups, ["1x2"])
        self.assertAlmostEqual(half["1"]["p"], 0.5 * 0.5 + 0.5 * q[0], places=3)
        self.assertEqual((half["1"]["model_p"], round(half["1"]["market_p"], 4)), (0.5, round(q[0], 4)))

    def test_no_odds_keeps_model_exactly(self):
        mk = model_markets()
        out, groups = blend.blend_markets(mk, {}, 0.5)
        self.assertIs(out, mk)
        self.assertEqual(groups, [])

    def test_probabilities_stay_consistent(self):
        out, groups = blend.blend_markets(model_markets(), ODDS_ALL, 0.6)
        self.assertEqual(set(groups), {"1x2", "ou2.5", "btts"})
        p = {k: v["p"] for k, v in out.items()}
        self.assertAlmostEqual(p["1"] + p["X"] + p["2"], 1.0, places=3)
        self.assertAlmostEqual(p["Ü2.5"] + p["A2.5"], 1.0, places=3)
        self.assertAlmostEqual(p["KG Var"] + p["KG Yok"], 1.0, places=3)
        # çifte şans iddaa'nın 1X/X2 oranından değil, harmanlı 1X2'den türer
        self.assertAlmostEqual(p["1X"], p["1"] + p["X"], places=3)
        self.assertAlmostEqual(p["12"], p["1"] + p["2"], places=3)
        self.assertAlmostEqual(p["X2"], p["X"] + p["2"], places=3)

    def test_unquoted_markets_keep_model(self):
        mk = model_markets()
        out, _ = blend.blend_markets(mk, ODDS_1X2, 0.5)
        for key in ("Ü2.5", "A2.5", "Ü1.5", "KG Var", "Ev 0.5Ü"):
            self.assertEqual(out[key]["p"], mk[key]["p"])
            self.assertIsNone(out[key]["market_p"])

    def test_suggested_and_fair_follow_blended_probability(self):
        out, _ = blend.blend_markets(model_markets(), ODDS_1X2, 0.5)
        self.assertEqual(out["1"]["fair"], P.odds(out["1"]["p"])["fair"])
        self.assertEqual(out["1"]["suggested"], P.odds(out["1"]["p"])["suggested"])

    def test_weight_is_clamped_and_read_from_config(self):
        mk = model_markets()
        over, _ = blend.blend_markets(mk, ODDS_1X2, 7)
        one, _ = blend.blend_markets(mk, ODDS_1X2, 1.0)
        self.assertEqual(over["1"]["p"], one["1"]["p"])
        with patch.dict(blend.MODEL, {"market_weight": 0.0}):
            self.assertIs(blend.blend_markets(mk, ODDS_1X2)[0], mk)


class BlendPredictionTest(unittest.TestCase):
    def test_no_odds_keeps_probabilities_but_does_not_offer_a_fake_quote(self):
        pred = prediction()
        out = blend.blend_prediction(pred, {}, 0.5)
        self.assertEqual(out["markets"], pred["markets"])
        self.assertEqual((out["pick"], out["confidence"]), (None, 0))
        self.assertFalse(out["blend"]["applied"])

    def test_pick_and_confidence_recomputed(self):
        pred = prediction(p1=0.40, px=0.30, p2=0.30)  # model: zayıf favori
        odds = {"1": 1.5, "X": 4.5, "2": 7.0}  # piyasa: net ev sahibi favorisi
        out = blend.blend_prediction(pred, odds, 0.8)
        self.assertTrue(out["blend"]["applied"])
        self.assertGreater(out["markets"]["1"]["p"], pred["markets"]["1"]["p"])
        self.assertEqual(out["confidence"], P.confidence(out["pick"]["p"], 20)["confidence"])  # önerinin olasılığından
        self.assertEqual(out["confidence_label"], P.confidence_label(out["confidence"]))
        self.assertEqual(out["pick"]["p"], P.best_pick(out["markets"], actual_odds=odds)["p"])
        self.assertEqual(pred["markets"]["1"]["p"], 0.40)  # girdi değişmez

    def test_blend_predictions_attaches_odds_from_db(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(db, "DB_PATH", Path(tmp) / "t.db"), \
                patch.object(db, "DATABASE_URL", None):
            db.init_db()
            with db.session() as conn:
                conn.execute("INSERT INTO iddaa_events(id,league,competition_id,kickoff,home,away,match_id,match_score,updated_at) VALUES('e1','eng.1',1,'2026-10-17T14:00:00+00:00','A','B','m1',1,'t')")
                for market, odd in ODDS_1X2.items():
                    conn.execute("INSERT INTO iddaa_odds VALUES('e1',?,?)", (market, odd))
            with db.session() as conn:
                out = blend.blend_predictions(conn, [prediction(), {**prediction(), "id": "m2"}, {"id": "m1"}])
                future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                blend.blend_predictions(conn, [{**prediction(), "kickoff": future, "league": "eng.1", "home": "A", "away": "B"}])
                self.assertEqual(conn.execute("SELECT COUNT(*) n FROM prediction_snapshots").fetchone()["n"], 0)
        self.assertTrue(out[0]["blend"]["applied"])
        self.assertEqual((out[0]["iddaa_odds"], out[0]["iddaa_event_id"]), (ODDS_1X2, "e1"))
        self.assertFalse(out[1]["blend"]["applied"])
        self.assertEqual((out[1]["iddaa_odds"], out[1]["iddaa_event_id"]), ({}, None))
        self.assertNotIn("blend", out[2])  # markets içermeyen kayıtlara yalnız oran eklenir
        self.assertEqual(out[2]["iddaa_odds"], ODDS_1X2)


class BlendEvalTest(unittest.TestCase):
    def test_market_beats_model_when_market_is_right(self):
        ev = blend.BlendEval(0.5)
        for _ in range(10):  # model beraberlik diyor, piyasa ve sonuç ev sahibi
            ev.add(model_markets(0.3, 0.4, 0.3), {"1": 1.4, "X": 5.0, "2": 8.0}, 2, 0)
        g = ev.result()["groups"]["1x2"]
        self.assertEqual(g["n"], 10)
        self.assertLess(g["market"]["logloss"], g["blend"]["logloss"])
        self.assertLess(g["blend"]["logloss"], g["model"]["logloss"])
        self.assertLess(g["blend"]["brier"], g["model"]["brier"])
        self.assertEqual((g["model"]["accuracy"], g["market"]["accuracy"]), (0.0, 1.0))
        self.assertEqual(g["sweep"]["0"], g["model"]["logloss"])
        self.assertEqual(g["sweep"]["1"], g["market"]["logloss"])
        self.assertEqual(min(g["sweep"], key=g["sweep"].get), "1")

    def test_only_groups_with_odds_are_evaluated(self):
        ev = blend.BlendEval(0.5)
        self.assertFalse(ev.add(model_markets(), {}, 1, 1))
        self.assertTrue(ev.add(model_markets(), {"KG Var": 1.7, "KG Yok": 2.0}, 1, 1))
        res = ev.result()
        self.assertEqual((res["matches"], set(res["groups"])), (1, {"btts"}))
        self.assertEqual(blend.BlendEval(0.5).result(), {"weight": 0.5, "matches": 0, "groups": {}})

    def test_binary_outcomes(self):
        ev = blend.BlendEval(0.5)
        ev.add(model_markets(over=0.6), {"Ü2.5": 1.9, "A2.5": 1.9}, 2, 1)  # 3 gol -> Üst
        g = ev.result()["groups"]["ou25"]
        self.assertAlmostEqual(g["model"]["logloss"], -math.log(0.6), places=3)
        self.assertEqual(g["model"]["accuracy"], 1.0)


class OddsLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(db, "DB_PATH", Path(self.tmp.name) / "t.db"), patch.object(db, "DATABASE_URL", None)):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()

    def test_record_keeps_first_odd_and_updates_latest(self):
        with db.session() as conn:
            odds_log.record(conn, [("m1", "1", 2.0), ("m1", "X", 3.0)], "t1")
            odds_log.record(conn, [("m1", "1", 1.8)], "t2")
            rows = {r["market"]: dict(r) for r in conn.execute("SELECT * FROM odds_log")}
        self.assertEqual((rows["1"]["odd"], rows["1"]["first_odd"], rows["1"]["first_seen"], rows["1"]["updated_at"]),
                         (1.8, 2.0, "t1", "t2"))
        self.assertEqual((rows["X"]["odd"], rows["X"]["updated_at"]), (3.0, "t1"))

    def test_record_many_rows_in_chunks(self):
        rows = [(f"m{i}", "1", 2.0) for i in range(250)]
        with db.session() as conn:
            odds_log.record(conn, rows, "t")
            self.assertEqual(conn.execute("SELECT COUNT(*) n FROM odds_log").fetchone()["n"], 250)

    def test_record_safely_survives_missing_table(self):
        with db.session() as conn:
            conn.execute("DROP TABLE odds_log")
        with db.session() as conn:
            self.assertFalse(odds_log.record_safely(conn, [("m1", "1", 2.0)], "t"))
            self.assertIn("odds_log", db.get_meta(conn, "odds_log_error"))
        self.assertEqual(odds_log.current_summary(), {"available": False})

    def seed_match(self, conn, mid, completed, league="eng.1"):
        conn.execute("INSERT INTO matches(id, league, kickoff, home, away, completed) VALUES(?,?,?,?,?,?)",
                     (mid, league, "2026-10-17T14:00Z", "A", "B", completed))

    def test_sync_odds_logs_only_prematch_matched_events(self):
        future = datetime.now(timezone.utc) + timedelta(days=2)
        past = datetime.now(timezone.utc) - timedelta(hours=1)

        def raw(i, when):
            return {"i": i, "ci": 584, "d": int(when.timestamp()), "hn": "Fenerbahçe", "an": "Beşiktaş",
                    "m": [{"t": 1, "st": 1, "s": 1, "o": [{"no": 1, "odd": 1.9}, {"no": 2, "odd": 3.2},
                                                           {"no": 3, "odd": 3.8}]}]}

        with db.session() as conn:
            for mid, when in (("f1", future), ("f2", past)):
                conn.execute("INSERT INTO matches(id, league, kickoff, home, away, completed) VALUES(?,?,?,?,?,0)",
                             (mid, "tur.1", when.strftime("%Y-%m-%dT%H:%MZ"), "Fenerbahçe", "Beşiktaş"))
            with patch.object(iddaa, "fetch_json", lambda retries=3: {"data": {"events": [raw(1, future), raw(2, past)]}}):
                iddaa.sync_odds(conn)
        with db.session() as conn:
            logged = {r["match_id"] for r in conn.execute("SELECT match_id FROM odds_log")}
            self.assertEqual(logged, {"f1"})
            self.assertEqual(odds_log.summary(conn)["matches"], 1)

    def test_summary_and_backtest_loader(self):
        with db.session() as conn:
            self.seed_match(conn, "m1", 1)
            self.seed_match(conn, "m2", 0)
            self.seed_match(conn, "m3", 1, league="tur.1")
            odds_log.record(conn, [("m1", "1", 2.0), ("m2", "1", 2.0), ("m3", "1", 2.0)], "2026-10-01T00:00:00+00:00")
        with db.session() as conn:
            s = odds_log.summary(conn)
            self.assertEqual((s["matches"], s["completed"], s["since"]), (3, 2, "2026-10-01T00:00:00+00:00"))
            self.assertEqual(set(odds_log.load_for_league(conn, "eng.1")), {"m1", "m2"})
        self.assertEqual(odds_log.current_summary()["matches"], 3)

    def test_backtest_without_logged_odds_has_empty_blend(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with db.session() as conn:
            for i in range(30):
                conn.execute(
                    "INSERT INTO matches(id, league, kickoff, home, away, home_goals, away_goals, completed) "
                    "VALUES(?,?,?,?,?,?,?,1)",
                    (f"x{i}", "eng.1", (base + timedelta(days=i)).strftime("%Y-%m-%dT%H:%MZ"),
                     f"T{i % 4}", f"T{(i + 1) % 4}", i % 3, (i + 1) % 2))
        res = engine.backtest("eng.1", min_history=10)
        self.assertEqual(res["n"], 20)
        self.assertEqual(res["blend"]["groups"], {})
        with db.session() as conn:
            odds_log.record(conn, [("x15", "1", 2.1), ("x15", "X", 3.3), ("x15", "2", 3.6)], "t")
        res = engine.backtest("eng.1", min_history=10)
        self.assertEqual((res["blend"]["matches"], res["blend"]["groups"]["1x2"]["n"]), (1, 1))
        self.assertEqual(res["n"], 20)  # ana metrikler (harmansız) aynı


if __name__ == "__main__":
    unittest.main()
