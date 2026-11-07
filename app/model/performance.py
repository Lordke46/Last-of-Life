"""Maç öncesi kaydedilmiş iddaa fiyatlı seçimlerin gerçekleşen performansı.

Seçim değiştirilmiyor; ilk snapshot'ta hangi pazar/gerçek oran yayımlandıysa
yalnız o değerlendirilir. Sonradan görülen oranlar veya sonuçlar seçime sızmaz.
"""
import json
import math
from datetime import datetime, timedelta, timezone

from .settlement import outcome

MIN_RELIABLE_SAMPLES = 30


def _ts(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def _valid(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _wilson(won, n):
    if not n:
        return None
    z = 1.96
    rate = won / n
    denom = 1 + z * z / n
    center = (rate + z * z / (2 * n)) / denom
    radius = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / denom
    return [round(max(0, center - radius), 4), round(min(1, center + radius), 4)]


def _summarize(rows):
    n = len(rows)
    if not n:
        return {"n": 0, "reliable": False}
    won = sum(r["won"] for r in rows)
    return {
        "n": n, "won": won, "hit_rate": round(won / n, 4),
        "predicted_rate": round(sum(r["p"] for r in rows) / n, 4),
        "calibration_gap": round((won - sum(r["p"] for r in rows)) / n, 4),
        "realized_roi": round(sum(r["net"] for r in rows) / n, 4),
        "expected_roi": round(sum(r["p"] * r["odds"] - 1 for r in rows) / n, 4),
        "brier": round(sum((r["p"] - r["won"]) ** 2 for r in rows) / n, 4),
        "interval95": _wilson(won, n),
        "reliable": n >= MIN_RELIABLE_SAMPLES,
    }


def report(conn, now=None):
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT s.league, s.captured_at, s.kickoff, s.payload, "
        "m.kickoff AS actual_kickoff, m.home_goals, m.away_goals "
        "FROM prediction_snapshots s JOIN matches m ON m.id=s.match_id "
        "WHERE m.completed=1 AND m.home_goals IS NOT NULL AND m.away_goals IS NOT NULL"
    ).fetchall()
    picks = []
    for r in rows:
        try:
            if not _ts(r["captured_at"]) < min(_ts(r["kickoff"]), _ts(r["actual_kickoff"])) < now:
                continue
            data = json.loads(r["payload"])
            pick = data.get("pick") or {}
            market = pick.get("market")
            odds = pick.get("odds")
            p = pick.get("p")
            if (pick.get("odds_source") != "iddaa" or not _valid(odds)
                    or not _valid(p) or odds <= 1 or not 0 < p < 1):
                continue
            quoted = (data.get("odds") or {}).get(market)
            if not _valid(quoted) or abs(quoted - odds) > 0.011:
                continue  # gerçek bültende kaydedilmemiş veya fiyat uyuşmuyor
            won = outcome(market, r["home_goals"], r["away_goals"])
            if won is None:
                continue
            picks.append({"league": r["league"], "market": market, "odds": odds,
                          "p": p, "won": int(won),
                          "net": (odds - 1) if won else -1})
        except (TypeError, ValueError, KeyError):
            continue

    groups = {}
    for r in picks:
        groups.setdefault(("league", r["league"]), []).append(r)
        groups.setdefault(("market", r["market"]), []).append(r)
        groups.setdefault(("bucket", min(9, int(r["p"] * 10))), []).append(r)
    segments = {}
    for dimension in ("league", "market", "bucket"):
        segments[dimension] = [
            {"key": key, **_summarize(values)}
            for (typ, key), values in sorted(groups.items(), key=lambda a: str(a[0]))
            if typ == dimension
        ]
    return {
        "scope": "first_recorded_prematch_iddaa_only", "min_reliable_samples": MIN_RELIABLE_SAMPLES,
        "summary": _summarize(picks), "segments": segments,
        "notes": "ROI seçim başına 1 birim sabit stake; yalnız ilk gerçek iddaa fiyatı ve sonuçlanan maçlar. "
                 "Örnek sayısı düşükse sonuç istatistiksel kanıt sayılmaz.",
    }
