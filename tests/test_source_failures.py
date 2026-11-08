import io
import ssl
import tempfile
import unittest
import urllib.error
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
        self.assertLessEqual(len(seen), 7 + espn.FIXTURE_DAYS_AHEAD + 1)
        self.assertEqual(len(seen), len(set(seen)))

    def test_espn_json_is_primary(self):
        with patch.object(espn, "fetch_html", side_effect=RuntimeError("empty HTML")), \
                patch.object(espn, "fetch_api_window", return_value={"20261106": []}) as fallback:
            self.assertEqual(espn.fetch_window("eng.1", date(2026, 11, 6)),
                             {"20261106": []})
            fallback.assert_called_once()

    def test_espn_both_sources_fail_visibly(self):
        with patch.object(espn, "fetch_html", side_effect=RuntimeError("empty HTML")), \
                patch.object(espn, "fetch_api_window", side_effect=RuntimeError("JSON offline")):
            with self.assertRaisesRegex(RuntimeError, "JSON ve HTML"):
                espn.fetch_window("eng.1", date(2026, 11, 6))

    def test_espn_scoreboard_url_uses_single_day(self):
        self.assertEqual(
            espn.API_URL.format(league="eng.1", day="20261103"),
            "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?dates=20261103",
        )

    def test_espn_daily_sync_does_not_skip_empty_days(self):
        from datetime import timedelta
        today = date.today()
        days = []
        with patch.object(espn, "fetch_window", side_effect=lambda league, day, **kw: (days.append(day) or {})), \
                patch.object(espn.time, "sleep"), db.session() as conn:
            espn.sync_league(conn, "eng.1", strict=True)
        self.assertEqual(days, [
            today - timedelta(days=7) + timedelta(days=i)
            for i in range(8 + espn.FIXTURE_DAYS_AHEAD)
        ])

    def test_espn_quick_checks_today_and_yesterday(self):
        from datetime import timedelta
        today = date.today()
        days = []
        with patch.object(espn, "fetch_window", side_effect=lambda league, day, **kw: (days.append(day) or {})), \
                db.session() as conn:
            self.assertEqual(espn.sync_recent(conn, "eng.1", strict=True), (0, True))
        self.assertEqual(days, [today, today - timedelta(days=1)])

    def test_espn_json_event_normalization(self):
        import io
        import json

        class Response:
            status = 200
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({"events": [{
                    "id": "123", "date": "2026-11-03T19:00Z",
                    "status": {"type": {"state": "post", "shortDetail": "FT", "completed": True}},
                    "competitions": [{"competitors": [
                        {"homeAway": "home", "team": {"id": "1", "displayName": "A"}, "score": "2"},
                        {"homeAway": "away", "team": {"id": "2", "displayName": "B"}, "score": "1"},
                    ]}],
                }]}).encode()

        with patch.object(espn.urllib.request, "urlopen", return_value=Response()) as opener:
            window = espn.fetch_api_window("eng.1", date(2026, 11, 3))
        self.assertIn("dates=20261103", opener.call_args.args[0].full_url)
        self.assertNotIn("20261103-", opener.call_args.args[0].full_url)
        self.assertIsNone(opener.call_args.kwargs.get("context"))
        self.assertEqual(espn.parse_event(window["20261103"][0], "eng.1")["home_goals"], 2)

    def test_espn_retries_transient_tls_then_succeeds(self):
        tls = urllib.error.URLError(ssl.SSLError(1, "[SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error"))
        busy = urllib.error.HTTPError(
            "https://site.api.espn.com/apis/site/v2/sports/soccer/ned.1/scoreboard?dates=20261107",
            503, "unavailable", None, io.BytesIO(b""))
        payload = b'{"events":[]}'

        class Response:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return payload

        with patch.object(espn.urllib.request, "urlopen", side_effect=[tls, busy, Response()]) as opener, \
                patch.object(espn.time, "sleep") as sleep:
            window = espn.fetch_api_window("ned.1", date(2026, 11, 7), retries=3)
        self.assertEqual(window, {})
        self.assertEqual(opener.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        for call in opener.call_args_list:
            self.assertIsNone(call.kwargs.get("context"))

    def test_espn_tls_failure_stays_visible_after_retries(self):
        tls = urllib.error.URLError(ssl.SSLError(1, "[SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error"))
        with patch.object(espn.urllib.request, "urlopen", side_effect=tls) as opener, \
                patch.object(espn.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError) as caught:
                espn.fetch_window("uefa.europa", date(2026, 11, 7), retries=3)
        self.assertEqual(opener.call_count, 6)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 2, 4])
        message = str(caught.exception)
        self.assertIn("JSON", message)
        self.assertIn("HTML", message)
        self.assertIn("3 deneme", message)
        self.assertIn("TLSV1_ALERT_INTERNAL_ERROR", message)

    def test_espn_permanent_http_error_is_not_retried(self):
        def reject(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", None, io.BytesIO(b"no"))

        with patch.object(espn.urllib.request, "urlopen", side_effect=reject) as opener, \
                patch.object(espn.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError) as caught:
                espn.fetch_window("por.1", date(2026, 11, 7), retries=3)
        self.assertEqual(opener.call_count, 2)
        sleep.assert_not_called()
        self.assertIn("400", str(caught.exception))

    def test_espn_html_retries_timeout_and_empty_body(self):
        class Response:
            def __init__(self, raw):
                self.status = 200
                self.headers = {}
                self._raw = raw

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self._raw

        with patch.object(espn.urllib.request, "urlopen", side_effect=[
            TimeoutError("timed out"), Response(b""), Response(b"<html>fixture</html>"),
        ]) as opener, patch.object(espn.time, "sleep") as sleep:
            body = espn.fetch_html("https://www.espn.com/soccer/fixtures", retries=3)
        self.assertEqual(body, "<html>fixture</html>")
        self.assertEqual(opener.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        for call in opener.call_args_list:
            self.assertIsNone(call.kwargs.get("context"))

    def test_fetch_window_forwards_retry_budget(self):
        with patch.object(espn, "fetch_api_window", return_value={}) as api:
            espn.fetch_window("eng.1", date(2026, 11, 7), timeout=12, retries=2)
        api.assert_called_once_with("eng.1", date(2026, 11, 7), timeout=12, retries=2)

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
