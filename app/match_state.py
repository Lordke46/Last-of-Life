"""Shared fixture lifecycle and quote availability, without inferring completion from time."""
from datetime import datetime, timezone
from .quote_provenance import timestamp, fresh


def describe(item, now=None):
    now=now or datetime.now(timezone.utc)
    start=timestamp(item.get('kickoff'))
    status=str(item.get('status') or '').lower()
    if any(word in status for word in ('cancel','postpon','abandon','suspend')):
        phase='inactive'
    elif item.get('completed'):
        phase='completed'
    elif start and start<=now:
        phase='started_result_pending'
    else:
        phase='prematch' if start else 'unknown'
    odds=item.get('iddaa_odds') or {}
    if not odds:price='missing'
    elif phase!='prematch':price='historical'
    elif item.get('bulletin_present') is False or item.get('is_archived'):
        price='removed_from_bulletin'
    elif not fresh(item,now=now):price='unverified_or_stale'
    elif (item.get('quote_provenance') or {}).get('last_fetch_status')=='failed':price='recent_observation'
    else:price='current'
    return {'phase':phase,'price_status':price,'can_offer_prematch':phase=='prematch' and price in ('current','recent_observation')}
