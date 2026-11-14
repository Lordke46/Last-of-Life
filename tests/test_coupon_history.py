import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import db, updater
from app.coupon_history_api import get_coupon_history
from app.model import coupon_history as ch

OLD = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%MZ")


def pick(mid, market="1", odds=1.5, p=0.8, kickoff=OLD):
    return {"id": mid, "league": "eng.1", "league_name": "Premier League", "kickoff": kickoff, "home": f"H{mid}",
            "away": f"A{mid}", "market": market, "odds": odds, "odds_source": "iddaa", "p": p}


def coupon(picks):
    total = 1.0
    prob = 1.0
    for x in picks:
        total *= x["odds"]
        prob *= x["p"]
    return {"picks": picks, "total_odds": round(total, 2), "total_probability": round(prob, 4),
            "odds_source": "iddaa"}


def day_coupons(day, banko=(), surprise=()):
    return {"date": day, "banko": coupon(list(banko)), "surprise": coupon(list(surprise))}


class ResultTest(unittest.TestCase):
    def test_markets(self):
        cases = [("1", 2, 1, "won"), ("1", 1, 1, "lost"), ("X", 1, 1, "won"), ("2", 0, 1, "won"),
                 ("1X", 1, 1, "won"), ("1X", 0, 1, "lost"), ("X2", 2, 1, "lost"), ("12", 1, 1, "lost"),
                 ("Ü2.5", 2, 1, "won"), ("Ü2.5", 1, 1, "lost"), ("A2.5", 1, 1, "won"), ("A2.5", 2, 1, "lost"),
                 ("Ü3.5", 2, 2, "won"), ("A1.5", 1, 0, "won"), ("KG Var", 1, 1, "won"), ("KG Var", 2, 0, "lost"),
                 ("KG Yok", 2, 0, "won"), ("Ev 0.5Ü", 0, 3, "lost"), ("Dep 0.5Ü", 0, 3, "won")]
        for market, h, a, expected in cases:
            self.assertEqual(ch.pick_result(market, h, a), expected, (market, h, a))
        self.assertIsNone(ch.pick_result("İY/MS 1/1", 1, 0))

    def test_coupon_status(self):
        self.assertEqual(ch.coupon_status(["won", "won"]), "won")
        self.assertEqual(ch.coupon_status(["won", "pending"]), "pending")
        self.assertEqual(ch.coupon_status(["pending", "lost"]), "lost")  # biri yattıysa bekleme gereksiz
        self.assertEqual(ch.coupon_status(["won", "void"]), "won")
        self.assertEqual(ch.coupon_status(["void", "void"]), "void")


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(db, "DB_PATH", Path(self.tmp.name) / "t.db"), patch.object(db, "DATABASE_URL", None)):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()

    def add_match(self, mid, hg=None, ag=None, completed=True, status="FT"):
        with db.session() as conn:
            conn.execute("INSERT INTO matches(id, league, kickoff, home, away, home_goals, away_goals, completed, "
                         "status) VALUES(?, 'eng.1', ?, 'h', 'a', ?, ?, ?, ?)",
                         (str(mid), OLD, hg, ag, int(completed), status))

    def freeze(self, coupons):
        with db.session() as conn:
            return ch.freeze(conn, coupons)

    def settle(self):
        with db.session() as conn:
            return ch.settle(conn)

    def history(self, days=60):
        with db.session() as conn:
            return ch.history(conn, days)


class FreezeTest(DbCase):
    def test_frozen_calibration_evidence_does_not_change_with_live_predictions(self):
        now = datetime.now(timezone.utc)
        target = now.astimezone(ch.TZ).date()
        original = day_coupons(target.isoformat(), banko=[pick(1), pick(2)])
        original["banko"].update(selection_policy="hit_rate_v3", calibration_status="limited",
                                 calibration_note="0/2 seçim kalibre edildi.", stake_note="İlk yayın kuralı")
        for p in original["banko"]["picks"]:
            p.update(calibration_applied=False, calibration_n=18, model_probability=.85)
        self.freeze(original)
        later = day_coupons(target.isoformat(), banko=[pick(1), pick(2)])
        later["banko"].update(calibration_status="complete", calibration_note="Yeni kanıt")
        result = ch.daily_coupons(target, build=lambda _: later)
        published = result["banko"]
        self.assertEqual(published["calibration_status"], "limited")
        self.assertEqual(published["calibration_note"], "0/2 seçim kalibre edildi.")
        self.assertEqual(published["stake_note"], "İlk yayın kuralı")
        self.assertTrue(all(not p["calibration_applied"] and p["calibration_n"] == 18 for p in published["picks"]))
        self.assertTrue(all(p["model_probability"] == .85 for p in published["picks"]))


    def test_web_telegram_and_history_use_same_published_coupon(self):
        from app import main, telegram_bot as tg
        now = datetime.now(timezone.utc)
        target = now.astimezone(ch.TZ).date()
        kickoff = (now + timedelta(hours=2)).isoformat()
        first = day_coupons(target.isoformat(), banko=[pick(1, kickoff=kickoff), pick(2, kickoff=kickoff)])
        changed = day_coupons(target.isoformat(), banko=[pick(9, odds=2.0, kickoff=kickoff)])
        changed["banko_alternatives"] = [changed["banko"]]
        with patch.object(main, "build_coupons", return_value=first):
            web = main._coupons(target)
        with patch.object(main, "build_coupons", return_value=changed):
            later_web = main._coupons(target)
        self.assertEqual(later_web["banko"], web["banko"])
        self.assertEqual(later_web["banko_alternatives"][1]["publication"], "live")
        self.assertEqual(web["banko"]["coupon_id"], f"{target}:banko:v1")
        sent = []
        tg.post_daily_coupons(now, lambda text, reply_to=None: sent.append(text) or "1",
                              force=True, build=lambda day: changed)
        with db.session() as conn:
            import json
            payload = json.loads(conn.execute("SELECT payload FROM telegram_posts").fetchone()["payload"])
        ids = [p["id"] for p in web["banko"]["picks"]]
        self.assertEqual([p["id"] for p in payload[0]["picks"]], ids)
        self.assertEqual(payload[0]["coupon_id"], web["banko"]["coupon_id"])
        self.assertEqual([p["match_id"] for p in self.history()["days"][0]["banko"]["picks"]], ids)

    def test_frozen_once_per_day_and_kind(self):
        first = day_coupons("2026-10-06", banko=[pick(1), pick(2)], surprise=[pick(3, "X", 3.0, 0.3)])
        self.assertEqual(self.freeze(first), ["banko", "surprise"])
        later = day_coupons("2026-10-06", banko=[pick(9, odds=1.2)], surprise=[pick(8, "2", 4.0, 0.25)])
        self.assertEqual(self.freeze(later), [])  # ilk üretilen hali korunur
        day = self.history()["days"][0]
        self.assertEqual([p["match_id"] for p in day["banko"]["picks"]], ["1", "2"])
        self.assertEqual(day["banko"]["total_odds"], 2.25)
        self.assertEqual(day["surprise"]["picks"][0]["market"], "X")

    def test_empty_coupon_not_saved_and_filled_in_later(self):
        self.assertEqual(self.freeze(day_coupons("2026-10-06", surprise=[pick(3, "X", 3.0, 0.3)])), ["surprise"])
        self.assertIsNone(self.history()["days"][0]["banko"])
        self.assertEqual(self.freeze(day_coupons("2026-10-06", banko=[pick(1), pick(2)])), ["banko"])
        self.assertIsNotNone(self.history()["days"][0]["banko"])

    def test_freeze_if_today_only_saves_today_and_swallows_errors(self):
        today = datetime.now(ch.TZ).date().isoformat()
        self.assertEqual(ch.freeze_if_today(day_coupons("2000-01-01", banko=[pick(1), pick(2)])), [])
        self.assertEqual(ch.freeze_if_today(day_coupons(today, banko=[pick(1), pick(2)])), ["banko"])
        with patch.object(db, "connect", side_effect=RuntimeError("db down")):
            self.assertEqual(ch.freeze_if_today(day_coupons(today, banko=[pick(5), pick(6)])), [])


class SettleTest(DbCase):
    def test_pending_until_matches_finish_then_won(self):
        self.freeze(day_coupons("2026-10-06", banko=[pick(1), pick(2, "Ü2.5")]))
        self.add_match(1, 2, 0)
        self.add_match(2, completed=False, status="STATUS_SCHEDULED")
        self.assertEqual(self.settle(), {"picks": 1, "coupons": 0, "closing": 0})
        banko = self.history()["days"][0]["banko"]
        self.assertEqual((banko["status"], [p["status"] for p in banko["picks"]]), ("pending", ["won", "pending"]))
        self.assertEqual((banko["picks"][0]["home_goals"], banko["picks"][0]["away_goals"]), (2, 0))
        self.assertIsNone(banko["profit"])
        with db.session() as conn:
            conn.execute("UPDATE matches SET completed=1, home_goals=2, away_goals=1 WHERE id='2'")
        self.assertEqual(self.settle(), {"picks": 1, "coupons": 1, "closing": 0})
        banko = self.history()["days"][0]["banko"]
        self.assertEqual(banko["status"], "won")
        self.assertIsNotNone(banko["settled_at"])
        self.assertEqual(self.settle(), {"picks": 0, "coupons": 0, "closing": 0})  # idempotent

    def test_one_lost_pick_loses_coupon_immediately(self):
        self.freeze(day_coupons("2026-10-06", banko=[pick(1), pick(2)]))
        self.add_match(1, 0, 1)
        self.add_match(2, completed=False, status="STATUS_SCHEDULED")
        self.assertEqual(self.settle(), {"picks": 1, "coupons": 1, "closing": 0})
        banko = self.history()["days"][0]["banko"]
        self.assertEqual((banko["status"], banko["profit"]), ("lost", -100.0))

    def test_postponed_match_is_void_and_stale_unfinished_match_too(self):
        self.freeze(day_coupons("2026-10-06", banko=[pick(1, odds=2.0), pick(2, odds=1.5), pick(3)]))
        self.add_match(1, 1, 0)
        self.add_match(2, completed=False, status="Postponed")
        self.assertEqual(self.settle(), {"picks": 2, "coupons": 0, "closing": 0})  # 3. maç kayıtlı değil, 2 günlük -> bekliyor
        with db.session() as conn:
            conn.execute("UPDATE saved_coupon_picks SET kickoff=? WHERE match_id='3'",
                         ((datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%MZ"),))
        self.assertEqual(self.settle(), {"picks": 1, "coupons": 1, "closing": 0})
        banko = self.history()["days"][0]["banko"]
        self.assertEqual({p["match_id"]: p["status"] for p in banko["picks"]}, {"1": "won", "2": "void", "3": "void"})
        self.assertEqual((banko["status"], banko["effective_odds"], banko["profit"]), ("won", 2.0, 100.0))


class ClosingLineTest(DbCase):
    """Kapanış oranı: etkinlik geçmişi (dondurmada kaydedilen iddaa etkinlik kimliğiyle) > odds_log; CLV özetlenir."""

    def add_history(self, event_id, market, *quotes):
        with db.session() as conn:
            for odd, minutes_before in quotes:
                at = (datetime.fromisoformat(OLD.replace("Z", "+00:00")) - timedelta(minutes=minutes_before)).isoformat()
                conn.execute("INSERT INTO iddaa_odds_history(event_id, market, odd, captured_at, kickoff) VALUES(?,?,?,?,?)",
                             (event_id, market, odd, at, OLD))

    def test_settle_records_closing_from_event_history_then_odds_log_and_history_reports_clv(self):
        with_event = {**pick(1, odds=1.85), "iddaa_event_id": "ev-1"}
        self.freeze(day_coupons("2026-10-06", banko=[with_event, pick(2, "Ü2.5", odds=2.0)],
                                surprise=[pick(3, "X", 3.2, 0.3), pick(4, "2", 2.5, 0.4)]))
        # ESPN maç kimliği "1" için geçmiş yok, etkinlik kimliği "ev-1" için maçtan sonra (sayılmaz) ve önce kayıt var
        self.add_history("ev-1", "1", (1.9, 600), (1.75, 30), (1.6, -5))
        with db.session() as conn:
            kickoff = datetime.fromisoformat(OLD.replace("Z", "+00:00"))
            conn.execute("INSERT INTO odds_log(match_id, market, odd, first_odd, first_seen, updated_at) VALUES(?,?,?,?,?,?)",
                         ("2", "Ü2.5", 2.2, 2.0, OLD, (kickoff - timedelta(minutes=10)).isoformat()))
            conn.execute("INSERT INTO odds_log(match_id, market, odd, first_odd, first_seen, updated_at) VALUES(?,?,?,?,?,?)",
                         ("3", "X", 3.0, 3.2, OLD, (kickoff + timedelta(minutes=10)).isoformat()))  # maçtan sonra: sayılmaz
        self.add_match(1, 2, 0)
        self.add_match(2, 1, 1)
        self.add_match(3, completed=False, status="STATUS_POSTPONED")
        self.assertEqual(self.settle(), {"picks": 3, "coupons": 1, "closing": 2})
        h = self.history()
        banko, surprise = h["days"][0]["banko"], h["days"][0]["surprise"]
        self.assertEqual([(p["closing_odds"], p["clv"]) for p in banko["picks"]], [(1.75, round(1.85 / 1.75 - 1, 4)), (2.2, round(2.0 / 2.2 - 1, 4))])
        self.assertEqual([p["closing_odds"] for p in surprise["picks"]], [None, None])
        self.assertEqual((h["summary"]["banko"]["clv_picks"], h["summary"]["banko"]["clv_positive"]), (2, 1))
        self.assertAlmostEqual(h["summary"]["banko"]["clv"], round(((1.85 / 1.75 - 1) + (2.0 / 2.2 - 1)) / 2, 4))
        self.assertEqual((h["summary"]["surprise"]["clv_picks"], h["summary"]["surprise"]["clv"]), (0, None))
        self.assertEqual(h["summary"]["total"]["clv_picks"], 2)
        # Kapanışı bulunamayan seçim sonraki turda yeniden denenir; bulunanlar yeniden yazılmaz
        self.add_history("4", "2", (2.8, 15))
        self.assertEqual(self.settle()["closing"], 1)
        self.assertEqual(self.history()["days"][0]["surprise"]["picks"][1]["closing_odds"], 2.8)
        self.assertIsNone(ch.pick_clv({"status": "void", "odds": 2.0, "closing_odds": 1.5}))


class HistoryTest(DbCase):
    def test_summary_roi_and_list(self):
        days = {
            "2026-10-04": dict(banko=[pick(1, odds=1.5), pick(2, odds=1.4)], surprise=[pick(3, "X", 4.0, 0.3)]),
            "2026-10-05": dict(banko=[pick(4, odds=1.5), pick(5, odds=1.4)], surprise=[pick(6, "2", 5.0, 0.25)]),
            "2026-10-06": dict(banko=[pick(7), pick(8)]),
        }
        for day, c in days.items():
            self.freeze(day_coupons(day, **c))
        # 10-04: banko tuttu (2.1), sürpriz tuttu (4.0); 10-05: banko yattı, sürpriz yattı; 10-06: bekliyor
        for mid, (hg, ag) in {1: (1, 0), 2: (2, 0), 3: (1, 1), 4: (1, 0), 5: (0, 0), 6: (1, 0)}.items():
            self.add_match(mid, hg, ag)
        self.add_match(7, completed=False, status="STATUS_SCHEDULED")
        self.add_match(8, completed=False, status="STATUS_SCHEDULED")
        self.settle()
        h = self.history()
        banko, surprise, total = h["summary"]["banko"], h["summary"]["surprise"], h["summary"]["total"]
        self.assertEqual((banko["won"], banko["lost"], banko["pending"], banko["hit_rate"]), (1, 1, 1, 0.5))
        self.assertEqual((banko["picks_settled"], banko["picks_won"]), (4, 3))  # 1, 2, 4 tuttu; 5 yattı
        self.assertEqual((banko["stake"], banko["returned"], banko["profit"]), (200.0, 210.0, 10.0))
        self.assertAlmostEqual(banko["roi"], 0.05)
        self.assertEqual((surprise["won"], surprise["lost"], surprise["hit_rate"]), (1, 1, 0.5))
        self.assertEqual((surprise["returned"], surprise["profit"]), (400.0, 200.0))
        self.assertAlmostEqual(surprise["roi"], 1.0)
        self.assertEqual((total["stake"], total["profit"]), (400.0, 210.0))
        self.assertAlmostEqual(total["roi"], 0.525)
        self.assertEqual([d["date"] for d in h["days"]], ["2026-10-06", "2026-10-05", "2026-10-04"])
        self.assertEqual([d["profit"] for d in h["days"]], [0.0, -200.0, 410.0])
        self.assertIsNone(h["days"][0]["surprise"])
        self.assertEqual(self.history(days=2)["days"][-1]["date"], "2026-10-05")
        self.assertEqual(self.history(days=2)["total_days"], 3)

    def test_empty_history(self):
        h = get_coupon_history(days=60)
        self.assertEqual((h["days"], h["summary"]["total"]["coupons"], h["summary"]["banko"]["roi"]), ([], 0, None))
        self.assertEqual(h["stake"], 100.0)


class UpdaterIntegrationTest(DbCase):
    def test_quick_update_settles_but_does_not_freeze(self):
        self.freeze(day_coupons("2026-10-06", banko=[pick(1), pick(2)]))
        self.add_match(1, 0, 3)  # yattı
        with patch.object(updater.espn, "sync_recent", return_value=(1, True)), \
                patch.object(updater.tff, "sync_recent", return_value=(1, True)), \
                patch.object(updater.iddaa, "sync_odds", return_value={}), \
                patch.object(ch, "freeze_today") as freeze:
            self.assertEqual(updater.run_quick_update()["status"], "updated")
        freeze.assert_not_called()
        self.assertEqual(self.history()["days"][0]["banko"]["status"], "lost")

    def test_full_update_freezes_and_settles_and_survives_errors(self):
        self.freeze(day_coupons("2026-10-06", banko=[pick(1), pick(2)]))
        self.add_match(1, 0, 3)
        with patch.object(updater.espn, "sync_league", return_value=1), \
                patch.object(updater.tff, "sync_league", return_value=1), \
                patch.object(updater.iddaa, "sync_odds", return_value={}), \
                patch.object(ch, "freeze_today", return_value=["banko"]) as freeze:
            self.assertEqual(updater.run_update(with_news=False), "ok")
        freeze.assert_called_once()
        self.assertEqual(self.history()["days"][0]["banko"]["status"], "lost")
        with patch.object(updater.espn, "sync_league", return_value=1), \
                patch.object(updater.tff, "sync_league", return_value=1), \
                patch.object(updater.iddaa, "sync_odds", return_value={}), \
                patch.object(ch, "freeze_today", side_effect=RuntimeError("boom")):
            self.assertEqual(updater.run_update(with_news=False), "ok")  # kupon hatası güncellemeyi bozmaz


if __name__ == "__main__":
    unittest.main()


class IddaaResultTest(DbCase):
    """Bülten kimlikli (ESPN eşleşmesiz) bacaklar iddaa canlı sonuç servisiyle sonuçlanır; eski Handikap pazarı okunur."""

    def test_settle_grades_bulletin_legs_from_iddaa_results(self):
        calls = []

        def fetch(day):
            calls.append(day)
            if day == datetime.fromisoformat(OLD.replace("Z", "+00:00")).astimezone(ch.TZ).date().isoformat():
                return {"3294853": (3, 1), "3294309": (0, 0)}
            return {}

        self.freeze(day_coupons("2026-10-06", banko=[pick("3294853", "Ü3.5", odds=1.67), pick("3294309", "12", odds=1.1)],
                                surprise=[pick("3300000", "A2.5", 2.4, 0.35), pick(7, "1", 1.5, 0.6)]))
        self.add_match(7, 2, 0)
        with db.session() as conn:
            done = ch.settle(conn, fetch_results=fetch)
        self.assertEqual((done["picks"], done["coupons"]), (3, 1))
        self.assertEqual(len(calls), 1)  # aynı gün bir kez sorgulanır
        h = self.history()["days"][0]
        self.assertEqual(sorted((p["status"], p["home_goals"], p["away_goals"]) for p in h["banko"]["picks"]),
                         [("lost", 0, 0), ("won", 3, 1)])  # 3-1 → Ü3.5 tuttu; 0-0 → "12" yattı
        self.assertEqual(h["banko"]["status"], "lost")
        self.assertEqual([p["status"] for p in h["surprise"]["picks"]], ["pending", "won"])  # skoru gelmeyen bekler
        self.assertEqual(h["surprise"]["status"], "pending")

    def test_settle_without_fetcher_or_with_failing_fetcher_keeps_waiting(self):
        self.freeze(day_coupons("2026-10-06", banko=[pick("3294853", "Ü3.5", odds=1.67)]))
        self.assertEqual(self.settle()["picks"], 0)

        def boom(day):
            raise RuntimeError("ağ yok")

        with db.session() as conn:
            self.assertEqual(ch.settle(conn, fetch_results=boom)["picks"], 0)
        self.assertEqual(self.history()["days"][0]["banko"]["status"], "pending")

    def test_frozen_event_id_bridges_espn_pick_to_iddaa_score(self):
        self.freeze(day_coupons("2026-10-06", banko=[{**pick(1, "KG Var", odds=1.8), "iddaa_event_id": "555"}]))
        self.add_match(1, completed=False, status="STATUS_SCHEDULED")  # ESPN skoru gecikmiş
        with db.session() as conn:
            done = ch.settle(conn, fetch_results=lambda day: {"555": (1, 1)})
        self.assertEqual(done["picks"], 1)
        self.assertEqual(self.history()["days"][0]["banko"]["status"], "won")

    def test_picks_outside_the_service_window_are_not_queried(self):
        old = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%MZ")
        fresh = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%MZ")
        self.freeze(day_coupons("2026-10-06", banko=[pick("111", "1", 1.5, 0.8, old), pick("222", "1", 1.5, 0.8, fresh)]))
        calls = []
        with db.session() as conn:
            ch.settle(conn, fetch_results=lambda day: calls.append(day) or {})
        self.assertEqual(calls, [])  # 5 gün önce servis dışı, 30 dk önce henüz bitmemiş

    def test_legacy_handicap_market_is_graded_and_unknown_market_voids_after_timeout(self):
        old = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%MZ")
        self.freeze(day_coupons("2026-10-01", banko=[pick(1, "Handikap -2 1", 1.9, 0.5, old), pick(2, "Handikap 2 2", 1.7, 0.6, old),
                                                     pick(3, "İY A1.5", 1.3, 0.8, old)]))
        self.add_match(1, 3, 0)
        self.add_match(2, 1, 2)
        self.add_match(3, 1, 0)
        self.assertEqual(self.settle()["picks"], 3)
        banko = self.history()["days"][0]["banko"]
        self.assertEqual([p["status"] for p in banko["picks"]], ["won", "lost", "void"])
        self.assertEqual(banko["status"], "lost")


class TrackingCalibrationTest(DbCase):
    def test_alternatives_are_tracked_settled_and_calibrated_but_not_listed(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%MZ")
        day = datetime.now(ch.TZ).date().isoformat()
        built = day_coupons(day, banko=[pick(1, kickoff=future)])
        built["banko_alternatives"] = [coupon([pick(1, "X", kickoff=future), pick(2, p=0.45, kickoff=future),
                                               {**pick(3, kickoff=future), "small_league": True}])]
        built["surprise_alternatives"] = [coupon([pick(4, kickoff=OLD)])]  # başlamış: izlenmez
        with db.session() as conn:
            self.assertEqual(ch.track_alternatives(conn, built), 3)
            self.assertEqual(ch.track_alternatives(conn, built), 0)
            rows = conn.execute("SELECT match_id, market FROM saved_coupon_picks WHERE kind='tracked' "
                                "ORDER BY match_id").fetchall()
            meta = db.get_meta(conn, f"coupon_metadata:{day}:tracked")
        self.assertEqual([(r["match_id"], r["market"]) for r in rows], [("1", "1"), ("2", "1"), ("3", "1")])
        self.assertIn('"iddaa_event_id": 3', meta)
        self.freeze(built)
        with db.session() as conn:
            conn.execute("UPDATE saved_coupon_picks SET status='won' WHERE match_id IN ('1', '3')")
            conn.execute("UPDATE saved_coupon_picks SET status='lost' WHERE match_id='2'")
            h = ch.history(conn)
        self.assertEqual(h["summary"]["banko"]["coupons"], 1)  # izlenenler geçmişe/kâra girmez
        bins = {b["range"]: b for b in h["calibration"]}
        self.assertEqual((bins["%40–50"]["n"], bins["%40–50"]["actual"]), (1, 0.0))
        self.assertEqual((bins["%80+"]["n"], bins["%80+"]["predicted"], bins["%80+"]["actual"]), (2, 0.8, 1.0))

    def test_odds_moves_annotate_picks(self):
        with db.session() as conn:
            for odd, at in ((2.0, "2026-10-01T10:00Z"), (1.9, "2026-10-01T12:00Z"), (1.8, "2026-10-01T14:00Z")):
                conn.execute("INSERT INTO iddaa_odds_history(event_id, market, odd, captured_at, kickoff) "
                             "VALUES('e1', '1', ?, ?, '2026-10-02T18:00Z')", (odd, at))
            built = {"banko": {"picks": [{**pick(1), "iddaa_event_id": "e1"}, pick(2)]},
                     "surprise_alternatives": [{"picks": [{**pick(9), "iddaa_event_id": "e1", "market": "X"}]}]}
            out = ch.odds_moves(conn, built)
        first, second = out["banko"]["picks"]
        self.assertEqual((first["odds_open"], first["odds_current"], first["odds_move"]), (2.0, 1.8, -0.1))
        self.assertNotIn("odds_move", second)
        self.assertNotIn("odds_move", out["surprise_alternatives"][0]["picks"][0])
