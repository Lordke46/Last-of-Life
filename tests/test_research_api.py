import copy
import contextlib
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app import db, research_store as store, research_service as service
from app.main import app
from app.model import decision_quality

# Deterministic TEST-ONLY values, registered solely in disposable databases.
KEY_A = 'test_a.' + 'test-only-not-a-real-secret-a' * 2
KEY_B = 'test_b.' + 'test-only-not-a-real-secret-b' * 2


class ResearchApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(db, 'DATABASE_URL', None), patch.object(db, 'DB_PATH', Path(self.tmp.name)/'research.db'),
                  # Legacy SQLite fixtures test transport/leases only; quota is exercised on PostgreSQL.
                  patch.object(service, '_assert_guarded_backend', return_value=None),
                  patch.dict('os.environ', {'RESEARCH_DATABASE_URL':'', 'RESEARCH_API_ENABLED':'1',
                                            'AUTH_REQUIRED':'1', 'LOCAL_AUTO_UPDATE':'0'})):
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        store.initialize_local()
        self.at = datetime.now(timezone.utc)
        self.kickoff = (self.at + timedelta(hours=6)).isoformat()
        with db.session() as conn:
            conn.execute('INSERT INTO matches(id,league,kickoff,home,away) VALUES(?,?,?,?,?)',
                         ('m1','eng.1',self.kickoff,'Home','Away'))
            conn.execute('INSERT INTO iddaa_events(id,league,competition_id,kickoff,home,away,match_id,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                         ('e1','eng.1',1,self.kickoff,'Home','Away','m1',self.at.isoformat()))
            conn.execute('INSERT INTO iddaa_odds(event_id,market,odd) VALUES(?,?,?)', ('e1','1',1.45))
            conn.execute('INSERT INTO news(id,league,team,title,link,source,published) VALUES(?,?,?,?,?,?,?)',
                         ('n1','eng.1','Home','Fixture news','https://example.com/news','Example',self.at.isoformat()))
            conn.execute('INSERT INTO prediction_snapshots(match_id,league,kickoff,captured_at,model_version,parameters,payload) VALUES(?,?,?,?,?,?,?)',
                         ('m1','eng.1',self.kickoff,self.at.isoformat(),'existing-model','{}',json.dumps({
                             'model':{'1':.72},'market':{'1':.68},'blend':{'1':.70},'calibration_status':'insufficient_data'})))
        for kid, owner, key in [('test_a','grok-a',KEY_A),('test_b','grok-b',KEY_B)]:
            service.provision(kid, owner, service.sha(key), service.SCOPES, (self.at+timedelta(days=1)).isoformat())
        self.client = TestClient(app, base_url='https://testserver')
        self.addCleanup(self.client.close)

    def headers(self, key=KEY_A, **more):
        return {'Authorization':'Bearer '+key, **more}

    def job(self, owner='grok-a', match_id='m1', mode='full', request_id='initial'):
        return service.enqueue(owner, match_id, mode, (self.at+timedelta(hours=2)).isoformat(), request_id)

    def claim(self, job, key=KEY_A):
        r = self.client.post(f'/api/research/jobs/{job["id"]}/claim', headers=self.headers(key))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['claim_token']

    def payload(self, job):
        return {'schema_version':'1.0','job_id':job['id'],'match_id':job['match_id'],'mode':job['mode'],
                'input_version':job['input_version'],'skill_name':'football-match-intel','skill_version':'pilot-1',
                'researched_at':datetime.now(timezone.utc).isoformat(), 'summary':None,
                'sources':[{'id':'s1','url':'https://example.com/team-news','kind':'club',
                            'published_at':None,'accessed_at':self.at.isoformat()}],
                'findings':[{'category':'injury','statement':'Availability unknown',
                             'verification_status':'unknown','source_ids':['s1']}],
                'injury_assessment':{'summary':None,'verification_status':'unknown'},
                'lineup_assessment':{'lineup_status':'unknown'},'risks':['Official lineup unavailable']}

    def submit(self, job, lease, data=None, key=KEY_A, idem='result-1'):
        return self.client.post(f'/api/research/jobs/{job["id"]}/results', json=data or self.payload(job),
                                headers=self.headers(key, **{'X-Research-Claim':lease,'Idempotency-Key':idem}))

    def test_unauthorized_wrong_revoked_and_expired_keys_rejected(self):
        self.assertEqual(self.client.get('/api/research/jobs').status_code, 401)
        self.assertEqual(self.client.get('/api/research/jobs',headers=self.headers(KEY_A+'wrong')).status_code,401)
        for column,value in [('revoked',1),('expires_at',(self.at-timedelta(hours=1)).isoformat())]:
            with db.session() as conn:
                conn.execute("UPDATE research_service_keys SET revoked=0 WHERE key_id='test_a'")
                conn.execute(f'UPDATE research_service_keys SET {column}=? WHERE key_id=?',(value,'test_a'))
            r=self.client.get('/api/research/jobs',headers=self.headers())
            self.assertEqual(r.status_code,401)
            self.assertIn('no-store',r.headers['cache-control'])

    def test_disabled_by_default_and_missing_schema_fail_closed(self):
        with patch.dict('os.environ',{'RESEARCH_API_ENABLED':''}):
            self.assertEqual(self.client.get('/api/research/jobs',headers=self.headers()).status_code,503)
        with db.session() as conn:
            conn.execute('DROP TABLE research_service_keys')
        self.assertEqual(self.client.get('/api/research/jobs',headers=self.headers()).status_code,503)

    def test_production_application_credentials_are_never_reused(self):
        # The sentinel is never connected to: fail before any connect() call.
        with patch.object(db,'DATABASE_URL','production-sentinel-do-not-connect'), \
             patch.object(db,'connect',side_effect=AssertionError('must not connect')):
            self.assertEqual(self.client.get('/api/research/jobs',headers=self.headers()).status_code,503)

    def test_schema_and_unmatched_bulletin_context(self):
        schema=self.client.get('/api/research/schema',headers=self.headers())
        self.assertEqual(schema.status_code,200)
        self.assertIn('input_version',schema.json()['required'])
        with db.session() as conn:
            conn.execute('INSERT INTO iddaa_events(id,league,competition_id,kickoff,home,away,is_small,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                         ('small1','small:example',1,self.kickoff,'Small Home','Small Away',1,self.at.isoformat()))
        job=self.job(match_id='iddaa:small1',mode='lineup_check')
        response=self.client.get('/api/research/matches/iddaa:small1/context',params={'job_id':job['id']},headers=self.headers())
        self.assertEqual(response.status_code,200,response.text)
        self.assertIsNone(response.json()['context']['prediction_snapshot'])

    def test_cookie_gate_unchanged_service_key_not_admin_access(self):
        self.assertEqual(self.client.get('/api/research/jobs',headers=self.headers()).status_code,200)
        self.assertEqual(self.client.get('/api/leagues',headers=self.headers()).status_code,401)
        self.assertEqual(self.client.get('/api/research-other',headers=self.headers()).status_code,401)
        self.assertEqual(self.client.post('/api/update',headers=self.headers()).status_code,401)

    def test_manual_queue_deduplicates_and_invalid_matches_rejected(self):
        first=self.job()
        self.assertEqual(first['id'],self.job()['id'])
        with self.assertRaises(Exception):self.job(match_id='does-not-exist')
        with db.session() as conn:
            conn.execute("UPDATE matches SET status='Postponed' WHERE id='m1'")
        with self.assertRaises(ValueError):self.job(request_id='new')

    def test_manual_cli_creates_job_and_registers_only_a_key_digest(self):
        from app.research_admin import main
        capture=io.StringIO()
        args=['--db',str(db.DB_PATH)]
        with contextlib.redirect_stdout(capture):
            self.assertEqual(main([*args,'create-job','--owner','grok-a','--match-id','m1','--mode','full',
                                   '--deadline',(self.at+timedelta(hours=2)).isoformat()]),0)
        self.assertEqual(json.loads(capture.getvalue())['match_id'],'m1')
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture):
            self.assertEqual(main([*args,'register-key','--key-id','hash-only','--principal-id','grok-c',
                                   '--token-sha256','a'*64,'--expires-at',(self.at+timedelta(days=1)).isoformat()]),0)
        self.assertFalse(json.loads(capture.getvalue())['plaintext_key_generated'])

    def test_manual_cli_refuses_postgres_even_when_db_argument_is_local(self):
        from app.research_admin import main
        with patch.object(db,'DATABASE_URL','production-sentinel-do-not-connect'),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(['--db',str(db.DB_PATH),'init-local'])

    def test_context_is_assigned_snapshot_without_refresh_or_model_calls(self):
        job=self.job()
        with patch('app.model.engine.get_model',side_effect=AssertionError('model called')), \
             patch('app.bulletin_refresh.refresh_if_stale',side_effect=AssertionError('refresh called')):
            r=self.client.get('/api/research/matches/m1/context',params={'job_id':job['id']},headers=self.headers())
        self.assertEqual(r.status_code,200,r.text)
        data=r.json()
        self.assertEqual(data['input_version'],job['input_version'])
        self.assertEqual(data['context']['prices']['quotes'][0]['odd'],1.45)
        self.assertEqual(data['context']['prediction_snapshot']['payload']['model']['1'],.72)
        self.assertEqual(self.client.get('/api/research/matches/wrong/context',params={'job_id':job['id']},headers=self.headers()).status_code,409)
        self.assertEqual(self.client.get('/api/research/matches/m1/context',headers=self.headers()).status_code,422)

    def test_foreign_jobs_context_claim_and_report_inaccessible(self):
        job=self.job()
        self.assertFalse(self.client.get('/api/research/jobs',headers=self.headers(KEY_B)).json()['jobs'])
        self.assertEqual(self.client.post(f'/api/research/jobs/{job["id"]}/claim',headers=self.headers(KEY_B)).status_code,404)
        self.assertEqual(self.client.get('/api/research/matches/m1/context',params={'job_id':job['id']},headers=self.headers(KEY_B)).status_code,404)
        lease=self.claim(job)
        result=self.submit(job,lease).json()
        self.assertEqual(self.client.get('/api/research/reports/'+result['report_id'],headers=self.headers(KEY_B)).status_code,404)
        self.assertEqual(self.submit(job,lease,key=KEY_B).status_code,404)

    def test_end_to_end_unknown_fields_and_idempotent_result(self):
        job=self.job()
        lease=self.claim(job)
        payload=self.payload(job)
        first=self.submit(job,lease,payload)
        self.assertEqual(first.status_code,200,first.text)
        repeated=self.submit(job,lease,payload)
        self.assertTrue(repeated.json()['duplicate'])
        self.assertEqual(first.json()['report_id'],repeated.json()['report_id'])
        with db.session() as conn:
            self.assertEqual(conn.execute('SELECT count(*) n FROM research_report_revisions').fetchone()['n'],1)
        result=self.client.get('/api/research/reports/'+first.json()['report_id'],headers=self.headers()).json()
        self.assertFalse(result['applied_to_model'])
        self.assertEqual(result['report']['lineup_assessment']['lineup_status'],'unknown')
        self.assertEqual(result['freshness_status'],'current')
        self.assertEqual(result['validation_status'],'schema_valid')
        payload['summary']='different'
        self.assertEqual(self.submit(job,lease,payload).status_code,409)

    def test_report_revisions_append_and_previous_version_is_preserved(self):
        first=self.job()
        saved=self.submit(first,self.claim(first)).json()
        second=self.job(request_id='intentional-correction-1')
        data=self.payload(second)
        data['summary']='An updated assessment'
        updated=self.submit(second,self.claim(second),data,idem='result-2').json()
        self.assertEqual(updated['report_id'],saved['report_id'])
        self.assertEqual(updated['revision'],2)
        old=self.client.get('/api/research/reports/'+saved['report_id'],params={'revision':1},headers=self.headers()).json()
        self.assertIsNone(old['report']['summary'])

    def test_wrong_match_job_mode_and_input_version_rejected(self):
        job=self.job()
        lease=self.claim(job)
        for key,value in [('match_id','m2'),('job_id','other'),('mode','lineup_check'),('input_version','f'*64)]:
            payload=self.payload(job)
            payload[key]=value
            self.assertEqual(self.submit(job,lease,payload).status_code,409,key)

    def test_lease_required_exclusive_and_reclaimable_after_timeout(self):
        job=self.job()
        old=self.claim(job)
        self.assertEqual(self.client.post(f'/api/research/jobs/{job["id"]}/claim',headers=self.headers()).status_code,409)
        self.assertEqual(self.submit(job,'bad-lease').status_code,409)
        with db.session() as conn:
            conn.execute('UPDATE research_jobs SET lease_expires_at=? WHERE id=?',((self.at-timedelta(minutes=1)).isoformat(),job['id']))
        new=self.claim(job)
        self.assertNotEqual(old,new)
        self.assertEqual(self.submit(job,old).status_code,409)
        self.assertEqual(self.submit(job,new).status_code,200)

    def test_expired_job_refuses_claim_and_late_valid_result_is_archived(self):
        job=self.job()
        lease=self.claim(job)
        with db.session() as conn:
            conn.execute('UPDATE research_jobs SET deadline=? WHERE id=?',((self.at-timedelta(minutes=1)).isoformat(),job['id']))
        self.assertEqual(self.client.post(f'/api/research/jobs/{job["id"]}/claim',headers=self.headers()).status_code,410)
        result=self.submit(job,lease)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['freshness_status'],'expired')
        self.assertEqual(self.client.get('/api/research/jobs',headers=self.headers()).json()['jobs'][0]['status'],'expired')

    def test_changed_fixture_or_input_marks_result_expired(self):
        job=self.job()
        lease=self.claim(job)
        with db.session() as conn:
            conn.execute("UPDATE iddaa_odds SET odd=1.55 WHERE event_id='e1'")
        self.assertEqual(self.submit(job,lease).json()['freshness_status'],'expired')

    def test_saved_report_expires_when_match_starts_without_overwriting_revision(self):
        job=self.job()
        result=self.submit(job,self.claim(job)).json()
        with db.session() as conn:
            original=conn.execute('SELECT payload_json FROM research_report_revisions').fetchone()['payload_json']
            conn.execute('UPDATE matches SET kickoff=? WHERE id=?',((self.at-timedelta(minutes=1)).isoformat(),'m1'))
        response=self.client.get('/api/research/reports/'+result['report_id'],headers=self.headers()).json()
        self.assertEqual(response['freshness_status'],'expired')
        self.assertEqual(response['freshness_at_ingest'],'current')
        with db.session() as conn:
            self.assertEqual(conn.execute('SELECT payload_json FROM research_report_revisions').fetchone()['payload_json'],original)

    def test_concurrent_result_retries_create_one_revision(self):
        from app.research_models import ResearchResult
        job=self.job()
        lease=self.claim(job)
        payload=ResearchResult.model_validate_json(json.dumps(self.payload(job)))
        principal={'id':'grok-a','scopes':list(service.SCOPES)}
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(lambda _:service.save_result(principal,job['id'],payload,lease,'same-request'),range(2)))
        self.assertEqual(sorted(r['duplicate'] for r in results),[False,True])
        self.assertEqual({r['revision'] for r in results},{1})

    def test_bad_json_unknown_fields_and_unsupported_facts_rejected(self):
        job=self.job()
        lease=self.claim(job)
        for change in ({'owner_id':'grok-b'}, {'applied_to_model':True}, {'sources':'invalid'},
                       {'mode':'other'}, {'researched_at':'not-a-date'},
                       {'findings':[{'category':'injury','statement':'Absent','verification_status':'confirmed'}]},
                       {'findings':[{'category':'news','statement':'News','source_ids':['missing']}]},
                       {'lineup_assessment':{'lineup_status':'official','verification_status':'unknown'}}):
            payload=self.payload(job)
            payload.update(change)
            r=self.submit(job,lease,payload)
            self.assertEqual(r.status_code,422,r.text)
        r=self.client.post(f'/api/research/jobs/{job["id"]}/results',content='{broken',headers=self.headers(**{
            'Content-Type':'application/json','Idempotency-Key':'bad','X-Research-Claim':lease}))
        self.assertEqual(r.status_code,422)

    def test_body_limit_and_content_type(self):
        job=self.job()
        url=f'/api/research/jobs/{job["id"]}/results'
        self.assertEqual(self.client.post(url,content='x'*140000,headers=self.headers()).status_code,413)
        self.assertEqual(self.client.post(url,content='{}',headers=self.headers()).status_code,415)

    def test_scope_enforcement(self):
        job=self.job()
        with db.session() as conn:
            conn.execute('UPDATE research_service_keys SET scopes=? WHERE key_id=?',(json.dumps(['jobs:read']),'test_a'))
        self.assertEqual(self.client.get('/api/research/jobs',headers=self.headers()).status_code,200)
        self.assertEqual(self.client.post(f'/api/research/jobs/{job["id"]}/claim',headers=self.headers()).status_code,403)

    def test_rate_limit_persists_and_no_tokens_are_stored(self):
        for _ in range(60):
            self.assertEqual(self.client.get('/api/research/jobs',headers=self.headers()).status_code,200)
        self.assertEqual(self.client.get('/api/research/jobs',headers=self.headers()).status_code,429)
        with db.session() as conn:
            self.assertNotIn(KEY_A,str([dict(r) for r in conn.execute('SELECT * FROM research_service_keys')]))

    def test_existing_tables_and_single_match_engine_are_unchanged(self):
        tables=['matches','news','iddaa_events','iddaa_odds','prediction_snapshots','meta','saved_coupons','rate_limits','users','sessions']
        def snapshot():
            with db.session() as conn:
                return {t:[tuple(r) for r in conn.execute('SELECT * FROM '+t)] for t in tables}
        existing={'kickoff':self.kickoff,'data_n':25,'iddaa_updated_at':self.at.isoformat(),
                  'markets':{'1':{'p':.75}},'iddaa_odds':{'1':1.45}}
        before=snapshot()
        prediction=decision_quality.apply(copy.deepcopy(existing),self.at)
        job=self.job()
        saved=self.submit(job,self.claim(job))
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(before,snapshot())
        self.assertEqual(prediction,decision_quality.apply(copy.deepcopy(existing),self.at))

    def test_two_simultaneous_claims_only_one_wins(self):
        job=self.job()
        principal={'id':'grok-a','scopes':list(service.SCOPES)}
        def attempt():
            from fastapi import HTTPException
            try:
                service.claim(principal,job['id'])
                return 200
            except HTTPException as exc:
                return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(lambda _:attempt(),range(2))),[200,409])


if __name__=='__main__':
    unittest.main()
