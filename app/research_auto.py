"""Opt-in scheduled queue bridge; the database remains authoritative for quotas and identity."""
import json
import os
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from . import research_context, research_planner, research_service, research_store
from .model import coupons
from .research_trio import select


def enqueue_candidates(owner, entries, *, at=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', owner):
        raise ValueError('Invalid research owner')
    if not research_store.postgres():
        raise RuntimeError('Guarded PostgreSQL queue required')
    at = at or datetime.now(timezone.utc)
    trio = select(entries, now=at)
    contexts = []
    with research_store.session() as conn:
        jobs = [dict(row) for row in conn.execute(
            f'SELECT match_id,mode,created_at FROM {research_store.table("jobs")}').fetchall()]
        for pick in trio['picks']:
            try:
                contexts.append(research_context.read(conn, str(pick['id']), at))
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
    planned = research_planner.plan(contexts, jobs, mode='full', now=at, history_complete=True)
    results = []
    for candidate in planned['candidates']:
        if candidate['decision'] != 'propose':
            continue
        try:
            job = research_service.enqueue(owner, candidate['match_id'], 'full', candidate['deadline'], 'auto-full-v1')
            results.append({'match_id': candidate['match_id'], 'status': job['status']})
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            results.append({'match_id': candidate['match_id'], 'status': 'skipped', 'reason': detail.get('code', 'conflict')})
    return {'selected': len(trio['picks']), 'results': results}


def main():
    if os.environ.get('RESEARCH_AUTO_ENABLED') != '1':
        print('Research auto queue disabled')
        return
    if not research_store.postgres():
        raise RuntimeError('RESEARCH_DATABASE_URL and guarded PostgreSQL queue required')
    owner = os.environ.get('RESEARCH_AUTO_OWNER', '')
    day = datetime.now(research_planner.TZ).date()
    output = enqueue_candidates(owner, coupons._day_entries(day))
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
