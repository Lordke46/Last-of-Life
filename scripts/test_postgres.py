"""Yalnız yerel, geçici CI PostgreSQL veritabanında migration ve SQL uyumluluk testi."""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg
from psycopg.rows import dict_row
from app.db import Connection
from app.updater import acquire_lock, release_lock, renew_lock, LostLease
from app.model import snapshots
from app import quote_provenance
from app.scrapers import espn
from datetime import datetime,timezone,timedelta

url = os.environ['TEST_DATABASE_URL']
parsed = urlparse(url)
assert parsed.hostname in ('127.0.0.1', 'localhost') and parsed.path == '/lol_ci', 'Only disposable CI DB allowed'
files = sorted((Path(__file__).resolve().parent.parent / 'supabase/migrations').glob('*.sql'))
assert len({p.name.split('_', 1)[0] for p in files}) == len(files)
with psycopg.connect(url, row_factory=dict_row, prepare_threshold=None) as raw:
    conn = Connection(raw, True)
    for path in files:
        conn.executescript(path.read_text())
    repair = files[-1].read_text()
    conn.executescript(repair)
    # Legacy duplicate-version case: recorded migration did not install auth/news.
    conn.execute('DROP TABLE public.sessions,public.password_resets,public.users CASCADE')
    conn.execute('ALTER TABLE public.news DROP COLUMN category, DROP COLUMN reason')
    conn.executescript(repair)
    conn.executescript(repair)
    assert conn.execute("SELECT to_regclass('public.users') AS name").fetchone()['name'] == 'users'
    first = acquire_lock(conn, 3)
    assert first and acquire_lock(conn, 3) is None
    assert not release_lock(conn, 'other-owner')
    renew_lock(conn, first, 3)
    assert release_lock(conn, first)
    assert acquire_lock(conn, 3)
    snapshots.record_safely(conn, [])
    assert conn.execute('SELECT COUNT(*) n FROM prediction_snapshots').fetchone()['n'] == 0
    # Exercise production SQL paths added by the data-integrity audit.
    at=datetime.now(timezone.utc)
    event={'id':'ci','quote_metadata':{'1':{'identity':['ci',1,1],'odd':1.8,
        'quote_observed_at':at.isoformat(),'cache_status':'network_no_cache_evidence'}}}
    quote_provenance.save(conn,[event],at.isoformat(),{})
    assert quote_provenance.load(conn,['ci'])['events']['ci']['quotes']['1']['odd']==1.8
    quote_provenance.failed(conn,'offline',at.isoformat())
    assert quote_provenance.for_event(quote_provenance.load(conn,['ci']),'ci')['quote_status']=='fetch_failed'
    match={'id':'ci-match','league':'eng.1','kickoff':(at-timedelta(days=1)).isoformat(),
           'home':'A','away':'B','home_id':'1','away_id':'2','home_goals':1,'away_goals':0,'completed':1,'status':'FT'}
    espn.upsert(conn,match)
    espn.upsert(conn,{**match,'home_goals':None,'away_goals':None,'completed':0,'status':'Scheduled'})
    assert conn.execute("SELECT home_goals FROM matches WHERE id='ci-match'").fetchone()['home_goals']==1
print('PostgreSQL migrations, idempotent repair and lease SQL passed')
