"""Kalibrasyonun yalnız maç öncesi snapshot kullandığını doğrular."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import db
from app.model import calibration


class CalibrationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(db, "DATABASE_URL", None),
                  patch.object(db, "DB_PATH", Path(tmp.name) / "test.db")):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()

    def store(self, match_id, captured="2026-11-01T08:00:00+00:00",
              kickoff="2026-11-01T18:00:00+00:00", probability=.65, goals=(1, 0)):
        with db.session() as conn:
            conn.execute(
                "INSERT INTO matches(id, league, kickoff, home, away, home_goals, away_goals, completed) "
                "VALUES(?, 'eng.1', ?, 'A', 'B', ?, ?, 1)",
                (match_id, kickoff, *goals),
            )
            conn.execute(
                "INSERT INTO prediction_snapshots "
                "(match_id,league,kickoff,captured_at,model_version,parameters,payload) "
                "VALUES (?, 'eng.1', ?, ?, 'v1', '{}', ?)",
                (match_id, kickoff, captured, json.dumps({"blend": {"1": probability}})),
            )

    def test_only_pre_match_completed_records_are_used(self):
        self.store("valid")
        self.store("late", captured="2026-11-01T19:00:00+00:00")
        self.store("future", kickoff="2026-12-01T18:00:00+00:00")
        with db.session() as conn:
            stats = calibration.load(conn, now=datetime(2026, 11, 6, tzinfo=timezone.utc))
        self.assertEqual(stats[("eng.1", "1", 6)]["n"], 1)

    def test_insufficient_samples_do_not_fabricate_adjustment(self):
        sample = {("eng.1", "1", 6): {"n": 12, "wins": 11, "predicted_sum": 7.8}}
        result = calibration.calibrate(.65, "eng.1", "1", sample)
        self.assertEqual(result["p"], .65)
        self.assertFalse(result["applied"])

    def test_sufficient_samples_receive_bounded_adjustment(self):
        sample = {("eng.1", "1", 6): {"n": 40, "wins": 20, "predicted_sum": 26}}
        result = calibration.calibrate(.65, "eng.1", "1", sample)
        self.assertTrue(result["applied"])
        self.assertEqual(result["n"], 40)
        self.assertGreater(result["p"], .57)
        self.assertLess(result["p"], .65)


if __name__ == "__main__":
    unittest.main()
