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
        if match and float(match.group(1)).is_integer():
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
    for key,item in out.items():
        item['probability_source']='experimental_score_model'
        item['settlement_definition'] = {'period':'first_half' if key.startswith('İY ') else 'half_time_and_full_time',
            'rule':'Match result compares goals; totals compare goals with line; integer totals exclude push mass.',
            'selection':key}
    from .blend import devig
    from .decision_quality import comparison
    groups = {'first_half_1x2':[f'İY {v}' for v in ('1','X','2')],
              'half_full':[f'İY/MS {a}/{b}' for a in ('1','X','2') for b in ('1','X','2')]}
    groups.update({'half_total_'+line:[f'İY A{line}',f'İY Ü{line}'] for line in half_totals if not float(line).is_integer()})
    for group,keys in groups.items():
        probs = devig([(iddaa_odds or {}).get(k) for k in keys])
        for i,key in enumerate(keys):
            out[key].update(model_p=out[key]['p'],market_p=round(probs[i],4) if probs else None,
                            model_assumptions='Fixed goal share; independent half score distributions; not empirically validated')
            out[key].update(comparison(out[key],group=group,
                book=sum(1/iddaa_odds[k] for k in keys) if probs else None))
    for line in half_totals:
        if float(line).is_integer():
            keys=[f'İY A{line}',f'İY Ü{line}']
            push=max(0,1-sum(out[k]['p'] for k in keys))
            for key in keys:
                out[key].update(fair=None,suggested=None,push_probability=round(push,4))
                out[key]['settlement_definition']['rule']='First-half integer total: equal goals refund stake; p excludes push mass.'
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
            item['settlement_definition']['market_type']='three_way_european_handicap'
            item.update(comparison(item,group='handicap_'+line,book=sum(1/(iddaa_odds or {})[key] for key in keys) if probs else None))
    for key in (iddaa_odds or {}):
        if key.startswith(("Korner ", "Kart ")):
            out[key] = {"p": None, "fair": None, "suggested": None, "model_available": False, "validation_status":"unmodelled", "settlement_definition":None}
    return out
