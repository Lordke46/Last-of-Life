"""Destructive fixtures only in the explicitly isolated local lol_queue_sandbox DB."""
import concurrent.futures
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from app import db, research_service as service, research_store as store

socket=os.environ.get('RESEARCH_TEST_SOCKET','')
assert socket.startswith('/var/folders/') and '/lol-staging-isolated-' in socket
opts=dict(host=socket,port=55439,user='postgres',dbname='lol_queue_sandbox',row_factory=dict_row,prepare_threshold=None)

def connect():
    raw=psycopg.connect(**opts)
    identity=raw.execute("select current_database() db,current_setting('port') port,current_setting('data_directory') dir").fetchone()
    assert identity['db']=='lol_queue_sandbox' and identity['port']=='55439' and identity['dir']==socket+'/data'
    return raw

@contextmanager
def session():
    with connect() as raw:
        yield db.Connection(raw,True)

at=datetime.now(timezone.utc); kickoff=(at+timedelta(hours=6)).isoformat(); deadline=(at+timedelta(hours=2)).isoformat()
with connect() as raw:
    raw.execute("insert into ai_research.service_keys(key_id,principal_id,token_hash,scopes,expires_at,created_at) values(%s,%s,%s,%s,%s,%s)",
        ('synthetic','tester','a'*64,json.dumps(sorted(service.SCOPES)),(at+timedelta(days=1)).isoformat(),at.isoformat()))
    for i in range(10):
        mid='fixture-'+str(i); home='Home '+str(i); away='Away '+str(i); cid='canonical-'+str(i)
        raw.execute('insert into public.matches(id,league,kickoff,home,away) values(%s,%s,%s,%s,%s)',(mid,'eng.1',kickoff,home,away))
        raw.execute('insert into ai_research.match_identities(canonical_id,competition,home,away,kickoff,reviewed_by,review_reason) values(%s,%s,%s,%s,%s,%s,%s)',(cid,'eng.1',home,away,kickoff,'operator-test','synthetic verified fixture'))
        raw.execute('insert into ai_research.match_aliases(alias,canonical_id,provider,source_competition,source_home,source_away,source_kickoff,reviewed_by,review_reason) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',(mid,cid,'fixture-provider','eng.1',home,away,kickoff,'operator-test','synthetic verified mapping'))
    baseline=tuple(raw.execute('select (select count(*) from public.matches) as matches,(select count(*) from public.prediction_snapshots) as predictions,(select count(*) from public.saved_coupons) as coupons').fetchone().values())

with patch.dict(os.environ,{'RESEARCH_DATABASE_URL':'test-only-placeholder'}),patch.object(store,'session',session):
    def attempt(i):
        try:
            service.enqueue('tester','fixture-'+str(i),'full',deadline,request_id='concurrent-'+str(i)); return 'ok'
        except HTTPException as exc:
            return exc.detail['code']
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results=list(pool.map(attempt,range(10)))
    assert results.count('ok')==3 and results.count('RESEARCH_DAILY_QUOTA_EXCEEDED')==7,results
    with connect() as c:
        jobs=c.execute("select match_id,canonical_id,input_version from ai_research.jobs where mode='full' order by match_id").fetchall()
        chosen=jobs[0]; i=int(chosen['match_id'].split('-')[1]); home='Home '+str(i);away='Away '+str(i)
        c.execute("update ai_research.jobs set status='expired' where match_id=%s",(chosen['match_id'],))
        c.execute("update ai_research.jobs set status='failed' where match_id=%s",(jobs[1]['match_id'],))
        c.execute('insert into public.iddaa_events(id,league,competition_id,kickoff,home,away,match_id,updated_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',('alias-1','eng.1',1,kickoff,home,away,chosen['match_id'],at.isoformat()))
        c.execute('insert into ai_research.match_aliases(alias,canonical_id,provider,source_competition,source_home,source_away,source_kickoff,reviewed_by,review_reason) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',('iddaa:alias-1',chosen['canonical_id'],'iddaa','eng.1',home,away,kickoff,'operator-test','same fixture reviewed'))
    def rejected(match_id,request,code,**kw):
        try: service.enqueue('tester',match_id,'full',deadline,request_id=request,**kw)
        except HTTPException as e: assert e.status_code==409 and e.detail['code']==code,(e.status_code,e.detail)
        else: raise AssertionError('request unexpectedly accepted')
    rejected('iddaa:alias-1','different-provider','RESEARCH_REPEAT_APPROVAL_REQUIRED')
    with patch.object(service.context,'digest',return_value='b'*64):
        rejected(chosen['match_id'],'new-version','RESEARCH_REPEAT_APPROVAL_REQUIRED')
    rejected(chosen['match_id'],'unauthorized-approval','RESEARCH_REPEAT_APPROVAL_REQUIRED',rerun_approval_id='not-approved')
    rejected('fixture-'+str(next(i for i,r in enumerate(results) if r!='ok')),'quota-again','RESEARCH_DAILY_QUOTA_EXCEEDED')
    with connect() as c:
        c.execute('insert into ai_research.rerun_approvals(id,canonical_id,mode,owner_id,approved_by,reason,expires_at) values(%s,%s,%s,%s,%s,%s,%s)',('approval-test',chosen['canonical_id'],'full','tester','operator-test','explicit test rerun',at+timedelta(hours=1)))
        assert c.execute("select approved_by from ai_research.rerun_approvals where id='approval-test'").fetchone()['approved_by']=='postgres'
    with patch.object(service.context,'digest',return_value=chosen['input_version']):
        rejected(chosen['match_id'],'concurrent-'+str(i),'RESEARCH_IDEMPOTENCY_CONFLICT',rerun_approval_id='approval-test')
    with connect() as c:
        assert c.execute("select consumed_by_job from ai_research.rerun_approvals where id='approval-test'").fetchone()['consumed_by_job'] is None
    renewed=service.enqueue('tester',chosen['match_id'],'full',deadline,request_id='approved-rerun',rerun_approval_id='approval-test')
    assert renewed['match_id']==chosen['match_id']
    assert service.enqueue('tester',chosen['match_id'],'full',deadline,request_id='approved-rerun',rerun_approval_id='approval-test')['id']==renewed['id']
    rejected(chosen['match_id'],'reuse-approval','RESEARCH_REPEAT_APPROVAL_REQUIRED',rerun_approval_id='approval-test')
    # Separate mode is not counted against the full-match quota.
    lineup=service.enqueue('tester',chosen['match_id'],'lineup_check',deadline,request_id='lineup-test')
    assert lineup['mode']=='lineup_check'
    from app.research_models import ResearchResult
    lease=service.claim({'id':'tester','scopes':list(service.SCOPES)},lineup['id'])['claim_token']
    payload=ResearchResult.model_validate_json(json.dumps({'schema_version':'1.0','job_id':lineup['id'],
        'match_id':lineup['match_id'],'mode':'lineup_check','input_version':lineup['input_version'],
        'skill_name':'football-match-intel','skill_version':'test','researched_at':at.isoformat()}))
    saved=service.save_result({'id':'tester','scopes':list(service.SCOPES)},lineup['id'],payload,lease,'synthetic-result')
    assert saved['revision']==1
    # The application also refuses a fixture that has started.
    with connect() as c:
        c.execute('insert into public.matches(id,league,kickoff,home,away) values(%s,%s,%s,%s,%s)',('past','eng.1',(at-timedelta(minutes=1)).isoformat(),'P','Q'))
    try: service.enqueue('tester','past','full',deadline,request_id='past-test')
    except ValueError: pass
    else: raise AssertionError('past fixture unexpectedly accepted')
    with connect() as c:
        c.execute('insert into public.matches(id,league,kickoff,home,away) values(%s,%s,%s,%s,%s)',('unverified','eng.1',kickoff,'U','V'))
    rejected('unverified','unknown-identity','RESEARCH_IDENTITY_UNVERIFIED')
    with connect() as c:
        c.execute('insert into ai_research.match_identities(canonical_id,competition,home,away,kickoff,reviewed_by,review_reason) values(%s,%s,%s,%s,%s,%s,%s)',('mismatch-c','eng.1','U','V',kickoff,'fake','test'))
        c.execute('insert into ai_research.match_aliases(alias,canonical_id,provider,source_competition,source_home,source_away,source_kickoff,reviewed_by,review_reason) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',('unverified','mismatch-c','test','eng.1','WRONG','V',kickoff,'fake','test'))
    rejected('unverified','mismatched-source','RESEARCH_IDENTITY_MISMATCH')
    # Direct DB INSERT is rejected as well, even if bypassing Python's precheck.
    past_kickoff=(at-timedelta(minutes=1)).isoformat()
    with connect() as c:
        c.execute('insert into ai_research.match_identities(canonical_id,competition,home,away,kickoff,reviewed_by,review_reason) values(%s,%s,%s,%s,%s,%s,%s)',('past-c','eng.1','P','Q',past_kickoff,'fake','test'))
        c.execute('insert into ai_research.match_aliases(alias,canonical_id,provider,source_competition,source_home,source_away,source_kickoff,reviewed_by,review_reason) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',('past','past-c','test','eng.1','P','Q',past_kickoff,'fake','test'))
    try:
        with connect() as c:
            context=json.dumps({'match':{'id':'past','league':'eng.1','home':'P','away':'Q','kickoff':past_kickoff}})
            c.execute("insert into ai_research.jobs(id,owner_id,match_id,mode,status,deadline,input_version,context_json,request_id,created_at) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",('past-direct','tester','past','full','queued',deadline,'c'*64,context,'direct',at.isoformat()))
    except psycopg.errors.RaiseException as e: assert e.diag.message_primary=='RESEARCH_MATCH_NOT_UPCOMING'
    else: raise AssertionError('DB unexpectedly accepted past match')
    try:
        with psycopg.connect(**opts) as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            c.execute("insert into ai_research.jobs(id,owner_id,match_id,mode,status,deadline,input_version,context_json,request_id,created_at) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",('wrong-isolation','tester','past','full','queued',deadline,'c'*64,context,'direct-rr',at.isoformat()))
    except psycopg.errors.RaiseException as e: assert e.diag.message_primary=='RESEARCH_ISOLATION_UNSUPPORTED'
    else: raise AssertionError('Unsupported isolation unexpectedly accepted')
    with connect() as c: c.execute('ALTER TABLE ai_research.jobs DISABLE TRIGGER research_guard_job_insert')
    try:
        try: service.enqueue('tester',chosen['match_id'],'lineup_check',deadline,request_id='disabled-guard')
        except HTTPException as e: assert e.status_code==503 and e.detail['code']=='RESEARCH_QUEUE_GUARD_NOT_INSTALLED'
        else: raise AssertionError('Disabled database guard unexpectedly accepted')
    finally:
        with connect() as c: c.execute('ALTER TABLE ai_research.jobs ENABLE TRIGGER research_guard_job_insert')

# Real SQL privileges, separate transactions; writes must fail, even when touching no rows.
for statement in ('INSERT INTO ai_research.jobs DEFAULT VALUES','UPDATE ai_research.jobs SET status=status WHERE false',
                  'DELETE FROM ai_research.jobs WHERE false','UPDATE ai_research.report_revisions SET payload_json=payload_json WHERE false',
                  'SELECT * FROM ai_research.service_keys','SELECT * FROM ai_research.jobs'):
    try:
        with connect() as c:
            c.execute('SET LOCAL ROLE lol_research_ui_reader'); c.execute(statement)
    except psycopg.errors.InsufficientPrivilege: pass
    else: raise AssertionError('reader write unexpectedly allowed')
with connect() as c:
    c.execute('SET LOCAL ROLE lol_research_ui_reader');c.execute('select count(*) from ai_research.ui_reports').fetchone()
original_connect=psycopg.connect
def reader_connect(*args,**kwargs):
    raw=original_connect(**opts,autocommit=True)
    raw.execute('SET ROLE lol_research_ui_reader')
    raw.autocommit=False
    return raw
with patch.dict(os.environ,{'RESEARCH_DATABASE_URL':'bot-not-used','RESEARCH_UI_DATABASE_URL':'reader-test'}), patch('psycopg.connect',side_effect=reader_connect):
    with store.ui_session() as reader:
        assert reader.execute('SELECT COUNT(*) AS n FROM ai_research.ui_reports').fetchone()['n']==1
    from fastapi.testclient import TestClient
    from app.main import app
    with patch.dict(os.environ,{'AUTH_REQUIRED':'0','RESEARCH_UI_ENABLED':'1','RESEARCH_UI_OWNER':'tester'}), \
         patch('app.auth.current_user',return_value={'id':'admin','role':'admin'}):
        # No context manager: skip lifespan and never initialize the main DB.
        client=TestClient(app)
        try:
            response=client.get('/api/grok-intelligence/'+chosen['match_id'])
            assert response.status_code==200 and len(response.json()['reports'])==1
        finally:
            client.close()
with patch.dict(os.environ,{'RESEARCH_DATABASE_URL':'bot-not-used','RESEARCH_UI_DATABASE_URL':''}):
    try:
        with store.ui_session(): pass
    except RuntimeError: pass
    else: raise AssertionError('UI unexpectedly fell back to bot credentials')
def bot_connect(*args,**kwargs):
    raw=original_connect(**opts,autocommit=True)
    raw.execute('SET ROLE lol_research_runtime')
    raw.autocommit=False
    return raw
with patch.dict(os.environ,{'RESEARCH_DATABASE_URL':'bot-not-used','RESEARCH_UI_DATABASE_URL':'wrong-role'}), patch('psycopg.connect',side_effect=bot_connect):
    try:
        with store.ui_session(): pass
    except RuntimeError: pass
    else: raise AssertionError('UI unexpectedly accepted the Bot database role')
try:
    with connect() as c:
        c.execute('SET LOCAL ROLE lol_research_runtime')
        c.execute("INSERT INTO ai_research.rerun_approvals(id,canonical_id,mode,owner_id,approved_by,reason,expires_at) VALUES('forbidden','x','full','x','x','x',now())")
except psycopg.errors.InsufficientPrivilege: pass
else: raise AssertionError('Bot role unexpectedly created an approval')
with connect() as c:
    after=tuple(c.execute('select (select count(*) from public.matches)-2 as matches,(select count(*) from public.prediction_snapshots) as predictions,(select count(*) from public.saved_coupons) as coupons').fetchone().values())
    assert after==baseline
    c.execute("INSERT INTO research_evaluation.baseline_candidates(id,match_identity,market,kickoff,model_version,model_snapshot,model_snapshot_hash,snapshot_captured_at,frozen_at,data_status) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)",
        ('baseline-test',chosen['canonical_id'],'1',kickoff,'model-v1','{}','f'*64,at,at-timedelta(days=2),'synthetic'))
    recorded=c.execute("SELECT frozen_at FROM research_evaluation.baseline_candidates WHERE id='baseline-test'").fetchone()['frozen_at']
    assert recorded>=at
    c.execute("INSERT INTO research_evaluation.research_decisions(id,baseline_id,decision,evaluator) VALUES('decision-test','baseline-test','no_research','operator-test')")
    c.execute("INSERT INTO research_evaluation.coupon_snapshots(id,kind) VALUES('coupon-test','shadow')")
    c.execute("INSERT INTO research_evaluation.coupon_selections(coupon_id,baseline_id,market) VALUES('coupon-test','baseline-test','1')")
for statement in ("UPDATE research_evaluation.baseline_candidates SET model_version='x' WHERE id='baseline-test'",
                  "DELETE FROM research_evaluation.research_decisions WHERE id='decision-test'",
                  "INSERT INTO research_evaluation.baseline_candidates(id,match_identity,market,kickoff,model_version,model_snapshot,model_snapshot_hash,snapshot_captured_at,data_status) SELECT 'retro',canonical_id,'1',now()-interval '1 day','v','{}','hash',now()-interval '2 days','old' FROM ai_research.match_identities LIMIT 1"):
    try:
        with connect() as c: c.execute(statement)
    except psycopg.errors.RaiseException: pass
    else: raise AssertionError('evaluation mutation or backdating unexpectedly accepted')
with connect() as c:
    c.execute('insert into ai_research.match_aliases(alias,canonical_id,provider,source_competition,source_home,source_away,source_kickoff,reviewed_by,review_reason) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',('legacy-alias',chosen['canonical_id'],'legacy','eng.1',home,away,kickoff,'fake','verified legacy alias'))
    c.execute('ALTER TABLE ai_research.jobs DISABLE TRIGGER research_guard_job_insert')
    legacy_context=json.dumps({'match':{'id':'legacy-alias','league':'eng.1','home':home,'away':away,'kickoff':kickoff}})
    c.execute("insert into ai_research.jobs(id,owner_id,match_id,mode,status,deadline,input_version,context_json,request_id,created_at) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",('legacy-test','tester','legacy-alias','full','expired',deadline,'d'*64,legacy_context,'legacy-test',at.isoformat()))
    c.execute('ALTER TABLE ai_research.jobs ENABLE TRIGGER research_guard_job_insert')
with patch.dict(os.environ,{'RESEARCH_DATABASE_URL':'test-only-placeholder'}),patch.object(store,'session',session):
    try: service.enqueue('tester',chosen['match_id'],'full',deadline,request_id='backfill-blocked')
    except HTTPException as e: assert e.detail['code']=='RESEARCH_IDENTITY_BACKFILL_REQUIRED'
    else: raise AssertionError('Unreviewed legacy identity did not block enqueue')
    with connect() as c: c.execute('update ai_research.jobs set canonical_id=%s where id=%s',(chosen['canonical_id'],'legacy-test'))
    try: service.enqueue('tester',chosen['match_id'],'full',deadline,request_id='after-backfill')
    except HTTPException as e: assert e.detail['code']=='RESEARCH_REPEAT_APPROVAL_REQUIRED'
    else: raise AssertionError('Legacy backfill unexpectedly bypassed repeat guard')
print('PostgreSQL: 10 concurrent enqueues, quota including expired/failed, alias dedup, rerun authorization, lineup separation, started match and reader write denial passed.')
