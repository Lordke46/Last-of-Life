"""Last actually displayed prematch choice, separate from live or final scores."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .db import session, get_meta, set_meta
from .quote_provenance import timestamp, fresh

PREFIX = 'displayed_picks:'
TZ = ZoneInfo('Europe/Istanbul')


def decorate(items):
    now = datetime.now(timezone.utc)
    days = {}
    changed = set()
    with session() as conn:
        for item in items:
            kickoff = timestamp(item.get('kickoff'))
            if not kickoff:
                continue
            day = kickoff.astimezone(TZ).date().isoformat()
            if day not in days:
                days[day] = json.loads(get_meta(conn, PREFIX+day) or '{}')
            records = days[day]
            key = 'iddaa:'+str(item['iddaa_event_id']) if item.get('iddaa_event_id') else str(item['id'])
            pick = item.get('pick')
            if (kickoff > now and not item.get('completed') and pick
                    and pick.get('odds_source') == 'iddaa' and fresh(item, pick['market'], now)):
                previous = records.get(key)
                if not previous or previous['pick'] != pick:
                    records[key] = {'pick':pick, 'captured_at':now.isoformat(),
                                    'kickoff':item['kickoff'], 'probability_source':pick.get('probability_source')}
                    changed.add(day)
                item['prematch_recommendation'] = records[key]
            elif kickoff <= now or item.get('completed'):
                saved = records.get(key) or records.get(str(item['id']))
                if not saved:
                    # Existing model snapshots can recover choices captured before this feature.
                    row = conn.execute('SELECT captured_at,kickoff,payload FROM prediction_snapshots WHERE match_id=?',
                                       (str(item['id']),)).fetchone()
                    if row:
                        payload = json.loads(row['payload'])
                        saved = {'pick':payload.get('pick'), 'captured_at':row['captured_at'], 'kickoff':row['kickoff']}
                if (saved and saved.get('pick') and timestamp(saved.get('captured_at'))
                        and timestamp(saved['captured_at']) < min(kickoff, timestamp(saved.get('kickoff')) or kickoff)):
                    item['prematch_recommendation'] = saved
                scores = json.loads(get_meta(conn, 'verified_scores:'+day) or '{}')
                if key in scores:
                    item['live_result'] = scores[key]
                    from . import explain
                    item['summary'] = explain.summarize(item)
        for day in changed:
            set_meta(conn, PREFIX+day, json.dumps(days[day], ensure_ascii=False))
        if changed:
            cutoff = (now.astimezone(TZ).date()-timedelta(days=30)).isoformat()
            conn.execute('DELETE FROM meta WHERE key LIKE ? AND key<?', (PREFIX+'%', PREFIX+cutoff))
    return items
