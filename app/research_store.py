"""Explicitly provisioned research storage; never reuse production credentials."""
import os
import sqlite3
from contextlib import contextmanager

from . import db

TABLES = ('service_keys', 'jobs', 'reports', 'report_revisions', 'rate_limits')

# SQLite is for the isolated pilot only. PostgreSQL has a private schema and role.
SQLITE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS research_service_keys (
 key_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 scopes TEXT NOT NULL, expires_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_jobs (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, match_id TEXT NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('full','lineup_check')),
 status TEXT NOT NULL CHECK(status IN ('queued','claimed','completed','expired')),
 deadline TEXT NOT NULL, input_version TEXT NOT NULL, context_json TEXT NOT NULL,
 request_id TEXT NOT NULL, created_at TEXT NOT NULL, lease_hash TEXT,
 lease_expires_at TEXT, report_id TEXT,
 UNIQUE(owner_id,match_id,mode,input_version,request_id)
);
CREATE INDEX IF NOT EXISTS ix_research_jobs_owner ON research_jobs(owner_id,status,deadline);
CREATE TABLE IF NOT EXISTS research_reports (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, match_id TEXT NOT NULL,
 mode TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(owner_id,match_id,mode)
);
CREATE TABLE IF NOT EXISTS research_report_revisions (
 id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES research_reports(id),
 revision INTEGER NOT NULL, job_id TEXT NOT NULL UNIQUE REFERENCES research_jobs(id),
 idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
 received_at TEXT NOT NULL, researched_at TEXT NOT NULL, skill_version TEXT NOT NULL,
 freshness_status TEXT NOT NULL CHECK(freshness_status IN ('current','expired')),
 validation_status TEXT NOT NULL CHECK(validation_status='schema_valid'),
 UNIQUE(report_id,revision)
);
CREATE TABLE IF NOT EXISTS research_rate_limits (
 bucket TEXT NOT NULL, window_id INTEGER NOT NULL, hits INTEGER NOT NULL,
 PRIMARY KEY(bucket,window_id)
);
'''


def postgres():
    return bool(os.environ.get('RESEARCH_DATABASE_URL'))


def table(name):
    if name not in TABLES:
        raise ValueError('Unknown research table')
    return 'ai_research.' + name if postgres() else 'research_' + name


def source(name):
    if name not in ('matches', 'events', 'odds', 'news', 'predictions', 'meta'):
        raise ValueError('Unknown context source')
    local = {'events': 'iddaa_events', 'odds': 'iddaa_odds', 'predictions': 'prediction_snapshots'}
    return 'ai_research.source_' + name if postgres() else local.get(name, name)


@contextmanager
def session():
    url = os.environ.get('RESEARCH_DATABASE_URL')
    if not url and db.DATABASE_URL:
        raise RuntimeError('A separate RESEARCH_DATABASE_URL is required')
    if url:
        import psycopg
        from psycopg.rows import dict_row
        raw = psycopg.connect(url, row_factory=dict_row, prepare_threshold=None, connect_timeout=5)
        conn = db.Connection(raw, True)
    else:
        conn = db.connect()
    try:
        if url:
            conn.execute("SET LOCAL statement_timeout = '5s'")
            conn.execute("SET LOCAL lock_timeout = '3s'")
            role = conn.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls,"
                                "pg_has_role(current_user,'lol_research_runtime','MEMBER') AS member "
                                "FROM pg_roles WHERE rolname=current_user").fetchone()
            writable = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public' AND c.relkind IN ('r','p') "
                "AND has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')) AS allowed").fetchone()
            if (not role or not role['member'] or any(role[k] for k in ('rolsuper','rolcreatedb','rolcreaterole','rolbypassrls'))
                    or writable['allowed']):
                raise RuntimeError('Research connection is not a least-privilege runtime role')
        else:
            conn.execute('PRAGMA foreign_keys=ON')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def ui_session():
    """Separate read-only role/views for the administrator UI; never fall back to Bot credentials."""
    if not postgres():
        with session() as conn:
            yield conn
        return
    url = os.environ.get('RESEARCH_UI_DATABASE_URL')
    if not url:
        raise RuntimeError('Separate research UI database URL required')
    import psycopg
    from psycopg.rows import dict_row
    raw = psycopg.connect(url, row_factory=dict_row, prepare_threshold=None, connect_timeout=5)
    conn = db.Connection(raw, True)
    try:
        conn.execute('SET TRANSACTION READ ONLY')
        role = conn.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls,"
            "pg_has_role(current_user,'lol_research_ui_reader','MEMBER') AS reader,"
            "pg_has_role(current_user,'lol_research_runtime','MEMBER') AS runtime "
            "FROM pg_roles WHERE rolname=current_user").fetchone()
        writable = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname IN ('public','ai_research','research_evaluation') AND c.relkind IN ('r','p') "
            "AND has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES')) AS allowed").fetchone()
        if (not role or not role['reader'] or role['runtime'] or writable['allowed']
                or any(role[k] for k in ('rolsuper','rolcreatedb','rolcreaterole','rolbypassrls'))):
            raise RuntimeError('Research UI connection is not read-only')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_local():
    if postgres() or db.DATABASE_URL:
        raise RuntimeError('This command initializes isolated SQLite only; PostgreSQL migrations require review')
    # No existing schema changes and no automatic initialization on requests/startup.
    with session() as conn:
        conn.executescript(SQLITE_SCHEMA)


def unavailable_errors():
    errors = (sqlite3.Error, RuntimeError)
    try:
        import psycopg
        errors += (psycopg.Error,)
    except ImportError:
        pass
    return errors
