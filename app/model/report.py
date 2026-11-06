"""Model Karnesi: walk-forward isabet/Brier/log-loss, kalibrasyon, son 30 gün ve kupon özeti.

Ağır hesap `python -m app.model.report` (GitHub Actions cron) ile yapılıp `meta.model_report` anahtarına
JSON olarak yazılır; /api/model-report yalnız bu önbelleği okur.
"""
import json
import math
from datetime import datetime, timedelta, timezone

from ..config import LEAGUES, is_cup
from ..db import get_meta, init_db, session, set_meta
from . import coupon_history, snapshots, calibration as calibration_model
from .engine import LeagueModel, _load, walkforward_samples
from .ratings import Elo, Strengths

META_KEY = "model_report"
BUCKETS = 10
TREND_DAYS = 30
MAX_AGE_HOURS = 12


def _outcome(m) -> int:
    return 0 if m["home_goals"] > m["away_goals"] else 1 if m["home_goals"] == m["away_goals"] else 2


def league_samples(done: list[dict], min_history: int = 120) -> list[dict]:
    """Her maç için yalnız öncesindeki veriyle 1X2 olasılıkları + gerçek sonuç."""
    if len(done) <= min_history:
        return []
    out = []
    for m, p in walkforward_samples("", done, min_history):
        out.append({"day": m["kickoff"][:10], "probs": [p["1"]["p"], p["X"]["p"], p["2"]["p"]], "actual": _outcome(m)})
    return out


def metrics(samples: list[dict]) -> dict:
    n = len(samples)
    if not n:
        return {"n": 0}
    hit = sum(max(range(3), key=lambda i: s["probs"][i]) == s["actual"] for s in samples)
    brier = sum(sum((s["probs"][i] - (i == s["actual"])) ** 2 for i in range(3)) for s in samples) / n
    logloss = sum(-math.log(max(1e-9, s["probs"][s["actual"]])) for s in samples) / n
    home_rate = sum(s["actual"] == 0 for s in samples) / n
    return {"n": n, "accuracy": round(hit / n, 4), "brier": round(brier, 4), "logloss": round(logloss, 4),
            "home_baseline": round(home_rate, 4), "random_baseline": round(1 / 3, 4),
            "vs_home": round(hit / n - home_rate, 4), "vs_random": round(hit / n - 1 / 3, 4)}


def calibration(samples: list[dict]) -> list[dict]:
    acc = [[0, 0.0, 0] for _ in range(BUCKETS)]  # n, toplam tahmin, gerçekleşen
    for s in samples:
        for i in range(3):
            b = acc[min(BUCKETS - 1, int(s["probs"][i] * BUCKETS))]
            b[0] += 1
            b[1] += s["probs"][i]
            b[2] += s["actual"] == i
    return [{"lo": i / BUCKETS, "hi": (i + 1) / BUCKETS, "n": n,
             "predicted": round(p / n, 4) if n else None, "actual": round(a / n, 4) if n else None}
            for i, (n, p, a) in enumerate(acc)]


def trend(samples: list[dict], today: datetime | None = None) -> list[dict]:
    today = today or datetime.now(timezone.utc)
    since = (today - timedelta(days=TREND_DAYS)).strftime("%Y-%m-%d")
    by_day: dict[str, list[dict]] = {}
    for s in samples:
        if s["day"] >= since:
            by_day.setdefault(s["day"], []).append(s)
    return [{"day": d, **metrics(v)} for d, v in sorted(by_day.items())]


def coupon_summary(conn) -> dict:
    summary = coupon_history.history(conn, 1)["summary"]
    keep = ("coupons", "won", "lost", "hit_rate", "pick_hit_rate", "stake", "profit", "roi")
    return {kind: {k: s[k] for k in keep} for kind, s in summary.items()}


def build(min_history: int = 120) -> dict:
    leagues, everything = [], []
    for lg, cfg in LEAGUES.items():
        if is_cup(lg):
            continue
        with session() as conn:
            done, _ = _load(conn, lg)
        samples = league_samples(done, min_history)
        if samples:
            leagues.append({"league": lg, "name": cfg.get("name", lg), **metrics(samples)})
            everything += samples
    with session() as conn:
        try:
            coupons = coupon_summary(conn)
        except Exception:
            conn.rollback()
            coupons = None
    with session() as conn:
        try:
            prospective = snapshots.report(conn)
        except Exception:
            conn.rollback()
            prospective = {"available": False}
    with session() as conn:
        try:
            calibration_audit = calibration_model.report(calibration_model.load(conn))
        except Exception:
            calibration_audit = {"available": False}
    return {"prospective": prospective, "banko_calibration": calibration_audit,
            "computed_at": datetime.now(timezone.utc).isoformat(), "overall": metrics(everything),
            "leagues": sorted(leagues, key=lambda r: -r["n"]), "calibration": calibration(everything),
            "trend": trend(everything), "coupons": coupons}


def refresh() -> dict:
    report = build()
    with session() as conn:
        set_meta(conn, META_KEY, json.dumps(report))
    return report


def cached() -> dict | None:
    with session() as conn:
        v = get_meta(conn, META_KEY)
    return json.loads(v) if v else None


def is_fresh(max_age_hours: float = MAX_AGE_HOURS) -> bool:
    r = cached()
    if not r or not r.get("computed_at"):
        return False
    return datetime.now(timezone.utc) - datetime.fromisoformat(r["computed_at"]) < timedelta(hours=max_age_hours)


if __name__ == "__main__":
    import sys
    init_db()
    if "--force" not in sys.argv and is_fresh():
        print("model report güncel, atlandı")
        sys.exit(0)
    r = refresh()
    print(f"model report: {r['overall'].get('n', 0)} maç, {len(r['leagues'])} lig")
