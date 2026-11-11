"""SELECT-only context. Never call refresh, history decoration or the model."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from . import research_store as store
from .quote_provenance import timestamp


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _json(value, fallback=None):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def read(conn, match_id, now=None):
    now = now or datetime.now(timezone.utc)
    fixture, event = None, None
    if match_id.startswith('iddaa:'):
        event = conn.execute(f'SELECT * FROM {store.source("events")} WHERE id=?', (match_id[6:],)).fetchone()
        if event and event['match_id']:
            fixture = conn.execute(f'SELECT * FROM {store.source("matches")} WHERE id=?', (event['match_id'],)).fetchone()
    else:
        fixture = conn.execute(f'SELECT * FROM {store.source("matches")} WHERE id=?', (match_id,)).fetchone()
        if fixture:
            event = conn.execute(f'SELECT * FROM {store.source("events")} WHERE match_id=? ORDER BY id LIMIT 1', (match_id,)).fetchone()
    if not fixture and not event:
        raise HTTPException(404, 'Maç bulunamadı')
    fixture, event = dict(fixture) if fixture else {}, dict(event) if event else {}
    match = fixture or event
    identity = {'id': str(match_id), 'fixture_id': fixture.get('id'), 'iddaa_event_id': event.get('id'),
                **{k: match.get(k) for k in ('league', 'home', 'away', 'kickoff', 'status', 'completed')}}
    quotes = [dict(row) for row in conn.execute(
        f'SELECT market,odd FROM {store.source("odds")} WHERE event_id=? ORDER BY market', (event.get('id') or '',))]
    meta = {}
    keys = ['last_updated_at', 'iddaa_last_update', 'update_data_version', 'iddaa_quote_provenance']
    if event:
        keys.append('iddaa_quote_provenance:' + str(event['id']))
    for key in keys:
        row = conn.execute(f'SELECT value FROM {store.source("meta")} WHERE key=?', (key,)).fetchone()
        if row:
            meta[key] = row['value']
    snapshot = None
    if fixture:
        row = conn.execute(f'SELECT match_id,captured_at,model_version,data_version,payload FROM {store.source("predictions")} WHERE match_id=?',
                           (fixture['id'],)).fetchone()
        if row:
            payload = _json(row['payload'], {})
            # Explicit allowlist: no user data, prompts, credentials or arbitrary metadata.
            snapshot = {k: row[k] for k in ('match_id', 'captured_at', 'model_version', 'data_version')}
            snapshot['payload'] = {k: payload.get(k) for k in ('model', 'market', 'blend', 'pick', 'odds',
                'calibration_status', 'confidence_scope', 'data_integrity', 'quote_provenance')}
    news = [dict(row) for row in conn.execute(
        f'SELECT id,team,title,link,source,published,injury FROM {store.source("news")} '
        'WHERE league=? AND (team=? OR team=?) AND published>=? AND published<=? ORDER BY published DESC,id LIMIT 40',
        (match.get('league'), match.get('home'), match.get('away'), (now-timedelta(days=7)).isoformat(), now.isoformat()))]
    history = []
    for team in (match.get('home'), match.get('away')):
        history.extend(dict(row) for row in conn.execute(
            f'SELECT id,kickoff,home,away,home_goals,away_goals FROM {store.source("matches")} '
            'WHERE league=? AND completed=1 AND kickoff<? AND (home=? OR away=?) ORDER BY kickoff DESC,id LIMIT 5',
            (match.get('league'), now.isoformat(), team, team)))
    payload = {'schema_version': '1.0', 'match': identity,
               'prices': {'source': 'iddaa', 'quotes': quotes, 'observed_at': event.get('updated_at'),
                          'quote_provenance': _json(meta.pop('iddaa_quote_provenance:' + str(event.get('id')), None)),
                          'fetch_provenance': _json(meta.pop('iddaa_quote_provenance', None))},
               'prediction_snapshot': snapshot, 'news': news, 'recent_matches': history, 'data_versions': meta,
               'limitations': ['Tahmin kaydı yayımlandığı ana aittir; güncel fiyatla aynı zamanın tahmini değildir.',
                              'Eksik tahmin ve kadro bilgileri doldurulmaz; araştırma motora uygulanmaz.']}
    return payload


def eligible(context, now):
    match = context['match']
    kickoff = timestamp(match.get('kickoff'))
    inactive = any(word in str(match.get('status') or '').lower() for word in ('postpon', 'cancel', 'abandon', 'suspend'))
    return bool(kickoff and kickoff > now and not match.get('completed') and not inactive)
