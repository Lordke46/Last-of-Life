"""Kupondaki eski maçları da okuyabilen, tahmin üretmeyen sonuç uç noktası."""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Annotated
from fastapi import APIRouter, HTTPException, Query
from .db import session, get_meta, set_meta
from .quote_provenance import timestamp

router = APIRouter()


@router.get("/api/results")
def results(ids: Annotated[list[str], Query(min_length=1, max_length=30)]):
    if any(not mid or len(mid) > 256 for mid in ids):
        raise HTTPException(400, "Geçersiz maç kimliği")
    ids = list(dict.fromkeys(ids))
    marks = ",".join("?" for _ in ids)
    bulletin_kickoffs={}
    refresh_day=None
    with session() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT id,kickoff,completed,home_goals,away_goals,status,updated_at FROM matches WHERE id IN ({marks})", ids)}
        aliases = [mid.removeprefix('iddaa:') for mid in ids if mid.startswith('iddaa:')]
        if aliases:
            placeholders = ','.join('?' for _ in aliases)
            for row in conn.execute('SELECT id,kickoff FROM iddaa_events WHERE id IN ('+placeholders+')',aliases):
                bulletin_kickoffs['iddaa:'+row['id']]=row['kickoff']
            for row in conn.execute(
                'SELECT e.id AS event_id,m.kickoff,m.completed,m.home_goals,m.away_goals,m.status,m.updated_at '
                'FROM iddaa_events e JOIN matches m ON m.id=e.match_id WHERE e.id IN ('+placeholders+')', aliases):
                mid='iddaa:'+row['event_id']
                rows[mid]={**dict(row),'id':mid}
        if aliases:
            from . import bulletin_archive
            for event_id in aliases:
                mid='iddaa:'+event_id
                if mid in rows:continue
                archived=bulletin_archive.find_event(conn,event_id)
                if archived:
                    bulletin_kickoffs.setdefault(mid,archived['kickoff'])
                else:
                    active=conn.execute('SELECT kickoff FROM iddaa_events WHERE id=?',(event_id,)).fetchone()
                    if active:bulletin_kickoffs[mid]=active['kickoff']
                if not archived or not archived.get('match_id'):continue
                row=conn.execute('SELECT kickoff,completed,home_goals,away_goals,status,updated_at FROM matches WHERE id=?',
                                 (archived['match_id'],)).fetchone()
                if row:rows[mid]={**dict(row),'id':mid}
        # Small leagues have no matches-table row. Keep verified provider finals available
        # to personal coupons after the fixture disappears from the current scoreboard.
        import json
        for record in conn.execute('SELECT value FROM meta WHERE key LIKE ?', ('verified_scores:%',)):
            scores=json.loads(record['value'])
            for mid in ids:
                if not mid.startswith('iddaa:') and mid in rows:
                    continue
                key='iddaa:'+mid.removeprefix('iddaa:')
                score=scores.get(key)
                expected=timestamp(bulletin_kickoffs.get(mid) or bulletin_kickoffs.get(key))
                observed=timestamp((score or {}).get('kickoff'))
                if expected and (not observed or abs((observed-expected).total_seconds())>15*60
                                 or expected>datetime.now(timezone.utc)):
                    continue
                if score and score.get('state')=='post' and not (rows.get(mid) or {}).get('completed'):
                    rows[mid]={**score,'id':mid,'completed':1,'status':'FT','updated_at':score.get('updated_at')}
        now=datetime.now(timezone.utc)
        days=sorted({timestamp(kickoff).astimezone(ZoneInfo('Europe/Istanbul')).date()
                     for mid,kickoff in bulletin_kickoffs.items() if not (rows.get(mid) or {}).get('completed')
                     and timestamp(kickoff) and now-timedelta(days=30)<=timestamp(kickoff)<=now},reverse=True)
        for day in days:
            key='result_refresh_attempt:'+str(day)
            attempted=timestamp(get_meta(conn,key))
            if not attempted or now-attempted>timedelta(minutes=2):
                set_meta(conn,key,now.isoformat())
                refresh_day=day
                break  # One bounded provider refresh per request, no 30-date fanout.
    if refresh_day:
        from .live import get_live_snapshot
        scores={str(m['id']):m for m in get_live_snapshot(refresh_day)['matches']}
        for mid in ids:
            score=scores.get(mid)
            if score and not (rows.get(mid) or {}).get('completed'):
                rows[mid]={**score,'completed':int(score['state']=='post'),'status':score.get('minute')}
    now = datetime.now(timezone.utc)
    out = {}
    for mid in ids:
        m = rows.get(mid)
        if not m:
            out[mid] = {"id": mid, "state": "unknown", "home_goals": None, "away_goals": None}
            continue
        if m.get('state')=='in':
            out[mid]={**m,'id':mid}
            continue
        final = bool(m["completed"]) and m["home_goals"] is not None and m["away_goals"] is not None
        upcoming = datetime.fromisoformat(m["kickoff"].replace("Z", "+00:00")) > now
        out[mid] = {**m, "state": "post" if final else "pre" if upcoming else "unknown",
                    "home_goals": m["home_goals"] if final else None,
                    "away_goals": m["away_goals"] if final else None, "minute": "MS" if final else ""}
    return {"matches": out, "updated_at": now.isoformat()}
