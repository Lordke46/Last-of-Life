"""Independent, explicitly provisional small-league estimates from licensed history.

Import is opt-in and source-specific. No forecast substitutes stale history for a
current team model. Market-only recommendations remain independently labelled.
"""
import json
from datetime import datetime,timedelta,timezone
from functools import lru_cache
from ..db import session,set_meta,get_meta
from ..scrapers.openfootball import parse_es2,load_json
from .fitted import FittedGoals
from . import predict as P

KEY='licensed_history:1951'


def import_history(conn, document, season, now=None):
    now=now or datetime.now(timezone.utc)
    rows=[r for r in parse_es2(document,season,as_of=now,before=now)
          if r.get('source',{}).get('result_format')!='score_array_status_unverified']
    if len(rows)<120:raise ValueError('At least 120 completed source fixtures are required')
    payload={'rows':rows,'imported_at':now.isoformat(),'license':'CC0-1.0',
             'source':'https://github.com/openfootball/football.json','season':season}
    set_meta(conn,KEY,json.dumps(payload,ensure_ascii=False))
    _fit.cache_clear()
    return len(rows)


@lru_cache(maxsize=8)
def _fit(serialized, day):
    payload=json.loads(serialized)
    cutoff=datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    rows=[]
    for raw in payload['rows']:
        # Date-only results become available the following UTC day, never at invented kickoff times.
        available=datetime.fromisoformat(raw['kickoff']).replace(tzinfo=timezone.utc)+timedelta(days=1)
        if available>cutoff:continue
        rows.append({**raw,'ts':available-timedelta(hours=3),'home_id':None,'away_id':None})
    return payload,rows,FittedGoals(rows,cutoff)


def assess(event, serialized, now=None):
    now=now or datetime.now(timezone.utc)
    base={'available':False,'confidence_status':'not_validated','affects_recommendation':False}
    if not serialized:return {**base,'reason':'licensed_team_history_unavailable'}
    payload,rows,model=_fit(serialized,now.date().isoformat())
    from ..scrapers.iddaa import team_similarity
    names=model.teams
    def resolve(team):
        ranked=sorted(((team_similarity(team,name),name) for name in names),reverse=True)
        if not ranked or ranked[0][0]<.90:return None
        if len(ranked)>1 and ranked[0][0]-ranked[1][0]<.05:return None
        return ranked[0][1]
    home,away=resolve(event['home']),resolve(event['away'])
    info={**base,'source':payload['source'],'license':payload['license'],'season':payload['season'],
          'history_matches':len(rows),'kickoff_precision':'date','imported_at':payload['imported_at']}
    if not home or not away or home==away:return {**info,'reason':'team_identity_unverified'}
    latest=[max((r['ts'] for r in rows if team in (r['home'],r['away'])),default=None) for team in (home,away)]
    if any(t is None or now-t>timedelta(days=45) for t in latest):
        return {**info,'reason':'team_history_stale','last_team_results':[t.date().isoformat() if t else None for t in latest]}
    if not model.available(home,away):return {**info,'reason':'insufficient_team_history'}
    return {**info,'available':True,'reason':'provisional_independent_estimate',
            'model_type':'regularized_poisson_with_fitted_low_score_correction',
            'markets':P.markets(model.matrix(home,away))['markets'],
            'team_match_counts':{'home':model.counts[home],'away':model.counts[away]}}


def for_event(event):
    if str(event.get('competition_id'))!='1951':
        return {'available':False,'reason':'licensed_team_history_unavailable',
                'confidence_status':'not_validated','affects_recommendation':False}
    from ..quote_provenance import timestamp
    kickoff=timestamp(event.get('kickoff'))
    if kickoff and kickoff<=datetime.now(timezone.utc):
        return {'available':False,'reason':'not_prematch','confidence_status':'not_validated','affects_recommendation':False}
    with session() as conn:serialized=get_meta(conn,KEY)
    try:return assess(event,serialized)
    except (ValueError,KeyError,TypeError):
        return {'available':False,'reason':'history_payload_invalid','confidence_status':'not_validated','affects_recommendation':False}


if __name__=='__main__':
    import argparse
    from ..db import init_db
    p=argparse.ArgumentParser();p.add_argument('--file',required=True);p.add_argument('--season',required=True)
    a=p.parse_args();init_db()
    with session() as conn:n=import_history(conn,load_json(a.file),a.season)
    print(f'Imported {n} licensed historical results; recommendation validation remains separate.')
