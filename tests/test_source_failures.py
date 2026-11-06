import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app import db
from app.scrapers import espn, news, tff


class StrictSourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(db, "DATABASE_URL", None), patch.object(db, "DB_PATH", Path(tmp.name) / "test.db")):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()

    def test_espn_failure_does_not_advance_resume_checkpoint(self):
        with db.session() as conn:
            db.set_meta(conn, "espn_done_eng.1", "2026-10-01")
            db.set_meta(conn, "espn_from_eng.1", espn.HISTORY_START.isoformat())
        with patch.object(espn, "fetch_window", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError), db.session() as conn:
                espn.sync_league(conn, "eng.1", strict=True)
        with db.session() as conn:
            self.assertEqual(db.get_meta(conn, "espn_done_eng.1"), "2026-10-01")

    def test_empty_espn_response_is_success_not_network_failure(self):
        with patch.object(espn, "fetch_window", return_value={}), db.session() as conn:
            self.assertEqual(espn.sync_recent(conn, "eng.1", max_requests=1, strict=True), (0, True))

    def test_quick_espn_propagates_failed_request(self):
        with patch.object(espn, "fetch_window", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError), db.session() as conn:
                espn.sync_recent(conn, "eng.1", strict=True)


    def test_scheduled_sync_does_not_revisit_2021(self):
        from datetime import timedelta
        today = date.today()
        seen = []
        with patch.object(espn, "fetch_window", side_effect=lambda league, day, **kw: (seen.append(day) or {})), \
                patch.object(espn, "REQUEST_DELAY_SEC", 0), \
                patch.object(espn.time, "sleep"), db.session() as conn:
            count = espn.sync_league(conn, "eng.1", strict=True)
        self.assertEqual(count, 0)
        self.assertTrue(seen)
        self.assertGreaterEqual(min(seen), today - timedelta(days=7))
        self.assertLess(len(seen), 20)

    def test_espn_html_failure_uses_json_fallback(self):
        with patch.object(espn, "fetch_html", side_effect=RuntimeError("empty HTML")), \
                patch.object(espn, "fetch_api_window", return_value={"20261106": []}) as fallback:
            self.assertEqual(espn.fetch_window("eng.1", date(2026, 11, 6)),
                             {"20261106": []})
            fallback.assert_called_once()

    def test_espn_both_sources_fail_visibly(self):
        with patch.object(espn, "fetch_html", side_effect=RuntimeError("empty HTML")), \
                patch.object(espn, "fetch_api_window", side_effect=RuntimeError("JSON offline")):
            with self.assertRaisesRegex(RuntimeError, "HTML ve JSON"):
                espn.fetch_window("eng.1", date(2026, 11, 6))

    def test_tff_previous_week_failure_is_not_complete_success(self):
        with patch.object(tff, "fetch_week", side_effect=[{"week": 2, "matches": []}, RuntimeError("offline")]):
            with self.assertRaises(RuntimeError), db.session() as conn:
                tff.sync_recent(conn, "tur.2", strict=True)

    def test_news_failure_is_visible_to_updater(self):
        with db.session() as conn:
            conn.execute("INSERT INTO matches(id, league, kickoff, home, away) VALUES('m', 'eng.1', ?, 'A', 'B')",
                         (news.datetime.now(news.timezone.utc).isoformat(),))
        with patch.object(news, "fetch_team_news", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError), db.session() as conn:
                news.sync_news(conn, strict=True)
