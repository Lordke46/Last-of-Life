import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, main, ops


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(db, "DATABASE_URL", None),
                        patch.object(db, "DB_PATH", Path(self.tmp.name) / "t.db")]
        for p in self.patches:
            p.start()
        db.init_db()
        ops._cache.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_rate_limit_blocks_after_limit(self):
        results = [ops.rate_limited("k", 3) for _ in range(5)]
        self.assertEqual(results, [False, False, False, True, True])
        self.assertFalse(ops.rate_limited("other", 3))

    def test_cache_reuses_until_version_changes(self):
        calls = []
        build = lambda: calls.append(1) or len(calls)
        self.assertEqual(ops.cached(("a",), build), 1)
        self.assertEqual(ops.cached(("a",), build), 1)
        with db.session() as conn:
            db.set_meta(conn, "last_updated_at", "2026-01-01T00:00:00+00:00")
        self.assertEqual(ops.cached(("a",), build), 2)

    def test_health_reports_stale_data(self):
        self.assertFalse(ops.health()["ok"])
        with db.session() as conn:
            now = ops.datetime.now(ops.timezone.utc).isoformat()
            db.set_meta(conn, "last_updated_at", now)
            db.set_meta(conn, "iddaa_last_update", now)
        self.assertTrue(ops.health()["ok"])

    def test_health_does_not_hide_failed_source_behind_recent_global_stamp(self):
        import json
        with db.session() as conn:
            now = ops.datetime.now(ops.timezone.utc).isoformat()
            db.set_meta(conn, "last_updated_at", now)
            db.set_meta(conn, "iddaa_last_update", now)
            db.set_meta(conn, "update_sources", json.dumps({"eng.1": {"status": "error", "last_success_at": now}}))
        self.assertFalse(ops.health()["ok"])
        self.assertIn("source_stale:eng.1", ops.health()["problems"])

    def test_health_endpoint_status_and_cdn_header(self):
        client = TestClient(main.app)
        stale = client.get("/api/health")
        self.assertEqual(stale.status_code, 200)
        self.assertTrue(stale.json()["db"])
        self.assertFalse(stale.json()["ok"])
        self.assertIn("update_stale", stale.json()["problems"])
        for header in ("cache-control", "cdn-cache-control", "vercel-cdn-cache-control"):
            self.assertEqual(stale.headers[header], "no-store")
        with patch.object(ops, "session", side_effect=OSError("connection refused")):
            down = client.get("/api/health")
        self.assertEqual(down.status_code, 503)
        self.assertFalse(down.json()["db"])
        self.assertIn("db", down.json()["problems"])
        self.assertEqual(down.headers["cache-control"], "no-store")
        for path in ("/api/leagues", "/api/update/status"):
            headers = client.get(path).headers
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertEqual(headers["cdn-cache-control"], "no-store")
            self.assertEqual(headers["vercel-cdn-cache-control"], "no-store")
