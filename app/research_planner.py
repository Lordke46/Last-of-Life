"""Pure, manual dry-run planning; no DB access, model calls, scheduler or enqueue.

Inputs must be ordered model candidates resolved to research contexts and a
complete owner-specific job history. This does not enforce concurrency or daily
limits in the actual queue: a transactional guard is required before automation.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .quote_provenance import timestamp

TZ = ZoneInfo('Europe/Istanbul')


def plan(contexts, existing_jobs, *, mode, now=None, history_complete=False):
    if mode not in ('full', 'lineup_check') or not history_complete:
        raise ValueError('Supported mode and explicitly complete job history required')
    at=now or datetime.now(timezone.utc)
    if not at.tzinfo: raise ValueError('Timezone-aware planning time required')
    if not isinstance(contexts,list) or not isinstance(existing_jobs,list):
        raise ValueError('Candidate and job history lists required')
    today=at.astimezone(TZ).date(); used=0; seen=set()
    for job in existing_jobs:
        if not isinstance(job,dict): raise ValueError('Invalid job history')
        created=timestamp(job.get('created_at'))
        if not created: raise ValueError('Job history has missing creation time')
        if job.get('mode')=='full' and created.astimezone(TZ).date()==today: used+=1
        if job.get('mode')==mode and isinstance(job.get('match_id'),str): seen.add(job['match_id'])
    remaining=max(0,3-used) if mode=='full' else None
    result=[]
    for context in contexts:
        if not isinstance(context,dict) or not isinstance(context.get('match'),dict):
            result.append({'decision':'skip','reason':'invalid_context'});continue
        match=context['match']; mid=match.get('id'); kickoff=timestamp(match.get('kickoff'))
        row={'match_id':mid,'mode':mode,'decision':'skip','warnings':[]}
        if not isinstance(mid,str) or not mid:
            row['reason']='missing_match_id'
        elif mid in seen:
            row['reason']='duplicate_match_mode'
        elif not kickoff or kickoff<=at or match.get('completed') or any(x in str(match.get('status') or '').lower() for x in ('postpon','cancel','abandon','suspend')):
            row['reason']='not_upcoming'
        elif mode=='lineup_check' and kickoff-at>timedelta(minutes=90):
            row['reason']='lineup_window_not_open'
        elif mode=='full' and remaining<=0:
            row['reason']='daily_full_limit'
        else:
            deadline=kickoff-timedelta(minutes=30 if mode=='full' else 5)
            if deadline<=at:
                row['reason']='deadline_passed'
            else:
                row.update(decision='propose',deadline=deadline.isoformat())
                seen.add(mid)
                if mode=='full': remaining-=1
                if context.get('prediction_snapshot') is None:
                    row['warnings'].append('missing_model_snapshot')
                prices=context.get('prices') if isinstance(context.get('prices'),dict) else {}
                observed=timestamp(prices.get('observed_at'))
                provenance=prices.get('quote_provenance') if isinstance(prices.get('quote_provenance'),dict) else {}
                quotes=provenance.get('quotes') if isinstance(provenance.get('quotes'),dict) else {}
                quote_times=[timestamp(q.get('quote_observed_at')) if isinstance(q,dict) else None for q in quotes.values()]
                if (not observed or observed>at or at-observed>timedelta(hours=2)
                        or provenance.get('pilot_freshness_status')=='stale' or not quote_times
                        or any(not t or t>at or at-t>timedelta(hours=2) for t in quote_times)):
                    row['warnings'].append('stale_or_unknown_prices')
                # A recent transport timestamp alone cannot establish a current quote.
                row['warnings'].append('verify_quote_provenance')
        result.append(row)
    return {'mode':mode,'day':today.isoformat(),'history_full_count':used,
            'remaining_full_slots':remaining,'candidates':result}
