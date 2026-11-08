import unittest

from app.model.extra_markets import for_match
from app.value_bets import collect
from app.model.coupons import _choices


class ExtraMarketsTest(unittest.TestCase):
    def test_first_half_and_half_full_probabilities_are_modelled(self):
        markets = for_match(1.4, 1.0, {"İY A1.5": 1.9, "İY Ü1.5": 1.9,
                                     "Handikap -1 1": 2.0, "Handikap -1 X": 3.0,
                                     "Handikap -1 2": 4.0})
        self.assertAlmostEqual(sum(markets[f"İY {r}"]["p"] for r in ("1", "X", "2")), 1, places=3)
        self.assertAlmostEqual(sum(markets[f"İY/MS {a}/{b}"]["p"] for a in ("1", "X", "2")
                                   for b in ("1", "X", "2")), 1, places=3)
        self.assertAlmostEqual(sum(markets[f"Handikap -1 {r}"]["p"] for r in ("1", "X", "2")), 1, places=3)
        self.assertAlmostEqual(markets["İY A1.5"]["p"] + markets["İY Ü1.5"]["p"], 1, places=3)
        integer_line = for_match(1.4, 1.0, {"İY A1": 1.9, "İY Ü1": 1.9})
        self.assertLess(integer_line["İY A1"]["p"] + integer_line["İY Ü1"]["p"], 1)
        self.assertGreater(integer_line["İY A1"]["p"] + integer_line["İY Ü1"]["p"], 0.5)

    def test_negative_handicap_gives_the_away_team_a_goal_advantage(self):
        markets = for_match(2.0, 0.5, {"Handikap -1 1": 1.9,
                                       "Handikap -1 X": 1.9,
                                       "Handikap -1 2": 1.9})
        # Source -1 displays 0:1: home must win by at least two goals.
        plain = for_match(2.0, 0.5, {"Handikap 0 1": 1.9})
        self.assertLess(markets["Handikap -1 1"]["p"], plain["Handikap 0 1"]["p"])
        self.assertGreater(markets["Handikap -1 2"]["p"], plain["Handikap 0 2"]["p"])

    def test_handicap_zero_matches_the_standard_full_time_distribution(self):
        from app.model import predict as P
        extra=for_match(2.0,.8,{'Handikap 0 1':1.8})
        normal=P.markets(P.score_matrix(2.0,.8))['markets']
        for side in ('1','X','2'):
            self.assertEqual(extra[f'Handikap 0 {side}']['p'],normal[side]['p'])

    def test_real_handicap_quote_is_not_confused_with_model_probability(self):
        from app.scrapers.iddaa import _market_odds
        odds=_market_odds([{'t':2,'st':100,'s':1,'sov':'-2','o':[
            {'no':1,'odd':2.0},{'no':2,'odd':3.85},{'no':3,'odd':2.24}]}])
        self.assertEqual(odds['Handikap -2 1'],2.0)
        item=for_match(2.1,1.0,odds)['Handikap -2 1']
        self.assertEqual(item['settlement_definition']['home_goal_adjustment'],-2)
        self.assertEqual(item['settlement_definition']['selection'],'1')
        self.assertAlmostEqual(item['market_p'],.5/(.5+1/3.85+1/2.24),places=4)
        self.assertNotEqual(item['p'],item['market_p'])
        self.assertEqual(item['validation_status'],'experimental')

    def test_corner_and_card_quotes_have_no_model_probability(self):
        markets = for_match(1.4, 1.0, {"Korner A9.5": 1.9, "Kart Ü4.5": 2.1})
        self.assertIsNone(markets["Korner A9.5"]["p"])
        self.assertIsNone(markets["Kart Ü4.5"]["suggested"])
        prediction = {"id": "1", "league": "eng.1", "kickoff": "2026-10-17T14:00Z",
                      "home": "H", "away": "A", "lambda_home": 1.4, "lambda_away": 1.0,
                      "markets": {}, "extra_markets": markets}
        odds = {"1": {"odds": {"Korner A9.5": 2.5, "Kart Ü4.5": 2.5}}}
        self.assertEqual(collect([prediction], odds)["items"], [])
        choices = _choices(prediction, odds["1"]["odds"])
        self.assertFalse(any(c["market"].startswith(("Korner ", "Kart ")) for c in choices))


if __name__ == "__main__":
    unittest.main()
