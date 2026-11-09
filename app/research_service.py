"""Scoped identities, leases and append-only reports. No prediction writes."""
import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from . import research_context as context, research_store as store
from .quote_provenance import timestamp

SCOPES = frozenset(('jobs:read', 'jobs:claim', 'context:read', 'reports:read', 'reports:write'))
LEASE_MINUTES = 20


def now():
    return datetime.now(timezone.utc)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def provision(key_id, principal_id, token_hash, scopes, expires_at):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key_id) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', principal_id):
        raise ValueError('Invalid identity')
    if not re.fullmatch(r'[a-f0-9]{64}', token_hash) or not set(scopes) <= SCOPES or not scopes:
        raise ValueError('Invalid hash or scopes')
    expiry = timestamp(expires_at)
    if not expiry or expiry <= now():
        raise ValueError('A future, timezone-aware expiry is required')
    with store.session() as conn:
        conn.execute(f'INSERT INTO {store.table("service_keys")}(key_id,principal_id,token_hash,scopes,expires_at,created_at) VALUES(?,?,?,?,?,?)',
                     (key_id, principal_id, token_hash, json.dumps(sorted(set(scopes))), expiry.astimezone(timezone.utc).isoformat(), now().isoformat()))


def _rate(conn, bucket, limit, at):
    window = int(at.timestamp()) // 60
    table = store.table('rate_limits')
    relation = table.rsplit('.', 1)[-1]
    conn.execute(f'DELETE FROM {table} WHERE window_id<?', (window-65,))
    row = conn.execute(f'INSERT INTO {table}(bucket,window_id,hits) VALUES(?,?,1) '
                       f'ON CONFLICT(bucket,window_id) DO UPDATE SET hits={relation}.hits+1 RETURNING hits', (bucket, window)).fetchone()
    return row['hits'] > limit


def authenticate(authorization, client_ip):
    at = now()
    principal, error = None, None
    with store.session() as conn:
        if _rate(conn, 'ip:' + sha(client_ip or 'unknown'), 180, at):
            error = HTTPException(429, 'Araştırma istek sınırı aşıldı', headers={'Retry-After': '60'})
        else:
            raw = authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
            key_id, _, secret = raw.partition('.')
            row = conn.execute(f'SELECT * FROM {store.table("service_keys")} WHERE key_id=?', (key_id[:64],)).fetchone()
            expected = row['token_hash'] if row else '0'*64
            matched = hmac.compare_digest(sha(raw), expected)
            if (not row or not matched or not 32 <= len(secret) <= 256 or row['revoked']
                    or not timestamp(row['expires_at']) or timestamp(row['expires_at']) <= at):
                error = HTTPException(401, 'Geçerli araştırma servis anahtarı gerekli', headers={'WWW-Authenticate': 'Bearer'})
            elif _rate(conn, 'principal:' + row['principal_id'], 60, at):
                error = HTTPException(429, 'Araştırma istek sınırı aşıldı', headers={'Retry-After': '60'})
            else:
                principal = {'id': row['principal_id'], 'scopes': json.loads(row['scopes'])}
    if error:
        raise error  # Commit rate counters before raising.
    return principal


def scope(principal, name):
    if name not in principal['scopes']:
        raise HTTPException(403, 'Araştırma işlem yetkisi yok')


def _job(conn, job_id, owner, lock=False):
    suffix = ' FOR UPDATE' if lock and store.postgres() else ''
    row = conn.execute(f'SELECT * FROM {store.table("jobs")} WHERE id=? AND owner_id=?' + suffix, (job_id, owner)).fetchone()
    if not row:
        raise HTTPException(404, 'Araştırma işi bulunamadı')
    return dict(row)


def _transaction_lock(conn):
    if not store.postgres():
        conn.execute('BEGIN IMMEDIATE')


def job_view(job, at):
    expired = timestamp(job['deadline']) <= at
    return {k: job[k] for k in ('id', 'match_id', 'mode', 'deadline', 'input_version', 'created_at', 'report_id')} | {
        'status': 'expired' if expired else job['status'], 'lease_expires_at': job['lease_expires_at']}


def enqueue(owner, match_id, mode, deadline, request_id='initial'):
    if mode not in ('full', 'lineup_check'):
        raise ValueError('Unsupported research mode')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', request_id):
        raise ValueError('Invalid request ID')
    at = now()
    end = timestamp(deadline)
    with store.session() as conn:
        _transaction_lock(conn)
        if not conn.execute(f'SELECT key_id FROM {store.table("service_keys")} WHERE principal_id=? AND revoked=0 AND expires_at>?',
                            (owner, at.isoformat())).fetchone():
            raise ValueError('Owner needs an active service identity')
        payload = context.read(conn, match_id, at)
        if not context.eligible(payload, at):
            raise ValueError('Only upcoming active fixtures can be researched')
        if not end or not at < end <= timestamp(payload['match']['kickoff']):
            raise ValueError('Deadline must be in the future and no later than kickoff')
        end = end.astimezone(timezone.utc)
        version = context.digest(payload)
        job_id = str(uuid.uuid4())
        conn.execute(f'INSERT INTO {store.table("jobs")}(id,owner_id,match_id,mode,status,deadline,input_version,context_json,request_id,created_at) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,match_id,mode,input_version,request_id) DO NOTHING',
                     (job_id, owner, match_id, mode, 'queued', end.isoformat(), version, context.canonical(payload), request_id, at.isoformat()))
        row = conn.execute(f'SELECT * FROM {store.table("jobs")} WHERE owner_id=? AND match_id=? AND mode=? AND input_version=? AND request_id=?',
                           (owner, match_id, mode, version, request_id)).fetchone()
        return job_view(dict(row), at)


def list_jobs(principal, limit=25):
    scope(principal, 'jobs:read')
    with store.session() as conn:
        rows = conn.execute(f'SELECT * FROM {store.table("jobs")} WHERE owner_id=? ORDER BY created_at DESC,id LIMIT ?',
                            (principal['id'], limit)).fetchall()
    return {'jobs': [job_view(dict(row), now()) for row in rows]}


def match_context(principal, match_id, job_id):
    scope(principal, 'context:read')
    with store.session() as conn:
        job = _job(conn, job_id, principal['id'])
    if match_id != job['match_id']:
        raise HTTPException(409, 'Maç kimliği araştırma işiyle eşleşmiyor')
    return {'job': job_view(job, now()), 'context': json.loads(job['context_json']),
            'context_captured_at': job['created_at'], 'input_version': job['input_version']}


def claim(principal, job_id):
    scope(principal, 'jobs:claim')
    at = now()
    with store.session() as conn:
        _transaction_lock(conn)
        job = _job(conn, job_id, principal['id'], lock=True)
        if timestamp(job['deadline']) <= at:
            raise HTTPException(410, 'Araştırma işinin süresi doldu')
        if job['status'] in ('completed', 'expired'):
            raise HTTPException(409, 'Araştırma işi kapalı')
        if job['lease_expires_at'] and timestamp(job['lease_expires_at']) > at:
            raise HTTPException(409, 'Araştırma işi zaten üstlenildi')
        token = secrets.token_urlsafe(32)  # Short-lived job lease, never a service/API key.
        until = at + timedelta(minutes=LEASE_MINUTES)
        conn.execute(f'UPDATE {store.table("jobs")} SET status=?,lease_hash=?,lease_expires_at=? WHERE id=?',
                     ('claimed', sha(token), until.isoformat(), job_id))
        job.update(status='claimed', lease_expires_at=until.isoformat())
    return {'job': job_view(job, at), 'claim_token': token}


def _freshness(conn, job, at):
    # Re-check live SELECT-only context for reschedules, cancellations or changed inputs.
    try:
        current = context.read(conn, job['match_id'], at)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return 'expired'
    if (timestamp(job['deadline']) <= at or not context.eligible(current, at)
            or context.digest(current) != job['input_version']):
        return 'expired'
    return 'current'


def save_result(principal, job_id, result, lease, idempotency_key):
    scope(principal, 'reports:write')
    at = now()
    payload = result.model_dump(mode='json')
    payload_hash = context.digest(payload)
    with store.session() as conn:
        _transaction_lock(conn)
        job = _job(conn, job_id, principal['id'], lock=True)
        if (result.job_id != job_id or result.match_id != job['match_id'] or result.mode != job['mode']
                or result.input_version != job['input_version']):
            raise HTTPException(409, 'Sonuç iş, maç, mod veya girdi sürümüyle eşleşmiyor')
        existing = conn.execute(f'SELECT * FROM {store.table("report_revisions")} WHERE job_id=?', (job_id,)).fetchone()
        if existing:
            if existing['idempotency_key'] != idempotency_key or existing['payload_hash'] != payload_hash:
                raise HTTPException(409, 'Bu iş için farklı bir sonuç zaten kaydedildi')
            return {'report_id': existing['report_id'], 'revision': existing['revision'], 'duplicate': True,
                    'freshness_status': existing['freshness_status'], 'validation_status': 'schema_valid'}
        if (job['status'] != 'claimed' or not lease or not job['lease_hash']
                or not hmac.compare_digest(sha(lease), job['lease_hash'])
                or timestamp(job['lease_expires_at']) <= at):
            raise HTTPException(409, 'Geçerli iş üstlenme belirteci gerekli')
        if result.researched_at < timestamp(job['created_at']) - timedelta(minutes=5):
            raise HTTPException(422, 'Araştırma zamanı işin oluşturulmasından eski')
        # Immutable payload includes findings, source dates, assessments and risks.
        report_id = str(uuid.uuid4())
        conn.execute(f'INSERT INTO {store.table("reports")}(id,owner_id,match_id,mode,created_at) VALUES(?,?,?,?,?) '
                     'ON CONFLICT(owner_id,match_id,mode) DO NOTHING',
                     (report_id, principal['id'], job['match_id'], job['mode'], at.isoformat()))
        suffix = ' FOR UPDATE' if store.postgres() else ''
        report = conn.execute(f'SELECT * FROM {store.table("reports")} WHERE owner_id=? AND match_id=? AND mode=?' + suffix,
                              (principal['id'], job['match_id'], job['mode'])).fetchone()
        report_id = report['id']
        revision = conn.execute(f'SELECT COALESCE(MAX(revision),0)+1 AS n FROM {store.table("report_revisions")} WHERE report_id=?',
                                (report_id,)).fetchone()['n']
        freshness = _freshness(conn, job, at)
        conn.execute(f'INSERT INTO {store.table("report_revisions")}(id,report_id,revision,job_id,idempotency_key,payload_hash,payload_json,'
                     'received_at,researched_at,skill_version,freshness_status,validation_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     (str(uuid.uuid4()), report_id, revision, job_id, idempotency_key, payload_hash, context.canonical(payload),
                      at.isoformat(), result.researched_at.isoformat(), result.skill_version, freshness, 'schema_valid'))
        conn.execute(f'UPDATE {store.table("jobs")} SET status=?,report_id=? WHERE id=?',
                     ('completed' if freshness == 'current' else 'expired', report_id, job_id))
    return {'report_id': report_id, 'revision': revision, 'duplicate': False,
            'freshness_status': freshness, 'validation_status': 'schema_valid'}


def read_report(principal, report_id, revision=None):
    scope(principal, 'reports:read')
    with store.session() as conn:
        report = conn.execute(f'SELECT * FROM {store.table("reports")} WHERE id=? AND owner_id=?',
                              (report_id, principal['id'])).fetchone()
        if not report:
            raise HTTPException(404, 'Araştırma raporu bulunamadı')
        args = [report_id]
        clause = ''
        if revision is not None:
            clause = ' AND revision=?'
            args.append(revision)
        row = conn.execute(f'SELECT * FROM {store.table("report_revisions")} WHERE report_id=?' + clause + ' ORDER BY revision DESC LIMIT 1', args).fetchone()
        if not row:
            raise HTTPException(404, 'Araştırma sürümü bulunamadı')
        job = _job(conn, row['job_id'], principal['id'])
        freshness = 'expired' if row['freshness_status'] == 'expired' else _freshness(conn, job, now())
    return {**dict(report), 'revision': row['revision'], 'job_id': row['job_id'],
            'received_at': row['received_at'], 'freshness_status': freshness,
            'freshness_at_ingest': row['freshness_status'], 'validation_status': row['validation_status'],
            'verification_note': 'JSON şeması doğrulandı; haber doğruluğu veya model başarısı doğrulanmadı.',
            'applied_to_model': False, 'report': json.loads(row['payload_json'])}
