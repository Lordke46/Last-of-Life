import json
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from app import db
from app import telegram_bot as tg

UTC = timezone.utc


def pick(mid, market, odds=1.5, kickoff="2026-10-17T14:00Z", p=0.8):
    return {"id": str(mid), "league_name": "Süper Lig", "kickoff": kickoff, "home": f"H{mid}",
            "away": f"A&{mid}", "market": market, "odds": odds, "p": p}


def coupon(kind, picks):
    return {"type": kind, "picks": picks, "total_odds": 3.0, "total_probability": 0.6}


class PickOutcomeTest(unittest.TestCase):
    def test_every_automatic_market_matches_history_settlement(self):
        from app.model.coupon_history import pick_result
        from app.model.settlement import SUPPORTED_MARKETS
        for market in SUPPORTED_MARKETS:
            for h in range(5):
                for a in range(5):
                    self.assertEqual(tg.pick_outcome(market, h, a), pick_result(market, h, a) == "won")

    def test_unknown_completed_market_is_not_silently_voided(self):
        c = coupon("banko", [pick(1, "İY A1.5")])
        result = {"1": {"completed": 1, "home_goals": 2, "away_goals": 0}}
        self.assertIsNone(tg.settle_coupon(c, result, datetime(2026, 10, 18, tzinfo=UTC)))

    def test_markets(self):
        cases = [("1", 2, 1, True), ("1", 1, 1, False), ("X", 0, 0, True), ("2", 0, 1, True),
                 ("1X", 1, 1, True), ("1X", 0, 1, False), ("X2", 1, 1, True), ("12", 1, 1, False),
                 ("Ü2.5", 2, 1, True), ("Ü2.5", 1, 1, False), ("A2.5", 1, 1, True), ("Ü1.5", 1, 0, False),
                 ("A1.5", 1, 0, True), ("Ü3.5", 2, 2, True), ("A3.5", 2, 2, False),
                 ("KG Var", 1, 1, True), ("KG Var", 2, 0, False), ("KG Yok", 2, 0, True)]
        for market, h, a, expected in cases:
            self.assertEqual(tg.pick_outcome(market, h, a), expected, (market, h, a))
        self.assertIsNone(tg.pick_outcome("???", 1, 1))


class MessageTest(unittest.TestCase):
    def test_missing_result_uses_same_timeout_as_history(self):
        from app.model.coupon_history import _pick_outcome
        p = pick(1, "1")
        row = {"market": "1", "kickoff": p["kickoff"], "completed": 0,
               "home_goals": None, "away_goals": None, "match_status": None}
        now = datetime(2026, 10, 18, 4, 0, tzinfo=UTC)
        with patch("app.model.coupon_history._now", return_value=now):
            self.assertEqual(_pick_outcome(row)[0], "pending")
        self.assertIsNone(tg.settle_coupon(coupon("banko", [p]), {}, now))

    def test_coupon_message_escapes_html_and_uses_turkey_time(self):
        text = tg.format_coupon_message("2026-10-17", [coupon("banko", [pick(1, "1")])])
        self.assertIn("17 Ekim 2026", text)
        self.assertIn("17:00", text)  # 14:00Z = 17:00 TR
        self.assertIn("A&amp;1", text)
        self.assertIn("BANKO", text)
        self.assertNotIn("SÜRPRİZ", text)

    def test_coupon_message_is_plain_list_with_total_and_stake(self):
        p = pick(1, "1", odds=1.8, p=.7)
        p.update(model_probability=.74, calibration_n=53, calibration_applied=True)
        c = {**coupon("banko", [p, pick(2, "Ü2.5", kickoff="2026-10-17T12:00Z")]), "stake_plan": {"percent": 1.5}}
        text = tg.format_coupon_message("2026-10-17", [tg._compact_coupon(c)])
        self.assertIn("• 15:00 H2 – A&amp;2 → <b>2.5 Üst</b> (1.50)\n• 17:00 H1 – A&amp;1 → <b>MS 1</b> (1.80)", text)
        self.assertIn("Toplam oran <b>3.00</b> · tutma ihtimali %60", text)
        self.assertIn("Önerilen miktar: kasanın %1.5'i", text)
        self.assertNotIn("model %", text)
        self.assertNotIn("kalibrasyon", text)

    def test_fallback_coupon_is_marked_and_target_flag_survives_compaction(self):
        met = tg.format_coupon_message("2026-10-17", [{**coupon("banko", [pick(1, "1")]), "target_met": True}])
        self.assertNotIn("yedek kupon", met)
        compact = tg._compact_coupon({**coupon("surprise", [pick(1, "1")]), "target_met": False,
                                      "stake_note": "düşük kasa"})
        self.assertEqual((compact["target_met"], compact["stake_note"]), (False, "düşük kasa"))
        self.assertIn("yedek kupon", tg.format_coupon_message("2026-10-17", [compact]))

    def test_only_surprise_sends_explicit_no_bank_statement(self):
        text = tg.format_coupon_message(
            "2026-10-17", [coupon("surprise", [pick(1, "2")])],
            banko_note="Yeterli kanıt bulunamadı.",
        )
        self.assertIn("Banko yok", text)
        self.assertIn("Yeterli kanıt", text)

    def test_settle_waits_for_unfinished_then_voids_after_timeout(self):
        c = coupon("banko", [pick(1, "1"), pick(2, "1")])
        results = {"1": {"completed": 1, "home_goals": 2, "away_goals": 0}}
        before = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)
        self.assertIsNone(tg.settle_coupon(c, results, before))
        after = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
        s = tg.settle_coupon(c, results, after)
        self.assertEqual((s["status"], s["odds"]), ("won", 1.5))
        self.assertEqual([p["state"] for p in s["picks"]], ["won", "void"])

    def test_lost_leg_settles_coupon_without_waiting_for_unfinished_legs(self):
        c = coupon("surprise", [pick(1, "Ü2.5"), pick(2, "1")])
        results = {"1": {"completed": 1, "home_goals": 1, "away_goals": 0}}
        s = tg.settle_coupon(c, results, datetime(2026, 10, 17, 18, 0, tzinfo=UTC))
        self.assertEqual(s["status"], "lost")
        self.assertEqual([p["state"] for p in s["picks"]], ["lost", "pending"])
        self.assertIn("⏳ H2 - A&amp;2 <b>bekleniyor</b>", tg.format_result_message("2026-10-17", {"surprise": s}))

    def test_settle_lost_and_message(self):
        c = coupon("surprise", [pick(1, "X"), pick(2, "2")])
        results = {"1": {"completed": 1, "home_goals": 1, "away_goals": 1},
                   "2": {"completed": 1, "home_goals": 3, "away_goals": 0}}
        s = tg.settle_coupon(c, results, datetime(2026, 10, 18, tzinfo=UTC))
        self.assertEqual(s["status"], "lost")
        text = tg.format_result_message("2026-10-17", {"surprise": s})
        self.assertIn("KAYBETTİ", text)
        self.assertIn("1-1", text)
        self.assertIn("3-0", text)


class SendMessageTest(unittest.TestCase):
    def test_returns_message_id_and_sends_reply(self):
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent["url"], sent["body"] = req.full_url, json.loads(req.data)
            return BytesIO(json.dumps({"ok": True, "result": {"message_id": 42}}).encode())

        with patch.object(tg.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(tg.send_message("TOK", "@kanal", "merhaba", reply_to="7"), "42")
        self.assertTrue(sent["url"].endswith("/botTOK/sendMessage"))
        self.assertEqual((sent["body"]["chat_id"], sent["body"]["reply_to_message_id"]), ("@kanal", 7))

    def test_error_never_leaks_token(self):
        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                         BytesIO(b'{"ok":false,"description":"Unauthorized TOK"}'))

        with patch.object(tg.urllib.request, "urlopen", boom):
            with self.assertRaises(tg.TelegramError) as ctx:
                tg.send_message("TOK", "1", "x")
        self.assertNotIn("TOK", str(ctx.exception))
        self.assertIn("401", str(ctx.exception))


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = patch.object(db, "DB_PATH", Path(self.tmp.name) / "t.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        db.init_db()
        self.sent = []

    def send(self, text, reply_to=None):
        self.sent.append((text, reply_to))
        return str(100 + len(self.sent))

    def built(self, with_picks=True):
        banko = coupon("banko", [pick(1, "1"), pick(2, "1X")] if with_picks else [])
        return lambda target: {"date": target.isoformat(), "banko": banko,
                               "surprise": coupon("surprise", [])}


class DailyCouponTest(DbTestCase):
    morning = datetime(2026, 10, 17, 6, 30, tzinfo=UTC)  # 09:30 TR

    def test_sends_once_per_day(self):
        build = self.built()
        self.assertEqual(tg.post_daily_coupons(self.morning, self.send, build=build), "kupon: gönderildi")
        self.assertEqual(tg.post_daily_coupons(self.morning + timedelta(hours=1), self.send, build=build),
                         "kupon: bugün zaten gönderildi")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Günlük Kupon", self.sent[0][0])

    def test_outside_morning_window_and_empty_coupon_not_sent_or_recorded(self):
        night = datetime(2026, 10, 17, 20, 0, tzinfo=UTC)
        self.assertIn("pencere", tg.post_daily_coupons(night, self.send, build=self.built()))
        self.assertEqual(tg.post_daily_coupons(self.morning, self.send, build=self.built(False)),
                         "kupon: uygun kupon yok")
        self.assertEqual(self.sent, [])
        with db.session() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) n FROM telegram_posts").fetchone()["n"], 0)

    def test_grok_replacement_is_sent_instead_of_vetoed_coupon(self):
        from app import grok_audit
        swap = {**coupon("banko_grok", [pick(5, "X2", odds=1.4)]), "grok_note": "Grok veto'su: H1 - A&1"}
        with patch.object(grok_audit, "decorate", lambda built, now=None: {**built, "banko_grok": swap}):
            self.assertEqual(tg.post_daily_coupons(self.morning, self.send, build=self.built()), "kupon: gönderildi")
        text = self.sent[0][0]
        self.assertIn("BANKO (Grok değişimi)", text)
        self.assertIn("H5 – A&amp;5", text)
        self.assertNotIn("H1 –", text)
        self.assertIn("Önerilen miktar", text)
        with db.session() as conn:
            payload = json.loads(conn.execute("SELECT payload FROM telegram_posts WHERE kind='coupon'").fetchone()[0])
        self.assertEqual([c["type"] for c in payload], ["banko_grok"])

    def test_failed_send_releases_claim_so_next_run_retries(self):
        def failing(text, reply_to=None):
            raise tg.TelegramError("kapalı")

        with self.assertRaises(tg.TelegramError):
            tg.post_daily_coupons(self.morning, failing, build=self.built())
        self.assertEqual(tg.post_daily_coupons(self.morning, self.send, build=self.built()), "kupon: gönderildi")

    def test_concurrent_claim_blocks_second_sender_until_stale(self):
        self.assertTrue(tg.claim("2026-10-17", "coupon", self.morning))
        self.assertFalse(tg.claim("2026-10-17", "coupon", self.morning + timedelta(minutes=5)))
        self.assertTrue(tg.claim("2026-10-17", "coupon", self.morning + timedelta(minutes=tg.STALE_CLAIM_MINUTES + 1)))
        tg.mark_sent("2026-10-17", "coupon", "9", self.morning)
        self.assertFalse(tg.claim("2026-10-17", "coupon", self.morning + timedelta(days=1)))


class WeeklyTest(DbTestCase):
    def test_monday_summary_sent_once_after_ten(self):
        from app.model import coupon_history as ch
        week = [{"date": "2026-10-12", "banko": {"status": "won", "effective_odds": 2.0, "picks": []}, "surprise": None},
                {"date": "2026-10-18", "banko": {"status": "lost", "effective_odds": 1.8, "picks": [{"status": "lost", "clv": .05}]},
                 "surprise": {"status": "won", "effective_odds": 4.0, "picks": []}},
                {"date": "2026-10-19", "banko": {"status": "lost", "effective_odds": 9, "picks": []}}]  # bugünkü sayılmaz
        monday = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)  # 09:00 TR
        with patch.object(ch, "history", lambda conn, days=14: {"days": week}):
            self.assertEqual(tg.post_weekly_summary(monday, self.send), "haftalık: zamanı değil")
            self.assertEqual(tg.post_weekly_summary(monday + timedelta(hours=2), self.send), "haftalık: gönderildi")
            self.assertEqual(tg.post_weekly_summary(monday + timedelta(hours=3), self.send), "haftalık: zaten gönderildi")
        text = self.sent[0][0]
        self.assertIn("12 Ekim – 18 Ekim", text)
        self.assertIn("💎 BANKO: 1 kazandı / 1 kaybetti · kâr <b>+0 TL</b>", text)
        self.assertIn("🎲 SÜRPRİZ: 1 kazandı / 0 kaybetti · kâr <b>+300 TL</b>", text)
        self.assertIn("CLV): %+5.0 (1 seçim)", text)


class ResultsTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.morning = datetime(2026, 10, 17, 6, 30, tzinfo=UTC)
        tg.post_daily_coupons(self.morning, self.send, build=self.built())
        self.sent.clear()

    def finish(self, mid, hg, ag):
        with db.session() as conn:
            conn.execute("INSERT INTO matches(id, league, kickoff, home, away, home_goals, away_goals, completed) "
                         "VALUES(?, 'tur.1', '2026-10-17T14:00Z', 'a', 'b', ?, ?, 1)", (str(mid), hg, ag))

    def test_waits_for_all_matches_then_replies_once(self):
        now = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)
        self.finish(1, 2, 0)
        self.assertEqual(tg.post_results(now, self.send),
                         ["bacak 2026-10-17: H1 - A&1 won", "sonuç 2026-10-17: maçlar bitmedi"])
        self.assertEqual(tg.post_results(now, self.send), ["sonuç 2026-10-17: maçlar bitmedi"])  # bacak bir kez
        self.assertIn("✅ H1 – A&amp;1 <b>2-0</b> → MS 1", self.sent[0][0])
        self.finish(2, 1, 1)
        self.assertEqual(tg.post_results(now, self.send), ["sonuç 2026-10-17: gönderildi"])
        self.assertEqual(tg.post_results(now, self.send), [])
        self.assertEqual(len(self.sent), 2)
        text, reply_to = self.sent[1]
        self.assertEqual(reply_to, "101")  # kupon mesajına yanıt
        self.assertIn("KAZANDI", text)
        self.assertIn("2-0", text)

    def test_bulletin_leg_without_espn_row_settles_from_iddaa_results(self):
        now = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)
        self.finish(1, 2, 0)
        calls = []

        def fetch(day):
            calls.append(day)
            return {"2": (1, 1)}

        self.assertEqual(tg.post_results(now, self.send, fetch_results=fetch), ["sonuç 2026-10-17: gönderildi"])
        self.assertEqual(calls, ["2026-10-17"])
        text, _ = self.sent[0]
        self.assertIn("KAZANDI", text)
        self.assertIn("H2 - A&amp;2 <b>1-1</b>", text)

    def test_iddaa_results_not_queried_before_result_delay_or_without_fetcher(self):
        early = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)  # 14:00Z başlangıçtan 1 saat sonra
        self.finish(1, 2, 0)
        calls = []
        self.assertEqual(tg.post_results(early, self.send, fetch_results=lambda d: calls.append(d) or {})[-1],
                         "sonuç 2026-10-17: maçlar bitmedi")
        self.assertEqual(calls, [])
        late = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)
        self.assertEqual(tg.post_results(late, self.send)[-1], "sonuç 2026-10-17: maçlar bitmedi")


    def test_lost_leg_reply_says_coupon_lost_while_other_coupon_pending(self):
        with db.session() as conn:
            conn.execute("UPDATE telegram_posts SET payload=? WHERE kind='coupon'", (json.dumps([
                coupon("banko", [pick(1, "1")]), coupon("surprise", [pick(3, "2", kickoff="2026-10-18T14:00Z")])]),))
        self.finish(1, 0, 1)
        out = tg.post_results(datetime(2026, 10, 17, 18, 0, tzinfo=UTC), self.send)
        self.assertEqual(out[-1], "sonuç 2026-10-17: maçlar bitmedi")
        self.assertIn("❌", self.sent[0][0])
        self.assertIn("Bu kupon kaybetti", self.sent[0][0])
        self.assertEqual(self.sent[0][1], "101")

    def test_saved_live_scores_back_up_iddaa_results_feed(self):
        with db.session() as conn:
            db.set_meta(conn, "verified_scores:2026-10-17", json.dumps(
                {"iddaa:2": {"state": "post", "home_goals": 0, "away_goals": 2},
                 "iddaa:3": {"state": "in", "home_goals": 1, "away_goals": 0}}))
        with patch.object(tg.iddaa, "fetch_results", side_effect=RuntimeError("akış yok")):
            self.assertEqual(tg.saved_and_iddaa_results("2026-10-17"), {"2": (0, 2)})
        with patch.object(tg.iddaa, "fetch_results", return_value={"2": (1, 2), "4": (0, 0)}):
            self.assertEqual(tg.saved_and_iddaa_results("2026-10-17"), {"2": (1, 2), "4": (0, 0)})


class TestModeTest(DbTestCase):
    def test_test_day_sends_test_header_without_idempotency_record(self):
        calls = []
        with patch.dict(tg.os.environ, {"TELEGRAM_BOT_TOKEN": "TOK", "TELEGRAM_CHAT_ID": "@kanal"}), \
                patch.object(tg, "build_coupons", return_value={
                    "date": "2026-10-17", "banko": coupon("banko", [pick(1, "1")]),
                    "surprise": coupon("surprise", []),
                }), \
                patch.object(tg, "send_message", side_effect=lambda token, chat, text, reply_to=None: calls.append(
                    (token, chat, text, reply_to))):
            self.assertEqual(tg.main(["--test", "--day", "2026-10-17"]), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ("TOK", "@kanal"))
        self.assertIn("🧪 <b>TEST</b>", calls[0][2])
        self.assertIsNone(calls[0][3])
        with db.session() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) n FROM telegram_posts").fetchone()["n"], 0)


class MainTest(unittest.TestCase):
    def test_missing_secrets_skip_silently(self):
        with patch.dict(tg.os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}), \
                patch.object(tg, "init_db", side_effect=AssertionError("DB'ye dokunulmamalı")), \
                patch.object(tg, "send_message", side_effect=AssertionError("gönderilmemeli")):
            self.assertEqual(tg.main([]), 0)


class TelegramWorkflowTest(unittest.TestCase):
    def test_dispatch_values_are_passed_through_env_not_interpolated_into_shell(self):
        workflow = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "telegram.yml").read_text(encoding="utf-8")
        self.assertIn("TEST_DAY: ${{ inputs.test_day }}", workflow)
        self.assertIn('"$TEST_DAY"', workflow)
        self.assertIn('"$FORCE_SEND"', workflow)
        self.assertNotIn("format('--test --day {0}', inputs.test_day)", workflow)


if __name__ == "__main__":
    unittest.main()
