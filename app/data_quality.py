"""Maç ve iddaa bülteni eşleşmelerinin salt-okunur veri kalite denetimi.

Sınırlı son-7-gün penceresinde çalışır; veri silmez, otomatik düzeltme yapmaz.
Hatalı oran-müsabaka bağı modelin hesaplarını etkilemeden görünür olur.
"""
import argparse
import json
import math
from datetime import datetime, timedelta, timezone

from .db import init_db, session
from .scrapers.iddaa import team_similarity

WINDOW_DAYS = 7
MAX_EXAMPLES = 12


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def audit(conn, now=None):
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=WINDOW_DAYS)).isoformat()
    until = (now + timedelta(days=21)).isoformat()
    rows = conn.execute(
        "SELECT id,league,kickoff,home,away,completed,home_goals,away_goals "
        "FROM matches WHERE kickoff>=? AND kickoff<?", (since, until)
    ).fetchall()
    by_league = {}
    problems = []
    for match in rows:
        league = match["league"]
        metrics = by_league.setdefault(league, {"fixtures": 0, "completed": 0, "upcoming": 0})
        metrics["fixtures"] += 1
        metrics["completed" if match["completed"] else "upcoming"] += 1
        if match["completed"] and (
            match["home_goals"] is None or match["away_goals"] is None
            or match["home_goals"] < 0 or match["away_goals"] < 0
        ):
            problems.append({"type": "invalid_final_score", "match_id": match["id"]})

    linked = conn.execute(
        "SELECT e.id AS event_id,e.match_id,e.league AS event_league,"
        "e.kickoff AS event_kickoff,e.home AS event_home,e.away AS event_away,"
        "m.league AS fixture_league,m.kickoff AS fixture_kickoff,"
        "m.home AS fixture_home,m.away AS fixture_away "
        "FROM iddaa_events e LEFT JOIN matches m ON m.id=e.match_id "
        "WHERE e.match_id IS NOT NULL AND e.kickoff>=? AND e.kickoff<?",
        (since, until)
    ).fetchall()
    seen = {}
    for r in linked:
        mid = r["match_id"]
        if mid in seen:
            problems.append({"type": "duplicate_iddaa_link", "match_id": mid,
                             "event_ids": [seen[mid], r["event_id"]]})
        else:
            seen[mid] = r["event_id"]
        if r["fixture_league"] is None:
            problems.append({"type": "orphan_iddaa_link", "match_id": mid})
            continue
        if r["fixture_league"] != r["event_league"]:
            problems.append({"type": "league_mismatch", "match_id": mid})
        try:
            hours = abs((_ts(r["event_kickoff"]) - _ts(r["fixture_kickoff"])).total_seconds()) / 3600
        except (TypeError, ValueError):
            hours = math.inf
        if hours > 6:
            problems.append({"type": "kickoff_mismatch", "match_id": mid,
                             "hours": round(hours, 1) if math.isfinite(hours) else None})
        home = team_similarity(r["event_home"], r["fixture_home"])
        away = team_similarity(r["event_away"], r["fixture_away"])
        if min(home, away) < 0.72 or (home + away) / 2 < 0.84:
            problems.append({"type": "team_mismatch", "match_id": mid,
                             "home_similarity": round(home, 3), "away_similarity": round(away, 3)})
    odds = conn.execute(
        "SELECT COUNT(*) AS n FROM iddaa_odds WHERE odd IS NULL OR odd <= 1 OR odd > 10000"
    ).fetchone()["n"]
    if odds:
        problems.append({"type": "invalid_odds", "count": odds})
    return {
        "checked_at": now.isoformat(), "scope_days_past": WINDOW_DAYS,
        "fixtures": len(rows), "linked_iddaa_events": len(linked), "leagues": by_league,
        "hard_error_count": len(problems), "examples": problems[:MAX_EXAMPLES],
        "healthy": not problems,
    }


def main(argv=None):
    p = argparse.ArgumentParser(description="Veri bütünlüğü kontrolü")
    p.add_argument("--strict", action="store_true", help="Bozuk veride hata kodu döndür")
    args = p.parse_args(argv)
    init_db()
    with session() as conn:
        result = audit(conn)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.strict and not result["healthy"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
