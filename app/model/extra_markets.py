"""Score-matrix probabilities for the extra football markets on the iddaa bulletin."""
import re

from ..config import MODEL
from . import predict as P

_HANDICAP = re.compile(r"^Handikap (-?\d+(?:\.\d+)?) ([1X2])$")
_HALF_TOTAL = re.compile(r"^İY [AÜ](\d+(?:\.\d+)?)$")


def _result(home: int, away: int) -> str:
    return "1" if home > away else "2" if home < away else "X"


def _out(p: float) -> dict:
    return {**P.odds(p), "model_available": True, "validation_status": "experimental"}


def for_match(lambda_home: float, lambda_away: float, iddaa_odds: dict | None = None) -> dict:
    """Return modelled first-half/handicap markets and unmodelled corner/card quote slots."""
    share = MODEL["first_half_goal_share"]
    first = P.score_matrix(lambda_home * share, lambda_away * share)
    second = P.score_matrix(lambda_home * (1 - share), lambda_away * (1 - share))
    result = {f"İY {r}": 0.0 for r in ("1", "X", "2")}
    result.update({f"İY/MS {a}/{b}": 0.0 for a in ("1", "X", "2") for b in ("1", "X", "2")})
    half_totals = {}
    handis = {}
    for line in (iddaa_odds or {}):
        match = _HALF_TOTAL.match(line)
        if match:
            half_totals[match.group(1)] = None
        match = _HANDICAP.match(line)
        if match:
            handis.setdefault(match.group(1), {})
    for line in half_totals:
        result[f"İY A{line}"] = 0.0
        result[f"İY Ü{line}"] = 0.0
    for line in handis:
        for outcome in ("1", "X", "2"):
            result[f"Handikap {line} {outcome}"] = 0.0

    for hi, row in enumerate(first):
        for ai, ph in enumerate(row):
            half_result = _result(hi, ai)
            result[f"İY {half_result}"] += ph
            half_goals = hi + ai
            for line in half_totals:
                threshold = float(line)
                # Asian integer lines push; exclude exact-line mass from both sides.
                result[f"İY A{line}"] += ph * (half_goals < threshold)
                result[f"İY Ü{line}"] += ph * (half_goals > threshold)
            for hj, row2 in enumerate(second):
                for aj, ps in enumerate(row2):
                    probability = ph * ps
                    full_result = _result(hi + hj, ai + aj)
                    result[f"İY/MS {half_result}/{full_result}"] += probability

    # Tam maç handikabı, standart MS ile AYNI tam maç skor matrisi kullanır.
    full = P.score_matrix(lambda_home, lambda_away)
    for line in handis:
        for h, row in enumerate(full):
            for a, probability in enumerate(row):
                result[f"Handikap {line} {_result(h + float(line), a)}"] += probability
    out = {key: _out(probability) for key, probability in result.items()}
    from .blend import devig
    for line in handis:
        keys = [f"Handikap {line} {side}" for side in ('1', 'X', '2')]
        probs = devig([(iddaa_odds or {}).get(key) for key in keys])
        for i, key in enumerate(keys):
            item = out[key]
            market_p = probs[i] if probs else None
            item.update(model_p=item['p'], market_p=round(market_p, 4) if market_p is not None else None,
                        probability_source='experimental_score_model',
                        settlement_definition={'period':'full_time', 'home_goal_adjustment':float(line),
                                               'selection':('1','X','2')[i],
                                               'rule':'Compare home_goals + home_goal_adjustment with away_goals; 1 greater, X equal, 2 smaller'},
                        model_market_gap_pp=round(abs(item['p']-market_p)*100,1) if market_p is not None else None)
    for key in (iddaa_odds or {}):
        if key.startswith(("Korner ", "Kart ")):
            out[key] = {"p": None, "fair": None, "suggested": None, "model_available": False}
    return out
