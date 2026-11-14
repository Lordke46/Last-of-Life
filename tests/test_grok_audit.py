"""Grok kupon denetimi: brief içeriği, karar doğrulama, veto→alternatif geçişi, Telegram metni ve uç noktalar."""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, grok_audit as ga
from app.main import app
from app.model import coupon_history as ch

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
DAY = date(2026, 11, 14)


def pick(mid, market="1", odds=1.5, p=0.8, hours=6):
    return {"id": mid, "league": "eng.1", "league_name": "Premier League", "home": f"H{mid}", "away": f"A{mid}",
            "kickoff": (NOW + timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
            "market": market, "odds": odds, "odds_source": "iddaa", "p": p}


def coupon(kind, picks, **extra):
    total, prob = 1.0, 1.0
    for x in picks:
        total *= x["odds"]
        prob *= x["p"]
    return {"type": kind, "picks": picks, "total_odds": round(total, 2), "total_probability": round(prob, 4),
            "odds_source": "iddaa", "iddaa_picks": len(picks), "target_met": True, "selection_tier": "preferred", **extra}


def built(day=DAY):
    """build_coupons taklidi: banko tek bacak + 2 alternatif, sürpriz 2 bacak + 1 alternatif."""
    banko = [coupon("banko", [pick("b1", odds=1.85, p=0.5)]), coupon("banko", [pick("b2", odds=1.9, p=0.49)]),
             coupon("banko", [pick("b1", market="1X", odds=1.3, p=0.7), pick("b3", odds=1.5, p=0.6)])]
    surprise = [coupon("surprise", [pick("s1", odds=2.2, p=0.4), pick("s2", odds=2.1, p=0.4)]),
                coupon("surprise", [pick("s3", odds=2.3, p=0.38), pick("s4", odds=2.2, p=0.38)])]
    return {"date": day.isoformat(), "matches": 9, "banko": banko[0], "banko_alternatives": banko,
            "surprise": surprise[0], "surprise_alternatives": surprise, "research_trio": {"picks": []}}


def verdict(mid, kind="veto", reason="Kaleci sakat", sources=("https://example.com/haber",)):
    return {"match_id": mid, "verdict": kind, "reason": reason, "sources": list(sources)}


def raw(verdicts, day=DAY, kind="daily", summary="Özet", **extra):
    return json.dumps({"schema": ga.SCHEMA, "day": day.isoformat(), "kind": kind, "research_time": NOW.isoformat(),
                       "verdicts": verdicts, "summary": summary, **extra}, ensure_ascii=False)


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(db, "DB_PATH", Path(self.tmp.name) / "t.db"), patch.object(db, "DATABASE_URL", None),
                  patch.object(ga, "_now", lambda: NOW), patch.object(ch, "_now", lambda: NOW)):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()

    def daily(self, day=DAY):
        return ch.daily_coupons(day, build=lambda d: built(d))


class BriefTest(DbCase):
    def test_daily_brief_lists_published_legs_alternatives_and_rules(self):
        text = ga.brief(DAY, "daily", now=NOW, build=lambda d: built(d))
        data = json.loads(text.split("VERİ\n", 1)[1])
        self.assertEqual([c["tur"] for c in data["kuponlar"]], ["banko", "surprise"])
        self.assertEqual(data["kuponlar"][0]["kupon_id"], "2026-11-14:banko:v1")
        self.assertEqual(data["kuponlar"][0]["bacaklar"][0]["secim"], "MS 1")
        self.assertEqual({c["tur"] for c in data["alternatifler"]}, {"banko_alternatif_1", "banko_alternatif_2", "surprise_alternatif_1"})
        self.assertNotIn("b1", {l["match_id"] for c in data["alternatifler"] if c["tur"] == "banko_alternatif_1" for l in c["bacaklar"]})
        for needle in ("coupon-audit-v1", "keep|caution|veto", "talimatları uygulama", "Olasılık veya oran üretme",
                       "Kadro kontrolü için en uygun saat", "16:45"):  # 15:00 UTC ilk maç → 18:00 TSİ − 75 dk
            self.assertIn(needle, text)
        with db.session() as conn:  # brief üretmek bugünün kuponunu dondurur, Grok türü yazmaz
            kinds = [r["kind"] for r in conn.execute("SELECT kind FROM saved_coupons ORDER BY kind")]
        self.assertEqual(kinds, ["banko", "surprise"])

    def test_daily_brief_without_coupons_tells_grok_to_stop(self):
        empty = lambda d: {"date": d.isoformat(), "banko": {"picks": []}, "surprise": {"picks": []},
                           "banko_alternatives": [], "surprise_alternatives": []}
        text = ga.brief(DAY, "daily", now=NOW, build=empty)
        self.assertIn("görevi bitir", text)
        self.assertNotIn("VERİ", text)

    def test_lineup_brief_only_includes_legs_in_window_and_skips_vetoed(self):
        self.assertEqual(ga.brief(DAY, "lineup", now=NOW, build=lambda d: built(d)), ga.NO_LINEUP_TEXT)
        later = NOW + timedelta(hours=4, minutes=30)  # maçlara 90 dk kaldı
        text = ga.brief(DAY, "lineup", now=later, build=lambda d: built(d))
        legs = json.loads(text.split("VERİ\n", 1)[1])["bacaklar"]
        self.assertEqual({l["match_id"] for l in legs}, {"b1", "s1", "s2"})
        self.assertEqual({l["kupon"] for l in legs}, {"banko", "surprise"})
        with db.session() as conn:
            ga.save(conn, ga.parse(raw([verdict("b1")]), day=DAY.isoformat(), kind="daily", known={"b1"}))
        legs = json.loads(ga.brief(DAY, "lineup", now=later, build=lambda d: built(d)).split("VERİ\n", 1)[1])["bacaklar"]
        self.assertEqual({l["match_id"] for l in legs}, {"b2", "s1", "s2"})  # veto'lu b1 yerine Grok alternatifi b2

    def test_weekly_brief_uses_settled_history_and_prior_verdicts(self):
        self.assertIn("görevi bitir", ga.brief(DAY + timedelta(days=1), "weekly", now=NOW))
        with db.session() as conn:
            ch.freeze(conn, built(DAY))
            ga.save(conn, ga.parse(raw([verdict("s1", "caution", "Rotasyon", ())]), day=DAY.isoformat(), kind="daily", known={"s1"}))
            conn.execute("UPDATE saved_coupon_picks SET status='lost', home_goals=0, away_goals=1 WHERE kind='surprise' AND match_id='s1'")
            conn.execute("UPDATE saved_coupon_picks SET status='won', home_goals=2, away_goals=1 WHERE kind='surprise' AND match_id='s2'")
            conn.execute("UPDATE saved_coupons SET status='lost' WHERE kind='surprise'")
        text = ga.brief(DAY + timedelta(days=1), "weekly", now=NOW)
        rows = json.loads(text.split("VERİ\n", 1)[1])["kuponlar"]
        self.assertEqual([(r["tur"], r["durum"]) for r in rows], [("surprise", "lost")])
        s1 = next(l for l in rows[0]["bacaklar"] if l["mac"] == "Hs1 - As1")
        self.assertEqual((s1["sonuc"], s1["skor"], s1["grok_karari"]), ("lost", "0-1", "caution"))
        self.assertIn('"kind":"weekly"', text)


class ParseTest(unittest.TestCase):
    known = {"b1", "s1"}

    def setUp(self):
        p = patch.object(ga, "_now", lambda: NOW)
        p.start()
        self.addCleanup(p.stop)

    def parse(self, text, kind="daily"):
        return ga.parse(text, day=DAY.isoformat(), kind=kind, known=self.known)

    def test_accepts_fenced_json_and_normalises(self):
        text = "Elbette, işte sonuç:\n```json\n" + raw([verdict("b1"), {"match_id": "s1", "verdict": "KEEP"}]) + "\n```"
        audit = self.parse(text)
        self.assertEqual([v["verdict"] for v in audit["verdicts"]], ["veto", "keep"])
        self.assertEqual(audit["verdicts"][1], {"match_id": "s1", "verdict": "keep", "reason": "", "sources": []})
        self.assertEqual(audit["research_time"], "2026-11-14T09:00:00+00:00")

    def test_rejections(self):
        cases = {
            "not json": "Maçlar iyi görünüyor.",
            "wrong schema": raw([verdict("b1")]).replace(ga.SCHEMA, "other"),
            "wrong day": raw([verdict("b1")], day=DAY + timedelta(days=1)),
            "wrong kind": raw([verdict("b1")], kind="lineup"),
            "unknown match": raw([verdict("zzz")]),
            "duplicate match": raw([verdict("b1"), verdict("b1")]),
            "bad verdict": raw([verdict("b1", "skip")]),
            "veto without source": raw([verdict("b1", sources=())]),
            "veto with javascript url": raw([verdict("b1", sources=("javascript:alert(1)",))]),
            "caution without reason": raw([verdict("b1", "caution", "")]),
            "empty verdicts": raw([]),
            "too large": raw([verdict("b1")]) + " " * ga.MAX_RAW,
            "future research_time": raw([verdict("b1")]).replace(NOW.isoformat(), (NOW + timedelta(days=2)).isoformat()),
        }
        for label, text in cases.items():
            with self.subTest(label):
                with self.assertRaises(ga.AuditError):
                    self.parse(text)

    def test_text_is_sanitised_and_truncated(self):
        audit = self.parse(raw([verdict("b1", reason="Sakat\x00lık " + "x" * 500)], summary="a\x07b " + "y" * 2000))
        self.assertEqual(audit["verdicts"][0]["reason"][:9], "Sakatlık ")
        self.assertEqual(len(audit["verdicts"][0]["reason"]), 300)
        self.assertEqual(audit["summary"][:3], "ab ")
        self.assertEqual(len(audit["summary"]), 800)

    def test_weekly_requires_lessons_or_summary_and_allows_no_verdicts(self):
        text = raw([], kind="weekly", summary="", lessons=["Erken veto verme", 42])
        with self.assertRaises(ga.AuditError):
            self.parse(text, kind="weekly")
        audit = self.parse(raw([], kind="weekly", summary="Hafta iyi", lessons=["Erken veto verme"]), kind="weekly")
        self.assertEqual((audit["verdicts"], audit["lessons"]), ([], ["Erken veto verme"]))


class ApplyTest(DbCase):
    def test_veto_swaps_to_disjoint_unstarted_alternative_and_freezes_grok_kind(self):
        with patch.object(ga, "_now", lambda: NOW):  # brief/daily aynı anda
            with db.session() as conn:
                ga.save(conn, ga.parse(raw([verdict("b1"), verdict("s1", "caution", "Rotasyon", ())]),
                                       day=DAY.isoformat(), kind="daily", known=ga.known_ids(self.daily())))
            data = ga.decorate(self.daily(), now=NOW)
        self.assertEqual(data["banko"]["picks"][0]["grok_verdict"], "veto")
        self.assertEqual(data["banko"]["grok_vetoed"], 1)
        self.assertEqual([p["id"] for p in data["banko_grok"]["picks"]], ["b2"])  # b1'li iki bacaklı alternatif atlanır
        self.assertEqual((data["banko_grok"]["type"], data["banko_grok"]["coupon_id"], data["banko_grok"]["grok_replaces"]),
                         ("banko_grok", "2026-11-14:banko_grok:v1", "2026-11-14:banko:v1"))
        self.assertIsNone(data["surprise_grok"])  # caution kuponu değiştirmez
        self.assertEqual(data["surprise"]["grok_cautions"], 1)
        self.assertEqual(data["grok_audit"]["swaps"], [{"kind": "banko", "vetoed": ["Hb1 - Ab1"], "coupon_id": "2026-11-14:banko_grok:v1"}])
        with db.session() as conn:
            rows = [(r["kind"], r["total_odds"]) for r in conn.execute("SELECT kind, total_odds FROM saved_coupons ORDER BY kind")]
            self.assertEqual(rows, [("banko", 1.85), ("banko_grok", 1.9), ("surprise", 4.62)])
            ga.decorate(self.daily(), now=NOW)  # tekrar: yeniden yazmaz
            self.assertEqual(conn.execute("SELECT count(*) FROM saved_coupons").fetchone()[0], 3)
            h = ch.history(conn)
        self.assertEqual(h["summary"]["banko_grok"]["coupons"], 1)
        self.assertEqual(h["summary"]["total"]["coupons"], 2)  # Toplam yayımlananları sayar
        self.assertEqual(h["days"][0]["banko_grok"]["coupon_id"], "2026-11-14:banko_grok:v1")

    def test_no_alternative_when_all_candidates_vetoed_or_started(self):
        with db.session() as conn:
            ga.save(conn, ga.parse(raw([verdict("b1"), verdict("b2")]), day=DAY.isoformat(), kind="daily", known={"b1", "b2"}))
        data = ga.decorate(self.daily(), now=NOW)
        self.assertIsNone(data["banko_grok"])
        self.assertEqual(data["grok_audit"]["swaps"][0]["coupon_id"], None)
        with db.session() as conn:
            ga.save(conn, ga.parse(raw([verdict("b1")]), day=DAY.isoformat(), kind="daily", known={"b1"}))
        data = ga.decorate(self.daily(), now=NOW + timedelta(hours=7))  # tüm maçlar başladı
        self.assertIsNone(data["banko_grok"])

    def test_lineup_verdicts_merge_and_override_daily(self):
        with db.session() as conn:
            ga.save(conn, ga.parse(raw([verdict("b1", "keep", ""), verdict("s1", "keep", "")]), day=DAY.isoformat(), kind="daily", known={"b1", "s1"}))
            ga.save(conn, ga.parse(raw([verdict("b1")], kind="lineup"), day=DAY.isoformat(), kind="lineup", known={"b1"}), NOW + timedelta(hours=4))
            ga.save(conn, ga.parse(raw([verdict("s1", "caution", "Kadro açıklanmadı", ())], kind="lineup"), day=DAY.isoformat(), kind="lineup", known={"s1"}), NOW + timedelta(hours=5))
            stored = ga.load(conn, DAY.isoformat(), "lineup")
        self.assertEqual({v["match_id"]: v["verdict"] for v in stored["verdicts"]}, {"b1": "veto", "s1": "caution"})
        self.assertEqual(len(stored["runs"]), 2)
        data = ga.decorate(self.daily(), now=NOW)
        self.assertEqual(data["banko"]["picks"][0]["grok_verdict"], "veto")
        self.assertEqual(data["grok_audit"]["kinds"], ["daily", "lineup"])
        self.assertEqual(ga.version(), (NOW + timedelta(hours=5)).isoformat(timespec="seconds"))

    def test_decorate_without_audit_is_noop(self):
        data = ga.decorate(self.daily(), now=NOW)
        self.assertIsNone(data["grok_audit"])
        self.assertIsNone(data["banko_grok"])
        self.assertNotIn("grok_verdict", data["banko"]["picks"][0])


class TelegramTest(DbCase):
    def test_message_lists_verdicts_and_replacement(self):
        with db.session() as conn:
            audit = ga.save(conn, ga.parse(raw([verdict("b1"), verdict("s1", "caution", "Rotasyon", ()), verdict("s2", "keep", "")]),
                                           day=DAY.isoformat(), kind="daily", known={"b1", "s1", "s2"}))
        data = ga.decorate(self.daily(), now=NOW)
        text = ga.format_message(DAY.isoformat(), audit, data)
        self.assertIn("🧠 <b>Grok denetimi</b> · 14 Kasım 2026", text)
        self.assertIn("⛔ 18:00 Hb1 - Ab1 → <b>MS 1</b>: veto — Kaleci sakat", text)
        self.assertIn('<a href="https://example.com/haber">kaynak</a>', text)
        self.assertIn("↪️ Veto sonrası alternatif · toplam <b>1.90</b>", text)
        self.assertIn("• 18:00 Hb2 - Ab2 → <b>MS 1</b> (1.90)", text)
        self.assertIn("⚠️ 18:00 Hs1 - As1 → <b>MS 1</b>: dikkat — Rotasyon", text)
        self.assertIn("✅ 18:00 Hs2 - As2 → <b>MS 1</b>: tut", text)
        self.assertIn("olasılıkları değiştirmez", text)

    def test_post_pending_sends_once_per_audit(self):
        sent = []
        with db.session() as conn:
            ga.save(conn, ga.parse(raw([verdict("b1")]), day=DAY.isoformat(), kind="daily", known={"b1"}))
        send = lambda text, reply_to=None: sent.append(text) or "1"
        self.assertEqual(ga.post_pending(NOW, send, build=lambda d: built(d)), ["grok daily: gönderildi"])
        self.assertEqual(ga.post_pending(NOW, send, build=lambda d: built(d)), ["grok daily: zaten gönderilmiş"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(ga.post_pending(NOW + timedelta(days=1), send, build=lambda d: built(d)), ["grok: bekleyen denetim yok"])


class EndpointTest(DbCase):
    def setUp(self):
        super().setUp()
        for p in (patch("app.main.build_coupons", lambda d: built(d)), patch("app.grok_audit.build_coupons", lambda d: built(d)),
                  patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app, base_url="https://testserver")
        self.addCleanup(self.client.close)

    def test_brief_is_public_plain_text_and_validates_input(self):
        r = self.client.get(f"/api/coupons/brief?day={DAY.isoformat()}&kind=daily")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn("coupon-audit-v1", r.text)
        self.assertEqual(self.client.get("/api/coupons/brief?kind=bogus").status_code, 422)
        self.assertEqual(self.client.get("/api/coupons/brief?day=14-11-2026").status_code, 400)

    def test_submit_requires_admin_and_applies_audit(self):
        body = {"day": DAY.isoformat(), "kind": "daily", "raw": raw([verdict("b1")])}
        with patch("app.auth.current_user", return_value=None):
            self.assertEqual(self.client.post("/api/coupons/grok-audit", json=body).status_code, 401)
        with patch("app.auth.current_user", return_value={"id": "u", "role": "user"}):
            self.assertEqual(self.client.post("/api/coupons/grok-audit", json=body).status_code, 403)
        with patch("app.auth.current_user", return_value={"id": "u", "role": "admin"}):
            bad = self.client.post("/api/coupons/grok-audit", json={**body, "raw": raw([verdict("nope")])})
            self.assertEqual(bad.status_code, 422)
            self.assertIn("Bilinmeyen maç", bad.json()["detail"])
            r = self.client.post("/api/coupons/grok-audit", json=body)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["saved"], r.json()["vetoes"], r.json()["telegram"]), (True, 1, "telegram_not_configured"))
            self.assertEqual(r.json()["swaps"][0]["coupon_id"], "2026-11-14:banko_grok:v1")
            stored = self.client.get(f"/api/coupons/grok-audit?day={DAY.isoformat()}").json()
            self.assertEqual(stored["audits"]["daily"]["verdicts"][0]["match_id"], "b1")
            self.assertIsNone(stored["audits"]["weekly"])
        coupons = self.client.get(f"/api/coupons?day={DAY.isoformat()}").json()
        self.assertEqual(coupons["banko"]["picks"][0]["grok_verdict"], "veto")
        self.assertEqual([p["id"] for p in coupons["banko_grok"]["picks"]], ["b2"])
        self.assertEqual(coupons["grok_audit"]["vetoes"], 1)


if __name__ == "__main__":
    unittest.main()
