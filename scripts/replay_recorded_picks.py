"""Counterfactual selection replay on immutable recorded forecasts/prices.
This does not replay quote freshness, injuries or the new fitted goal model.
"""
import argparse,json,sqlite3,sys
from pathlib import Path
from datetime import datetime,timezone,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db
from app.model import calibration,predict as P,snapshots
from app.model.settlement import outcome
from app.model.performance import _wilson


def run(data):
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    conn.executescript(db.SCHEMA);conn.executescript(snapshots.SCHEMA)
    matches={str(m['id']):m for m in data['matches']}
    for m in matches.values():
        keys=list(m);conn.execute('INSERT INTO matches('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',list(m.values()))
    for s in data['snapshots']:
        conn.execute('INSERT INTO prediction_snapshots(match_id,league,kickoff,captured_at,model_version,parameters,payload) VALUES(?,?,?,?,?,?,?)',
                     (s['match_id'],s['league'],s['kickoff'],s['captured_at'],s['model_version'],'{}',json.dumps(s['payload'])))
    rows={k:[] for k in ('original_published','balanced_replay','highest_probability_replay')}
    now=datetime.fromisoformat(data['exported_at'])
    cohort=0
    for s in data['snapshots']:
        m=matches.get(str(s['match_id']));payload=s['payload']
        parse=lambda t:datetime.fromisoformat(t.replace('Z','+00:00'))
        if not m or not m['completed'] or m['home_goals'] is None or m['away_goals'] is None:continue
        start,actual,captured=map(parse,(s['kickoff'],m['kickoff'],s['captured_at']))
        if not captured<min(start,actual) or max(start,actual)+timedelta(hours=3)>now:continue
        if any(x in str(m.get('status','')).lower() for x in ('cancel','postpon','abandon','suspend')):continue
        mk={k:P.odds(p) for k,p in (payload.get('blend') or {}).items() if type(p) in (float,int) and 0<p<1}
        quotes=payload.get('odds') or {}
        if not mk or not quotes:continue
        cohort+=1
        stats=calibration.load(conn,now=captured)
        calibrated={k:P.odds(calibration.calibrate(v['p'],s['league'],k,stats)['p']) for k,v in mk.items()}
        policies={'original_published':payload.get('pick'),
                  'balanced_replay':P.best_pick(calibrated,actual_odds=quotes),
                  'highest_probability_replay':P.best_pick(calibrated,min_odds=1.15,actual_odds=quotes)}
        for name,pick in policies.items():
            if not pick or pick.get('odds_source')!='iddaa':continue
            price=quotes.get(pick['market'])
            if type(price) not in (int,float) or abs(price-pick['odds'])>.011:continue
            won=outcome(pick['market'],m['home_goals'],m['away_goals'])
            if won is None:continue
            rows[name].append({'id':s['match_id'],'p':pick['p'],'odds':price,'won':int(won)})
    def summary(values):
        n=len(values);w=sum(v['won'] for v in values)
        return {'n':n,'won':w,'hit_rate':round(w/n,4) if n else None,'interval95':_wilson(w,n),
                'mean_odds':round(sum(v['odds'] for v in values)/n,4) if n else None,
                'predicted_rate':round(sum(v['p'] for v in values)/n,4) if n else None,
                'roi':round(sum(v['won']*v['odds']-1 for v in values)/n,4) if n else None}
    common=set.intersection(*(set(v['id'] for v in vals) for vals in rows.values()))
    conn.close()
    return {'scope':'counterfactual_selection_on_recorded_probabilities_and_prices',
            'exported_at':data['exported_at'],'eligible_quote_cohort':cohort,
            'all_available':{k:summary(v) for k,v in rows.items()},
            'same_matches':{k:summary([r for r in v if r['id'] in common]) for k,v in rows.items()},
            'limitations':['No quote provenance/freshness replay; candidates may be rejected by current production gates.',
                          'Calibration uses only results available before original capture time.',
                          'Stored probabilities are existing model versions; this does not validate the new fitted goal model.',
                          'Counterfactual replay is development evidence, not forward performance or a profitability guarantee.']}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    result=run(json.loads(Path(a.input).read_text()));Path(a.output).write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
