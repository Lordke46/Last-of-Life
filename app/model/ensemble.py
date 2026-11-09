"""Chronological challenger selection. Never choose a model on its training scores."""
import math
from datetime import timedelta
import hashlib
import json
from ..db import get_meta, set_meta
from .fitted import FittedGoals
from . import predict as P
from .ratings import played_before
from .settlement import outcome

EVALUATION_MARKETS = ('1','X','2','Ü1.5','Ü2.5','Ü3.5','KG Var')
MIN_TRAIN = 120
VALIDATION_SIZE = 160


def loss(markets, match):
    return sum((markets[k]['p']-int(outcome(k,match['home_goals'],match['away_goals'])))**2
               for k in EVALUATION_MARKETS)/len(EVALUATION_MARKETS)


def mix(left, right, weight):
    return [[(1-weight)*a+weight*b for a,b in zip(x,y)] for x,y in zip(left,right)]


def select(pairs):
    """Paired loss interval + minimum improvement; no promise of future superiority."""
    evidence = {}
    for w in (.5,1.0):
        diffs = [row[1][w]-row[0] for row in pairs]
        blocks = {}
        for i,row in enumerate(pairs):
            blocks.setdefault(row[2] if len(row)>2 else i//10,[]).append(diffs[i])
        n = len(diffs)
        avg = sum(diffs)/n if n else 0
        # Cluster-robust mean SE: matchweek outcomes are not independent replicates.
        g = len(blocks)
        se = math.sqrt(g/(g-1)*sum(sum(d-avg for d in block)**2 for block in blocks.values())/n**2) if g>1 else math.inf
        evidence[str(w)] = {'n':n,'delta_brier':round(avg,6),
                            'blocks':g, 'upper95':round(avg+1.96*se,6) if g>1 else None}
    eligible = [float(w) for w,e in evidence.items()
                if e['n']>=80 and e['blocks']>=8 and e['delta_brier']<-.002 and e['upper95']<0]
    weight = min(eligible,key=lambda w:evidence[str(w)]['delta_brier']) if eligible else 0.0
    return {'fitted_weight':weight,'validation':evidence,
            'status':'challenger_selected' if weight else 'incumbent_retained',
            'scope':'chronological_result_reconstruction', 'point_in_time_verified':False}


def prepare(model, at):
    history = sorted((m for m in model.done if played_before(m,at)),key=lambda m:m['ts'])
    pairs = []
    fitted = None
    start = max(MIN_TRAIN,len(history)-VALIDATION_SIZE)
    last_fit = None
    for i in range(start,len(history)):
        m = history[i]
        # Fixed blocks, never train on another result from the same kickoff/date.
        if last_fit is None or (m['ts']-last_fit).days>=21:
            train = [r for r in history[:i] if played_before(r,m['ts'])]
            fitted = FittedGoals(train,m['ts'])
            last_fit = m['ts']
        if not fitted.available(m['home'],m['away']):continue
        old = model.predict(m['home'],m['away'],m['ts'],history=history[:i],news={},detail=False)
        matrix = fitted.matrix(m['home'],m['away'])
        learned = P.markets(matrix)['markets']
        # Linear mixture of score distributions implies the same mixture of market probabilities.
        candidates = {w:{k:{'p':(1-w)*old['markets'][k]['p']+w*learned[k]['p']}
                          for k in EVALUATION_MARKETS} for w in (.5,1.0)}
        pairs.append((loss(old['markets'],m),{w:loss(p,m) for w,p in candidates.items()},m['ts'].strftime('%G-%V')))
    selection = select(pairs)
    selection['trained_before'] = at.isoformat()
    selection['training_matches'] = len(history)
    return FittedGoals(history,at),selection


def code_signature():
    from pathlib import Path
    digest = hashlib.sha256()
    digest.update(Path(__file__).resolve().parents[1].joinpath("config.py").read_bytes())
    for name in ('ensemble.py','fitted.py','engine.py','ratings.py','predict.py'):
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()[:16]


def save_selection(conn, league, selection):
    set_meta(conn,'ensemble_selection:'+league,json.dumps({'code':code_signature(),**selection}))


def load_selection(conn, league, at):
    try:
        data=json.loads(get_meta(conn,'ensemble_selection:'+league) or '{}')
        from .ratings import parse_ts
        age=at-parse_ts(data['trained_before'])
        if data.get('code')==code_signature() and timedelta(0)<=age<=timedelta(days=14):
            return data
    except (ValueError,TypeError,KeyError):
        pass
    return {'fitted_weight':0.0,'status':'awaiting_chronological_validation',
            'scope':'chronological_result_reconstruction','point_in_time_verified':False}
