"""Offline, chronological comparison on local openfootball JSON files (CC0).
Usage: python scripts/benchmark_ensemble.py --train a.json --test b.json --output report.json
No production DB, fabricated odds, ROI, or future-results training.
"""
import argparse
import hashlib
import subprocess
import json
import sys
from collections import Counter
from datetime import datetime,timezone,timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.model.engine import LeagueModel
from app.model.fitted import FittedGoals
from app.model.ensemble import prepare,loss,EVALUATION_MARKETS
from app.model import predict as P
from app.model.report import metrics
from app.model.settlement import outcome
from app.model.ratings import played_before


def read(path):
    out=[]
    for m in json.loads(Path(path).read_text())['matches']:
        score=m.get('score',{})
        goals=score.get('ft') if isinstance(score,dict) else score
        if not isinstance(goals,list) or len(goals)!=2 or any(type(g) is not int or g<0 for g in goals):continue
        # Date-only input: all matches at midnight; 3h result lag excludes same-day results.
        ts=datetime.fromisoformat(m['date']).replace(tzinfo=timezone.utc)
        out.append({'id':m['date']+m['team1']+m['team2'],'league':'benchmark','ts':ts,
                    'kickoff':ts.isoformat(),'home':m['team1'],'away':m['team2'],
                    'home_goals':goals[0],'away_goals':goals[1],'completed':1})
    return sorted(out,key=lambda m:m['ts'])


def run(train,test, external_root=None):
    results={k:[] for k in ('legacy','fitted','adaptive')}
    if external_root:
        sys.path.insert(0,external_root)
        import pandas as pd
        from football_mle.data import prepare_data
        from football_mle.fitting import fit
        import math
        results['football_mle']=[]
    binary={k:[] for k in results}
    recommendations={k:[] for k in results}
    weights=Counter()
    refit=None
    all_rows=train+test
    for m in test:
        hist=[r for r in all_rows if played_before(r,m['ts'])]
        if refit is None or (m['ts']-refit).days>=28:
            model=LeagueModel('benchmark',hist,[],{})
            fitted,selection=prepare(model,m['ts'])
            refit=m['ts']
            if external_root:
                df=pd.DataFrame([{**r,'date':r['ts'].replace(tzinfo=None)} for r in hist])
                data=prepare_data(df,reference_date=m['ts'].replace(tzinfo=None),xi=math.log(2)/200)
                external=fit(data,with_rho=True,model_name='Dixon-Coles')
        old=model.predict(m['home'],m['away'],m['ts'],history=hist,detail=False)['markets']
        new=P.markets(fitted.matrix(m['home'],m['away']))['markets'] if fitted.available(m['home'],m['away']) else old
        w=selection['fitted_weight'] if fitted.available(m['home'],m['away']) else 0
        weights[str(w)]+=1
        combined={k:{'p':(1-w)*v['p']+w*new[k]['p']} for k,v in old.items()}
        actual=0 if m['home_goals']>m['away_goals'] else 1 if m['home_goals']==m['away_goals'] else 2
        variants={'legacy':old,'fitted':new,'adaptive':combined}
        if external_root:
            if external.converged and fitted.available(m['home'],m['away']):
                lh,la=external.rates(m['home'],m['away'])
                rho=max(-.99/max(lh,la),min(external.rho,.99/(lh*la),.99))
                variants['football_mle']=P.markets(P.score_matrix(lh,la,rho))['markets']
            else:
                variants['football_mle']=old
        for name,mk in variants.items():
            results[name].append({'probs':[mk[k]['p'] for k in ('1','X','2')],'actual':actual})
            binary[name].append(loss(mk,m))
            pick=max(P.PICK_MARKETS,key=lambda k:mk[k]['p'])
            recommendations[name].append((mk[pick]['p'],int(outcome(pick,m['home_goals'],m['away_goals']))))
    out={}
    for name,rows in results.items():
        rec=recommendations[name]
        out[name]={**metrics(rows),'multi_market_brier':round(sum(binary[name])/len(rows),6),
                   'unpriced_top_probability_hit_rate':round(sum(w for _,w in rec)/len(rec),4),
                   'unpriced_top_probability_mean':round(sum(p for p,_ in rec)/len(rec),4)}
    return {'scope':'held_out_next_season_rolling_origin','point_in_time_verified':False,
            'note':'No historical quotes: unpriced top-choice hit rate is NOT iddaa recommendation performance or ROI.',
            'train_n':len(train),'test_n':len(test),'from':test[0]['kickoff'],'to':test[-1]['kickoff'],
            'adaptive_weights':dict(weights),'results':out}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--train',required=True);parser.add_argument('--test',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--football-mle-root')
    args=parser.parse_args()
    result=run(read(args.train),read(args.test),args.football_mle_root)
    result['inputs']=[{'path':str(Path(p).name),'sha256':hashlib.sha256(Path(p).read_bytes()).hexdigest()} for p in (args.train,args.test)]
    if args.football_mle_root:
        result['external_reference']={'repository':'https://github.com/jpmouracodex/football-mle',
            'commit':subprocess.check_output(['git','-C',args.football_mle_root,'rev-parse','HEAD'],text=True).strip(),
            'fit':'joint_Dixon_Coles_MLE_time_half_life_200_days',
            'fallback':'legacy_for_unseen_or_under_8_matches_or_unconverged_fit'}
    Path(args.output).write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps(result,ensure_ascii=False))
