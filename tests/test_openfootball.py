import unittest
from datetime import date, datetime, timezone

from app.scrapers.openfootball import parse_es2


def document(matches):
    return {"name": "Spain Segunda División 2024/25", "matches": matches}


def match(**overrides):
    row = {"round": "Matchday 1", "date": "2024-08-15", "time": "20:30",
           "team1": "Granada CF", "team2": "Albacete", "score": {"ht": [1, 2], "ft": [1, 2]}}
    row.update(overrides)
    return row


class OpenFootballParseTests(unittest.TestCase):
    def test_normalizes_result_preserving_home_away_and_date_precision(self):
        [row] = parse_es2(document([match()]), "2024-25", as_of=date(2024, 8, 16))
        self.assertTrue(row["id"].startswith("openfootball:es.2:2024-08-15:Matchday 1:"))
        self.assertEqual((row["home"], row["away"]), ("Granada CF", "Albacete"))
        self.assertEqual((row["home_goals"], row["away_goals"]), (1, 2))
        self.assertEqual(row["kickoff"], "2024-08-15")
        self.assertEqual(row["source"]["kickoff_precision"], "date")
        self.assertEqual(row["source"]["source_time_local_unzoned"], "20:30")
        self.assertNotIn("ts", row)
        self.assertEqual(row["source"]["license"], "CC0-1.0")
        self.assertEqual(parse_es2(document([match()]), "2024-25", as_of=date(2024, 8, 16))[0]["id"], row["id"])

    def test_skips_unplayed_and_half_time_only_rows(self):
        rows = parse_es2(document([
            match(score={}),
            match(team2="Córdoba CF", score={"ht": [0, 0]}),
            match(team2="Málaga CF", date="2024-08-17", score=[0, 0]),
        ]), "2024-25", as_of=date(2024, 8, 16))
        self.assertEqual(rows, [])

    def test_retains_past_score_array_with_uncertainty_metadata(self):
        [row] = parse_es2(document([match(score=[0, 0])]), "2024-25", as_of=date(2024, 8, 16))
        self.assertEqual((row["home_goals"], row["away_goals"]), (0, 0))
        self.assertEqual(row["source"]["result_format"], "score_array_status_unverified")

    def test_cutoff_excludes_same_day_date_only_fixtures(self):
        before = datetime(2024, 8, 15, 23, 0, tzinfo=timezone.utc)
        rows = parse_es2(document([match(), match(date="2024-08-14", team1="A", team2="B")]),
                         "2024-25", as_of=date(2024, 8, 16), before=before)
        self.assertEqual([row["kickoff"] for row in rows], ["2024-08-14"])

    def test_rejects_future_scored_result_and_bad_full_time_scores(self):
        with self.assertRaisesRegex(ValueError, "future fixture"):
            parse_es2(document([match(date="2024-08-16")]), "2024-25", as_of=date(2024, 8, 15))
        with self.assertRaisesRegex(ValueError, "invalid full-time score"):
            parse_es2(document([match(score={"ft": [1, True]})]), "2024-25", as_of=date(2024, 8, 16))

    def test_deduplicates_identical_fixture_and_rejects_contradictory_duplicate(self):
        original = match()
        duplicate = dict(original)
        rows = parse_es2(document([original, duplicate]), "2024-25", as_of=date(2024, 8, 16))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["home"], rows[0]["away"]), ("Granada CF", "Albacete"))
        duplicate["score"] = {"ft": [2, 1]}
        with self.assertRaisesRegex(ValueError, "contradictory duplicate"):
            parse_es2(document([original, duplicate]), "2024-25", as_of=date(2024, 8, 16))
        duplicate = {**original, "team1": "Albacete", "team2": "Granada CF",
                     "score": {"ft": [2, 1]}}
        with self.assertRaisesRegex(ValueError, "reverses home/away"):
            parse_es2(document([original, duplicate]), "2024-25", as_of=date(2024, 8, 16))

    def test_rejects_wrong_league_and_season_mismatch(self):
        with self.assertRaisesRegex(ValueError, "Spain Segunda"):
            parse_es2({"name": "Spain Primera División 2024/25", "matches": []}, "2024-25")
        with self.assertRaisesRegex(ValueError, "does not match"):
            parse_es2(document([]), "2025-26")


if __name__ == "__main__":
    unittest.main()
