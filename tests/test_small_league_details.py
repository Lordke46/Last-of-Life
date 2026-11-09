import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import db, main
from app.model.small_leagues import bulletin_event, market_favorite
from app.scrapers import iddaa


class SmallLeagueDetailsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (patch.object(db, "DB_PATH", Path(self.tmp.name) / "test.db"),
                        patch.object(db, "DATABASE_URL", None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()

    def test_scraper_event_and_competition_ids_reach_upcoming_and_detail(self):
        event_id, competition_id = "small-event-93001", 98765
        kickoff = datetime.now(timezone.utc) + timedelta(days=1)
        raw = {
            "i": event_id,
            "ci": str(competition_id),
            "d": int(kickoff.timestamp()),
            "hn": "Example Home",
            "an": "Example Away",
            "countryName": "Faroe Islands",
            "competitionName": "Premier League",
            "m": [{"t": 1, "st": 1, "s": 1, "o": [
                {"no": 1, "odd": 1.3}, {"no": 2, "odd": 6.0}, {"no": 3, "odd": 9.0},
            ]}],
        }
        with patch.object(iddaa, "fetch_json", return_value={"data": {"events": [raw]}}), \
                patch.object(iddaa, "fetch_competitions", return_value={}):
            with db.session() as conn:
                stats = iddaa.sync_odds(conn)

        league = "small:faroe-islands:premier-league"
        rows = main.upcoming(league=league, days=14)
        self.assertEqual(stats["small_league_events"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["event_id"], rows[0]["iddaa_event_id"]),
                         (event_id, event_id, event_id))
        self.assertEqual(rows[0]["competition_id"], competition_id)
        self.assertEqual(rows[0]["iddaa_odds"], {"1": 1.3, "X": 6.0, "2": 9.0})

        detail = main.match(event_id, competition_id=competition_id)
        self.assertEqual((detail["iddaa_event_id"], detail["competition_id"]), (event_id, competition_id))
        self.assertEqual(detail["iddaa_odds"], rows[0]["iddaa_odds"])
        self.assertEqual(detail["confidence_label"], "Model güveni yok")
        self.assertEqual(detail["recommendation_tools"]["probability_source"], "iddaa_market")
        self.assertNotIn("recommendation_tools", rows[0])  # Günlük listeye ağır karşılaştırma eklenmez.
        self.assertTrue({"1", "X", "2", "1X", "12", "X2", "Ü1.5", "A1.5", "Ü2.5",
                         "A2.5", "Ü3.5", "A3.5", "KG Var", "KG Yok", "Ev 0.5Ü", "Dep 0.5Ü"}
                        <= set(detail["markets"]))
        self.assertIsNone(detail["lambda_home"])
        self.assertIsNone(detail["lambda_away"])
        self.assertNotIn("factors", detail)
        self.assertFalse(detail["data_availability"]["xg"])
        self.assertFalse(detail["data_availability"]["scores"])
        self.assertAlmostEqual(detail["markets"]["1"]["market_p"],
                               (1 / 1.3) / (1 / 1.3 + 1 / 6 + 1 / 9), places=4)

        # The recommendation helper consumes the detail API's bulletin DTO. It
        # returns only source odds and market-implied probability, never a model price.
        candidate = market_favorite(detail)
        self.assertEqual(candidate["market"], "1")
        self.assertEqual(candidate["market_source"], "iddaa")
        self.assertEqual(candidate["market_source_label"], "İddaa piyasa oranları")
        self.assertEqual(candidate["probability_source"], "iddaa_market")
        self.assertEqual(candidate["odds_source"], "iddaa")
        self.assertEqual(candidate["odds"], 1.3)
        self.assertAlmostEqual(candidate["p"], detail["markets"]["1"]["market_p"], places=4)
        self.assertEqual(candidate["candidate_type"], "market_favorite")
        self.assertIsNone(candidate["suggested"])
        self.assertIsNone(candidate["confidence"])
        self.assertEqual(candidate["confidence_label"], "Düşük · model güveni yok")
        self.assertFalse(candidate["model_available"])

        with self.assertRaises(HTTPException):
            main.match(event_id, competition_id=competition_id + 1)

    def test_missing_bulletin_markets_stay_unavailable_without_synthetic_model_values(self):
        result = bulletin_event({"id": "event-no-odds", "iddaa_odds": {}})

        self.assertIsNone(result["lambda_home"])
        self.assertIsNone(result["lambda_away"])
        self.assertIsNone(result["markets"]["1"]["p"])
        self.assertIsNone(result["markets"]["1"]["suggested"])
        self.assertIsNone(result["confidence"])
        self.assertEqual(result["confidence_label"], "Model güveni yok")
        self.assertFalse(result["data_availability"]["form"])
        self.assertFalse(result["data_availability"]["standings"])

    def test_market_favorite_requires_complete_valid_1x2_odds(self):
        partial = bulletin_event({"id": "partial", "iddaa_odds": {"1": 1.3, "X": 6.0}})
        invalid_book = bulletin_event({"id": "invalid", "iddaa_odds": {"1": 1.01, "X": 1.01, "2": 1.01}})
        no_clear_favorite = bulletin_event({"id": "balanced", "iddaa_odds": {"1": 2.0, "X": 3.5, "2": 4.0}})

        self.assertIsNone(market_favorite(partial))
        self.assertIsNone(market_favorite(invalid_book))
        self.assertIsNone(market_favorite(no_clear_favorite))


if __name__ == "__main__":
    unittest.main()


class BulletinCandidateTests(unittest.TestCase):
    def test_low_probability_choice_keeps_below_threshold_status_in_api_summary(self):
        from app.explain import summarize
        result=bulletin_event({'id':'low','small_league':True,
                              'iddaa_odds':{'1':2.28,'X':2.86,'2':2.75}})
        self.assertLess(result['pick']['p'],.56)
        self.assertEqual(result['pick']['candidate_status'],'below_threshold')
        self.assertFalse(result['pick']['target_met'])
        self.assertEqual(result['pick']['selection_tier'],'best_available')
        self.assertEqual(summarize(result)['recommendation_label'],'Aday seçenek')
        self.assertIsNone(result['confidence'])

    def test_preferred_market_choice_is_still_not_model_confidence(self):
        result=bulletin_event({'id':'preferred','iddaa_odds':{'1':1.6,'X':4.,'2':6.}})
        self.assertTrue(result['pick']['target_met'])
        self.assertEqual(result['pick']['candidate_status'],'market_candidate')
        self.assertIsNone(result['confidence'])

    def test_outside_band_choice_keeps_qualification(self):
        result=bulletin_event({'id':'outside','iddaa_odds':{'1':1.1,'X':9.,'2':15.}})
        self.assertFalse(result['pick']['target_met'])
        self.assertEqual(result['pick']['candidate_status'],'outside_band')

    def test_small_league_gets_a_medium_price_market_candidate_without_model_confidence(self):
        result=bulletin_event({'id':'source','iddaa_odds':{'1':2.0,'X':3.5,'2':4.0,'A2.5':1.8,'Ü2.5':1.9}})
        pick=result['pick']
        self.assertIsNotNone(pick)
        self.assertTrue(1.4<=pick['odds']<=2.3)
        self.assertEqual(pick['candidate_type'],'market_candidate')
        self.assertEqual(pick['probability_source'],'iddaa_market')
        self.assertIsNone(result['confidence'])
        self.assertFalse(pick['model_available'])

    def test_incomplete_or_closed_quote_groups_do_not_get_invented_probabilities(self):
        self.assertIsNone(bulletin_event({'id':'partial','iddaa_odds':{'1':1.8}})['pick'])
        self.assertIsNone(bulletin_event({'id':'archive','iddaa_odds':{'1':2,'X':3.5,'2':4},'odds_current':False})['pick'])
