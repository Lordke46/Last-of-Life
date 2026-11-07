"""P1: sadece maç öncesi gerçek iddaa fiyatından geçmiş performans."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import db
from app.model import performance
from app.model import coupons


NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
KICKOFF = "2026-11-05T18:00:00+00:00"


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for p in (patch.object(db, "DATABASE_URL", None),
                  patch.object(db, "DB_PATH", Path(temp.name) / "p1.db")):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()

    def add(self, mid, *, p=.75, odds=1.8, source="iddaa",
            captured="2026-11-05T10:00:00+00:00", quote=None,
            goals=(2, 0)):
        payload = {"pick": {"market": "1", "p": p, "odds": odds,
                             "odds_source": source},
                   "odds": {"1": odds if quote is None else quote}}
        with db.session() as conn:
            conn.execute(
                "INSERT INTO matches(id,league,kickoff,home,away,completed,home_goals,away_goals) "
                "VALUES (?, 'eng.1', ?, 'a','b',1,?,?)", (mid, KICKOFF, *goals))
            conn.execute(
                "INSERT INTO prediction_snapshots "
                "(match_id,league,kickoff,captured_at,model_version,parameters,payload) "
                "VALUES (?, 'eng.1', ?, ?, 'v1', '{}', ?)",
                (mid, KICKOFF, captured, json.dumps(payload)))

    def result(self):
        with db.session() as conn:
            return performance.report(conn, NOW)

    def test_first_prematch_actual_odds_and_roi(self):
        self.add("a", goals=(2, 0))
        self.add("b", goals=(0, 2), p=.6, odds=2.0)
        summary = self.result()["summary"]
        self.assertEqual((summary["n"], summary["won"]), (2, 1))
        self.assertEqual(summary["realized_roi"], -0.1)
        self.assertAlmostEqual(summary["predicted_rate"], .675, places=4)
        self.assertFalse(summary["reliable"])

    def test_no_hindsight_or_synthetic_odds(self):
        self.add("valid")
        self.add("late", captured="2026-11-05T20:00:00+00:00")
        self.add("synthetic", source="model")
        self.add("changed_quote", quote=1.3)
        summary = self.result()["summary"]
        self.assertEqual(summary["n"], 1)

    def test_empty_is_truthful(self):
        self.assertEqual(self.result()["summary"], {"n": 0, "reliable": False})


class StaleOddsTests(unittest.TestCase):
    def test_fresh_quote_and_stale_quote(self):
        current = datetime.now(timezone.utc)
        pred = {"id": "m", "league": "eng.1",
                "markets": {"1": {"p": .7, "suggested": 1.5, "fair": 1.43}},
                "iddaa_event_id": "e", "iddaa_updated_at": current.isoformat()}
        self.assertTrue(coupons._choices(pred, {"1": 1.7}))
        pred["iddaa_updated_at"] = (current - timedelta(hours=4)).isoformat()
        self.assertEqual(coupons._choices(pred, {"1": 1.7}), [])
        pred["iddaa_updated_at"] = None
        self.assertEqual(coupons._choices(pred, {"1": 1.7}), [])


if __name__ == "__main__":
    unittest.main()
