"""Recorded prematch choices, separate from live or final scores."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .db import session, get_meta, set_meta, is_postgres as db_is_postgres
from .quote_provenance import timestamp, fresh

PREFIX = 'displayed_picks:'
TZ = ZoneInfo('Europe/Istanbul')


def decorate(items):
    now = datetime.now(timezone.utc)
    days = {}
    scores_by_day = {}
    changed = set()
    with session() as conn:
        snapshot_ids=list({str(item.get('model_match_id') or item['id']) for item in items
                           if (at:=timestamp(item.get('kickoff'))) and at<=now
                           and not str(item.get('model_match_id') or item['id']).startswith('iddaa:')})
        snapshots={}
        for offset in range(0,len(snapshot_ids),200):
            batch=snapshot_ids[offset:offset+200]
            marks=','.join('?' for _ in batch)
            snapshots.update({row['match_id']:dict(row) for row in conn.execute(
                f'SELECT match_id,captured_at,kickoff,payload FROM prediction_snapshots WHERE match_id IN ({marks})',batch)})
        for item in items:
            kickoff = timestamp(item.get('kickoff'))
            if not kickoff:
                continue
            day = kickoff.astimezone(TZ).date().isoformat()
            if day not in days:
                if db_is_postgres():
                    conn.execute('SELECT pg_advisory_xact_lock(hashtext(?))', (PREFIX+day,))
                days[day] = json.loads(get_meta(conn, PREFIX+day) or '{}')
                scores_by_day[day] = json.loads(get_meta(conn, 'verified_scores:'+day) or '{}')
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
                    row = snapshots.get(str(item.get('model_match_id') or item['id']))
                    if row:
                        payload = json.loads(row['payload'])
                        saved = {'pick':payload.get('pick'), 'captured_at':row['captured_at'], 'kickoff':row['kickoff']}
                if (saved and saved.get('pick') and timestamp(saved.get('captured_at'))
                        and timestamp(saved['captured_at']) < min(kickoff, timestamp(saved.get('kickoff')) or kickoff)):
                    item['prematch_recommendation'] = saved
                score=scores_by_day[day].get(key)
                score_kickoff=timestamp((score or {}).get('kickoff'))
                if score_kickoff and abs((score_kickoff-kickoff).total_seconds())<=15*60:
                    item['live_result'] = score
                    from . import explain
                    item['summary'] = explain.summarize(item)
        for day in changed:
            set_meta(conn, PREFIX+day, json.dumps(days[day], ensure_ascii=False))
        if changed:
            cutoff = (now.astimezone(TZ).date()-timedelta(days=30)).isoformat()
            conn.execute('DELETE FROM meta WHERE key LIKE ? AND key<?', (PREFIX+'%', PREFIX+cutoff))
    return items


def capture_bulletin():
    """Scheduled updater also preserves small-league choices before kickoff."""
    from .scrapers.iddaa import load_constraints
    from . import quote_provenance
    from .model.small_leagues import bulletin_event
    now=datetime.now(timezone.utc)
    with session() as conn:
        rows=conn.execute('SELECT e.*,o.market,o.odd FROM iddaa_events e JOIN iddaa_odds o ON o.event_id=e.id '
                          'WHERE (e.is_small=1 OR e.match_id IS NULL) AND e.kickoff>?',(now.isoformat(),)).fetchall()
        constraints=load_constraints(conn)
        provenance=quote_provenance.load(conn,[r['id'] for r in rows])
    grouped={}
    for row in rows:
        event=grouped.setdefault(row['id'],{**dict(row),'id':'iddaa:'+row['id'],'iddaa_event_id':row['id'],
            'iddaa_odds':{},'odds_current':True,'iddaa_updated_at':row['updated_at'],'small_league':True,
            'quote_provenance':quote_provenance.for_event(provenance,row['id']),
            'iddaa_min_matches':constraints.get(row['id'],{})})
        event['iddaa_odds'][row['market']]=row['odd']
    return decorate([bulletin_event(item) for item in grouped.values()])
