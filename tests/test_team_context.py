import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from app import db
from app.model import engine, predict
from app.scrapers import espn, team_context as context

NOW = datetime(2026,11,8,12,tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)
YEAR = context.season_year(NOW)


def event(mid, ago, league='eng.1', home='1', away='2', hg=2, ag=0):
    return {'id':mid,'date':(NOW-timedelta(days=ago)).isoformat(),'league':{'slug':league},
            'competitions':[{'status':{'type':{'completed':True,'state':'post','shortDetail':'FT'}},
                            'competitors':[{'team':{'id':home,'displayName':'A' if home=='1' else 'B'},'homeAway':'home','score':{'value':hg}},
                                           {'team':{'id':away,'displayName':'A' if away=='1' else 'B'},'homeAway':'away','score':{'value':ag}}]}]}


def table_entry(tid, pts, gf, ga):
    return {'team':{'id':tid,'displayName':'A' if tid=='1' else 'B'},
            'stats':[{'name':key,'value':value} for key,value in
                     {'gamesPlayed':2,'points':pts,'pointsFor':gf,'pointsAgainst':ga,'rank':int(tid)}.items()]}


class TeamContextTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        for patcher in (patch.object(db,'DATABASE_URL',None),patch.object(db,'DB_PATH',Path(tmp.name)/'test.db'),patch.object(engine,'datetime',FrozenDateTime)):
            patcher.start();self.addCleanup(patcher.stop)
        db.init_db();engine.invalidate();self.addCleanup(engine.invalidate)

    def test_missing_season_games_are_repaired_and_competitions_do_not_change_league_points(self):
        games=[event('a',14),event('b',7,hg=1,ag=1),event('cup',3,league='uefa.europa',away='3',hg=3,ag=1),
               event('friendly',2,league='club.friendly')]
        expected={'season':{'year':YEAR},'children':[{'standings':{'entries':[table_entry('1',4,3,1),table_entry('2',1,1,3)]}}]}
        def fetch(url):
            if 'standings' in url:return expected
            tid=url.split('/teams/')[1].split('/')[0]
            return {'team':{'id':tid},'events':games}
        with db.session() as conn:
            espn.upsert(conn,espn.parse_event(espn.normalize_api_event(games[0]),'eng.1'))
            with patch.object(context,'fetch_json',side_effect=fetch):
                context.reconcile_league(conn,'eng.1',now=NOW)
            info=context.load(conn,'eng.1')
            self.assertTrue(info['teams']['1']['complete'])
            self.assertEqual(info['teams']['1']['local']['p'],2)
            self.assertEqual(conn.execute("SELECT COUNT(*) n FROM matches WHERE league='club.friendly'").fetchone()['n'],0)
            done,upcoming=engine._load(conn,'eng.1')
            model=engine.LeagueModel('eng.1',done,upcoming,{})
            engine.attach_context(conn,model,'eng.1')
        result=model.predict('A','B',NOW+timedelta(days=1))
        self.assertEqual(result['factors']['table']['home']['pts'],4)
        self.assertAlmostEqual(result['factors']['rest']['home_days'],4,places=1)
        self.assertAlmostEqual(result['factors']['rest']['away_days'],8,places=1)
        self.assertEqual(result['factors']['form']['home'][0]['opponent_id'],'3')
        self.assertEqual(result['factors']['form']['home'][0]['competition'],'uefa.europa')
        self.assertTrue(result['data_integrity']['history_complete'])
        self.assertIn('uefa.europa',{m['league'] for m in model.all_history})
        # Walk-forward prediction supplied explicit history must not borrow later context.
        historical=model.predict('A','B',NOW+timedelta(days=1),history=done)
        self.assertAlmostEqual(historical['factors']['rest']['home_days'],8,places=1)

    def test_newer_quick_result_is_not_replaced_with_an_older_official_table(self):
        games=[event('a',14),event('b',7,hg=1,ag=1)]
        official={'season':YEAR,'checked_at':(NOW-timedelta(hours=1)).isoformat(),
                  'teams':{tid:{'official':{'id':tid,'p':2,'pts':4 if tid=='1' else 1,'gf':3 if tid=='1' else 1,
                                         'ga':1 if tid=='1' else 3,'pos':int(tid)},'complete':True,'schedule_checked':True} for tid in ('1','2')}}
        with db.session() as conn:
            for game in games+[event('new',1,hg=1,ag=0)]:
                espn.upsert(conn,espn.parse_event(espn.normalize_api_event(game),'eng.1'))
            db.set_meta(conn,'team_context:eng.1',json.dumps(official))
            done,upcoming=engine._load(conn,'eng.1')
            model=engine.LeagueModel('eng.1',done,upcoming,{})
            engine.attach_context(conn,model,'eng.1')
        result=model.predict('A','B',NOW+timedelta(days=1))
        self.assertEqual(result['factors']['table']['home']['p'],3)
        self.assertEqual(result['factors']['table']['home']['pts'],7)
        self.assertIsNone(result['data_integrity']['history_complete'])

    def test_failed_or_incomplete_schedule_never_claims_verified_history(self):
        game=event('a',7)
        with db.session() as conn:
            espn.upsert(conn,espn.parse_event(espn.normalize_api_event(game),'eng.1'))
            payload={'season':{'year':YEAR},'children':[{'standings':{'entries':[table_entry('1',4,3,1)]}}]}
            with patch.object(context,'fetch_json',side_effect=[payload,RuntimeError('offline')]):
                context.reconcile_league(conn,'eng.1',now=NOW)
            info=context.load(conn,'eng.1')['teams']['1']
        self.assertFalse(info['complete']);self.assertFalse(info['schedule_checked'])

    def test_verified_final_is_not_erased_by_incomplete_followup(self):
        good=espn.parse_event(espn.normalize_api_event(event('a',7)),'eng.1')
        bad={**good,'completed':0,'home_goals':None,'away_goals':None,'status':'Scheduled'}
        with db.session() as conn:
            espn.upsert(conn,good);espn.upsert(conn,bad)
            row=conn.execute("SELECT * FROM matches WHERE id='a'").fetchone()
        self.assertEqual((row['completed'],row['home_goals'],row['away_goals']),(1,2,0))

    def test_explicit_cancellation_can_correct_a_previously_final_status(self):
        good=espn.parse_event(espn.normalize_api_event(event('a',7)),'eng.1')
        with db.session() as conn:
            espn.upsert(conn,good)
            espn.upsert(conn,{**good,'completed':0,'home_goals':None,'away_goals':None,'status':'Cancelled'})
            row=conn.execute("SELECT * FROM matches WHERE id='a'").fetchone()
        self.assertEqual(row['completed'],0)
        self.assertIsNone(row['home_goals'])
        self.assertEqual(row['status'],'Cancelled')

    def test_unknown_context_is_truthful_and_known_missing_history_limits_confidence(self):
        model=engine.LeagueModel('eng.1',[],[],{})
        result=model.predict('A','B',NOW+timedelta(days=1))
        self.assertIsNone(result['data_integrity']['history_complete'])
        c=predict.confidence(.8,20,data_integrity={'history_complete':False})
        self.assertEqual(c['confidence_label'],'Orta')
        self.assertIn('eksik',c['confidence_reason'])

    def test_schedule_rejects_wrong_team_and_future_completed_result(self):
        with self.assertRaises(ValueError):context.schedule_matches({'team':{'id':'wrong'},'events':[]},'1',NOW-timedelta(days=60),NOW)
        items=context.schedule_matches({'team':{'id':'1'},'events':[event('future',-2)]},'1',NOW-timedelta(days=60),NOW)
        self.assertEqual(items,[])
