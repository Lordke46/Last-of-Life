import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from app import db, recommendation_history as history


class RecommendationHistoryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        for p in (patch.object(db,'DB_PATH',Path(tmp.name)/'test.db'),patch.object(db,'DATABASE_URL',None)):
            p.start();self.addCleanup(p.stop)
        db.init_db()
        self.now=datetime.now(timezone.utc)
        self.item={'id':'iddaa:one','iddaa_event_id':'one','league':'small:test',
                   'kickoff':(self.now+timedelta(hours=1)).isoformat(),
                   'iddaa_updated_at':self.now.isoformat(),'iddaa_odds':{'1':1.8},
                   'pick':{'market':'1','p':.6,'odds':1.8,'odds_source':'iddaa','probability_source':'iddaa_market'}}

    def test_prematch_selection_survives_start_without_reprediction(self):
        with patch.object(history,'datetime') as clock:
            clock.now.return_value=self.now
            history.decorate([self.item])
            clock.now.return_value=self.now+timedelta(hours=2)
            started={**self.item,'pick':None,'iddaa_odds':{'1':9.9},'result_only':True}
            history.decorate([started])
        self.assertIsNone(started['pick'])
        self.assertEqual(started['prematch_recommendation']['pick']['odds'],1.8)
        self.assertEqual(started['prematch_recommendation']['pick']['p'],.6)

    def test_started_match_cannot_create_or_replace_old_choice(self):
        started={**self.item,'kickoff':(self.now-timedelta(hours=1)).isoformat()}
        history.decorate([started])
        self.assertNotIn('prematch_recommendation',started)
        with db.session() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) n FROM meta WHERE key LIKE ?',('displayed_picks:%',)).fetchone()['n'],0)

    def test_existing_model_snapshot_is_recovered_but_after_start_capture_is_rejected(self):
        kickoff=self.now-timedelta(hours=1)
        item={**self.item,'id':'model-one','iddaa_event_id':None,'pick':None,'kickoff':kickoff.isoformat()}
        with db.session() as conn:
            conn.execute('INSERT INTO prediction_snapshots(match_id,league,kickoff,captured_at,model_version,parameters,payload) VALUES(?,?,?,?,?,?,?)',
                         ('model-one','eng.1',kickoff.isoformat(),(kickoff-timedelta(minutes=5)).isoformat(),'test','{}',json.dumps({'pick':self.item['pick']})))
        history.decorate([item])
        self.assertEqual(item['prematch_recommendation']['pick']['odds'],1.8)
        with db.session() as conn:
            conn.execute('UPDATE prediction_snapshots SET captured_at=?',(self.now.isoformat(),))
        item.pop('prematch_recommendation')
        history.decorate([item])
        self.assertNotIn('prematch_recommendation',item)

    def test_stale_price_does_not_create_prematch_record(self):
        item={**self.item,'iddaa_updated_at':(self.now-timedelta(hours=4)).isoformat()}
        history.decorate([item])
        self.assertNotIn('prematch_recommendation',item)

    def test_scheduled_capture_includes_small_leagues_before_any_page_view(self):
        from app import quote_provenance
        odds={'1':1.6,'X':4.,'2':6.}
        with db.session() as conn:
            conn.execute('INSERT INTO iddaa_events(id,league,competition_id,kickoff,home,away,is_small,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                         ('one','small:test',99,self.item['kickoff'],'A','B',1,self.now.isoformat()))
            conn.executemany('INSERT INTO iddaa_odds(event_id,market,odd) VALUES(?,?,?)',[('one',k,v) for k,v in odds.items()])
            quote_provenance.save(conn,[{'id':'one','quote_metadata':{k:{'identity':['one',k],'odd':v,
                'quote_observed_at':self.now.isoformat(),'cache_status':'network_no_cache_evidence'} for k,v in odds.items()}}],self.now.isoformat(),{})
        captured=history.capture_bulletin()
        self.assertEqual(len(captured),1)
        self.assertEqual(captured[0]['prematch_recommendation']['pick']['odds'],1.6)
