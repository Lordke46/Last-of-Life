import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from app import db, live


class BulletinLiveTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        for p in (patch.object(db,'DB_PATH',Path(tmp.name)/'test.db'),patch.object(db,'DATABASE_URL',None)):
            p.start();self.addCleanup(p.stop)
        db.init_db()
        self.day=date(2026,11,9)
        with db.session() as conn:
            conn.execute('INSERT INTO iddaa_events(id,league,competition_id,kickoff,home,away,league_name,country,is_small,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                         ('source','small:no:league',99,'2026-11-09T18:00:00+00:00','Kristiansund','Valerenga','League','NO',1,'2026-11-09T12:00Z'))
        self.score={'id':'provider','league':'all','source_key':'all','kickoff':'2026-11-09T18:00Z',
                    'home':'Kristiansund BK','away':'Vålerenga','home_goals':1,'away_goals':0,'state':'in','minute':"24'"}

    def test_other_league_provider_id_matches_both_bulletin_id_formats(self):
        rows=live._aliases([self.score],self.day)
        self.assertEqual({r['id'] for r in rows},{'provider','source','iddaa:source'})
        self.assertEqual(next(r for r in rows if r['id']=='iddaa:source')['home_goals'],1)

    def test_different_kickoff_reversed_teams_or_ambiguous_matches_are_rejected(self):
        for rows in ([{**self.score,'kickoff':'2026-11-09T15:00Z'}],
                     [{**self.score,'home':'Valerenga','away':'Kristiansund'}],
                     [self.score,{**self.score,'id':'second'}]):
            self.assertFalse(any(r['id']=='iddaa:source' for r in live._aliases(rows,self.day)))

    def test_verified_small_league_final_survives_scoreboard_removal(self):
        live._aliases([{**self.score,'state':'post','minute':'MS'}],self.day)
        from app.results_api import results
        row=results(['iddaa:source'])['matches']['iddaa:source']
        self.assertEqual((row['state'],row['home_goals'],row['away_goals']),('post',1,0))

    def test_selected_day_changes_cache_scope_even_when_previous_request_was_recent(self):
        cache={'at':0.,'leagues':{},'sources':{},'updated_at':None,'day':None}
        with patch.object(live,'_cache',cache),patch.object(live,'LEAGUES',{}),patch.object(live,'_fetch',return_value={'events':[]}) as fetch:
            live.get_live_snapshot(date(2026,11,9))
            live.get_live_snapshot(date(2026,11,10))
            self.assertEqual(fetch.call_count,4)
            self.assertEqual(cache['day'],date(2026,11,10))

    def test_saved_final_does_not_settle_a_rescheduled_fixture(self):
        live._aliases([{**self.score,'state':'post','minute':'MS'}],self.day)
        with db.session() as conn:
            conn.execute("UPDATE iddaa_events SET kickoff='2099-11-10T18:00Z' WHERE id='source'")
        from app.results_api import results
        self.assertEqual(results(['iddaa:source'])['matches']['iddaa:source']['state'],'unknown')

    def test_unresolved_coupon_fetches_provider_once_then_throttles_failures(self):
        from datetime import datetime,timedelta,timezone
        from app.results_api import results
        kickoff=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        with db.session() as conn:
            conn.execute('UPDATE iddaa_events SET kickoff=? WHERE id=?',(kickoff,'source'))
        with patch.object(live,'get_live_snapshot',return_value={'matches':[]}) as fetch:
            self.assertEqual(results(['iddaa:source'])['matches']['iddaa:source']['state'],'unknown')
            results(['iddaa:source'])
            self.assertEqual(fetch.call_count,1)
