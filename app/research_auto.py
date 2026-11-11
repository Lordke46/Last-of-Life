"""Opt-in scheduled queue bridge; the database remains authoritative for quotas and identity."""
import json
import math
import os
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from . import research_context, research_planner, research_service, research_store
from .model import coupons
from .research_trio import select
from .quote_provenance import timestamp


def _research_picks(entries, at):
    """Prefer a priced trio; otherwise research the strongest distinct eligible matches.

    A fallback is a research shortlist, NOT a published coupon or a probability
    adjustment. The database planner and guard still decide whether to enqueue.
    """
    trio = select(entries, now=at)
    if trio['picks']:
        return trio['picks'], 'priced_trio'

    ranked = []
    seen = set()
    for prediction, choices in entries:
        mid = str(prediction.get('id') or '')
        kickoff = timestamp(prediction.get('kickoff'))
        if (not mid or mid in seen or not kickoff or kickoff <= at
                or prediction.get('completed') or prediction.get('result_only')):
            continue
        seen.add(mid)
        eligible = []
        for choice in choices:
            p, odds = choice.get('p'), choice.get('odds')
            try:
                minimum = int(choice.get('min_matches', 1) or 1)
            except (ValueError, TypeError):
                continue
            if (choice.get('odds_source') != 'iddaa' or isinstance(p, bool)
                    or isinstance(odds, bool) or not isinstance(p, (int, float))
                    or not isinstance(odds, (int, float)) or not math.isfinite(p)
                    or not math.isfinite(odds) or not 0 < p < 1
                    or not 1.15 <= odds <= 2.30 or minimum > 3):
                continue
            eligible.append(choice)
        if eligible:
            best = max(eligible, key=lambda c: (c['p'], -c['odds']))
            ranked.append((best['p'], kickoff, mid))
    ranked.sort(key=lambda row: (-row[0], row[1], row[2]))
    return [{'id': mid} for _, _, mid in ranked[:3]], 'research_fallback'


def enqueue_candidates(owner, entries, *, at=None):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', owner):
        raise ValueError('Invalid research owner')
    if not research_store.postgres():
        raise RuntimeError('Guarded PostgreSQL queue required')
    at = at or datetime.now(timezone.utc)
    picks, selection_mode = _research_picks(entries, at)
    contexts = []
    with research_store.session() as conn:
        jobs = [dict(row) for row in conn.execute(
            f'SELECT match_id,mode,created_at FROM {research_store.table("jobs")}').fetchall()]
        for pick in picks:
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
    return {'selected': len(picks), 'selection_mode': selection_mode, 'resolved': len(contexts), 'results': results}


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
