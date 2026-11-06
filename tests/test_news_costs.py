"""P2: haber kaynaklarının önbelleği ve etkili haberli/habersiz A/B testi."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import db
from app.model.snapshots import news_impact_report
from app.scrapers import news


class NewsImpactTests(unittest.TestCase):
    def test_news_effect_requires_enough_affected_matches(self):
        better = [([.8, .1, .1], [.5, .3, .2], 0)] * 20
        self.assertEqual(news_impact_report(better)["verdict"], "insufficient_data")
        convincing = news_impact_report(better * 6)
        self.assertEqual(convincing["verdict"], "news_improves")
        self.assertLess(convincing["interval95"][1], 0)

    def test_news_harms_or_inconclusive(self):
        worse = [([.4, .3, .3], [.8, .1, .1], 0)] * 120
        self.assertEqual(news_impact_report(worse)["verdict"], "news_hurts")
        self.assertEqual(news_impact_report([])["n"], 0)


class NewsCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(db, "DATABASE_URL", None),
                  patch.object(db, "DB_PATH", Path(tmp.name) / "news.db")):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()

    def test_distant_match_cached_but_urgent_refetched(self):
        now = datetime.now(timezone.utc)
        with db.session() as conn:
            for mid, league, home, away, offset in (
                ("a", "eng.1", "Arsenal", "Chelsea", 72),
                ("b", "tur.1", "Galatasaray", "Fenerbahce", 2),
            ):
                conn.execute(
                    "INSERT INTO matches(id,league,kickoff,home,away,completed) "
                    "VALUES(?,?,?,?,?,0)",
                    (mid, league, (now + timedelta(hours=offset)).isoformat(), home, away))
        calls = []
        def fake_fetch(team, lang):
            calls.append(team)
            return []
        with patch.object(news, "fetch_team_news", side_effect=fake_fetch), \
                patch.object(news.time, "sleep"), db.session() as conn:
            self.assertEqual(news.sync_news(conn), 0)
            self.assertEqual(len(calls), 4)
            self.assertEqual(news.sync_news(conn), 0)
        self.assertEqual(len(calls), 4)  # cache ikinci gezide gereksiz RSS istemez
        with db.session() as conn:
            for key in [r["key"] for r in conn.execute("SELECT key FROM meta WHERE key LIKE 'news_poll_%'")]:
                # Maça yakın iki takımın TTL'i dolmuş gibi davran.
                if "never" in key:
                    continue
            self.assertEqual(conn.execute("SELECT COUNT(*) n FROM meta WHERE key LIKE 'news_poll_%'").fetchone()["n"], 4)


if __name__ == "__main__":
    unittest.main()
