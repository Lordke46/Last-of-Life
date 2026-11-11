"""Permission and enqueue checks; refuses anything except the disposable local test DB."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import research_auto

url = os.environ['TEST_DATABASE_URL']
parsed = urlsplit(url)
assert parsed.hostname == '127.0.0.1' and parsed.path == '/lol_writer_ci' and parsed.port == 55489
at = datetime.now(timezone.utc)
kickoff = (at + timedelta(hours=6)).isoformat()
with psycopg.connect(url, autocommit=True) as conn:
    for path in sorted((Path(__file__).resolve().parent.parent / 'supabase/migrations').glob('*.sql')):
        conn.execute(path.read_text(), prepare=False)
    conn.execute('CREATE ROLE lol_queue_test LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS')
    conn.execute('GRANT lol_research_queue_writer TO lol_queue_test')
    conn.execute('INSERT INTO ai_research.service_keys(key_id,principal_id,token_hash,scopes,expires_at,created_at) VALUES(%s,%s,%s,%s,%s,%s)',
        ('synthetic', 'owner', 'a'*64, json.dumps(['jobs:read']), (at+timedelta(days=1)).isoformat(), at.isoformat()))
    for i in range(3):
        mid=f'queue-test-{i}'; home=f'Home {i}'; away=f'Away {i}'
        conn.execute('INSERT INTO public.matches(id,league,kickoff,home,away) VALUES(%s,%s,%s,%s,%s)',(mid,'eng.1',kickoff,home,away))
        conn.execute('INSERT INTO ai_research.match_identities(canonical_id,competition,home,away,kickoff,reviewed_by,review_reason) VALUES(%s,%s,%s,%s,%s,%s,%s)',(mid,'eng.1',home,away,kickoff,'test','test fixture'))
        conn.execute('INSERT INTO ai_research.match_aliases(alias,canonical_id,provider,source_competition,source_home,source_away,source_kickoff,reviewed_by,review_reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',(mid,mid,'test','eng.1',home,away,kickoff,'test','test fixture'))
queue_url = urlunsplit((parsed.scheme, f'lol_queue_test@127.0.0.1:{parsed.port}', parsed.path, '', ''))
os.environ['RESEARCH_DATABASE_URL'] = queue_url
os.environ['RESEARCH_QUEUE_DATABASE_URL'] = queue_url
entries=[]
for i in range(3):
    prediction={'id':f'queue-test-{i}','league':'eng.1','home':f'Home {i}','away':f'Away {i}','kickoff':kickoff,'markets':{}}
    choice={'market':'1','p':.8,'raw_p':.8,'odds':1.5,'odds_source':'iddaa','min_matches':1}
    entries.append((prediction,[choice]))
result=research_auto.enqueue_candidates('owner',entries,at=at)
assert len(result['results'])==3 and all(x['status']=='queued' for x in result['results']), result
with psycopg.connect(queue_url, autocommit=True) as conn:
    for query in ('SELECT token_hash FROM ai_research.service_keys',
                  "UPDATE ai_research.jobs SET status='failed'",
                  "INSERT INTO public.matches(id) VALUES('unauthorized')"):
        try:
            conn.execute(query)
        except psycopg.errors.InsufficientPrivilege:
            continue
        raise AssertionError('Queue writer was granted forbidden access')
with psycopg.connect(url, autocommit=True) as conn:
    conn.execute('GRANT UPDATE ON ai_research.jobs TO lol_queue_test')
try:
    with research_auto.research_store.queue_session():
        raise AssertionError('Overprivileged queue login was accepted')
except RuntimeError as exc:
    assert 'least-privilege' in str(exc)
print('Queue writer: three jobs created; hashes, updates and public writes denied; excessive grant rejected')
