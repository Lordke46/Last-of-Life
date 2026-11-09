"""Bounded read-through refresh for stale public Iddaa prices."""
from datetime import datetime, timedelta, timezone

from . import db, quote_provenance
from .db import get_meta, set_meta, session

PRICE_MAX_AGE = timedelta(minutes=60)
ATTEMPT_COOLDOWN = timedelta(minutes=5)
LOCK_MINUTES = 2
ATTEMPT_KEY = "iddaa_readthrough_attempt_at"


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _latest_trusted_quote(conn):
    state = quote_provenance.load(conn)
    all_quotes = [quote for event in state.get("events", {}).values()
                  for quote in (event.get("quotes") or {}).values()]
    observed = [_timestamp(quote.get("quote_observed_at")) for quote in all_quotes
                if quote.get("cache_status") == "network_no_cache_evidence"]
    return max((item for item in observed if item), default=None)


def refresh_if_stale(now=None) -> dict:
    """Refresh once synchronously when stored prices are old; never runs in local preview."""
    now = now or datetime.now(timezone.utc)
    if not db.is_postgres():
        return {"status": "skipped", "reason": "local_database"}

    with session() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM iddaa_odds").fetchone()["n"]
        observed = _latest_trusted_quote(conn)
        age = now-observed if observed else None
        if count and age is not None and timedelta(minutes=-5) <= age <= PRICE_MAX_AGE:
            return {"status": "fresh", "age_seconds": max(0, int(age.total_seconds()))}
        attempted = _timestamp(get_meta(conn, ATTEMPT_KEY))
        if attempted and now - attempted < ATTEMPT_COOLDOWN:
            left = int((ATTEMPT_COOLDOWN - (now - attempted)).total_seconds())
            return {"status": "throttled", "retry_after_seconds": max(0, left)}

    from . import updater
    with session() as conn:
        token = updater.acquire_lock(conn, LOCK_MINUTES)
        if not token:
            return {"status": "busy"}
        attempted = _timestamp(get_meta(conn, ATTEMPT_KEY))
        if attempted and now-attempted < ATTEMPT_COOLDOWN:
            updater.release_lock(conn,token)
            return {'status':'throttled','retry_after_seconds':max(0,int((ATTEMPT_COOLDOWN-(now-attempted)).total_seconds()))}
        set_meta(conn, ATTEMPT_KEY, now.isoformat())

    try:
        with updater.leased_session(token, LOCK_MINUTES) as conn:
            stats = updater.iddaa.sync_odds(conn, retries=1, transport_timeout=4,
                                            detail_budget_seconds=2, catalog_timeout=2)
            if not isinstance(stats, dict) or not stats.get("supported_events"):
                raise RuntimeError("No parsed Iddaa events in refresh response")
        with session() as conn:
            updater.release_lock(conn, token)
            committed_at = _timestamp(get_meta(conn, "iddaa_last_update"))
        return {"status": "refreshed", "supported_events": stats["supported_events"],
                "updated_at": committed_at.isoformat() if committed_at else None}
    except Exception as exc:
        # The leased transaction rolled back, preserving last-good rows. Persist
        # failure evidence separately and keep the five-minute attempt throttle.
        with session() as conn:
            if get_meta(conn,'update_lock_owner') != token:
                return {'status':'busy','reason':'lease_replaced'}
            quote_provenance.failed(conn, exc)
            set_meta(conn, "iddaa_last_error", str(exc)[:300])
            updater.release_lock(conn, token)
        return {"status": "failed", "error": str(exc)[:180]}
