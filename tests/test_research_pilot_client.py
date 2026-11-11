import copy
import io
import json
import types
import unittest
from datetime import timedelta
from unittest.mock import patch

from scripts.research_pilot_client import PilotError, RecoveryRequired, ResearchPilotClient, SubmissionUncertain, ValidationRejected, load_skill_json, secure_http_client
from tests import test_research_api as support
from tests import test_grok_skill_adapter as skill_tests


class ResponseView:
    def __init__(self, response, transform):
        self.status_code = response.status_code
        self._response = response
        self._transform = transform

    def json(self):
        return self._transform(self._response.json())


class ClientTests(unittest.TestCase):
    setUp = support.ResearchApiTest.setUp
    headers = support.ResearchApiTest.headers
    job = support.ResearchApiTest.job
    skill = skill_tests.SkillAdapterTest.skill

    def pilot(self, job, http=None):
        pilot=ResearchPilotClient(self.client, support.KEY_A, job['id'])
        pilot.fetch_context(job['match_id'])
        if http: pilot._http=http
        return pilot

    def test_transport_requires_https_without_credentials(self):
        for url in ('http://example.com', 'https://user:pass@example.com', 'https://example.com/?token=x'):
            with self.subTest(url=url), self.assertRaises(PilotError): secure_http_client(url)
        fake=types.SimpleNamespace(Client=lambda **kwargs: kwargs)
        with patch.dict('sys.modules',{'httpx':fake}):
            config=secure_http_client('https://example.com')
        self.assertTrue(config['verify'])
        self.assertFalse(config['follow_redirects'])
        self.assertFalse(config['trust_env'])

    def test_actual_claim_and_renew_response_schema(self):
        job=self.job(); path='/api/research/jobs/'+job['id']
        first=self.client.post(path+'/claim',headers=self.headers()).json()
        self.assertEqual(set(first),{'job','claim_token'})
        self.assertIn('lease_expires_at',first['job'])
        self.assertNotIn('lease_expires_at',first)
        self.assertEqual(set(first['job']),{'id','match_id','mode','deadline','input_version','created_at',
                                             'report_id','status','lease_expires_at'})
        renewed=self.client.post(path+'/renew',headers=self.headers(**{'X-Research-Claim':first['claim_token']})).json()
        self.assertEqual(set(renewed),{'job','claim_token'})
        self.assertIn('lease_expires_at',renewed['job'])
        self.assertNotEqual(first['claim_token'],renewed['claim_token'])

    def test_missing_optional_expiry_and_caller_exception_keep_lease_in_process(self):
        job=self.job(); inner=self.client
        class MissingExpiry:
            def post(self,path,**kwargs):
                response=inner.post(path,**kwargs)
                def remove(value):
                    if path.endswith('/claim'): value['job'].pop('lease_expires_at',None)
                    return value
                return ResponseView(response,remove)
        pilot=self.pilot(job,MissingExpiry())
        self.assertIsNone(pilot.claim()['lease_expires_at'])
        try: raise RuntimeError('simulated caller failure')
        except RuntimeError: pass
        result=pilot.submit(self.skill(job),'test-same','test-1')
        self.assertFalse(result['duplicate'])
        self.assertTrue(pilot.retry_submission()['duplicate'])
        self.assertNotIn(support.KEY_A,repr(pilot))

    def test_lost_token_waits_for_expiry_and_old_token_cannot_submit(self):
        job=self.job(); first=self.pilot(job); first.claim(); old=first._lease
        recovered=self.pilot(job); raw=self.skill(job)
        with self.assertRaises(PilotError): recovered.reclaim_after_loss(now=self.at)
        future=self.at+timedelta(minutes=21)
        with patch('app.research_service.now',return_value=future):
            recovered.reclaim_after_loss(now=future)
            self.assertNotEqual(old,recovered._lease)
            headers=self.headers(**{'X-Research-Claim':old,'Idempotency-Key':'old-test','X-Skill-Version':'test'})
            response=self.client.post('/api/research/jobs/'+job['id']+'/skill-results',json=raw,headers=headers)
            self.assertEqual(response.status_code,409)

    def test_expired_job_cannot_be_reclaimed(self):
        job=self.job(); self.pilot(job).claim(); recovered=self.pilot(job)
        future=self.at+timedelta(hours=3)
        with patch('app.research_service.now',return_value=future):
            with self.assertRaises(PilotError): recovered.reclaim_after_loss(now=future)

    def test_missing_token_or_bad_schema_indicates_uncertain_claim(self):
        job=self.job(); inner=self.client
        class BadSchema:
            def post(self,path,**kwargs):
                return ResponseView(inner.post(path,**kwargs),lambda _: {'job':{'id':job['id']}})
        pilot=self.pilot(job,BadSchema())
        with self.assertRaises(RecoveryRequired): pilot.claim()
        self.assertIsNone(pilot._lease)
        with self.assertRaises(PilotError): pilot.reclaim_after_loss(now=self.at)

    def test_mismatched_job_keeps_token_but_disallows_submission(self):
        job=self.job(); inner=self.client
        class WrongJob:
            def post(self,path,**kwargs):
                def wrong(value): value['job']['id']='wrong'; return value
                return ResponseView(inner.post(path,**kwargs),wrong)
        pilot=self.pilot(job,WrongJob())
        with self.assertRaises(PilotError): pilot.claim()
        self.assertIsNotNone(pilot._lease)
        with self.assertRaises(PilotError): pilot.submit(self.skill(job),'same','test')

    def test_submission_response_lost_retries_identical_result(self):
        job=self.job(); inner=self.client
        class LossOnce:
            lost=False
            def post(self,path,**kwargs):
                response=inner.post(path,**kwargs)
                if path.endswith('/skill-results') and not self.lost:
                    self.lost=True
                    raise ConnectionError('synthetic transport failure')
                return response
        pilot=self.pilot(job,LossOnce()); pilot.claim(); payload=self.skill(job)
        with self.assertRaises(SubmissionUncertain): pilot.submit(payload,'same-idempotency','test')
        payload['matches'][0]['findings'][0]['claim']='changed after sending'
        self.assertTrue(pilot.retry_submission()['duplicate'])

    def test_lost_renew_response_requires_recovery(self):
        job=self.job(); inner=self.client
        class LoseRenew:
            def post(self,path,**kwargs):
                response=inner.post(path,**kwargs)
                if path.endswith('/renew'): raise ConnectionError('synthetic failure')
                return response
        pilot=self.pilot(job,LoseRenew()); pilot.claim()
        with self.assertRaises(RecoveryRequired): pilot.renew()
        self.assertIsNone(pilot._lease)
        with self.assertRaises(PilotError): pilot.submit(self.skill(job),'same','test')

    def test_end_to_end_skill_stream_jobs_context_renew_report(self):
        job=self.job(); jobs=ResearchPilotClient.list_jobs(self.client,support.KEY_A)
        self.assertIn(job['id'],[item['id'] for item in jobs])
        pilot=self.pilot(job); self.assertTrue(pilot.context_warnings())
        self.assertIsNotNone(pilot.claim()['lease_expires_at'])
        self.assertIsNotNone(pilot.renew()['lease_expires_at'])
        raw=pilot.prepare_skill(io.StringIO(json.dumps(self.skill(job))))
        out=pilot.submit(raw,'fixture-idempotency','fixture-version')
        self.assertEqual(pilot.report(out['report_id'])['report']['match_id'],job['match_id'])
        self.assertTrue(pilot.retry_submission()['duplicate'])

    def test_past_kickoff_research_time_mismatch_and_missing_time_fail_closed(self):
        job=self.job(); pilot=self.pilot(job); pilot.claim(); raw=self.skill(job)
        bad=copy.deepcopy(raw); bad['research_time']=self.kickoff
        with self.assertRaises(PilotError): pilot.submit(bad,'bad','v')
        bad=copy.deepcopy(raw); bad['matches'][0]['match_id']='different'
        with self.assertRaises(PilotError): pilot.submit(bad,'bad','v')
        bad=copy.deepcopy(raw); bad.pop('research_time')
        with self.assertRaises(PilotError): pilot.submit(bad,'bad','v')
        pilot._context['context']['match']['kickoff']=(self.at-timedelta(minutes=1)).isoformat()
        with self.assertRaises(PilotError): pilot.submit(raw,'bad','v')

    def test_skill_stream_size_and_invalid_json(self):
        with self.assertRaises(PilotError): load_skill_json(io.StringIO('x'*131073))
        with self.assertRaises(PilotError): load_skill_json(io.StringIO('{'))
        with self.assertRaises(PilotError): load_skill_json(io.StringIO('[]'))

    def test_old_prices_and_missing_snapshot_are_flagged(self):
        job=self.job(); pilot=self.pilot(job)
        pilot._context['context']['prices']['observed_at']=(self.at-timedelta(days=2)).isoformat()
        pilot._context['context']['prediction_snapshot']=None
        warnings=pilot.context_warnings()
        self.assertTrue(any('güncel sayılmamalı' in warning for warning in warnings))
        self.assertTrue(any('snapshotı yok' in warning for warning in warnings))

    def test_422_path_reason_sanitized_and_explicit_correction(self):
        job=self.job(); pilot=self.pilot(job); pilot.claim(); raw=self.skill(job)
        raw['matches'][0]['findings'][0]['category']='not-a-category'
        with self.assertRaises(ValidationRejected) as caught: pilot.submit(raw,'failed-key','v')
        self.assertEqual(caught.exception.path,'$.matches[0].findings[0].category')
        self.assertEqual(caught.exception.reason,'Desteklenmeyen bulgu kategorisi')
        with self.assertRaises(PilotError): pilot.retry_submission()
        pilot.discard_rejected()
        raw['matches'][0]['findings'][0]['category']='absence'
        self.assertFalse(pilot.submit(raw,'fixed-key','v')['duplicate'])

    def test_uncertain_submission_cannot_be_discarded(self):
        job=self.job(); inner=self.client
        class Loss:
            def post(self,path,**kwargs):
                if path.endswith('/skill-results'): raise ConnectionError('lost')
                return inner.post(path,**kwargs)
        pilot=self.pilot(job,Loss()); pilot.claim()
        with self.assertRaises(SubmissionUncertain): pilot.submit(self.skill(job),'key','v')
        with self.assertRaises(PilotError): pilot.discard_rejected()

    def test_hostile_422_does_not_echo_untrusted_path_or_reason(self):
        job=self.job(); inner=self.client
        class Hostile422:
            def post(self,path,**kwargs):
                response=inner.post(path,**kwargs)
                if path.endswith('/skill-results'):
                    return ResponseView(response,lambda _: {'detail':{'path':'$.secretvalue','reason':'secretvalue'}})
                return response
        pilot=self.pilot(job,Hostile422()); pilot.claim(); raw=self.skill(job)
        raw['matches'][0]['findings'][0]['category']='bad-category'
        with self.assertRaises(ValidationRejected) as caught: pilot.submit(raw,'bad','v')
        self.assertEqual(caught.exception.path,'$')
        self.assertEqual(caught.exception.reason,'Doğrulama reddedildi')
        self.assertNotIn('secretvalue',str(caught.exception))

if __name__=='__main__': unittest.main()
