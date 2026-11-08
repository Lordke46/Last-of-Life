import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import db, updater
from app.db import Connection, get_meta, set_meta


def iso(delta_minutes=0):
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat(timespec="seconds")


class FakeRaw:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


class PlaceholderTests(unittest.TestCase):
    def test_postgres_translation(self):
        raw = FakeRaw()
        conn = Connection(raw, postgres=True)
        conn.execute("SELECT * FROM t WHERE a=? AND b LIKE '50%' AND c=?", (1, 2))
        conn.execute("INSERT INTO t(a, kick) VALUES(:a, '14:00Z') ON CONFLICT(a) DO UPDATE SET b=:b::text", {"a": 1, "b": 2})
        conn.execute("SELECT 1")
        self.assertEqual(raw.calls[0][0], "SELECT * FROM t WHERE a=%s AND b LIKE '50%%' AND c=%s")
        self.assertEqual(raw.calls[1][0],
                         "INSERT INTO t(a, kick) VALUES(%(a)s, '14:00Z') ON CONFLICT(a) DO UPDATE SET b=%(b)s::text")
        self.assertEqual(raw.calls[2], ("SELECT 1", None))


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(db, "DB_PATH", Path(self.tmp.name) / "t.db"), patch.object(db, "DATABASE_URL", None)):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()

    def meta(self, key):
        with db.session() as conn:
            return get_meta(conn, key)

    def set(self, key, value):
        with db.session() as conn:
            set_meta(conn, key, value)

    def test_lock_is_exclusive_and_expires(self):
        with db.session() as conn:
            self.assertTrue(updater.acquire_lock(conn, 3))
        with db.session() as conn:
            self.assertFalse(updater.acquire_lock(conn, 3))
            set_meta(conn, "update_lock", iso(-1))  # süresi geçmiş kilit
        with db.session() as conn:
            token = updater.acquire_lock(conn, 3)
            self.assertTrue(token)
            updater.release_lock(conn, token)
        with db.session() as conn:
            self.assertTrue(updater.acquire_lock(conn, 3))

    def test_cooldown_left(self):
        self.assertEqual(updater.cooldown_left(None), 0)
        self.assertEqual(updater.cooldown_left(iso(-11)), 0)
        self.assertGreater(updater.cooldown_left(iso(-1)), 8 * 60)

    def test_quick_update_sets_shared_timestamp_then_cools_down(self):
        with patch.object(updater.espn, "sync_recent", return_value=(5, True)) as espn_sync, \
                patch.object(updater.tff, "sync_recent", return_value=(5, True)) as tff_sync, \
                patch.object(updater.iddaa, "sync_odds", return_value={}) as odds_sync:
            first = updater.run_quick_update()
            second = updater.run_quick_update()
        self.assertEqual(first["status"], "updated")
        self.assertIsNotNone(first["last_updated_at"])
        self.assertFalse(first["running"])
        self.assertEqual(second["status"], "cooldown")
        self.assertEqual(second["last_updated_at"], first["last_updated_at"])
        tff_leagues = [lg for lg, v in updater.LEAGUES.items() if v.get("source") == "tff"]
        self.assertEqual(tff_sync.call_count, len(tff_leagues))
        self.assertEqual(espn_sync.call_count, len(updater.LEAGUES) - len(tff_leagues))
        self.assertEqual(odds_sync.call_count, 1)  # ikinci çağrı kaynaklara gitmedi
        self.assertEqual(updater.status()["last_updated_at"], first["last_updated_at"])

    def test_iddaa_source_count_uses_bulletin_size(self):
        with patch.object(updater.espn, "sync_recent", return_value=(1, True)), \
                patch.object(updater.tff, "sync_recent", return_value=(1, True)), \
                patch.object(updater.iddaa, "sync_odds", return_value={"bulletin_events": 42, "odds": 100}):
            result = updater.run_quick_update()
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["sources"]["iddaa"], {**result["sources"]["iddaa"], "status": "success", "count": 42})
        self.assertEqual(result["sources"]["iddaa"]["count"], 42)

    def test_quick_exit_code_fails_when_the_lock_is_busy(self):
        self.assertEqual(updater.quick_exit_code("updated"), 0)
        self.assertEqual(updater.quick_exit_code("cooldown"), 0)
        self.assertEqual(updater.quick_exit_code("busy"), 1)
        self.assertEqual(updater.quick_exit_code("partial"), 1)
        self.assertEqual(updater.quick_exit_code("error"), 1)

    def test_quick_update_busy_when_locked(self):
        self.set("update_lock", iso(2))
        with patch.object(updater.espn, "sync_recent") as espn_sync:
            result = updater.run_quick_update()
        self.assertEqual(result["status"], "busy")
        self.assertTrue(result["running"])
        espn_sync.assert_not_called()

    def test_quick_update_error_does_not_stamp_or_keep_lock(self):
        with patch.object(updater.espn, "sync_recent", return_value=(0, False)), \
                patch.object(updater.tff, "sync_recent", return_value=(0, False)), \
                patch.object(updater.iddaa, "sync_odds", side_effect=RuntimeError("down")):
            result = updater.run_quick_update()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(self.meta("last_updated_at"))
        self.assertFalse(result["running"])
        self.assertEqual(result["cooldown_seconds"], 0)  # hata sonrası hemen tekrar denenebilir

    def test_iddaa_failure_keeps_recent_odds_but_wipes_stale(self):
        with db.session() as conn:
            conn.execute("INSERT INTO iddaa_events(id, league, competition_id, kickoff, home, away, updated_at) "
                         "VALUES('e1','eng.1',43,'2026-10-17T14:00Z','A','B','x')")
            conn.execute("INSERT INTO iddaa_odds(event_id, market, odd) VALUES('e1','1',1.5)")
            set_meta(conn, "iddaa_last_update", iso(-30))
        with patch.object(updater.iddaa, "sync_odds", side_effect=RuntimeError("down")), db.session() as conn:
            self.assertEqual(updater._sync_iddaa(conn), (False, {}))
        self.assertEqual(self.count("iddaa_odds"), 1)
        self.set("iddaa_last_update", iso(-60 * 7))
        with patch.object(updater.iddaa, "sync_odds", side_effect=RuntimeError("down")), db.session() as conn:
            self.assertEqual(updater._sync_iddaa(conn), (False, {}))
        self.assertEqual(self.count("iddaa_odds"), 0)

    def count(self, table):
        with db.session() as conn:
            return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]

    def test_full_update_skips_when_locked_and_stamps_when_done(self):
        self.set("update_lock", iso(5))
        self.assertEqual(updater.run_update(with_news=False), "skipped")
        self.set("update_lock", "")
        with patch.object(updater.espn, "sync_league", return_value=3), \
                patch.object(updater.tff, "sync_league", return_value=3), \
                patch.object(updater.iddaa, "sync_odds", return_value={}):
            self.assertEqual(updater.run_update(with_news=False), "ok")
        self.assertIsNotNone(self.meta("last_updated_at"))
        self.assertEqual(self.meta("update_lock"), "")

    def test_quick_update_stays_within_serverless_budget(self):
        """Web isteği yalnız hızlı güncellemeyi çalıştırır; en kötü durum süresi 300 sn'nin altında kalır."""
        full_called = []
        with patch.object(updater.espn, "sync_league", side_effect=lambda *a, **k: full_called.append(a)), \
                patch.object(updater.tff, "sync_league", side_effect=lambda *a, **k: full_called.append(a)), \
                patch.object(updater.espn, "sync_recent", return_value=(1, True)), \
                patch.object(updater.tff, "sync_recent", return_value=(1, True)), \
                patch.object(updater.iddaa, "sync_odds", return_value={}):
            updater.run_quick_update()
        self.assertEqual(full_called, [])  # ağır tam çekim hiçbir zaman istek içinde çalışmaz
        worst_iddaa = 2 * 25 + 1.5  # iki deneme x zaman aşımı + bekleme (iddaa.fetch_json)
        self.assertLess(updater.QUICK_LEAGUES_DEADLINE_SEC + worst_iddaa + 30, 300)

    def test_quick_update_skips_slow_league_after_deadline(self):
        import time
        calls = []

        def slow(conn, league, **kw):
            calls.append(league)
            if league == "tur.1":
                time.sleep(0.6)
            return (1, True)

        with patch.object(updater, "QUICK_LEAGUES_DEADLINE_SEC", 0.2), \
                patch.object(updater.espn, "sync_recent", side_effect=slow), \
                patch.object(updater.tff, "sync_recent", return_value=(1, True)), \
                patch.object(updater.iddaa, "sync_odds", return_value={}):
            result = updater.run_quick_update()
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["last_updated_at"])
        self.assertEqual(result["sources"]["tur.1"]["status"], "error")

    def test_full_update_cannot_succeed_when_every_source_fails(self):
        with patch.object(updater.espn, "sync_league", side_effect=RuntimeError("offline")), \
                patch.object(updater.tff, "sync_league", side_effect=RuntimeError("offline")), \
                patch.object(updater.iddaa, "sync_odds", side_effect=RuntimeError("offline")), \
                patch.object(updater, "_track_coupons"):
            self.assertEqual(updater.run_update(with_news=False), "error")
        self.assertIsNone(self.meta("last_updated_at"))
        self.assertEqual(updater.status()["outcome"], "failed")
        self.assertFalse(updater.status()["running"])

    def test_partial_update_retains_previous_success_time_and_reports_source(self):
        old = iso(-60)
        self.set("last_updated_at", old)
        with patch.object(updater.espn, "sync_league", return_value=0), \
                patch.object(updater.tff, "sync_league", return_value=0), \
                patch.object(updater.iddaa, "sync_odds", side_effect=RuntimeError("offline")), \
                patch.object(updater, "_track_coupons"):
            self.assertEqual(updater.run_update(with_news=False), "error")
        state = updater.status()
        self.assertEqual(state["outcome"], "partial")
        self.assertEqual(state["last_updated_at"], old)
        self.assertIsNotNone(state["sources"]["eng.1"]["last_success_at"])
        self.assertIsNone(state["sources"]["iddaa"]["last_success_at"])
        self.assertIsNotNone(self.meta("update_data_version"))

    def test_source_recovers_without_losing_history_on_failure(self):
        with patch.object(updater.espn, "sync_recent", return_value=(0, True)), \
                patch.object(updater.tff, "sync_recent", return_value=(0, True)), \
                patch.object(updater.iddaa, "sync_odds", return_value={}), \
                patch.object(updater, "_track_coupons"):
            self.assertEqual(updater.run_quick_update()["status"], "updated")
        last = updater.status()["sources"]["eng.1"]["last_success_at"]
        self.set("last_updated_at", iso(-60))
        with patch.object(updater.espn, "sync_recent", side_effect=RuntimeError("offline")), \
                patch.object(updater.tff, "sync_recent", return_value=(0, True)), \
                patch.object(updater.iddaa, "sync_odds", return_value={}), \
                patch.object(updater, "_track_coupons"):
            state = updater.run_quick_update()
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["sources"]["eng.1"]["last_success_at"], last)


if __name__ == "__main__":
    unittest.main()
