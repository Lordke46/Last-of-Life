"""iddaa oran geçmişi: her çekimde değişen oranlar zaman damgalı satır olarak eklenir.

`iddaa_odds` her çekimde baştan yazılır (güncel oran); `iddaa_odds_history` ise yalnız oran
değiştiğinde yeni satır alır, böylece ilk kayıt = "açılış", son kayıt = güncel oran olur.
Not: "açılış" bizim ilk gördüğümüz orandır (iddaa.com'un gerçek açılış oranı değil).
Bitmiş maçların geçmişi son kickoff'tan RETENTION_DAYS gün sonra temizlenir.
"""
from datetime import datetime, timedelta, timezone
from . import quote_provenance

RETENTION_DAYS = 30
DROP_THRESHOLD_PCT = 10.0  # açılışa göre en az bu kadar düşen oran "belirgin düşüş" sayılır


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _latest_recorded(conn) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        """SELECT h.event_id, h.market, h.odd FROM iddaa_odds_history h
           JOIN (SELECT event_id, market, MAX(captured_at) captured_at
                 FROM iddaa_odds_history GROUP BY event_id, market) l
             ON l.event_id = h.event_id AND l.market = h.market AND l.captured_at = h.captured_at"""
    ).fetchall()
    return {(r["event_id"], r["market"]): r["odd"] for r in rows}


def record_snapshot(conn, events: list[dict], captured_at: str | None = None) -> int:
    """Parse edilmiş iddaa etkinliklerinin oranlarını geçmişe ekler; son kayıtla aynıysa yazmaz."""
    captured_at = captured_at or _now_iso()
    latest = _latest_recorded(conn)
    rows = []
    provenance=quote_provenance.load(conn,[e['id'] for e in events])
    for event in events:
        for market, odd in event["odds"].items():
            previous = latest.get((event["id"], market))
            quote=quote_provenance.for_event(provenance,event['id']).get('quotes',{}).get(market,{})
            reset_at=quote_provenance.timestamp(quote.get('identity_first_observed_at'))
            observed_at=quote_provenance.timestamp(quote.get('quote_observed_at'))
            identity_reset=bool(reset_at and reset_at==observed_at)
            if previous is not None and round(previous, 2) == round(odd, 2) and not identity_reset:
                continue
            rows.append((event["id"], market, odd, captured_at, event["kickoff"]))
    conn.executemany(
        """INSERT INTO iddaa_odds_history(event_id, market, odd, captured_at, kickoff)
           VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""", rows)
    return len(rows)


def prune(conn, now: datetime | None = None, retention_days: int = RETENTION_DAYS) -> int:
    """Bültenden çıkmış (oynanmış) ve son maç saati retention_days'ten eski etkinliklerin geçmişini siler."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=retention_days)).isoformat()
    cur = conn.execute(
        """DELETE FROM iddaa_odds_history
           WHERE event_id NOT IN (SELECT id FROM iddaa_events)
             AND event_id IN (SELECT event_id FROM iddaa_odds_history
                              GROUP BY event_id HAVING MAX(kickoff) < ?)""", (cutoff,))
    return max(cur.rowcount, 0)


def _change(opening: float, current: float) -> dict:
    pct = round((current - opening) / opening * 100, 1) if opening else 0.0
    return {"open": opening, "current": current, "change_pct": pct,
            "direction": "down" if current < opening else "up" if current > opening else "flat"}


def _opening_vs_current(conn, where: str, params) -> list[dict]:
    """Güncel bültendeki her (etkinlik, pazar) için ilk kayıtlı (açılış) ve güncel oran."""
    rows = conn.execute(
        f"""SELECT e.id event_id, e.match_id, e.league, e.kickoff, e.home, e.away, o.market,
                   o.odd current_odd, h.odd opening_odd, h.captured_at opened_at
            FROM iddaa_events e
            JOIN iddaa_odds o ON o.event_id = e.id
            JOIN (SELECT event_id, market, MIN(captured_at) first_at
                  FROM iddaa_odds_history GROUP BY event_id, market) f
              ON f.event_id = o.event_id AND f.market = o.market
            JOIN iddaa_odds_history h
              ON h.event_id = f.event_id AND h.market = f.market AND h.captured_at = f.first_at
            WHERE {where}""", params).fetchall()
    rows = [dict(r) for r in rows]
    state = quote_provenance.load(conn)
    # Provider IDs/line/selection changes start a new comparison series, never bridge identities.
    reset = [r for r in rows if (quote_provenance.for_event(state,r['event_id']).get('quotes',{}).get(r['market'],{}).get('identity_first_observed_at') or '') > r['opened_at']]
    if reset:
        history = conn.execute('SELECT event_id,market,odd,captured_at FROM iddaa_odds_history ORDER BY captured_at').fetchall()
        first = {}
        for h in history:
            key = (h['event_id'],h['market'])
            cutoff = quote_provenance.for_event(state,h['event_id']).get('quotes',{}).get(h['market'],{}).get('identity_first_observed_at')
            if cutoff and h['captured_at']>=cutoff:first.setdefault(key,h)
        for r in reset:
            opening = first.get((r['event_id'],r['market']))
            r['opening_odd'] = opening['odd'] if opening else r['current_odd']
            r['opened_at'] = opening['captured_at'] if opening else None
    return [r for r in rows if r['opened_at']]


def movements_for_matches(conn, match_ids) -> dict[str, dict[str, dict]]:
    """ESPN maç kimliği -> {pazar: {open, current, change_pct, direction, opened_at}} (yalnız değişenler)."""
    match_ids = [str(m) for m in match_ids]
    if not match_ids:
        return {}
    marks = ",".join("?" for _ in match_ids)
    out: dict[str, dict[str, dict]] = {}
    for r in _opening_vs_current(conn, f"e.match_id IN ({marks})", match_ids):
        if round(r["opening_odd"], 2) == round(r["current_odd"], 2):
            continue
        out.setdefault(r["match_id"], {})[r["market"]] = {
            **_change(r["opening_odd"], r["current_odd"]), "opened_at": r["opened_at"]}
    return out


def history_for_match(conn, match_id: str) -> dict | None:
    """Maçın eşleşen iddaa etkinliğinin pazar başına zaman damgalı oran serisi."""
    event = conn.execute("SELECT id FROM iddaa_events WHERE match_id = ?", (str(match_id),)).fetchone()
    if not event:
        return None
    rows = conn.execute(
        """SELECT market, odd, captured_at FROM iddaa_odds_history
           WHERE event_id = ? ORDER BY captured_at""", (event["id"],)).fetchall()
    markets: dict[str, list[dict]] = {}
    provenance=quote_provenance.for_event(quote_provenance.load(conn,[event['id']]),event['id'])
    for r in rows:
        cutoff=provenance.get('quotes',{}).get(r['market'],{}).get('identity_first_observed_at')
        if cutoff and r['captured_at']<cutoff:continue
        markets.setdefault(r["market"], []).append({"at": r["captured_at"], "odd": r["odd"]})
    return {"event_id": event["id"], "markets": markets}


def drops(conn, min_pct: float = DROP_THRESHOLD_PCT, league: str | None = None, limit: int = 20,
          now: datetime | None = None) -> list[dict]:
    """Açılışa göre en az min_pct düşen oranı olan yaklaşan maçlar (en büyük düşüş önce)."""
    where, params = "e.kickoff >= ?", [(now or datetime.now(timezone.utc)).isoformat()]
    if league:
        where, params = where + " AND e.league = ?", params + [league]
    matches: dict[str, dict] = {}
    for r in _opening_vs_current(conn, where, params):
        change = _change(r["opening_odd"], r["current_odd"])
        if change["direction"] != "down" or -change["change_pct"] < min_pct:
            continue
        m = matches.setdefault(r["event_id"], {
            "event_id": r["event_id"], "match_id": r["match_id"], "league": r["league"],
            "kickoff": r["kickoff"], "home": r["home"], "away": r["away"], "markets": []})
        m["markets"].append({"market": r["market"], **change, "opened_at": r["opened_at"]})
    for m in matches.values():
        m["markets"].sort(key=lambda x: x["change_pct"])
        m["max_drop_pct"] = -m["markets"][0]["change_pct"]
    return sorted(matches.values(), key=lambda m: -m["max_drop_pct"])[:limit]
