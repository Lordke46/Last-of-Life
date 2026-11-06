"""P0: bozuk skor, yanlış lig ve çoklu iddaa eşleşmesi regresyonları."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import db, data_quality
from app.scrapers import espn


class DataQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for change in (patch.object(db, "DATABASE_URL", None),
                       patch.object(db, "DB_PATH", Path(self.tmp.name) / "audit.db")):
            change.start()
            self.addCleanup(change.stop)
        db.init_db()
        self.now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
        self.kickoff = (self.now + timedelta(hours=3)).isoformat()

    def fixture(self, *, completed=0, hg=None, ag=None):
        with db.session() as conn:
            conn.execute(
                "INSERT INTO matches(id,league,kickoff,home,away,completed,home_goals,away_goals) "
                "VALUES ('m1','eng.1',?,'Manchester City','Wolves',?,?,?)",
                (self.kickoff, completed, hg, ag),
            )

    def link(self, event_id, league="eng.1", home="Man City", odd=1.75):
        with db.session() as conn:
            conn.execute(
                "INSERT INTO iddaa_events "
                "(id,league,competition_id,kickoff,home,away,match_id,updated_at) "
                "VALUES (?,?,43,? ,?,'Wolves','m1',?)",
                (event_id, league, self.kickoff, home, self.now.isoformat()),
            )
            conn.execute("INSERT INTO iddaa_odds(event_id,market,odd) VALUES(?,'1',?)",
                         (event_id, odd))

    def audit(self):
        with db.session() as conn:
            return data_quality.audit(conn, self.now)

    def test_valid_fixture_and_iddaa_link_pass(self):
        self.fixture()
        self.link("e1")
        result = self.audit()
        self.assertTrue(result["healthy"])
        self.assertEqual(result["linked_iddaa_events"], 1)

    def test_duplicate_iddaa_mapping_is_visible(self):
        self.fixture()
        self.link("e1")
        self.link("e2")
        result = self.audit()
        self.assertFalse(result["healthy"])
        self.assertIn("duplicate_iddaa_link", [p["type"] for p in result["examples"]])

    def test_wrong_league_or_score_is_flagged(self):
        self.fixture(completed=1, hg=None, ag=0)
        self.link("e1", league="ita.1")
        result = self.audit()
        self.assertGreaterEqual(result["hard_error_count"], 2)

    def test_bad_odds_are_flagged(self):
        self.fixture()
        self.link("e1", odd=0.95)
        self.assertIn("invalid_odds", [p["type"] for p in self.audit()["examples"]])

    def test_final_without_goals_stays_incomplete(self):
        event = {"id": "321", "date": self.kickoff,
                 "status": {"state": "post", "detail": "FT"},
                 "completed": True,
                 "competitors": [
                     {"isHome": True, "displayName": "A", "id": "1", "score": None},
                     {"isHome": False, "displayName": "B", "id": "2", "score": 1},
                 ]}
        parsed = espn.parse_event(event, "eng.1")
        self.assertEqual(parsed["completed"], 0)
        self.assertIsNone(parsed["home_goals"])
        self.assertIsNone(parsed["away_goals"])


if __name__ == "__main__":
    unittest.main()
