import json
import os
import shutil
import subprocess
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import pwa
from app.config import STATIC_DIR
from app.main import app


def asgi_get(path: str):
    """Lifespan başlatmadan gerçek ASGI istemcisiyle GET; üretim DB/güncellemesi çalışmaz."""
    client = TestClient(app, base_url="https://iddia-oran.vercel.app")
    try:
        response = client.get(path)
        return response.status_code, dict(response.headers), response.content
    finally:
        client.close()


def png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


class ManifestTest(unittest.TestCase):
    def test_served_with_manifest_content_type(self):
        status, headers, body = asgi_get("/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("application/manifest+json"))
        self.assertEqual(headers["cache-control"], "no-cache")
        self.assertEqual(json.loads(body)["name"], "Last of Life V3")

    def test_installability_fields(self):
        m = pwa.MANIFEST
        self.assertEqual((m["theme_color"], m["start_url"], m["scope"], m["display"]), ("#080808", "/", "/", "standalone"))
        self.assertLessEqual(len(m["short_name"]), 12)
        self.assertTrue(m["background_color"])

    def test_icons_exist_with_declared_size_and_purposes(self):
        seen = set()
        for icon in pwa.MANIFEST["icons"]:
            status, headers, body = asgi_get(icon["src"])
            self.assertEqual((status, headers["content-type"]), (200, "image/png"), icon["src"])
            w, h = (int(x) for x in icon["sizes"].split("x"))
            self.assertEqual(png_size(body), (w, h), icon["src"])
            seen.add((w, icon["purpose"]))
        self.assertTrue({(192, "any"), (512, "any"), (192, "maskable"), (512, "maskable")} <= seen)

    def test_apple_touch_icon_and_index_meta(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for needle in ('rel="manifest" href="/manifest.webmanifest"', 'rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png"',
                       'name="apple-mobile-web-app-capable" content="yes"', 'name="theme-color" content="#080808"',
                       '<script src="/static/pwa.js">'):
            self.assertIn(needle, html)
        status, _, body = asgi_get("/static/icons/apple-touch-icon.png")
        self.assertEqual((status, png_size(body)), (200, (180, 180)))


class ServiceWorkerTest(unittest.TestCase):
    def test_gated_site_does_not_precache_login_redirect_as_shell(self):
        with patch.dict(os.environ, {"AUTH_REQUIRED": "1"}):
            self.assertNotIn("/", pwa.precache_urls())
            self.assertIn("const AUTH_REQUIRED = true", pwa.render_sw())

    @unittest.skipUnless(shutil.which("node"), "node kurulu değil")
    def test_service_worker_privacy_and_offline_behavior(self):
        run = subprocess.run(["node", "--test", "tests/js/service_worker.test.js"],
                             cwd=STATIC_DIR.parent, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_served_from_root_with_scope_header(self):
        status, headers, body = asgi_get("/sw.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/javascript"))
        self.assertEqual((headers["cache-control"], headers["service-worker-allowed"]), ("no-cache", "/"))
        self.assertNotIn(b"__VERSION__", body)
        self.assertNotIn(b"__PRECACHE__", body)

    def test_precache_covers_shell_manifest_and_all_static_files(self):
        urls = pwa.precache_urls()
        self.assertEqual(urls[:2], ["/", "/manifest.webmanifest"])
        for must in ("/static/app.js", "/static/style.css", "/static/pwa.js", "/static/icons/icon-512.png"):
            self.assertIn(must, urls)
        self.assertNotIn("/static/index.html", urls)
        self.assertEqual(len(urls), len(set(urls)))
        for icon in pwa.MANIFEST["icons"]:
            self.assertIn(icon["src"], urls)
        self.assertIn(json.dumps(urls), pwa.render_sw())

    def test_version_changes_when_a_static_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pwa, "STATIC_DIR", Path(tmp)):
            (Path(tmp) / "index.html").write_text("<html>")
            (Path(tmp) / "app.js").write_text("a")
            before = pwa.cache_version()
            self.assertEqual(before, pwa.cache_version())
            (Path(tmp) / "app.js").write_text("b")
            self.assertNotEqual(before, pwa.cache_version())

    def test_api_updates_are_never_cached_and_static_is_cache_first(self):
        sw = pwa.render_sw()
        self.assertIn("NO_CACHE_API", sw)
        self.assertRegex(sw, r"const STATIC_CACHE = `static-\$\{VERSION\}`")
        self.assertRegex(sw, r'const VERSION = "[0-9a-f]{12}"')


if __name__ == "__main__":
    unittest.main()
