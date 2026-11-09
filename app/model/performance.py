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


def report(conn, now=None, pick_field="pick"):
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT s.match_id, s.league, s.captured_at, s.kickoff, s.payload, "
        "m.kickoff AS actual_kickoff, m.home_goals, m.away_goals, m.status "
        "FROM prediction_snapshots s JOIN matches m ON m.id=s.match_id "
        "WHERE m.completed=1 AND m.home_goals IS NOT NULL AND m.away_goals IS NOT NULL"
    ).fetchall()
    closing = {(str(r["match_id"]), r["market"]): dict(r) for r in conn.execute(
        "SELECT l.match_id, l.market, l.odd, l.updated_at FROM odds_log l "
        "JOIN matches m ON m.id=l.match_id WHERE m.completed=1").fetchall()}
    picks = []
    for r in rows:
        try:
            captured,start,actual=map(_ts,(r['captured_at'],r['kickoff'],r['actual_kickoff']))
            if any(t.tzinfo is None for t in (captured,start,actual)):
                continue
            if (not captured < min(start,actual) or max(start,actual)+timedelta(hours=3)>now
                    or any(v in str(r['status'] or '').lower() for v in ('cancel','postpon','abandon','suspend'))):
                continue
            data = json.loads(r["payload"])
            pick = data.get(pick_field) or {}
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
            last = closing.get((str(r["match_id"]), market))
            price_change = None
            if last and _valid(last["odd"]) and last["odd"] > 1:
                try:
                    kickoff = min(_ts(r["kickoff"]), _ts(r["actual_kickoff"]))
                    at = _ts(last["updated_at"])
                    if max(_ts(r["captured_at"]), kickoff - timedelta(hours=1)) <= at < kickoff:
                        price_change = odds / last["odd"] - 1
                except (TypeError, ValueError):
                    pass
            market_p = (data.get("market") or {}).get(market)
            picks.append({"league": r["league"], "market": market, "odds": odds,
                          "price_change": price_change,
                          "market_p": market_p if _valid(market_p) and 0 < market_p < 1 else None,
                          "p": p, "won": int(won),
                          "net": (odds - 1) if won else -1})
        except (TypeError, ValueError, KeyError):
            continue

    groups = {}
    for r in picks:
        groups.setdefault(("league", r["league"]), []).append(r)
        groups.setdefault(("market", r["market"]), []).append(r)
        groups.setdefault(("bucket", min(9, int(r["p"] * 10))), []).append(r)
        band = "1.01–1.39" if r["odds"] < 1.4 else "1.40–1.79" if r["odds"] < 1.8 else "1.80–2.30" if r["odds"] <= 2.3 else "2.31+"
        groups.setdefault(("odds_band", band), []).append(r)
    segments = {}
    for dimension in ("league", "market", "bucket", "odds_band"):
        segments[dimension] = [
            {"key": key, **_summarize(values)}
            for (typ, key), values in sorted(groups.items(), key=lambda a: str(a[0]))
            if typ == dimension
        ]
    timed = [r["price_change"] for r in picks if r["price_change"] is not None]
    paired = [r for r in picks if r["market_p"] is not None]
    model_brier = sum((r["p"] - r["won"]) ** 2 for r in paired) / len(paired) if paired else None
    market_brier = sum((r["market_p"] - r["won"]) ** 2 for r in paired) / len(paired) if paired else None
    return {
        "price_tracking": {"n": len(timed), "missing": len(picks) - len(timed),
                           "mean_price_difference": round(sum(timed) / len(timed), 4) if timed else None,
                           "positive_rate": round(sum(v > 0 for v in timed) / len(timed), 4) if timed else None,
                           "scope": "last_observed_iddaa_within_60_minutes_before_kickoff",
                           "note": "İlk seçim fiyatı / son kayıtlı maç öncesi fiyat − 1. Marj düzeltilmez; resmi kapanış veya kârlılık kanıtı değildir."},
        "market_comparison": {"n": len(paired), "blend_brier": round(model_brier, 4) if paired else None,
                              "market_brier": round(market_brier, 4) if paired else None,
                              "delta": round(model_brier - market_brier, 4) if paired else None,
                              "reliable": len(paired) >= MIN_RELIABLE_SAMPLES,
                              "note": "Aynı ilk kayıtlarda harmanlı seçim olasılığı ile marjı temizlenmiş piyasa olasılığının Brier karşılaştırması; düşük daha iyi."},
        "scope": "first_recorded_prematch_iddaa_only", "selection_field":pick_field, "min_reliable_samples": MIN_RELIABLE_SAMPLES,
        "summary": _summarize(picks), "segments": segments,
        "notes": "ROI seçim başına 1 birim sabit stake; yalnız ilk gerçek iddaa fiyatı ve sonuçlanan maçlar. "
                 "Örnek sayısı düşükse sonuç istatistiksel kanıt sayılmaz.",
    }
