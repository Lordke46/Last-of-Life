import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import db, quote_provenance
from app.bulletin_refresh import ATTEMPT_KEY, refresh_if_stale
from app.db import get_meta, set_meta


class BulletinRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for item in (patch.object(db, "DB_PATH", Path(self.tmp.name) / "refresh.db"),
                     patch.object(db, "DATABASE_URL", None)):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.tmp.cleanup)
        db.init_db()
        postgres = patch.object(db, "is_postgres", return_value=True)
        postgres.start()
        self.addCleanup(postgres.stop)

    def seed_old_prices(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with db.session() as conn:
            set_meta(conn, "iddaa_last_update", old)
            conn.execute("INSERT INTO iddaa_events(id,league,competition_id,kickoff,home,away,updated_at) "
                         "VALUES('e1','small:test:league',123,'2099-01-01T00:00:00Z','H','A',?)", (old,))
            conn.execute("INSERT INTO iddaa_odds(event_id,market,odd) VALUES('e1','1',1.9)")

    def test_stale_prices_trigger_bounded_refresh_and_report_only_after_commit(self):
        self.seed_old_prices()

        def successful(conn, **kwargs):
            self.assertEqual(kwargs, {"retries": 1, "transport_timeout": 4,
                                      "detail_budget_seconds": 2, "catalog_timeout": 2})
            conn.execute("UPDATE iddaa_odds SET odd=1.8 WHERE event_id='e1' AND market='1'")
            set_meta(conn, "iddaa_last_update", datetime.now(timezone.utc).isoformat())
            return {"supported_events": 1}

        with patch("app.updater.iddaa.sync_odds", side_effect=successful) as sync:
            result = refresh_if_stale()
        self.assertEqual(result["status"], "refreshed")
        self.assertEqual(sync.call_count, 1)
        with db.session() as conn:
            self.assertEqual(conn.execute("SELECT odd FROM iddaa_odds WHERE event_id='e1'").fetchone()["odd"], 1.8)
            self.assertTrue(get_meta(conn, "iddaa_last_update"))

    def test_recent_prices_do_not_fetch(self):
        observed = datetime.now(timezone.utc).isoformat()
        with db.session() as conn:
            set_meta(conn, "iddaa_last_update", datetime.now(timezone.utc).isoformat())
            set_meta(conn, quote_provenance.KEY + ":e1", json.dumps({"quotes": {
                "1": {"odd": 1.9, "cache_status": "network_no_cache_evidence",
                      "quote_observed_at": observed}}}))
            set_meta(conn, quote_provenance.KEY, json.dumps({"last_fetch_status": "success"}))
            conn.execute("INSERT INTO iddaa_events(id,league,competition_id,kickoff,home,away,updated_at) "
                         "VALUES('e1','small:test:league',123,'2099-01-01T00:00:00Z','H','A','now')")
            conn.execute("INSERT INTO iddaa_odds(event_id,market,odd) VALUES('e1','1',1.9)")
        with patch("app.updater.iddaa.sync_odds") as sync:
            result = refresh_if_stale()
        self.assertEqual(result["status"], "fresh")
        sync.assert_not_called()

    def test_local_database_skips_readthrough(self):
        with patch.object(db, "is_postgres", return_value=False), \
             patch("app.updater.iddaa.sync_odds") as sync:
            result = refresh_if_stale()
        self.assertEqual(result["status"], "skipped")
        sync.assert_not_called()

    def test_failed_refresh_preserves_last_good_prices_and_throttles_retry(self):
        self.seed_old_prices()
        def mutate_then_fail(conn, **kwargs):
            conn.execute("UPDATE iddaa_odds SET odd=8.8 WHERE event_id='e1'")
            raise RuntimeError("provider unavailable")

        with patch("app.updater.iddaa.sync_odds", side_effect=mutate_then_fail) as sync:
            failed = refresh_if_stale()
            throttled = refresh_if_stale()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(throttled["status"], "throttled")
        self.assertEqual(sync.call_count, 1)
        with db.session() as conn:
            self.assertEqual(conn.execute("SELECT odd FROM iddaa_odds WHERE event_id='e1'").fetchone()["odd"], 1.9)
            self.assertIn("provider unavailable", get_meta(conn, "iddaa_last_error"))
            self.assertTrue(get_meta(conn, ATTEMPT_KEY))
            state = json.loads(get_meta(conn, quote_provenance.KEY))
            self.assertEqual(state["last_fetch_status"], "failed")

    def test_active_updater_lock_returns_busy_without_fetching(self):
        with db.session() as conn:
            set_meta(conn, "iddaa_last_update", (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat())
            set_meta(conn, "update_lock", (datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat())
            set_meta(conn, "update_lock_owner", "cron-token")
        with patch("app.updater.iddaa.sync_odds") as sync:
            result = refresh_if_stale()
        self.assertEqual(result["status"], "busy")
        sync.assert_not_called()

    def test_recent_timestamp_without_quote_provenance_still_requires_observation(self):
        self.seed_old_prices()
        with db.session() as conn:set_meta(conn,'iddaa_last_update',datetime.now(timezone.utc).isoformat())
        with patch('app.updater.iddaa.sync_odds',side_effect=RuntimeError('offline')) as sync:
            result=refresh_if_stale()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(sync.call_count,1)

    def test_replaced_lease_cannot_overwrite_another_workers_success(self):
        self.seed_old_prices()
        from app import updater
        from contextlib import contextmanager
        @contextmanager
        def replaced(*args):
            with db.session() as conn:
                set_meta(conn,'update_lock_owner','replacement')
                set_meta(conn,'iddaa_last_error','')
            raise updater.LostLease('ownership changed')
            yield
        with patch.object(updater,'leased_session',replaced):result=refresh_if_stale()
        self.assertEqual(result['status'],'busy')
        with db.session() as conn:self.assertEqual(get_meta(conn,'iddaa_last_error'),'')


if __name__ == "__main__":
    unittest.main()
