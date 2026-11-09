"""Kupondaki eski maçları da okuyabilen, tahmin üretmeyen sonuç uç noktası."""
from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, HTTPException, Query
from .db import session

router = APIRouter()


@router.get("/api/results")
def results(ids: Annotated[list[str], Query(min_length=1, max_length=30)]):
    if any(not mid or len(mid) > 256 for mid in ids):
        raise HTTPException(400, "Geçersiz maç kimliği")
    ids = list(dict.fromkeys(ids))
    marks = ",".join("?" for _ in ids)
    with session() as conn:
        rows = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT id,kickoff,completed,home_goals,away_goals,status,updated_at FROM matches WHERE id IN ({marks})", ids)}
        aliases = [mid.removeprefix('iddaa:') for mid in ids if mid.startswith('iddaa:')]
        if aliases:
            placeholders = ','.join('?' for _ in aliases)
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
                key='iddaa:'+mid.removeprefix('iddaa:')
                score=scores.get(key)
                if score and score.get('state')=='post' and not (rows.get(mid) or {}).get('completed'):
                    rows[mid]={**score,'id':mid,'completed':1,'status':'FT','updated_at':score.get('updated_at')}
    now = datetime.now(timezone.utc)
    out = {}
    for mid in ids:
        m = rows.get(mid)
        if not m:
            out[mid] = {"id": mid, "state": "unknown", "home_goals": None, "away_goals": None}
            continue
        final = bool(m["completed"]) and m["home_goals"] is not None and m["away_goals"] is not None
        upcoming = datetime.fromisoformat(m["kickoff"].replace("Z", "+00:00")) > now
        out[mid] = {**m, "state": "post" if final else "pre" if upcoming else "unknown",
                    "home_goals": m["home_goals"] if final else None,
                    "away_goals": m["away_goals"] if final else None, "minute": "MS" if final else ""}
    return {"matches": out, "updated_at": now.isoformat()}
