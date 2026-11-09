import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import db
from app.bulletin_api import build_bulletin
from app.scrapers import iddaa
from datetime import date


class DailyBulletinTest(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        for p in (patch.object(db,'DB_PATH',Path(temp.name)/'db.sqlite'),patch.object(db,'DATABASE_URL',None)):
            p.start();self.addCleanup(p.stop)
        db.init_db()

    def event(self,mid,league,kickoff,small=False):
        with db.session() as c:
            c.execute('INSERT INTO iddaa_events(id,league,competition_id,kickoff,home,away,league_name,country,is_small,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (mid,league,584,kickoff,'Home','Away',league,'TR',int(small),'2026-10-24T19:00:00+00:00'))
            c.executemany('INSERT INTO iddaa_odds(event_id,market,odd) VALUES(?,?,?)',[(mid,'1',1.8),(mid,'X',3.4),(mid,'2',4.0)])

    def test_istanbul_day_order_and_unmatched_main_events_remain_visible(self):
        for args in [('de','ger.1','2026-10-25T17:00:00+00:00'),('en','eng.1','2026-10-25T18:00:00+00:00'),
                     ('tr','tur.1','2026-10-24T21:01:00+00:00'),('before','tur.1','2026-10-24T20:59:00+00:00'),
                     ('tomorrow','tur.1','2026-10-25T21:00:00+00:00')]:self.event(*args)
        result=build_bulletin(date(2026,10,25),'main')
        self.assertEqual([g['league'] for g in result['groups']],['tur.1','eng.1','ger.1'])
        self.assertEqual((result['total'],result['modeled'],result['limited']),(3,0,3))
        first=result['groups'][0]['matches'][0]
        self.assertEqual(first['id'],'iddaa:tr')
        self.assertEqual(first['iddaa_odds']['1'],1.8)
        self.assertEqual(build_bulletin(date(2026,10,26),'main')['total'],1)
        self.assertEqual(build_bulletin(date(2026,10,25),'main','eng.1')['total'],1)

    def test_other_leagues_are_combined_for_the_selected_day(self):
        self.event('a','small:one:league','2026-10-25T13:00:00+00:00',True)
        self.event('b','small:two:league','2026-10-25T16:00:00+00:00',True)
        self.event('main','tur.1','2026-10-25T17:00:00+00:00')
        result=build_bulletin(date(2026,10,25),'other')
        self.assertEqual((result['total'],len(result['groups'])),(2,2))
        self.assertTrue(all(m['id'].startswith('iddaa:') for g in result['groups'] for m in g['matches']))
        from app.main import match
        detail=match('iddaa:a')
        self.assertEqual(detail['id'],'iddaa:a')
        self.assertEqual(detail['iddaa_odds']['2'],4.0)

    def test_list_drops_model_bulk_but_keeps_the_card_pick(self):
        with db.session() as c:
            c.execute("INSERT INTO matches(id,league,kickoff,home,away,completed) VALUES('future','tur.1','2099-01-02T17:00:00+00:00','Ev','Dep',0)")
        from app.model import predict as P
        matrix = P.markets(P.score_matrix(1.8, .8))['markets']
        fake = type('Model', (), {'predict': lambda self, *args: {
            'markets': matrix, 'pick': {'market': '1', 'p': matrix['1']['p'], 'odds': 1.8},
            'data_quality': 1, 'data_n': 20, 'factors': {'news': {'items': ['x' * 500]}},
            'scores': [{'score': '1-0', 'p': 0.2}], 'lambda_home': 1.8, 'lambda_away': 0.8}})()
        with patch('app.bulletin_api.engine.get_model', return_value=fake):
            item = build_bulletin(date(2099, 1, 2), 'main')['groups'][0]['matches'][0]
        shown = {((item.get('pick') or {}).get('market')), ((item.get('alternative') or {}).get('market'))} - {None}
        self.assertNotIn('factors', item)
        self.assertNotIn('scores', item)
        self.assertNotIn('lambda_home', item)
        self.assertLessEqual(set(item.get('markets') or {}), shown)
        self.assertLess(len(item.get('markets') or {}), len(matrix))
        self.assertLessEqual(set(item['iddaa_odds']), {'1', 'X', '2'} | shown)

    def test_http_bulletin_defaults_to_main_leagues(self):
        self.event('other', 'small:tr:league', '2026-10-25T16:00:00+00:00', True)
        self.event('major', 'tur.1', '2026-10-25T17:00:00+00:00')
        from fastapi.testclient import TestClient
        from app.main import app
        from app import ops
        ops._cache.clear()
        with patch('app.main.updater.run_in_background', return_value=False):
            with TestClient(app) as client:
                body = client.get('/api/bulletin', params={'day': '2026-10-25'}).json()
        self.assertEqual(body['category'], 'main')
        self.assertEqual([group['league'] for group in body['groups']], ['tur.1'])

    def test_full_bulletin_includes_other_leagues_when_main_day_is_empty(self):
        self.event('other-tomorrow','small:tr:league','2026-10-27T17:00:00+00:00',True)
        result=build_bulletin(date(2026,10,27),'all')
        self.assertEqual(result['total'],1)
        self.assertEqual(result['groups'][0]['league'],'small:tr:league')
        self.assertEqual(build_bulletin(date(2026,10,27),'main')['total'],0)
        self.event('major','tur.1','2026-10-27T18:00:00+00:00')
        result=build_bulletin(date(2026,10,27),'all')
        self.assertEqual([g['league'] for g in result['groups']],['tur.1','small:tr:league'])

    def test_source_event_alias_resolves_a_later_confirmed_result(self):
        self.event('source','tur.1','2026-10-25T17:00:00+00:00')
        with db.session() as c:
            c.execute("INSERT INTO matches(id,league,kickoff,home,away,completed,home_goals,away_goals) VALUES('final','tur.1','2026-10-25T17:00:00+00:00','H','A',1,0,0)")
            c.execute("UPDATE iddaa_events SET match_id='final' WHERE id='source'")
        from app.results_api import results
        item=results(['iddaa:source'])['matches']['iddaa:source']
        self.assertEqual((item['id'],item['state'],item['home_goals']),('iddaa:source','post',0))

    def test_empty_or_diff_source_cannot_erase_the_last_valid_bulletin(self):
        self.event('keep','tur.1','2026-10-25T17:00:00+00:00')
        for payload in ({'data':{'events':[]}}, {'data':{'isdiff':True,'events':[]}}):
            with patch.object(iddaa,'fetch_json',return_value=payload), db.session() as c:
                with self.assertRaises(RuntimeError): iddaa.sync_odds(c)
                self.assertEqual(c.execute('SELECT COUNT(*) n FROM iddaa_events').fetchone()['n'],1)



class FullOddsDetailTest(unittest.TestCase):
    def test_detail_adds_missing_markets_but_does_not_replace_a_newer_price(self):
        summary={'i':1,'ci':584,'d':1,'m':[{'i':10,'v':3,'t':1,'st':1,'s':1,'o':[{'no':1,'odd':1.80,'wodd':1.7}]}]}
        detail={'data':{'i':1,'m':[
            {'i':10,'v':2,'t':1,'st':1,'s':1,'o':[{'no':1,'odd':1.9}]},
            {'i':11,'v':2,'t':2,'st':60,'s':1,'sov':'1.5','o':[{'no':1,'odd':1.7},{'no':2,'odd':2}]}]}}
        with patch.object(iddaa,'fetch_json',return_value=detail):stats=iddaa.enrich_events([summary])
        odds=iddaa._market_odds(summary['m'])
        self.assertEqual((odds['1'],odds['İY A1.5']),(1.8,1.7))
        self.assertEqual(stats['detail_loaded'],1)

    def test_source_market_minimum_is_parsed(self):
        raw={'i':1,'ci':584,'d':1793000000,'hn':'A','an':'B','mbc':3,
             'm':[{'i':10,'t':1,'st':1,'s':1,'mbc':2,'o':[{'no':1,'odd':1.8}]}]}
        self.assertEqual(iddaa.parse_event(raw)['min_matches'],{'1':2})

    def test_failed_detail_keeps_valid_summary_odds(self):
        summary={'i':1,'ci':584,'d':1,'m':[{'i':10,'v':3,'t':1,'st':1,'s':1,'o':[{'no':1,'odd':1.8}]}]}
        with patch.object(iddaa,'fetch_json',side_effect=TimeoutError):stats=iddaa.enrich_events([summary])
        self.assertEqual(iddaa._market_odds(summary['m']),{'1':1.8})
        self.assertEqual(stats['detail_failed'],1)


class DateCoverageTests(unittest.TestCase):
    setUp=DailyBulletinTest.setUp
    event=DailyBulletinTest.event
    def fixture(self, mid, kickoff, completed=0, hg=None, ag=None):
        with db.session() as c:
            c.execute('INSERT INTO matches(id,league,kickoff,home,away,completed,home_goals,away_goals) VALUES(?,?,?,?,?,?,?,?)',
                      (mid,'tur.1',kickoff,'Fixture Home','Fixture Away',completed,hg,ag))

    def test_yesterday_results_survive_an_empty_prematch_source_without_new_predictions(self):
        self.fixture('yesterday','2026-10-25T17:00:00+00:00',1,2,1)
        from app import main
        with patch.object(main.engine,'get_model',side_effect=AssertionError('past model must not be rebuilt')):
            result=build_bulletin(date(2026,10,25),'main')
            detail=main.match('yesterday')
        item=result['groups'][0]['matches'][0]
        self.assertEqual((result['total'],item['summary']['headline']),(1,'Bitti'))
        self.assertIsNone(item['pick'])
        self.assertIn('2–1',detail['summary']['title'])

    def test_tomorrow_fixture_is_visible_before_real_odds_are_published(self):
        self.fixture('future','2099-01-02T17:00:00+00:00')
        from app.model import predict as P
        fake=type('Model',(),{'predict':lambda self,*args:{'markets':P.markets(P.score_matrix(1.8,.8))['markets'],
                                                        'pick':None,'data_quality':1,'data_n':20}})()
        with patch('app.bulletin_api.engine.get_model',return_value=fake):result=build_bulletin(date(2099,1,2),'main')
        item=result['groups'][0]['matches'][0]
        self.assertEqual(result['fixture_only'],1)
        self.assertEqual(item['alternative']['candidate_status'],'odds_missing')
        self.assertEqual(item['summary']['recommendation_label'],'Model ön tahmini')
        self.assertIsNone(item['summary']['odds'])
        self.assertFalse(item['odds_current'])

    def test_past_small_bulletin_is_preserved_when_active_events_are_replaced(self):
        from app import bulletin_archive,main
        self.event('old-small','small:tr:league','2026-10-25T17:00:00+00:00',True)
        with db.session() as c:
            bulletin_archive.preserve_current(c)
            c.execute('DELETE FROM iddaa_odds');c.execute('DELETE FROM iddaa_events')
        result=build_bulletin(date(2026,10,25),'other')
        item=result['groups'][0]['matches'][0]
        self.assertEqual((result['total'],item['iddaa_odds']['1']),(1,1.8))
        self.assertTrue(item['is_archived'])
        self.assertFalse(item['odds_current'])
        self.assertIsNone(item['pick'])
        detail=main.match('iddaa:old-small')
        self.assertEqual(detail['summary']['headline'],'Sonuç bekleniyor')

    def test_archived_source_alias_keeps_its_confirmed_match_result(self):
        from app import bulletin_archive,main
        from app.results_api import results
        self.fixture('paired','2026-10-25T17:00:00+00:00',1,1,0)
        self.event('paired-event','tur.1','2026-10-25T17:00:00+00:00')
        with db.session() as c:
            c.execute("UPDATE iddaa_events SET match_id='paired' WHERE id='paired-event'")
            bulletin_archive.preserve_current(c)
            c.execute('DELETE FROM iddaa_odds');c.execute('DELETE FROM iddaa_events')
        result=results(['iddaa:paired-event'])['matches']['iddaa:paired-event']
        self.assertEqual((result['state'],result['home_goals']),("post",1))
        detail=main.match('iddaa:paired-event')
        self.assertEqual(detail['id'],'iddaa:paired-event')
        self.assertIn('1–0',detail['summary']['title'])

    def test_no_older_small_archive_does_not_claim_there_were_no_matches(self):
        result=build_bulletin(date(2026,10,25),'other')
        self.assertEqual(result['total'],0)
        self.assertIn('arşivi bulunmuyor',result['empty_reason'])

    def test_historical_source_prices_can_be_shown_from_existing_match_odds_log(self):
        self.fixture('past','2026-10-25T17:00:00+00:00',1,0,0)
        with db.session() as c:
            c.execute("INSERT INTO odds_log(match_id,market,odd,first_odd,first_seen,updated_at) VALUES('past','X',3.2,3.1,'2026-10-25T12:00Z','2026-10-25T16:00Z')")
        item=build_bulletin(date(2026,10,25),'main')['groups'][0]['matches'][0]
        self.assertEqual(item['iddaa_odds']['X'],3.2)
        self.assertFalse(item['odds_current'])
        self.assertEqual(item['summary']['headline'],'Bitti')
