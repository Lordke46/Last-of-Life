"""Research SQL/privilege checks in the disposable local CI database only."""
import json
import os
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from unittest.mock import patch

import psycopg
from psycopg.rows import dict_row
from fastapi.testclient import TestClient

from app import db, research_store as store, research_service as service
from app.main import app


def verify(url):
    parsed = urlparse(url)
    assert parsed.hostname in ('localhost', '127.0.0.1') and parsed.path == '/lol_ci', 'Disposable local DB only'
    at = datetime.now(timezone.utc)
    key = 'pg_test.' + 'deterministic-test-only-secret-value' * 2

    @contextmanager
    def connection(role=False):
        with psycopg.connect(url, row_factory=dict_row, prepare_threshold=None) as raw:
            if role:
                raw.execute('SET ROLE lol_research_runtime')
            yield db.Connection(raw, True)

    with connection() as conn:
        conn.execute("DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='research_pilot_login') THEN "
                     "CREATE ROLE research_pilot_login LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; END IF; END $$")
        conn.execute('GRANT lol_research_runtime TO research_pilot_login')
        conn.execute('TRUNCATE ai_research.report_revisions,ai_research.reports,ai_research.jobs,ai_research.service_keys,ai_research.rate_limits')
        conn.execute('INSERT INTO public.matches(id,league,kickoff,home,away) VALUES(?,?,?,?,?) '
                     'ON CONFLICT(id) DO UPDATE SET kickoff=excluded.kickoff',
                     ('research-pg-match', 'eng.1', (at+timedelta(hours=6)).isoformat(), 'Home', 'Away'))
        conn.execute('INSERT INTO ai_research.service_keys(key_id,principal_id,token_hash,scopes,expires_at,created_at) VALUES(?,?,?,?,?,?)',
                     ('pg_test', 'pg-grok', service.sha(key), json.dumps(sorted(service.SCOPES)),
                      (at+timedelta(days=1)).isoformat(), at.isoformat()))

    runtime_url = parsed._replace(netloc='research_pilot_login@' + parsed.hostname + (':'+str(parsed.port) if parsed.port else '')).geturl()
    with patch.dict(os.environ, {'RESEARCH_DATABASE_URL': runtime_url, 'RESEARCH_API_ENABLED':'1', 'AUTH_REQUIRED':'1', 'LOCAL_AUTO_UPDATE':'0'}):
        with patch.object(store, 'session', lambda: connection(False)):
            job = service.enqueue('pg-grok', 'research-pg-match', 'full', (at+timedelta(hours=2)).isoformat())
            skill_job = service.enqueue('pg-grok', 'research-pg-match', 'lineup_check', (at+timedelta(hours=2)).isoformat())
        with closing(TestClient(app)) as client:
            headers = {'Authorization':'Bearer '+key}
            response = client.get('/api/research/jobs',headers=headers)
            assert response.status_code == 200, response.text
            assert job['id'] in {j['id'] for j in response.json()['jobs']}
            assert client.get('/api/research/schema',headers=headers).status_code == 200
            ctx = client.get('/api/research/matches/research-pg-match/context',params={'job_id':job['id']},headers=headers)
            assert ctx.status_code == 200, ctx.text
            response = client.post('/api/research/jobs/'+job['id']+'/claim',headers=headers)
            assert response.status_code == 200, response.text
            lease = response.json()['claim_token']
            renewed = client.post('/api/research/jobs/'+job['id']+'/renew',headers={**headers,'X-Research-Claim':lease})
            assert renewed.status_code == 200, renewed.text
            assert client.post('/api/research/jobs/'+job['id']+'/renew',headers={**headers,'X-Research-Claim':lease}).status_code == 409
            lease = renewed.json()['claim_token']
            payload = {'schema_version':'1.0','job_id':job['id'],'match_id':job['match_id'],'mode':'full',
                       'input_version':job['input_version'],'skill_name':'football-match-intel',
                       'skill_version':'test-only','researched_at':at.isoformat()}
            url_path = '/api/research/jobs/'+job['id']+'/results'
            upload = {**headers, 'Idempotency-Key':'pg-result', 'X-Research-Claim':lease}
            response = client.post(url_path, json=payload, headers=upload)
            assert response.status_code == 200, response.text
            assert response.json()['revision'] == 1
            assert client.post(url_path,json=payload,headers=upload).json()['duplicate']
            report = client.get('/api/research/reports/'+response.json()['report_id'],headers=headers)
            assert report.status_code == 200 and report.json()['applied_to_model'] is False, report.text
            claimed = client.post('/api/research/jobs/'+skill_job['id']+'/claim',headers=headers).json()
            skill = {'schema_version':'1.0','mode':'lineup_check','research_time':at.isoformat(),
                     'matches':[{'match_id':skill_job['match_id'],'findings':[]}]}
            upload_skill = {**headers,'Idempotency-Key':'pg-skill-result','X-Research-Claim':claimed['claim_token'],'X-Skill-Version':'test'}
            converted = client.post('/api/research/jobs/'+skill_job['id']+'/skill-results',json=skill,headers=upload_skill)
            assert converted.status_code == 200, converted.text
            replay = client.post('/api/research/jobs/'+skill_job['id']+'/skill-results',json=skill,headers=upload_skill)
            assert replay.status_code == 200 and replay.json()['duplicate'], replay.text
            skill_report = client.get('/api/research/reports/'+converted.json()['report_id'],headers=headers)
            assert skill_report.status_code == 200 and skill_report.json()['report']['mode'] == 'lineup_check', skill_report.text
            with patch.dict(os.environ, {'RESEARCH_DATABASE_URL':url}):
                assert client.get('/api/research/jobs',headers=headers).status_code == 503, 'Privileged DSN must fail closed'

        # Each attempted forbidden write has its own transaction; no mutation persists.
        forbidden = [
            "UPDATE public.matches SET status=status WHERE FALSE",
            "UPDATE public.news SET title=title WHERE FALSE",
            "UPDATE public.iddaa_odds SET odd=odd WHERE FALSE",
            "UPDATE public.prediction_snapshots SET payload=payload WHERE FALSE",
            "UPDATE public.saved_coupons SET status=status WHERE FALSE",
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.meta LIMIT 1",
            "UPDATE ai_research.service_keys SET revoked=1 WHERE FALSE",
            "UPDATE ai_research.report_revisions SET payload_json=payload_json WHERE FALSE",
            "DELETE FROM ai_research.report_revisions WHERE FALSE",
            "CREATE TABLE ai_research.forbidden_ddl(id TEXT)",
        ]
        for sql in forbidden:
            try:
                with connection(True) as conn:
                    conn.execute(sql)
                    raise AssertionError('Runtime unexpectedly permitted: '+sql)
            except psycopg.errors.InsufficientPrivilege:
                pass
    print('Research PostgreSQL: schema/jobs/context/claim/renew/skill-results/reports, idempotency, privileged-DSN rejection and 11 forbidden operations passed')
