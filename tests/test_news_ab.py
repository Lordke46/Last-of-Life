"""Haber katkısının yalnız eşleştirilmiş maç-öncesi tahminlerden ölçülmesi."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db
from app.model import snapshots


class NewsABTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(db, "DATABASE_URL", None),
                  patch.object(db, "DB_PATH", Path(tmp.name) / "test.db")):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()

    def test_paired_news_comparison_and_late_snapshot_exclusion(self):
        payload = {
            "model": {"1": .55, "X": .25, "2": .20},
            "blend": {"1": .55, "X": .25, "2": .20},
            "market": {},
            "no_news": {"1": .70, "X": .20, "2": .10},
            "pick": None, "confidence_label": "Düşük",
        }
        with db.session() as conn:
            for mid, captured in (("m1", "2026-10-05T08:00:00+00:00"),
                                  ("late", "2026-10-05T20:00:00+00:00")):
                kickoff = "2026-10-05T16:00:00+00:00"
                conn.execute(
                    "INSERT INTO matches(id,league,kickoff,home,away,completed,home_goals,away_goals) "
                    "VALUES(?, 'eng.1', ?, 'A', 'B', 1, 2, 0)", (mid, kickoff))
                conn.execute(
                    "INSERT INTO prediction_snapshots "
                    "(match_id,league,kickoff,captured_at,model_version,parameters,payload) "
                    "VALUES (?, 'eng.1', ?, ?, 'v1', '{}', ?)",
                    (mid, kickoff, captured, json.dumps(payload)),
                )
            result = snapshots.report(conn)
        ab = result["news_ab"]
        self.assertEqual(ab["comparable_matches"], 1)
        self.assertEqual(ab["with_news"]["n"], 1)
        self.assertEqual(ab["without_news"]["n"], 1)
        self.assertLess(ab["without_news"]["brier"], ab["with_news"]["brier"])


if __name__ == "__main__":
    unittest.main()
