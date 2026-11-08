"""Quote observations, transport cache evidence and fetch attempts; no invented provider time."""
import json
from datetime import datetime, timedelta, timezone
from .db import get_meta, set_meta

KEY = 'iddaa_quote_provenance'
MAX_AGE = timedelta(hours=2)


def timestamp(value):
    try:
        at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return at if at.tzinfo else None
    except (TypeError, ValueError):
        return None


def load(conn, event_ids=None):
    try:
        value = json.loads(get_meta(conn, KEY) or '{}')
        if not isinstance(value,dict):return {}
        if event_ids is None:
            rows = conn.execute('SELECT key,value FROM meta WHERE key LIKE ?', (KEY+':%',)).fetchall()
        else:
            keys = [KEY+':'+str(eid) for eid in set(event_ids)]
            rows = conn.execute('SELECT key,value FROM meta WHERE key IN ('+','.join('?' for _ in keys)+')',keys).fetchall() if keys else []
        value['events'] = {r['key'][len(KEY)+1:]:json.loads(r['value']) for r in rows}
        return value
    except (ValueError, TypeError):
        return {}


def for_event(state, event_id):
    event = state.get('events', {}).get(str(event_id), {})
    failed = state.get('last_fetch_status') == 'failed'
    return {**event, 'last_fetch_at': state.get('last_fetch_at'),
            'last_successful_fetch_at': state.get('last_successful_fetch_at'),
            'last_fetch_status': state.get('last_fetch_status', 'unknown'),
            'provider_price_updated_at':None,
            'quote_status': 'fetch_failed' if failed else event.get('quote_status', 'unverified')}


def failed(conn, error, at=None):
    state = load(conn,[])
    state.pop('events',None)
    state.update(last_fetch_at=at or datetime.now(timezone.utc).isoformat(),
                 last_fetch_status='failed', last_error=str(error)[:180])
    set_meta(conn, KEY, json.dumps(state))


def save(conn, events, at, previous):
    state = {'last_fetch_at': at, 'last_successful_fetch_at': at, 'last_fetch_status':'success', 'events':{}}
    for event in events:
        quotes = {}
        old_quotes = previous.get('events', {}).get(event['id'], {}).get('quotes', {})
        for market, value in event.get('quote_metadata', {}).items():
            old = old_quotes.get(market, {})
            same = old.get('identity') == value['identity']
            quotes[market] = {**value,
                'identity_first_observed_at': old.get('identity_first_observed_at') if same else value.get('quote_observed_at'),
                'last_observed_change_at': old.get('last_observed_change_at') if same and old.get('odd') == value['odd'] else value.get('quote_observed_at')}
        verified = bool(quotes) and all(v.get('cache_status') == 'network_no_cache_evidence' for v in quotes.values())
        state['events'][event['id']] = {'quotes':quotes, 'quote_source':'iddaa',
            'quote_status':'observed' if verified else 'cache_or_transport_unverified',
            'provider_price_updated_at':None}
    conn.executemany('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     [(KEY+':'+eid,json.dumps(event)) for eid,event in state['events'].items()])
    set_meta(conn, KEY, json.dumps({k:v for k,v in state.items() if k!='events'}))
    keys=[KEY+':'+eid for eid in state['events']]
    if keys:
        conn.execute('DELETE FROM meta WHERE key LIKE ? AND key NOT IN ('+','.join('?' for _ in keys)+')',[KEY+':%',*keys])
    return state


def fresh(prediction, market=None, now=None):
    now = now or datetime.now(timezone.utc)
    meta = prediction.get('quote_provenance')
    # Legacy snapshots keep their timestamp meaning; new observations require provenance.
    if meta is None:
        at = timestamp(prediction.get('iddaa_updated_at'))
        return bool(at and now-MAX_AGE <= at <= now+timedelta(minutes=5)
                    and prediction.get('odds_current') is not False)
    if meta.get('last_fetch_status') != 'success' or prediction.get('odds_current') is False:
        return False
    quotes = meta.get('quotes', {})
    keys = [market] if market is not None else list(prediction.get('iddaa_odds', {}))
    if not keys:
        return False
    kickoff = timestamp(prediction.get('kickoff'))
    for key in keys:
        quote = quotes.get(key, {})
        at = timestamp(quote.get('quote_observed_at'))
        if (quote.get('cache_status') != 'network_no_cache_evidence' or not at
                or not now-MAX_AGE <= at <= now or (kickoff and at>=kickoff)
                or quote.get('odd') != prediction.get('iddaa_odds', {}).get(key)):
            return False
    return True
