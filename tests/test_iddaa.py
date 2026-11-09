import unittest

from app.scrapers.iddaa import match_event, normalize_team, parse_event


class IddaaScraperTests(unittest.TestCase):
    def test_team_aliases_and_turkish_characters(self):
        self.assertEqual(normalize_team("İstanbul Başakşehir FK"), "basaksehir")
        self.assertEqual(normalize_team("Rams Basaksehir"), "basaksehir")
        self.assertEqual(normalize_team("Çaykur Rizespor"), "rizespor")
        self.assertEqual(normalize_team("Manchester City FC"), "manchester city")

    def test_required_markets_are_parsed(self):
        raw = {
            "i": 123,
            "ci": 584,
            "d": 1791220000,
            "hn": "Fenerbahçe",
            "an": "Beşiktaş",
            "m": [
                {"t": 1, "st": 1, "s": 1, "o": [
                    {"no": 1, "odd": 1.91}, {"no": 2, "odd": 3.2}, {"no": 3, "odd": 3.8}]},
                {"t": 2, "st": 92, "s": 1, "o": [
                    {"no": 1, "odd": 1.18}, {"no": 2, "odd": 1.25}, {"no": 3, "odd": 1.55}]},
                {"t": 2, "st": 101, "s": 1, "sov": "1.5", "o": [
                    {"no": 1, "odd": 2.4}, {"no": 2, "odd": 1.3}]},
                {"t": 2, "st": 101, "s": 1, "sov": "2.5", "o": [
                    {"no": 1, "odd": 1.75}, {"no": 2, "odd": 1.85}]},
                {"t": 2, "st": 101, "s": 1, "sov": "3.5", "o": [
                    {"no": 1, "odd": 1.35}, {"no": 2, "odd": 2.3}]},
                {"t": 2, "st": 89, "s": 1, "o": [
                    {"no": 1, "odd": 1.64}, {"no": 2, "odd": 1.77}]},
            ],
        }
        event = parse_event(raw)
        self.assertEqual(event["league"], "tur.1")
        self.assertEqual(event["odds"], {
            "1": 1.91, "X": 3.2, "2": 3.8,
            "1X": 1.18, "12": 1.25, "X2": 1.55,
            "A1.5": 2.4, "Ü1.5": 1.3,
            "A2.5": 1.75, "Ü2.5": 1.85,
            "A3.5": 1.35, "Ü3.5": 2.3,
            "KG Var": 1.64, "KG Yok": 1.77,
        })

    def test_event_matches_fixture_by_alias_and_time(self):
        event = {
            "league": "eng.1", "home": "Man City", "away": "Wolves",
            "kickoff": "2026-10-17T14:00:00+00:00",
        }
        fixtures = [
            {"id": "espn-1", "league": "eng.1", "home": "Manchester City",
             "away": "Wolverhampton Wanderers", "kickoff": "2026-10-17T14:00Z"},
            {"id": "espn-2", "league": "eng.1", "home": "Manchester United",
             "away": "Everton", "kickoff": "2026-10-17T14:00Z"},
        ]
        matched, score = match_event(event, fixtures)
        self.assertEqual(matched["id"], "espn-1")
        self.assertGreater(score, 0.95)

    def test_ambiguous_similar_fixtures_do_not_match(self):
        event = {"league": "eng.1", "home": "Man City", "away": "Wolves",
                 "kickoff": "2026-10-17T14:00:00+00:00"}
        fixtures = [
            {"id": "first", "league": "eng.1", "home": "Man City",
             "away": "Wolves", "kickoff": "2026-10-17T14:00Z"},
            {"id": "second", "league": "eng.1", "home": "Man City",
             "away": "Wolves", "kickoff": "2026-10-17T14:10Z"},
        ]
        self.assertEqual(match_event(event, fixtures), (None, 0.0))

    def test_extra_bulletin_markets_use_verified_market_ids(self):
        raw = {"i": 123, "ci": 584, "d": 1791220000, "hn": "Fenerbahçe", "an": "Beşiktaş", "m": [
            {"t": 2, "st": 88, "s": 1, "o": [{"no": 1, "odd": 2.1}, {"no": 2, "odd": 2.8}, {"no": 3, "odd": 3.2}]},
            {"t": 2, "st": 90, "s": 1, "o": [{"no": i, "odd": 2 + i} for i in range(1, 10)]},
            {"t": 2, "st": 100, "s": 1, "sov": "-1", "o": [{"no": 1, "odd": 2.1}, {"no": 2, "odd": 3.0}, {"no": 3, "odd": 2.5}]},
            {"t": 2, "st": 60, "s": 1, "sov": "1.5", "o": [{"no": 1, "odd": 1.8}, {"no": 2, "odd": 2.0}]},
            {"t": 2, "st": 48, "s": 1, "sov": "9.5", "o": [{"no": 1, "odd": 1.8}, {"no": 2, "odd": 2.0}]},
            {"t": 2, "st": 658, "s": 1, "sov": "4.5", "o": [{"no": 1, "odd": 1.8}, {"no": 2, "odd": 2.0}]},
        ]}
        self.assertEqual(parse_event(raw)["odds"], {
            "İY 1": 2.1, "İY X": 2.8, "İY 2": 3.2,
            **{f"İY/MS {a}/{b}": 2 + i for i, (a, b) in enumerate(
                [(a, b) for a in ("1", "X", "2") for b in ("1", "X", "2")], 1)},
            "Handikap -1 1": 2.1, "Handikap -1 X": 3.0, "Handikap -1 2": 2.5,
            "İY A1.5": 1.8, "İY Ü1.5": 2.0,
            "Korner A9.5": 1.8, "Korner Ü9.5": 2.0,
            "Kart A4.5": 1.8, "Kart Ü4.5": 2.0,
        })


if __name__ == "__main__":
    unittest.main()


class SyncOddsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from app import db
        self.tmp = tempfile.TemporaryDirectory()
        patcher = patch.object(db, "DB_PATH", Path(self.tmp.name) / "t.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        db.init_db()
        with db.session() as conn:
            conn.execute("INSERT INTO matches(id, league, kickoff, home, away, completed) VALUES "
                         "('espn-1','eng.1','2026-10-17T14:00Z','Manchester City','Wolverhampton Wanderers',0)")

    @staticmethod
    def raw(i, home, away, odd, ts=1792245600):
        return {"i": i, "ci": 43, "d": ts, "hn": home, "an": away,
                "m": [{"t": 1, "st": 1, "s": 1, "o": [{"no": 1, "odd": odd}, {"no": 2, "odd": 3.5}, {"no": 3, "odd": 5.0}]}]}

    def test_one_event_per_fixture_and_lookup(self):
        from unittest.mock import patch
        from app import db
        from app.scrapers import iddaa
        payload = {"data": {"events": [self.raw(1, "Man City", "Wolves", 1.45),
                                       self.raw(2, "Man City", "Wolves", 9.99, ts=1792245600 + 600)]}}
        with patch.object(iddaa, "fetch_json", return_value=payload), db.session() as conn:
            stats = iddaa.sync_odds(conn)
            odds = iddaa.odds_for_matches(conn, ["espn-1"])
        self.assertEqual(stats["matched"], 0)
        self.assertEqual(stats["ambiguous_fixtures"], 1)
        self.assertEqual(odds, {})  # birden çok eşdeğer kaynağı sessizce seçme

    def test_unreachable_iddaa_raises_and_keeps_tables_readable(self):
        from unittest.mock import patch
        from app import db
        from app.scrapers import iddaa
        with patch.object(iddaa, "fetch_json", side_effect=RuntimeError("down")), db.session() as conn:
            with self.assertRaises(RuntimeError):
                iddaa.sync_odds(conn)
            self.assertEqual(iddaa.odds_for_matches(conn, ["espn-1"]), {})

    def test_catalog_timeout_preserves_known_small_league_labels(self):
        from unittest.mock import patch
        from app import db
        from app.scrapers import iddaa
        raw = self.raw(3, 'Home', 'Away', 1.8)
        raw['ci'] = 999999
        with db.session() as conn:
            conn.execute("INSERT INTO iddaa_events(id,league,competition_id,kickoff,home,away,league_name,country,is_small,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ('old','small:bg:second',999999,'2026-11-10T12:00Z','Home','Away','Second League','BG',1,'2026-11-09T10:00Z'))
            with patch.object(iddaa, 'fetch_json', return_value={'data': {'events': [raw]}}), \
                 patch.object(iddaa, 'fetch_competitions', side_effect=TimeoutError), \
                 patch.object(iddaa, 'enrich_events', return_value={}):
                iddaa.sync_odds(conn)
            event = conn.execute('SELECT league_name,country FROM iddaa_events').fetchone()
            self.assertEqual((event['league_name'], event['country']), ('Second League','BG'))


def test_turkish_and_german_aliases_match_espn_names():
    from app.scrapers import iddaa
    pairs = [("Amed Sportif Faaliyetler", "Amed SFK"), ("Gençlerbirliği", "Genclerbirligi"),
             ("Köln", "FC Cologne"), ("Monchengladbach", "Borussia Mönchengladbach"),
             ("Çaykur Rizespor", "Caykur Rizespor"), ("Kasımpaşa", "Kasimpasa"),
             ("Erzurumspor", "Erzurum BB"), ("Çorum FK", "Corum FK"),
             ("İstanbul Başakşehir", "Istanbul Basaksehir")]
    for a, b in pairs:
        assert iddaa.team_similarity(a, b) == 1.0, (a, b)
