import contextlib
import io
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth, db, ops
from app.main import app

PW = "correct-horse-battery"
NEW_PW = "yeni-sifre-12345"


def _iso(dt):
    return auth._iso(dt)


class AuthTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        for p in (patch.object(db, "DATABASE_URL", None), patch.object(db, "DB_PATH", self.db_path),
                  patch.dict("os.environ", {"AUTH_REQUIRED": "", "AI_FEATURE_ENABLED": "", "SMTP_HOST": ""})):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)
        db.init_db()
        ops._cache.clear()
        # Secure çerezler yalnızca https üzerinden geri gönderilir; tarayıcıyı taklit için https.
        self.client = TestClient(app, base_url="https://testserver")
        self.addCleanup(self.client.close)

    def raw(self, sql, args=()):
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur.fetchall()
        finally:
            conn.close()

    def register(self, email="user@example.com", password=PW):
        return self.client.post("/api/auth/register", json={"email": email, "password": password})

    def login(self, email, password=PW):
        return self.client.post("/api/auth/login", json={"email": email, "password": password})

    def make_admin(self, email="admin@example.com"):
        auth.create_user(email, PW, role="admin")
        return self.login(email)

    def reset_token(self, email="user@example.com"):
        with patch.object(auth, "send_reset_email") as send:
            r = self.client.post("/api/auth/forgot", json={"email": email})
        self.assertEqual(r.status_code, 200)
        if not send.called:
            return None
        return send.call_args.args[1].split("reset=", 1)[1]



class RegisterLoginTest(AuthTestBase):
    def test_register_logs_in_and_sets_secure_cookie(self):
        r = self.register()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"]["role"], "user")
        cookie = r.headers["set-cookie"].lower()
        for flag in ("httponly", "secure", "samesite=strict"):
            self.assertIn(flag, cookie)
        self.assertEqual(self.client.get("/api/auth/me").json()["user"]["email"], "user@example.com")

    def test_password_is_argon2id(self):
        self.register()
        (stored,) = self.raw("SELECT password_hash FROM users")[0]
        self.assertTrue(stored.startswith("$argon2id$"))
        self.assertNotIn(PW, stored)

    def test_session_token_stored_only_as_digest(self):
        self.register()
        token = self.client.cookies.get(auth.SESSION_COOKIE)
        self.assertEqual([r[0] for r in self.raw("SELECT token_hash FROM sessions")], [auth._digest(token)])

    def test_duplicate_email_rejected_case_insensitively(self):
        self.register("dup@example.com")
        self.assertEqual(self.register("DUP@example.com").status_code, 409)

    def test_weak_password_and_bad_email_rejected(self):
        self.assertEqual(self.register(password="kisa").status_code, 400)
        self.assertEqual(self.register(email="not-an-email").status_code, 400)

    def test_login_wrong_password_and_unknown_user_look_the_same(self):
        self.register()
        wrong = self.login("user@example.com", "yanlis-sifre-1")
        unknown = self.login("yok@example.com", "yanlis-sifre-1")
        self.assertEqual((wrong.status_code, unknown.status_code), (401, 401))
        self.assertEqual(wrong.json(), unknown.json())

    def test_logout_invalidates_session(self):
        self.register()
        self.client.post("/api/auth/logout")
        self.assertIsNone(self.client.get("/api/auth/me").json()["user"])
        self.assertEqual(self.raw("SELECT COUNT(*) FROM sessions")[0][0], 0)

    def test_expired_session_rejected_and_removed(self):
        self.register()
        self.raw("UPDATE sessions SET expires_at=?", (_iso(datetime.now(timezone.utc) - timedelta(days=1)),))
        self.assertIsNone(self.client.get("/api/auth/me").json()["user"])
        self.assertEqual(self.raw("SELECT COUNT(*) FROM sessions")[0][0], 0)

    def test_roles_restricted_by_db(self):
        self.register()
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw("UPDATE users SET role='superuser'")


class GatingTest(AuthTestBase):
    def test_admin_actions_forbidden_for_anonymous_and_user(self):
        for path in ("/api/update", "/api/backtest"):
            self.assertEqual(self.client.post(path).status_code, 401, path)
        self.register()
        for path in ("/api/update", "/api/backtest"):
            self.assertEqual(self.client.post(path).status_code, 403, path)

    def test_admin_passes_admin_gate(self):
        self.make_admin()
        with patch("app.main.updater") as updater:
            updater.run_quick_update.return_value = {"ok": True}
            self.assertEqual(self.client.post("/api/update").status_code, 200)

    def test_pages_open_by_default(self):
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 200)
        self.assertEqual(self.client.get("/api/leagues").status_code, 200)

    def test_auth_required_gates_pages_and_data_only(self):
        with patch.dict("os.environ", {"AUTH_REQUIRED": "1"}):
            r = self.client.get("/", follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (302, "/giris"))
            for url in ("/api/leagues", "/api/picks", "/api/live", "/api/value-bets", "/api/update/status"):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 401, url)
                self.assertIn("no-store", r.headers["cache-control"])
            # herkese açık kalanlar
            for url in ("/api/health", "/robots.txt", "/sitemap.xml", "/giris", "/static/auth.js", "/manifest.webmanifest"):
                self.assertNotEqual(self.client.get(url, follow_redirects=False).status_code, 401, url)
                self.assertNotEqual(self.client.get(url, follow_redirects=False).status_code, 302, url)
            self.assertEqual(self.client.get("/mac/yok-1").status_code, 404)  # SEO yolu kapıya takılmaz (401 değil)
            self.register()
            self.assertEqual(self.client.get("/api/leagues").status_code, 200)
            self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 200)
            self.assertNotIn("s-maxage", self.client.get("/api/leagues").headers["cache-control"])


class PasswordResetTest(AuthTestBase):
    def test_reset_flow_single_use_and_kills_sessions(self):
        self.register()
        token = self.reset_token()
        self.assertIsNotNone(token)
        self.assertEqual(self.client.post("/api/auth/reset", json={"token": token, "password": NEW_PW}).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/reset", json={"token": token, "password": NEW_PW}).status_code, 400)
        self.assertIsNone(self.client.get("/api/auth/me").json()["user"])
        self.assertEqual(self.login("user@example.com", PW).status_code, 401)
        self.assertEqual(self.login("user@example.com", NEW_PW).status_code, 200)

    def test_reset_kills_every_session_of_user(self):
        self.register()
        other = TestClient(app, base_url="https://testserver")
        other.post("/api/auth/login", json={"email": "user@example.com", "password": PW})
        self.assertEqual(self.raw("SELECT COUNT(*) FROM sessions")[0][0], 2)
        self.client.post("/api/auth/reset", json={"token": self.reset_token(), "password": NEW_PW})
        self.assertEqual(self.raw("SELECT COUNT(*) FROM sessions")[0][0], 0)
        other.close()

    def test_reset_token_only_stored_as_digest_and_expires_in_60_min(self):
        self.register()
        token = self.reset_token()
        ((digest, expires),) = self.raw("SELECT token_hash, expires_at FROM password_resets")
        self.assertEqual(digest, auth._digest(token))
        delta = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
        self.assertLessEqual(delta, timedelta(minutes=60))
        self.assertGreater(delta, timedelta(minutes=59))

    def test_expired_reset_token_rejected(self):
        self.register()
        token = self.reset_token()
        self.raw("UPDATE password_resets SET expires_at=?", (_iso(datetime.now(timezone.utc) - timedelta(minutes=1)),))
        self.assertEqual(self.client.post("/api/auth/reset", json={"token": token, "password": NEW_PW}).status_code, 400)

    def test_weak_new_password_does_not_burn_token(self):
        self.register()
        token = self.reset_token()
        self.assertEqual(self.client.post("/api/auth/reset", json={"token": token, "password": "kisa"}).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/reset", json={"token": token, "password": NEW_PW}).status_code, 200)

    def test_new_request_invalidates_previous_link(self):
        self.register()
        first = self.reset_token()
        second = self.reset_token()
        self.assertEqual(self.client.post("/api/auth/reset", json={"token": first, "password": NEW_PW}).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/reset", json={"token": second, "password": NEW_PW}).status_code, 200)

    def test_forgot_response_identical_for_unknown_email(self):
        self.register()
        with contextlib.redirect_stdout(io.StringIO()):
            known = self.client.post("/api/auth/forgot", json={"email": "user@example.com"})
            unknown = self.client.post("/api/auth/forgot", json={"email": "yok@example.com"})
        self.assertEqual((known.status_code, known.json()), (unknown.status_code, unknown.json()))
        self.assertIsNone(self.reset_token("yok2@example.com"))

    def test_link_uses_app_url_not_host_header(self):
        self.register()
        with patch.dict("os.environ", {"APP_URL": "https://iddia.example/"}), patch.object(auth, "send_reset_email") as send:
            self.client.post("/api/auth/forgot", json={"email": "user@example.com"}, headers={"host": "evil.test"})
        self.assertTrue(send.call_args.args[1].startswith("https://iddia.example/giris?reset="))
        self.assertNotIn("evil.test", send.call_args.args[1])

    def test_missing_smtp_does_not_log_token_or_email(self):
        with patch.dict("os.environ", {"SMTP_HOST": ""}), contextlib.redirect_stdout(io.StringIO()) as out:
            auth.send_reset_email("sensitive@example.com", "https://example.com/giris?reset=secret-token")
        self.assertNotIn("sensitive@example.com", out.getvalue())
        self.assertNotIn("secret-token", out.getvalue())

    def test_smtp_used_when_configured(self):
        self.register()
        env = {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "2525", "SMTP_USER": "u", "SMTP_PASSWORD": "p", "SMTP_FROM": "a@b.c"}
        with patch("app.auth.smtplib.SMTP") as smtp_cls, patch.dict("os.environ", env):
            self.client.post("/api/auth/forgot", json={"email": "user@example.com"})
        self.assertEqual(smtp_cls.call_args.args[:2], ("smtp.example.com", 2525))
        smtp = smtp_cls.return_value.__enter__.return_value
        smtp.login.assert_called_once_with("u", "p")
        smtp.send_message.assert_called_once()

    def test_smtp_failure_does_not_leak(self):
        self.register()
        with patch("app.auth.smtplib.SMTP", side_effect=OSError("down")), patch.dict("os.environ", {"SMTP_HOST": "x"}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                r = self.client.post("/api/auth/forgot", json={"email": "user@example.com"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("reset=", out.getvalue())


class RateLimitTest(AuthTestBase):
    def test_login_blocked_after_repeated_failures_for_same_email(self):
        self.register()
        codes = [self.login("user@example.com", "yanlis-sifre-1").status_code for _ in range(6)]
        self.assertEqual(codes, [401] * 5 + [429])
        self.assertEqual(self.login("user@example.com").status_code, 429)  # doğru şifre bile

    def test_counters_live_in_db_and_emails_are_hashed(self):
        self.login("user@example.com", "yanlis-sifre-1")
        keys = [r[0] for r in self.raw("SELECT key FROM rate_limits")]
        self.assertTrue(any(k.startswith("auth:login:ip:") for k in keys))
        self.assertFalse(any("user@example.com" in k for k in keys))

    def test_reset_requests_are_limited(self):
        codes = [self.client.post("/api/auth/forgot", json={"email": "u@example.com"}).status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_register_limited_per_email(self):
        codes = [self.register("same@example.com").status_code for _ in range(4)]
        self.assertEqual(codes, [200, 409, 409, 429])
        self.assertEqual(self.register("other@example.com").status_code, 200)

    def test_register_limit_ignores_spoofed_forwarded_for(self):
        headers = {"x-real-ip": "203.0.113.8", "x-forwarded-for": "1.1.1.1"}
        codes = [self.client.post("/api/auth/register", headers=headers,
                                  json={"email": f"a{i}@example.com", "password": PW}).status_code
                 for i in range(11)]
        self.assertEqual(codes[-1], 429)
        again = self.client.post("/api/auth/register",
                                 headers={"x-real-ip": "203.0.113.8", "x-forwarded-for": "9.9.9.9"},
                                 json={"email": "fresh@example.com", "password": PW})
        self.assertEqual(again.status_code, 429)

    def test_register_limited_per_ip(self):
        codes = [self.register(f"u{i}@example.com").status_code for i in range(11)]
        self.assertEqual(codes[-1], 429)

    def test_long_window_counters_survive_short_window_cleanup(self):
        for _ in range(3):
            ops.rate_limited("auth:x", 99, 900)
        ops.rate_limited("update:ip", 5)  # farklı pencere boyutlu çağrı temizlik yapar
        self.assertEqual(self.raw("SELECT hits FROM rate_limits WHERE key LIKE 'auth:x'")[0][0], 3)


class AdminBootstrapTest(AuthTestBase):
    def test_create_admin_command_creates_admin(self):
        with patch("app.auth.getpass.getpass", side_effect=[PW, PW]):
            self.assertIn("oluşturuldu", auth._create_admin("boss@example.com"))
        self.assertEqual(auth.authenticate("boss@example.com", PW)["role"], "admin")

    def test_create_admin_promotes_existing_user(self):
        self.register("ali@example.com")
        self.assertIn("yapıldı", auth._create_admin("ali@example.com"))
        self.assertEqual(auth.authenticate("ali@example.com", PW)["role"], "admin")

    def test_ai_gate_closed_by_default_and_admin_only(self):
        admin = {"id": 1, "email": "a@x.com", "role": "admin"}
        with patch.dict("os.environ", {"AI_FEATURE_ENABLED": ""}):
            with self.assertRaises(HTTPException) as cm:
                auth.require_ai_admin(admin)
            self.assertEqual(cm.exception.status_code, 503)
        with patch.dict("os.environ", {"AI_FEATURE_ENABLED": "1"}):
            self.assertEqual(auth.require_ai_admin(admin), admin)
            with self.assertRaises(HTTPException) as cm:
                auth.require_ai_admin({"id": 2, "email": "u@x.com", "role": "user"})
            self.assertEqual(cm.exception.status_code, 403)


class MigrationTest(unittest.TestCase):
    def test_migration_declares_auth_tables(self):
        sql = Path("supabase/migrations/20261014000000_auth.sql").read_text()
        for table in ("users", "sessions", "password_resets"):
            self.assertIn(f"public.{table}", sql)
            self.assertIn(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY", sql)


if __name__ == "__main__":
    unittest.main()
