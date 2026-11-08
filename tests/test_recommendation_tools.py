import copy
import unittest
from datetime import datetime, timedelta, timezone

from app.model.recommendation_tools import analyse

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def prediction():
    return {'kickoff': (NOW + timedelta(hours=3)).isoformat(), 'iddaa_updated_at': NOW.isoformat(),
            'data_n': 20, 'markets': {
                '1': {'p': .72, 'model_p': .85, 'market_p': .59},
                'Ü2.5': {'p': .63, 'model_p': .66, 'market_p': .60},
                'KG Var': {'p': .60, 'model_p': .62, 'market_p': .58},
                'X': {'p': .21}, 'İY 1': {'p': .9}},
            'iddaa_odds': {'1': 1.5, 'Ü2.5': 1.9, 'KG Var': 1.85, 'X': 4.0, 'İY 1': 1.7}}


class RecommendationToolsTests(unittest.TestCase):
    def test_hit_probability_and_value_are_separate_without_mutating_prediction(self):
        p = prediction()
        before = copy.deepcopy(p)
        out = analyse(p, NOW)
        self.assertEqual(out['by_probability'][0]['market'], '1')
        self.assertEqual(out['by_value'][0]['market'], 'Ü2.5')
        self.assertEqual(len(out['by_probability']), 3)
        self.assertEqual(out['by_probability'][0]['fair_odds'], 1.39)
        self.assertTrue(out['by_probability'][0]['model_market_disagree'])
        self.assertEqual(out['by_probability'][0]['disagreement_pp'], 26)
        self.assertTrue(out['actionable'])
        self.assertEqual(p, before)

    def test_old_or_closed_quotes_cannot_be_actioned(self):
        for overrides in ({'iddaa_updated_at': (NOW-timedelta(hours=3)).isoformat()},
                          {'iddaa_updated_at': None}, {'odds_current': False},
                          {'kickoff': (NOW-timedelta(hours=1)).isoformat()}):
            with self.subTest(overrides=overrides):
                out = analyse({**prediction(), **overrides}, NOW)
                self.assertFalse(out['actionable'])
                self.assertTrue(all(not x['actionable'] for x in out['by_probability']))

    def test_missing_invalid_and_experimental_probabilities_are_not_invented(self):
        p = prediction()
        p['markets'] = {'1': {'p': float('nan')}, 'X': {'p': True}, '2': {'p': .7}, 'İY 1': {'p': .9}}
        p['iddaa_odds'].update({'X': 1.5, '2': None})
        self.assertEqual(analyse(p, NOW)['by_probability'], [])

    def test_small_league_stays_market_only_and_sparse_data_is_disclosed(self):
        p = prediction()
        for value in p['markets'].values():
            value['model_p'] = None
        out = analyse({**p, 'small_league': True}, NOW)
        self.assertEqual(out['probability_source'], 'iddaa_market')
        self.assertIsNone(out['by_probability'][0]['model_p'])
        self.assertTrue(any('Takım modeli yok' in x for x in out['warnings']))
        self.assertTrue(any('geçmişi sınırlı' in x for x in analyse({**p, 'data_n': 2}, NOW)['warnings']))

    def test_movements_must_match_the_current_quote(self):
        p = prediction()
        p['iddaa_movement'] = {'1': {'open': 1.8, 'current': 1.5, 'opened_at': NOW.isoformat()}}
        out = analyse(p, NOW)
        self.assertEqual(out['by_probability'][0]['movement']['change_pct'], -16.7)
        p['iddaa_movement']['1']['current'] = 1.6
        self.assertIsNone(analyse(p, NOW)['by_probability'][0]['movement'])

    def test_main_match_detail_attaches_tools_without_changing_the_prediction(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from app import db, main
        fixture = {'id': 'demo', 'home': 'A', 'away': 'B', 'kickoff': (NOW+timedelta(hours=3)).isoformat(),
                   'ts': NOW+timedelta(hours=3), 'home_id': 'a', 'away_id': 'b'}
        model = SimpleNamespace(upcoming=[fixture], predict=lambda *args: prediction())
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DATABASE_URL', None), \
                patch.object(db, 'DB_PATH', Path(directory)/'tools.db'):
            db.init_db()
            with patch.object(main.engine, 'get_model', return_value=model), \
                    patch.object(main, '_attach_iddaa_odds', side_effect=lambda items: items):
                result = main.match('demo')
        self.assertEqual(result['recommendation_tools']['by_probability'][0]['market'], '1')
        self.assertEqual(result['markets'], prediction()['markets'])
