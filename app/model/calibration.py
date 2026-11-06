"""Maç öncesi kaydedilmiş, sonuçlanmış tahminlerden piyasa bazlı kalibrasyon.

Yalnız maç başlamadan ÖNCE alınan snapshot kullanılır. Geçmiş sonuçları
yeniden tahmin etmek yerine, zamanında yayımlanan olasılıklar değerlendirilir.
Örnek sayısı yetersizse kalibrasyon uygulanmaz ve banko bu adayı kullanmaz.
"""
import json
import math
from datetime import datetime, timezone

from .settlement import outcome

MIN_SAMPLES = 30
PRIOR_STRENGTH = 60
MAX_CORRECTION = 0.08


def _bucket(p: float) -> int:
    return min(9, max(0, int(p * 10)))


def _valid(p) -> bool:
    return isinstance(p, (float, int)) and not isinstance(p, bool) and math.isfinite(p) and 0 < p < 1


def load(conn, now=None) -> dict:
    """{(league, market, 10%-dilim): {n, wins, predicted_sum}}.

    Sadece tamamlanmış maçlar ve kickoff anından önce kaydedilen snapshot'lar.
    Henüz başlamamış veya aynı maçın sonraki tahmini eğitime giremez.
    """
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT s.league, s.kickoff, s.captured_at, s.payload, "
        "m.home_goals, m.away_goals, m.completed "
        "FROM prediction_snapshots s JOIN matches m ON m.id=s.match_id "
        "WHERE m.completed=1 AND m.home_goals IS NOT NULL AND m.away_goals IS NOT NULL"
    ).fetchall()
    stats = {}
    for r in rows:
        try:
            captured = datetime.fromisoformat(r["captured_at"].replace("Z", "+00:00"))
            kickoff = datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00"))
            if not captured < kickoff < now:
                continue
            payload = json.loads(r["payload"])
        except (TypeError, ValueError, KeyError):
            continue
        for market, probability in (payload.get("blend") or {}).items():
            if not _valid(probability):
                continue
            won = outcome(market, r["home_goals"], r["away_goals"])
            if won is None:
                continue
            key = (r["league"], market, _bucket(probability))
            group = stats.setdefault(key, {"n": 0, "wins": 0, "predicted_sum": 0.0})
            group["n"] += 1
            group["wins"] += int(won)
            group["predicted_sum"] += probability
    return stats


def calibrate(probability: float, league: str, market: str, stats: dict) -> dict:
    """Empirik düzeltme; küçük veri setlerinde güvenilirlik icat etmez.

    Yumuşatma ve en çok 8 yüzde puan düzeltme büyük sıçramaları engeller.
    Bu tahminler hâlâ test edilmesi gereken istatistiksel tahminlerdir.
    """
    if not _valid(probability):
        return {"p": probability, "raw_p": probability, "n": 0, "applied": False}
    group = stats.get((league, market, _bucket(probability)), {})
    n = group.get("n", 0)
    if n < MIN_SAMPLES:
        return {"p": probability, "raw_p": probability, "n": n, "applied": False}
    observed = group["wins"] / n
    expected = group["predicted_sum"] / n
    shift = max(-MAX_CORRECTION, min(MAX_CORRECTION,
                (observed - expected) * n / (n + PRIOR_STRENGTH)))
    p = max(0.001, min(0.999, probability + shift))
    return {"p": round(p, 4), "raw_p": probability, "n": n, "applied": True,
            "observed_rate": round(observed, 4), "historical_predicted": round(expected, 4)}


def report(stats: dict) -> dict:
    groups = []
    for (league, market, bucket), data in sorted(stats.items()):
        n = data["n"]
        groups.append({"league": league, "market": market, "band": [bucket / 10, (bucket + 1) / 10],
                       "n": n, "predicted": round(data["predicted_sum"] / n, 4),
                       "actual": round(data["wins"] / n, 4),
                       "sufficient": n >= MIN_SAMPLES})
    return {"min_samples": MIN_SAMPLES, "groups": groups, "eligible_groups": sum(g["sufficient"] for g in groups)}
