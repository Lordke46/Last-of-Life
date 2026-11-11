"""Destructive synthetic checks exclusively on the disposable local measurement DB."""
import concurrent.futures
import hashlib
import json
import os
import time
from datetime import datetime,timedelta,timezone
import psycopg
from psycopg.rows import dict_row

socket=os.environ.get('RESEARCH_TEST_SOCKET','')
assert socket.startswith('/var/folders/') and '/lol-staging-isolated-' in socket
opts=dict(host=socket,port=55439,user='postgres',dbname='lol_measure_sandbox',row_factory=dict_row,prepare_threshold=None)
def connect():
    raw=psycopg.connect(**opts)
    row=raw.execute("select current_database() db,current_setting('port') port,current_setting('data_directory') dir").fetchone()
    assert row=={'db':'lol_measure_sandbox','port':'55439','dir':socket+'/data'}
    return raw

def fails(fn, code=None):
    try: fn()
    except psycopg.Error as exc:
        if code: assert exc.diag.message_primary==code, exc.diag.message_primary
    else: raise AssertionError('operation unexpectedly succeeded')

at=datetime.now(timezone.utc);kick=(at+timedelta(hours=6)).isoformat()
with connect() as c:
    c.execute("DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='lol_measure_fixture_writer') THEN CREATE ROLE lol_measure_fixture_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; END IF; END $$")
    c.execute('GRANT lol_measure_model_writer,lol_measure_result_writer TO lol_measure_fixture_writer')
    c.execute('GRANT SELECT,INSERT,UPDATE ON public.matches,public.prediction_snapshots TO lol_measure_fixture_writer')
    c.execute('CREATE POLICY measurement_test_matches ON public.matches TO lol_measure_fixture_writer USING (true) WITH CHECK (true)')
    c.execute('CREATE POLICY measurement_test_predictions ON public.prediction_snapshots TO lol_measure_fixture_writer USING (true) WITH CHECK (true)')
def seed(index, *, capture=None, odd=2.0, observed=None, cache='network_no_cache_evidence', past=False, kickoff_override=None, trusted=True):
    mid='measure-fixture-'+str(index); canonical='measure-canonical-'+str(index)
    start=kickoff_override or ((at-timedelta(hours=1)).isoformat() if past else kick)
    event='measure-event-'+str(index)
    payload={'model':{'1':0.55},'market':{'1':0.5},'blend':{'1':0.52},'confidence':0.6,
             'odds':{'1':odd,'X':3.0},'quote_provenance':{'quote_source':'iddaa','quotes':{
                 '1':{'odd':odd,'identity':[event,1,1,1,'',1,1],'quote_observed_at':(observed or at).isoformat(),'cache_status':cache},
                 'X':{'odd':3.0,'identity':[event,1,1,1,'',2,2],'quote_observed_at':(observed or at).isoformat(),'cache_status':cache}}}}
    with connect() as c:
        c.execute('insert into public.matches(id,league,kickoff,home,away,status) values(%s,%s,%s,%s,%s,%s)',(mid,'eng.1',start,'H'+str(index),'A'+str(index),'Scheduled'))
        c.execute('insert into ai_research.match_identities(canonical_id,competition,home,away,kickoff,reviewed_by,review_reason) values(%s,%s,%s,%s,%s,%s,%s)',(canonical,'eng.1','H'+str(index),'A'+str(index),start,'test','test'))
        c.execute('insert into ai_research.match_aliases(alias,canonical_id,provider,source_competition,source_home,source_away,source_kickoff,reviewed_by,review_reason) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',(mid,canonical,'synthetic','eng.1','H'+str(index),'A'+str(index),start,'test','test'))
        c.execute('insert into ai_research.match_aliases(alias,canonical_id,provider,source_competition,source_home,source_away,source_kickoff,reviewed_by,review_reason) values(%s,%s,%s,%s,%s,%s,%s,%s,%s)',('iddaa:'+event,canonical,'iddaa','eng.1','H'+str(index),'A'+str(index),start,'test','test'))
        c.execute('insert into public.iddaa_events(id,league,competition_id,kickoff,home,away,match_id,updated_at) values(%s,%s,%s,%s,%s,%s,%s,%s)',(event,'eng.1',1,start,'H'+str(index),'A'+str(index),mid,at.isoformat()))
        c.execute('insert into public.iddaa_odds(event_id,market,odd) values(%s,%s,%s),(%s,%s,%s)',(event,'1',odd,event,'X',3.0))
    with connect() as c:
        if trusted: c.execute('SET SESSION AUTHORIZATION lol_measure_fixture_writer')
        c.execute('insert into public.prediction_snapshots(match_id,league,kickoff,captured_at,model_version,parameters,payload) values(%s,%s,%s,%s,%s,%s,%s)',(mid,'eng.1',start,(capture or at).isoformat(),'synthetic-v1','{}',json.dumps(payload)))
    return mid,canonical

def candidate(mid, market='1'):
    with connect() as c:return c.execute('select research_measurement.capture_candidate(%s,%s,%s) id',(mid,market,market)).fetchone()['id']
def freeze_pool(ids):
    with connect() as c:return c.execute('select research_measurement.freeze_pool(%s) id',(ids,)).fetchone()['id']
def outcome(pool,path,ids,reason=None):
    with connect() as c:c.execute('select research_measurement.freeze_outcome(%s,%s,%s,%s)',(pool,path,ids,reason))

fixtures=[seed(i) for i in range(3)]; candidates=[candidate(x[0]) for x in fixtures]
with connect() as c:
    rows=c.execute("select jsonb_build_array(match_id,canonical_id,to_char(kickoff at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US'),to_char(captured_at at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS.US'),model_version,payload,writer_login)::text raw,payload_hash from research_measurement.model_events order by id").fetchall()
    assert len(rows)==3 and all(hashlib.sha256(x['raw'].encode()).hexdigest()==x['payload_hash'] for x in rows)
    row=c.execute('select independent_model_probability,market_probability,blended_probability,odds from research_measurement.candidates where id=%s',(candidates[0],)).fetchone()
    assert row['independent_model_probability'] is None and float(row['market_probability'])==.5 and float(row['blended_probability'])==.52 and float(row['odds'])==2
    before=c.execute('select payload_hash,payload from research_measurement.model_events where match_id=%s',(fixtures[0][0],)).fetchone()
    c.execute("update public.prediction_snapshots set payload='{}' where match_id=%s",(fixtures[0][0],))
    assert c.execute('select payload_hash,payload from research_measurement.model_events where match_id=%s',(fixtures[0][0],)).fetchone()==before

old,_=seed('old',capture=at-timedelta(days=1));fails(lambda:candidate(old),'MEASURE_SNAPSHOT_UNTRUSTED')
future,_=seed('future',capture=at+timedelta(days=1));fails(lambda:candidate(future),'MEASURE_SNAPSHOT_UNTRUSTED')
superuser,_=seed('untrusted-superuser',trusted=False);fails(lambda:candidate(superuser),'MEASURE_SNAPSHOT_UNTRUSTED')
past,_=seed('past',past=True);fails(lambda:candidate(past),'MEASURE_SNAPSHOT_UNTRUSTED')
for i,odd in enumerate((-1,0,1,10000)):
    mid,_=seed('bad-'+str(i),odd=odd);fails(lambda m=mid:candidate(m),'MEASURE_INVALID_ODDS')
stale,_=seed('stale',observed=at-timedelta(hours=3));fails(lambda:candidate(stale),'MEASURE_PRICE_STALE')
no_zone,_=seed('no-zone',observed=at.replace(tzinfo=None));fails(lambda:candidate(no_zone),'MEASURE_PRICE_UNVERIFIED')
unverified,_=seed('unknown',cache='unknown');fails(lambda:candidate(unverified),'MEASURE_PRICE_UNVERIFIED')
try:
    with connect() as c:
        c.execute('SET LOCAL ROLE lol_measure_operator')
        c.execute("INSERT INTO research_measurement.model_events(id,match_id,canonical_id,kickoff,captured_at,model_version,payload,payload_hash,writer_login) VALUES('fake','fake','fake',now()+interval '1 day',now(),'x','{}',%s,'fake')",('0'*64,))
except psycopg.errors.InsufficientPrivilege: pass
else: raise AssertionError('caller could insert arbitrary hash')
try:
    with connect() as c:c.execute("update research_measurement.model_events set payload='{}'::jsonb")
except psycopg.errors.RaiseException as e:assert e.diag.message_primary=='MEASURE_APPEND_ONLY'
else: raise AssertionError('historical model event changed')

pool=freeze_pool(candidates); outcome(pool,'A',candidates)
with connect() as c:
    frozen=c.execute('select legs,odds_product from research_measurement.path_outcomes where pool_id=%s and path=%s',(pool,'A')).fetchone()
    assert len(frozen['legs'])==3 and frozen['odds_product']==8
fails(lambda:outcome(pool,'A',candidates))
# A duplicate match in the same pool, even across two separate market snapshots.
extra=candidate(fixtures[0][0],'X'); bad_pool=freeze_pool([candidates[0],extra,candidates[1]])
fails(lambda:outcome(bad_pool,'A',[candidates[0],extra,candidates[1]]),'MEASURE_THREE_DISTINCT_MATCHES_REQUIRED')
fails(lambda:outcome(freeze_pool(candidates),'A',candidates[:2]),'MEASURE_THREE_LEGS_REQUIRED')
# A changed source quote and rescheduled kickoff fail closed before freeze.
changed,_=seed('changed');changed_candidate=candidate(changed)
changed_pool=freeze_pool([candidates[0],candidates[1],changed_candidate])
with connect() as c:c.execute("update public.iddaa_odds set odd=2.5 where event_id='measure-event-changed' and market='1'")
fails(lambda:outcome(changed_pool,'A',[candidates[0],candidates[1],changed_candidate]),'MEASURE_PRICE_STALE_OR_INVALID')
rescheduled,_=seed('rescheduled');rescheduled_candidate=candidate(rescheduled)
with connect() as c:c.execute('update public.matches set kickoff=%s where id=%s',((at+timedelta(hours=7)).isoformat(),rescheduled))
fails(lambda:freeze_pool([rescheduled_candidate]),'MEASURE_INVALID_POOL')
# Decisions preserve the same pool, including failed and unreviewed cases; none becomes a success.
with connect() as c:
    for cid,state in zip(candidates,['unreviewed','failed','incomplete']):
        c.execute('select research_measurement.record_decision(%s,%s,%s,%s,%s,%s)',(pool,cid,state,'not_reviewed',None,None))
fails(lambda:outcome(pool,'B',candidates),'MEASURE_INCOMPLETE_RESEARCH')
outcome(pool,'B',None,'No fully researched coupon')
with connect() as c:assert c.execute("select outcome from research_measurement.path_outcomes where pool_id=%s and path='B'",(pool,)).fetchone()['outcome']=='no_coupon'
empty_pool=freeze_pool([])
outcome(empty_pool,'A',None,'No eligible candidates')
outcome(empty_pool,'B',None,'No eligible candidates')
with connect() as c:assert c.execute('select count(*) n from research_measurement.path_outcomes where pool_id=%s and outcome=%s',(empty_pool,'no_coupon')).fetchone()['n']==2
missing=freeze_pool(candidates)
def claim_complete():
    with connect() as c:c.execute('select research_measurement.record_decision(%s,%s,%s,%s,%s,%s)',(missing,candidates[0],'complete','keep',None,None))
fails(claim_complete,'MEASURE_INCOMPLETE_RESEARCH')
# A schema-valid flag alone cannot convert an empty report into complete research.
with connect() as c:
    for mode, suffix, report_body in [('full','empty',{'findings':[],'sources':[]}),
                                      ('lineup_check','attested',{'findings':[{'original_status':'single_source'}],'sources':[{'id':'s'}]})]:
        mid=fixtures[0][0]; context=json.dumps({'match':{'id':mid,'league':'eng.1','home':'H0','away':'A0','kickoff':kick}})
        c.execute("insert into ai_research.jobs(id,owner_id,match_id,mode,status,deadline,input_version,context_json,request_id,created_at) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",('job-'+suffix,'test',mid,mode,'completed',(at+timedelta(hours=2)).isoformat(),'a'*64,context,suffix,at.isoformat()))
        c.execute('insert into ai_research.reports(id,owner_id,match_id,mode,created_at) values(%s,%s,%s,%s,%s)',('report-'+suffix,'test',mid,mode,at.isoformat()))
        c.execute('insert into ai_research.report_revisions(id,report_id,revision,job_id,idempotency_key,payload_hash,payload_json,received_at,researched_at,skill_version,freshness_status,validation_status) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',('revision-'+suffix,'report-'+suffix,1,'job-'+suffix,suffix,'f'*64,json.dumps(report_body),at.isoformat(),at.isoformat(),'test','current','schema_valid'))
def attest_empty():
    with connect() as c:c.execute("select research_measurement.attest_complete('report-empty',1,'manual review')")
fails(attest_empty,'MEASURE_INCOMPLETE_RESEARCH')
def unattested():
    with connect() as c:c.execute('select research_measurement.record_decision(%s,%s,%s,%s,%s,%s)',(missing,candidates[0],'complete','keep','report-attested',1))
fails(unattested,'MEASURE_INCOMPLETE_RESEARCH')
with connect() as c:
    c.execute("select research_measurement.attest_complete('report-attested',1,'operator checked source and research scope')")
    c.execute('select research_measurement.record_decision(%s,%s,%s,%s,%s,%s)',(missing,candidates[0],'complete','keep','report-attested',1))
# Both paths may freeze independently only from the same immutable candidate pool.
for i in (1,2):
    with connect() as c:
        mid=fixtures[i][0]; suffix='candidate-'+str(i)
        context=json.dumps({'match':{'id':mid,'league':'eng.1','home':'H'+str(i),'away':'A'+str(i),'kickoff':kick}})
        c.execute("insert into ai_research.jobs(id,owner_id,match_id,mode,status,deadline,input_version,context_json,request_id,created_at) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",('job-'+suffix,'test',mid,'lineup_check','completed',(at+timedelta(hours=2)).isoformat(),'a'*64,context,suffix,at.isoformat()))
        c.execute('insert into ai_research.reports(id,owner_id,match_id,mode,created_at) values(%s,%s,%s,%s,%s)',('report-'+suffix,'test',mid,'lineup_check',at.isoformat()))
        body=json.dumps({'findings':[{'original_status':'single_source'}],'sources':[{'id':'s'}]})
        c.execute('insert into ai_research.report_revisions(id,report_id,revision,job_id,idempotency_key,payload_hash,payload_json,received_at,researched_at,skill_version,freshness_status,validation_status) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',('revision-'+suffix,'report-'+suffix,1,'job-'+suffix,suffix,'f'*64,body,at.isoformat(),at.isoformat(),'test','current','schema_valid'))
        c.execute('select research_measurement.attest_complete(%s,%s,%s)',('report-'+suffix,1,'manual scope check'))
valid_pool=freeze_pool(candidates)
with connect() as c:
    for cid,report in zip(candidates,['report-attested','report-candidate-1','report-candidate-2']):
        c.execute('select research_measurement.record_decision(%s,%s,%s,%s,%s,%s)',(valid_pool,cid,'complete','keep',report,1))
outcome(valid_pool,'A',candidates);outcome(valid_pool,'B',candidates)
with connect() as c:assert c.execute('select count(*) n from research_measurement.path_outcomes where pool_id=%s and outcome=%s',(valid_pool,'coupon')).fetchone()['n']==2
# A failed transaction leaves neither coupon header nor legs.
rollback_pool=freeze_pool(candidates)
try:
    with connect() as c:
        c.execute('select research_measurement.freeze_outcome(%s,%s,%s,%s)',(rollback_pool,'A',candidates,None))
        raise RuntimeError('synthetic interruption')
except RuntimeError: pass
with connect() as c:assert c.execute('select count(*) n from research_measurement.path_outcomes where pool_id=%s',(rollback_pool,)).fetchone()['n']==0
# A transaction that attempts to commit after kickoff is rejected at the deferred guard.
short_start=(datetime.now(timezone.utc)+timedelta(seconds=3)).isoformat()
short,_=seed('commit-deadline',kickoff_override=short_start);short_candidate=candidate(short)
short_pool=freeze_pool([candidates[0],candidates[1],short_candidate])
def late_commit():
    with connect() as c:
        c.execute('select research_measurement.freeze_outcome(%s,%s,%s,%s)',(short_pool,'A',[candidates[0],candidates[1],short_candidate],None))
        time.sleep(3.1)
fails(late_commit,'MEASURE_COMMIT_AFTER_KICKOFF')
with connect() as c:assert c.execute('select count(*) n from research_measurement.path_outcomes where pool_id=%s',(short_pool,)).fetchone()['n']==0
# Ten requests for a single pool/path produce one immutable outcome.
concurrent_pool=freeze_pool(candidates)
def worker(_):
    try: outcome(concurrent_pool,'A',candidates);return 'ok'
    except psycopg.errors.UniqueViolation:return 'conflict'
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as p: results=list(p.map(worker,range(10)))
assert results.count('ok')==1 and results.count('conflict')==9,results
# There is no leg table to append to; direct outcome mutations fail.
try:
    with connect() as c:c.execute("update research_measurement.path_outcomes set legs=legs || '[{}]'::jsonb where pool_id=%s",(pool,))
except psycopg.errors.RaiseException as e:assert e.diag.message_primary=='MEASURE_APPEND_ONLY'
else: raise AssertionError('frozen legs changed')
def result_now():
    with connect() as c:c.execute('select research_measurement.record_final_result(%s)',(fixtures[0][1],))
fails(result_now,'MEASURE_RESULT_BEFORE_KICKOFF')
# Even a trusted writer cannot record a final result before kickoff.
with connect() as c:
    c.execute('SET SESSION AUTHORIZATION lol_measure_fixture_writer')
    c.execute("update public.matches set home_goals=1,away_goals=0,completed=1,status='FT' where id=%s",(fixtures[0][0],))
with connect() as c:
    assert c.execute('select count(*) n from research_measurement.result_observations where canonical_id=%s',(fixtures[0][1],)).fetchone()['n']==0
def result_unconfigured():
    with connect() as c:c.execute('select research_measurement.record_final_result(%s)',('measure-canonical-past',))
fails(result_unconfigured,'MEASURE_TRUSTED_RESULT_SOURCE_NOT_CONFIGURED')
# Only a post-kickoff final from the trusted writer creates a server-timed revision.
with connect() as c:
    c.execute('SET SESSION AUTHORIZATION lol_measure_fixture_writer')
    c.execute("update public.matches set home_goals=1,away_goals=0,completed=0,status='HT' where id=%s",(past,))
with connect() as c:
    assert c.execute('select count(*) n from research_measurement.result_observations where canonical_id=%s',('measure-canonical-past',)).fetchone()['n']==0
with connect() as c:
    c.execute('SET SESSION AUTHORIZATION lol_measure_fixture_writer')
    c.execute("update public.matches set home_goals=1,away_goals=0,completed=1,status='FT' where id=%s",(past,))
    c.execute("update public.matches set home_goals=1,away_goals=0,completed=1,status='FT' where id=%s",(past,))
    c.execute("update public.matches set home_goals=2,away_goals=0,completed=1,status='FT' where id=%s",(past,))
with connect() as c:
    scores=c.execute('select revision,home_goals,source,verification_status from research_measurement.result_observations where canonical_id=%s order by revision',('measure-canonical-past',)).fetchall()
    assert [(r['revision'],r['home_goals']) for r in scores]==[(1,1),(2,2)]
    assert all(r['source']=='trusted_backend_match_cache' and r['verification_status']=='unverified' for r in scores)
try:
    with connect() as c:c.execute("update research_measurement.result_observations set home_goals=9 where canonical_id='measure-canonical-past'")
except psycopg.errors.RaiseException as e:assert e.diag.message_primary=='MEASURE_APPEND_ONLY'
else: raise AssertionError('settlement revision changed')
print('Measurement PostgreSQL: trusted forward capture/hash; price guards; atomic three-leg freeze; A/B no-coupon; concurrency/rollback; append-only; final-result fail-closed passed.')
