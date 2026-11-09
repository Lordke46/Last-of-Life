"""Choose market weights on immutable pre-match forecasts, with temporal holdout.

All leagues are pooled only when league-specific evidence is insufficient. Four
challengers are compared to the incumbent 50/50 blend; matchweek clusters and a
conservative multiple-comparison interval avoid switching on tiny lucky samples.
"""
import json
import math
from datetime import datetime,timezone,timedelta
from .settlement import outcome

KEY = ('__market_weights__','','')
WEIGHTS = (0.0,.25,.5,.75,1.0)


def choose(rows):
    rows = sorted(rows,key=lambda r:r['at'])
    if len(rows)<120:return None
    split=int(len(rows)*.65)
    # Keep all fixtures from the boundary day on the holdout side.
    boundary=rows[split]['at'][:10]
    train=[r for r in rows if r['at'][:10]<boundary]
    test=[r for r in rows if r['at'][:10]>=boundary]
    if len(train)<60 or len(test)<40:return None
    def loss(row,w):
        return sum(((1-w)*p+w*q-y)**2 for p,q,y in zip(row['model'],row['market'],row['actual']))/len(row['model'])
    chosen=min(WEIGHTS,key=lambda w:sum(loss(r,w) for r in train))
    diffs=[loss(r,chosen)-loss(r,.5) for r in test]
    mean=sum(diffs)/len(diffs)
    blocks={}
    for r,d in zip(test,diffs):
        block=datetime.fromisoformat(r['at']).strftime('%G-%V')
        blocks.setdefault(block,[]).append(d)
    g=len(blocks)
    se=math.sqrt(g/(g-1)*sum(sum(d-mean for d in block)**2 for block in blocks.values())/len(diffs)**2) if g>1 else math.inf
    accepted=g>=8 and mean<-.002 and mean+2.5*se<0
    return {'weight':chosen if accepted else .5, 'candidate_weight':chosen,
            'status':'validated_challenger' if accepted else 'incumbent_retained',
            'train_n':len(train),'holdout_n':len(test),'holdout_blocks':g,
            'delta_brier':round(mean,6),'upper_bound':round(mean+2.5*se,6) if g>1 else None,
            'validation_start':boundary,'source':'immutable_prematch_snapshots'}


def load(conn, now=None, rows=None):
    from .blend import GROUPS
    now=now or datetime.now(timezone.utc)
    rows=rows if rows is not None else conn.execute('SELECT s.league,s.kickoff,s.captured_at,s.payload,m.kickoff AS actual_kickoff,'
                      'm.home_goals,m.away_goals,m.status FROM prediction_snapshots s JOIN matches m '
                      'ON m.id=s.match_id WHERE m.completed=1').fetchall()
    grouped={}
    for r in rows:
        try:
            parse=lambda v:datetime.fromisoformat(v.replace('Z','+00:00'))
            captured,start,actual=map(parse,(r['captured_at'],r['kickoff'],r['actual_kickoff']))
            if any(t.tzinfo is None for t in (captured,start,actual)):continue
            if not captured<min(start,actual) or max(start,actual)+timedelta(hours=3)>now:continue
            if any(t in str(r['status'] or '').lower() for t in ('cancel','postpon','abandon','suspend')):continue
            payload=json.loads(r['payload'])
            # The new goal challenger must accumulate its own forward evidence.
            if (payload.get('ensemble') or {}).get('fitted_weight',0):continue
            for group,keys in GROUPS.items():
                p=[(payload.get('model') or {}).get(k) for k in keys]
                q=[(payload.get('market') or {}).get(k) for k in keys]
                y=[outcome(k,r['home_goals'],r['away_goals']) for k in keys]
                valid=lambda values:all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and 0<=v<=1 for v in values)
                if not valid(p) or not valid(q) or any(v is None for v in y):continue
                if abs(sum(p)-1)>.01 or abs(sum(q)-1)>.01:continue
                entry={'at':actual.isoformat(),'model':p,'market':q,'actual':list(map(int,y))}
                grouped.setdefault((r['league'],group),[]).append(entry)
                grouped.setdefault(('*',group),[]).append(entry)
        except (ValueError,TypeError,KeyError):continue
    return {key:result for key,rows in grouped.items() if (result:=choose(rows)) is not None}


def for_prediction(prediction, selections):
    from .blend import GROUPS
    if (prediction.get('ensemble') or {}).get('fitted_weight',0):return {}
    result={}
    for group in GROUPS:
        local=selections.get((prediction.get('league'),group))
        pooled=selections.get(('*',group))
        evidence=local or pooled
        if evidence:
            result[group]={**evidence,'scope':'league' if local else 'pooled_leagues'}
    return result
