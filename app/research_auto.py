"""Opt-in scheduled queue bridge; the database remains authoritative for quotas and identity."""
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from fastapi import HTTPException
from . import research_context, research_planner, research_service, research_store
from .model import coupons
from .research_trio import select
from .quote_provenance import timestamp

IDENTITY_PROVIDER = 'last-of-life'
IDENTITY_REASON = 'auto: exact source id, no name matching (RESEARCH_AUTO_IDENTITY=1)'


def identity_enabled():
    return os.environ.get('RESEARCH_AUTO_IDENTITY') == '1'


def register_identities(conn, contexts):
    """Register the exact source fixture as its own reviewed identity (no fuzzy merge).

    The alias is the id the planner enqueues; competition/teams/kickoff are copied
    from the same frozen context the guard trigger later compares against. The
    audit trigger records the connecting DB login as reviewer. Existing rows are
    kept, so a rescheduled kickoff still fails the guard and is reported as skipped.
    """
    if not identity_enabled():
        return 0
    if not research_store.postgres():
        raise RuntimeError('Identity registration needs the guarded PostgreSQL queue')
    identities, aliases = 'ai_research.match_identities', 'ai_research.match_aliases'
    registered = 0
    for payload in contexts:
        match = payload['match']
        mid = str(match['id'])
        fields = (match.get('league'), match.get('home'), match.get('away'), match.get('kickoff'))
        if not all(isinstance(value, str) and value.strip() for value in fields):
            continue
        league, home, away, kickoff = fields
        conn.execute(f'INSERT INTO {identities}(canonical_id,competition,home,away,kickoff,reviewed_by,review_reason) '
                     'VALUES(?,?,?,?,CAST(? AS timestamptz),?,?) ON CONFLICT(canonical_id) DO NOTHING',
                     (mid, league, home, away, kickoff, 'auto', IDENTITY_REASON))
        conn.execute(f'INSERT INTO {aliases}(alias,canonical_id,provider,source_competition,source_home,source_away,'
                     'source_kickoff,reviewed_by,review_reason) VALUES(?,?,?,?,?,?,CAST(? AS timestamptz),?,?) '
                     'ON CONFLICT(alias) DO NOTHING',
                     (mid, mid, IDENTITY_PROVIDER, league, home, away, kickoff, 'auto', IDENTITY_REASON))
        registered += 1
    return registered


def notify_grok_bot(output, *, opener=urlopen):
    """Start the Grok Bot routine over its webhook only when new jobs were queued."""
    url, key = os.environ.get('GROK_BOT_WEBHOOK_URL', ''), os.environ.get('GROK_BOT_WEBHOOK_KEY', '')
    queued = sum(1 for row in output.get('results', []) if row.get('status') == 'queued')
    if not url or not key or not queued:
        return {'notified': False, 'queued': queued}
    if urlsplit(url).scheme != 'https':
        raise ValueError('Grok Bot webhook must use HTTPS')
    body = {'event': 'research_jobs_queued', 'queued': queued,
            'research_api': os.environ.get('RESEARCH_PUBLIC_BASE_URL', ''),
            'sent_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    request = Request(url, data=json.dumps(body).encode(), method='POST',
                      headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with opener(request, timeout=20) as response:
            status = getattr(response, 'status', 200)
    except OSError:
        # Never include the URL or exception text: both may carry the routine key.
        return {'notified': False, 'queued': queued, 'error': 'webhook_unreachable'}
    return {'notified': 200 <= status < 300, 'queued': queued, 'status': status}


def check_configuration(env=os.environ):
    """Shape check only; connects nowhere and never prints a value."""
    problems = []
    for name in ('DATABASE_URL', 'RESEARCH_DATABASE_URL', 'RESEARCH_AUTO_OWNER'):
        if not env.get(name):
            problems.append('missing ' + name)
    for name in ('DATABASE_URL', 'RESEARCH_DATABASE_URL'):
        url = urlsplit(env.get(name, ''))
        if env.get(name) and (url.scheme not in ('postgresql', 'postgresql+psycopg', 'postgres') or not url.hostname):
            problems.append(name + ' must be a PostgreSQL URL with a host')
    if env.get('DATABASE_URL') and env.get('DATABASE_URL') == env.get('RESEARCH_DATABASE_URL'):
        problems.append('source and research connections must be distinct')
    if env.get('GROK_BOT_WEBHOOK_URL') and urlsplit(env['GROK_BOT_WEBHOOK_URL']).scheme != 'https':
        problems.append('GROK_BOT_WEBHOOK_URL must use HTTPS')
    if bool(env.get('GROK_BOT_WEBHOOK_URL')) != bool(env.get('GROK_BOT_WEBHOOK_KEY')):
        problems.append('GROK_BOT_WEBHOOK_URL and GROK_BOT_WEBHOOK_KEY must be set together')
    return problems


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
    proposed = [c for c in planned['candidates'] if c['decision'] == 'propose']
    identities = 0
    if proposed and identity_enabled():
        chosen = {c['match_id'] for c in proposed}
        with research_store.session() as conn:
            identities = register_identities(conn, [p for p in contexts if str(p['match']['id']) in chosen])
    results = []
    for candidate in proposed:
        try:
            job = research_service.enqueue(owner, candidate['match_id'], 'full', candidate['deadline'], 'auto-full-v1')
            results.append({'match_id': candidate['match_id'], 'status': job['status']})
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            results.append({'match_id': candidate['match_id'], 'status': 'skipped', 'reason': detail.get('code', 'conflict')})
    return {'selected': len(picks), 'selection_mode': selection_mode, 'resolved': len(contexts),
            'identities_registered': identities, 'results': results}


def main(argv=()):
    argv = list(argv)
    if argv == ['--check']:
        problems = check_configuration()
        print('\n'.join(problems) if problems else 'Research queue configuration shape verified; no connection made.')
        return 1 if problems else 0
    if argv:
        raise SystemExit('Usage: python -m app.research_auto [--check]')
    if os.environ.get('RESEARCH_AUTO_ENABLED') != '1':
        print('Research auto queue disabled')
        return 0
    if not research_store.postgres():
        raise RuntimeError('RESEARCH_DATABASE_URL and guarded PostgreSQL queue required')
    owner = os.environ.get('RESEARCH_AUTO_OWNER', '')
    day = datetime.now(research_planner.TZ).date()
    output = enqueue_candidates(owner, coupons._day_entries(day))
    output['grok_bot'] = notify_grok_bot(output)
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
