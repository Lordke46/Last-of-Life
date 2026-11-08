import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.health_gate import judge


def _body(**overrides):
    payload = {"ok": True, "db": True, "problems": []}
    payload.update(overrides)
    return json.dumps(payload)


class HealthGateTests(unittest.TestCase):
    def test_healthy_response_passes(self):
        verdict = judge(0, "200", _body())
        self.assertEqual(verdict.level, "ok")
        self.assertIn("sağlıklı", verdict.message)

    def test_stale_200_warns(self):
        verdict = judge(0, "200", _body(ok=False, problems=["update_stale", "source_stale:eng.1"]))
        self.assertEqual(verdict.level, "warning")
        self.assertIn("::warning::Veri tazeliği sorunu: update_stale, source_stale:eng.1", verdict.message)

    def test_legacy_stale_503_warns(self):
        verdict = judge(0, "503", _body(ok=False, problems=["iddaa_stale", "source_stale:uefa.europa"]))
        self.assertEqual(verdict.level, "warning")

    def test_curl_fail_exit_still_warns_for_stale_body(self):
        verdict = judge(22, "503", _body(ok=False, problems=["update_stale"]))
        self.assertEqual(verdict.level, "warning")

    def test_database_down_fails(self):
        for status in ("503", "200"):
            verdict = judge(0, status, _body(ok=False, db=False, problems=["db"]))
            self.assertEqual(verdict.level, "error", status)
            self.assertIn("veritabanı", verdict.message)

    def test_transport_failure_ignores_stale_body(self):
        verdict = judge(7, "000", _body(ok=False, problems=["update_stale"]))
        self.assertEqual(verdict.level, "error")
        self.assertIn("curl çıkış 7", verdict.message)
        self.assertNotIn("::warning::", verdict.message)

    def test_tls_transport_failure(self):
        verdict = judge(35, "000", "")
        self.assertEqual(verdict.level, "error")
        self.assertIn("curl çıkış 35", verdict.message)

    def test_unexpected_http_status_fails_even_if_body_looks_stale(self):
        verdict = judge(0, "500", _body(ok=False, problems=["update_stale"]))
        self.assertEqual(verdict.level, "error")
        self.assertIn("HTTP 500", verdict.message)
        self.assertNotIn("::warning::", verdict.message)

    def test_non_json_body_fails(self):
        verdict = judge(0, "200", "<html>bad gateway</html>")
        self.assertEqual(verdict.level, "error")
        self.assertIn("bad gateway", verdict.message)

    def test_unknown_problem_fails(self):
        verdict = judge(0, "200", _body(ok=False, problems=["disk_full"]))
        self.assertEqual(verdict.level, "error")

    def test_empty_problem_list_with_ok_false_fails(self):
        verdict = judge(0, "200", _body(ok=False, problems=[]))
        self.assertEqual(verdict.level, "error")

    def test_ok_true_on_503_fails(self):
        verdict = judge(0, "503", _body())
        self.assertEqual(verdict.level, "error")

    def test_missing_fields_fail(self):
        verdict = judge(0, "200", json.dumps({"status": "ok"}))
        self.assertEqual(verdict.level, "error")

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            path.write_text(_body(ok=False, problems=["iddaa_stale"]), encoding="utf-8")
            warn = subprocess.run(
                [sys.executable, "-m", "app.health_gate", "--curl-exit", "0",
                 "--http-status", "200", "--body", str(path)],
                capture_output=True, text=True, check=False)
            self.assertEqual(warn.returncode, 0, warn.stderr)
            self.assertIn("::warning::", warn.stdout)
            path.write_text("nope", encoding="utf-8")
            bad = subprocess.run(
                [sys.executable, "-m", "app.health_gate", "--curl-exit", "0",
                 "--http-status", "404", "--body", str(path)],
                capture_output=True, text=True, check=False)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("::error::", bad.stdout)
            self.assertIn("HTTP 404", bad.stdout)
            missing = subprocess.run(
                [sys.executable, "-m", "app.health_gate", "--curl-exit", "28",
                 "--http-status", "000", "--body", str(Path(tmp) / "missing.json")],
                capture_output=True, text=True, check=False)
            self.assertEqual(missing.returncode, 1)
            self.assertIn("curl çıkış 28", missing.stdout)

    def test_health_workflow_reads_status_and_body(self):
        text = Path(".github/workflows/health.yml").read_text(encoding="utf-8")
        self.assertIn("app.health_gate", text)
        self.assertIn("http_status=", text)
        self.assertIn("response body", text)
        self.assertIn("lastoflife.vercel.app/api/health", text)
        self.assertNotIn("--fail", text)
