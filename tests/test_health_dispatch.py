import json
import tempfile
import unittest
from pathlib import Path

from app.health_dispatch import dispatch_quick, main, should_refresh


class HealthDispatchTests(unittest.TestCase):
    def test_only_odds_and_update_staleness_request_a_refresh(self):
        self.assertTrue(should_refresh(["iddaa_stale"]))
        self.assertTrue(should_refresh(["update_stale", "source_stale:eng.1"]))
        self.assertFalse(should_refresh(["source_stale:news"]))
        self.assertFalse(should_refresh([]))
        self.assertFalse(should_refresh(None))

    def test_cli_stays_quiet_when_data_is_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            path.write_text(json.dumps({"ok": True, "problems": []}), encoding="utf-8")
            self.assertEqual(main(["--body", str(path), "--dispatch"]), 0)

    def test_dispatch_skips_when_update_is_already_queued(self):
        calls = []

        def opener(url, token, data=None, method=None):
            calls.append(url)
            if "update.yml" in url and "status=queued" in url:
                return 200, json.dumps({"workflow_runs": [{"id": 9}]}).encode()
            return 200, json.dumps({"workflow_runs": []}).encode()

        self.assertEqual(dispatch_quick("acme/app", "token", opener), "skipped:update.yml:queued")
        self.assertFalse(any(url.endswith("/dispatches") for url in calls))

    def test_dispatch_posts_quick_workflow_when_idle(self):
        posts = []

        def opener(url, token, data=None, method=None):
            if url.endswith("/dispatches"):
                posts.append(json.loads(data.decode()))
                return 204, b""
            return 200, json.dumps({"workflow_runs": []}).encode()

        self.assertEqual(dispatch_quick("acme/app", "token", opener), "dispatched")
        self.assertEqual(posts, [{"ref": "main"}])
