"""Short lived ESPN live scoreboard cache for serverless requests."""
import time
import threading
from concurrent.futures import ThreadPoolExecutor, wait
import urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from .config import LEAGUES, USER_AGENT

_CACHE_SECONDS = 45
_cache = {"at": 0.0, "leagues": {}, "sources": {}, "updated_at": None, "day": None}
_refresh_lock = threading.Lock()
LIVE_DEADLINE_SECONDS = 7
LIVE_WORKERS = 6


def _parse_event(event, league):
    competitors = event.get("competitions", [{}])[0].get("competitors", [])
    home = next((x for x in competitors if x.get("homeAway") == "home"), None)
    away = next((x for x in competitors if x.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    from .scrapers.espn import _goals
    home_score, away_score = _goals(home.get('score')), _goals(away.get('score'))
    if home_score is None or away_score is None or min(home_score,away_score)<0:
        return None  # Missing scores are unknown, never a fabricated 0–0.
    status = event.get("status", {})
    typ = status.get("type", {})
    state = typ.get("state")
    if state not in ("in", "post"):
        return None
    clock = status.get("displayClock") or ""
    period = status.get("period")
    minute = str(clock).rstrip("'′’")+"'" if clock and state == "in" else (typ.get("shortDetail") or typ.get('detail') or ("Canlı" if state=='in' else "MS"))
    return {
        "id": str(event.get("id")), "league": league,
        "home": home.get("team", {}).get("displayName", ""),
        "away": away.get("team", {}).get("displayName", ""),
        "home_goals": home_score, "away_goals": away_score,
        "minute": minute, "period": period, "state": state,
        "kickoff": event.get('date'), "source_key": league,
    }


def _fetch(league, day):
    # Daily scoreboards support a single date. A separate all_previous request
    # covers matches after midnight Istanbul but still on the previous UTC day.
    if league == 'all_previous':
        league, day = 'all', day-timedelta(days=1)
    dates = day.strftime('%Y%m%d')
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard?dates={dates}&limit=1000"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=3) as response:
        import json
        return json.loads(response.read().decode("utf-8"))


def _aliases(rows, day):
    """Map provider matches to bulletin IDs only with both teams and kickoff verified."""
    import json
    from .db import session, get_meta, set_meta
    from . import bulletin_archive
    from .quote_provenance import timestamp
    from .scrapers.iddaa import team_similarity
    by_id = {m['id']:m for m in rows}
    with session() as conn:
        events = bulletin_archive.load_day(conn, day)
        for row in conn.execute('SELECT * FROM iddaa_events'):
            events[str(row['id'])] = dict(row)
        saved = json.loads(get_meta(conn, 'verified_scores:'+str(day)) or '{}')
        previous = dict(saved)
        for eid, event in events.items():
            kickoff = timestamp(event.get('kickoff'))
            if not kickoff or kickoff.astimezone(ZoneInfo('Europe/Istanbul')).date() != day:
                continue
            candidates = []
            for score in by_id.values():
                at = timestamp(score.get('kickoff'))
                if not at or abs((at-kickoff).total_seconds()) > 15*60:
                    continue
                h = team_similarity(event['home'],score['home'])
                a = team_similarity(event['away'],score['away'])
                if min(h,a) >= .92:
                    candidates.append((h+a,score))
            candidates.sort(key=lambda pair:pair[0],reverse=True)
            if not candidates or (len(candidates)>1 and candidates[0][0]-candidates[1][0]<.1):
                continue
            score=candidates[0][1]
            key='iddaa:'+str(eid)
            mapped={**score,'id':key,'provider_id':score['id'],'league':event['league']}
            rows.append(mapped)
            if str(eid) not in by_id:
                rows.append({**mapped,'id':str(eid)})
            if mapped['state']=='post':
                saved[key]=mapped
        if saved != previous:
            set_meta(conn,'verified_scores:'+str(day),json.dumps(saved,ensure_ascii=False))
        cutoff=(datetime.now(ZoneInfo('Europe/Istanbul')).date()-timedelta(days=30)).isoformat()
        conn.execute('DELETE FROM meta WHERE key LIKE ? AND key<?',('verified_scores:%','verified_scores:'+cutoff))
    return rows


def _snapshot():
    best={}
    rank=lambda m:(m.get('state')=='post',_cache['sources'].get(m.get('source_key'),{}).get('status')=='success',
                   _cache['sources'].get(m.get('source_key'),{}).get('last_success_at') or '')
    for items in _cache['leagues'].values():
        for m in items:
            if m['id'] not in best or rank(m)>rank(best[m['id']]):
                best[m['id']]=m
    rows=list(best.values())
    rows=[{**m,'updated_at':_cache['sources'].get(m['source_key'],{}).get('last_success_at'),
           'stale':_cache['sources'].get(m['source_key'],{}).get('status')!='success'} if m.get('source_key') else m for m in rows]
    try:
        rows = _aliases(rows, _cache['day'])
    except Exception:
        pass  # A DB outage must not hide provider scores that are still available.
    return {"matches": rows,
            "sources": dict(_cache["sources"]), "updated_at": _cache["updated_at"],
            "stale": any(s["status"] != "success" for s in _cache["sources"].values()) or not _cache["sources"]}


def get_live_snapshot(day=None):
    day = day or datetime.now(ZoneInfo('Europe/Istanbul')).date()
    if _cache.get('day')==day and time.monotonic() - _cache["at"] < _CACHE_SECONDS and _cache["sources"]:
        return _snapshot()
    if not _refresh_lock.acquire(blocking=False):
        return {**(_snapshot() if _cache.get('day')==day else {'matches':[],'sources':{},'updated_at':None}), "stale": True}
    pool = ThreadPoolExecutor(max_workers=LIVE_WORKERS)
    try:
        if _cache.get('day')!=day:
            _cache.update(day=day, at=0., leagues={}, sources={}, updated_at=None)
        futures = {pool.submit(_fetch, league, day): league for league in ['all','all_previous',*LEAGUES]}
        done, pending = wait(futures, timeout=LIVE_DEADLINE_SECONDS)
        now = datetime.now(timezone.utc).isoformat()
        successes = 0
        for future, league in futures.items():
            previous = _cache["sources"].get(league, {})
            try:
                if future not in done:
                    raise TimeoutError("live deadline")
                data = future.result()
                rows = [_parse_event(e, league) for e in data.get("events", [])]
                _cache["leagues"][league] = [m for m in rows if m]
                _cache["sources"][league] = {"status": "success", "last_success_at": now}
                successes += 1
            except Exception:
                _cache["sources"][league] = {"status": "error", "last_success_at": previous.get("last_success_at")}
        if successes:
            _cache["updated_at"] = now
        _cache["at"] = time.monotonic()
        return _snapshot()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        _refresh_lock.release()


def get_live_matches():
    return get_live_snapshot()["matches"]
