"""Manual operator actions for an explicitly isolated local PostgreSQL pilot only.

This tool never generates credentials. STAGING_ADMIN_DSN is consumed from the
environment, never printed. It intentionally refuses all nonlocal targets.
"""
import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg
from psycopg.rows import dict_row
from fastapi import HTTPException
from app import db, research_service as service, research_store as store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    key = actions.add_parser('register-key')
    key.add_argument('--key-id', required=True)
    key.add_argument('--principal-id', required=True)
    key.add_argument('--token-sha256', required=True, help='Digest only; never pass the plaintext service token')
    key.add_argument('--expires-at', required=True)
    key.add_argument('--scopes', nargs='+', default=sorted(service.SCOPES))
    revoke = actions.add_parser('revoke-key')
    revoke.add_argument('--key-id', required=True)
    job = actions.add_parser('create-job')
    job.add_argument('--owner', required=True)
    job.add_argument('--match-id', required=True)
    job.add_argument('--mode', choices=('full', 'lineup_check'), required=True)
    job.add_argument('--deadline', required=True)
    job.add_argument('--request-id', default='initial')
    args = parser.parse_args(argv)
    dsn = os.environ.get('STAGING_ADMIN_DSN', '')
    try:
        parsed = urlparse(dsn)
        valid = (parsed.scheme in ('postgres', 'postgresql') and parsed.hostname in ('localhost', '127.0.0.1')
                 and parsed.port == 55439 and parsed.path == '/lol_staging' and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid or os.environ.get('DATABASE_URL') or os.environ.get('RESEARCH_DATABASE_URL'):
        parser.error('Requires isolated local lol_staging on port 55439; other database settings must be empty')

    @contextmanager
    def operator_session():
        with psycopg.connect(dsn, row_factory=dict_row, prepare_threshold=None, connect_timeout=5) as raw:
            conn = db.Connection(raw, True)
            server = conn.execute("SELECT current_database() AS db, host(inet_server_addr()) AS address, current_setting('port') AS port").fetchone()
            if server['db'] != 'lol_staging' or server['address'] not in ('127.0.0.1', '::1') or server['port'] != '55439':
                raise RuntimeError('Local staging server identity check failed')
            yield conn

    # Select PostgreSQL table names without falling back to the main application DSN.
    with patch.dict(os.environ, {'RESEARCH_DATABASE_URL': 'isolated-local-staging'}), patch.object(store, 'session', operator_session):
        if args.action == 'register-key':
            service.provision(args.key_id, args.principal_id, args.token_sha256, args.scopes, args.expires_at)
            result = {'registered': True, 'key_id': args.key_id, 'plaintext_key_generated': False}
        elif args.action == 'revoke-key':
            with operator_session() as conn:
                conn.execute('UPDATE ai_research.service_keys SET revoked=1 WHERE key_id=?', (args.key_id,))
            result = {'revoked': True, 'key_id': args.key_id}
        else:
            result = service.enqueue(args.owner, args.match_id, args.mode, args.deadline, args.request_id)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (psycopg.Error, RuntimeError, ValueError, HTTPException):
        # Driver errors can contain connection details: do not log their message.
        raise SystemExit('Staging işlemi tamamlanamadı; hedefi ve izinleri kontrol edin.') from None
