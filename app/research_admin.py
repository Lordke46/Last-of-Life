"""Manual, SQLite-only pilot provisioning. Never generate or print API secrets."""
import argparse
import json
from pathlib import Path

from . import config, db, research_service as service, research_store as store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, help='Explicit isolated SQLite file; not production')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('init-local')
    identity = sub.add_parser('register-key')
    identity.add_argument('--key-id', required=True)
    identity.add_argument('--principal-id', required=True)
    identity.add_argument('--token-sha256', required=True, help='SHA-256 of complete key_id.secret, never the plaintext key')
    identity.add_argument('--expires-at', required=True)
    identity.add_argument('--scopes', nargs='+', default=sorted(service.SCOPES))
    revoke = sub.add_parser('revoke-key')
    revoke.add_argument('--key-id', required=True)
    job = sub.add_parser('create-job')
    job.add_argument('--owner', required=True)
    job.add_argument('--match-id', required=True)
    job.add_argument('--mode', choices=('full', 'lineup_check'), required=True)
    job.add_argument('--deadline', required=True)
    job.add_argument('--request-id', default='initial', help='Use a deliberate new tag only when requesting a report revision')
    args = parser.parse_args(argv)
    if db.DATABASE_URL or store.postgres():
        parser.error('Pilot CLI refuses PostgreSQL; use an isolated SQLite environment')
    path = Path(args.db).expanduser().resolve()
    if path == (config.BASE_DIR / 'data' / 'iddia.db').resolve():
        parser.error('Use a separate pilot database, not the default application database')
    db.DB_PATH = path
    if args.action == 'init-local':
        db.init_db()
        store.initialize_local()
        output = {'initialized': str(path)}
    elif args.action == 'register-key':
        service.provision(args.key_id, args.principal_id, args.token_sha256, args.scopes, args.expires_at)
        output = {'key_id': args.key_id, 'registered': True, 'plaintext_key_generated': False}
    elif args.action == 'revoke-key':
        with store.session() as conn:
            conn.execute(f'UPDATE {store.table("service_keys")} SET revoked=1 WHERE key_id=?', (args.key_id,))
        output = {'key_id': args.key_id, 'revoked': True}
    else:
        output = service.enqueue(args.owner, args.match_id, args.mode, args.deadline, args.request_id)
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
