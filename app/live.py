"""Short lived ESPN live scoreboard cache for serverless requests."""
import time
import threading
from concurrent.futures import ThreadPoolExecutor, wait
import urllib.request
from datetime import datetime, timezone

from .config import LEAGUES, USER_AGENT

_CACHE_SECONDS = 45
_cache = {"at": 0.0, "leagues": {}, "sources": {}, "updated_at": None}
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
    clock = status.get("displayClock") or typ.get("detail") or ""
    period = status.get("period")
    minute = f"{clock}'" if clock and state == "in" else (typ.get("shortDetail") or "MS")
    return {
        "id": str(event.get("id")), "league": league,
        "home": home.get("team", {}).get("displayName", ""),
        "away": away.get("team", {}).get("displayName", ""),
        "home_goals": home_score, "away_goals": away_score,
        "minute": minute, "period": period, "state": state,
    }


def _fetch(league):
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=3) as response:
        import json
        return json.loads(response.read().decode("utf-8"))


def _snapshot():
    return {"matches": [m for rows in _cache["leagues"].values() for m in rows],
            "sources": dict(_cache["sources"]), "updated_at": _cache["updated_at"],
            "stale": any(s["status"] != "success" for s in _cache["sources"].values()) or not _cache["sources"]}


def get_live_snapshot():
    if time.monotonic() - _cache["at"] < _CACHE_SECONDS and _cache["sources"]:
        return _snapshot()
    if not _refresh_lock.acquire(blocking=False):
        return {**_snapshot(), "stale": True}
    pool = ThreadPoolExecutor(max_workers=LIVE_WORKERS)
    try:
        futures = {pool.submit(_fetch, league): league for league in LEAGUES}
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
