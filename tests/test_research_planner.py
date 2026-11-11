import copy
import unittest
from unittest.mock import patch
from datetime import datetime,timedelta,timezone
from app.research_planner import plan
from app import research_service
from fastapi import HTTPException

class ResearchPlannerTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,11,12,tzinfo=timezone.utc)
    def context(self,mid='m1',hours=5):
        return {'match':{'id':mid,'kickoff':(self.now+timedelta(hours=hours)).isoformat()},
                'prediction_snapshot':None,'prices':{'observed_at':(self.now-timedelta(hours=3)).isoformat()}}
    def existing(self,mid,mode='full',created=None):
        return {'match_id':mid,'mode':mode,'status':'expired','created_at':(created or self.now).isoformat()}
    def test_cap_dedup_warnings_and_no_mutation(self):
        items=[self.context('m1'),self.context('m1'),self.context('m2'),self.context('m3')]
        old=[self.existing('old')];before=copy.deepcopy((items,old))
        output=plan(items,old,mode='full',now=self.now,history_complete=True)
        self.assertEqual([r.get('reason',r['decision']) for r in output['candidates']],['propose','duplicate_match_mode','propose','daily_full_limit'])
        self.assertEqual(output['remaining_full_slots'],0)
        self.assertIn('missing_model_snapshot',output['candidates'][0]['warnings'])
        self.assertIn('stale_or_unknown_prices',output['candidates'][0]['warnings'])
        self.assertEqual((items,old),before)
    def test_past_matches_deadline_and_existing_expired_jobs(self):
        x=[self.context('old',-1),self.context('late',.25),self.context('duplicate')]
        out=plan(x,[self.existing('duplicate',created=self.now-timedelta(days=2))],mode='full',now=self.now,history_complete=True)
        self.assertEqual([r['reason'] for r in out['candidates']],['not_upcoming','deadline_passed','duplicate_match_mode'])
    def test_lineup_window_is_separate_and_deadline_before_kickoff(self):
        out=plan([self.context('m1',5),self.context('m2',1)],[self.existing('m2')],mode='lineup_check',now=self.now,history_complete=True)
        self.assertEqual(out['candidates'][0]['reason'],'lineup_window_not_open')
        self.assertEqual(out['candidates'][1]['decision'],'propose')
        self.assertEqual(datetime.fromisoformat(out['candidates'][1]['deadline']),self.now+timedelta(minutes=55))
    def test_fail_closed_without_complete_history(self):
        with self.assertRaises(ValueError):plan([self.context()],[],mode='full',now=self.now)
        with self.assertRaises(ValueError):plan([self.context()],[{'mode':'full'}],mode='full',now=self.now,history_complete=True)

    def test_recent_event_timestamp_does_not_hide_stale_price(self):
        context=self.context()
        context['prices']={'observed_at':self.now.isoformat(),'quote_provenance':{'quotes':{
            '1':{'quote_observed_at':(self.now-timedelta(hours=3)).isoformat()}}}}
        out=plan([context],[],mode='full',now=self.now,history_complete=True)
        self.assertIn('stale_or_unknown_prices',out['candidates'][0]['warnings'])

    def test_sqlite_enqueue_fails_closed_without_database_guards(self):
        with patch.dict('os.environ',{'RESEARCH_DATABASE_URL':''}):
            with self.assertRaises(HTTPException) as exc:
                research_service.enqueue('owner','match','full',self.now.isoformat())
            self.assertEqual(exc.exception.status_code,503)
            self.assertEqual(exc.exception.detail['code'],'RESEARCH_QUEUE_POSTGRES_REQUIRED')

if __name__=='__main__':unittest.main()
