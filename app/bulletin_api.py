"""Günlük görünüm: güncel oranlar, kalıcı fikstür/sonuçlar ve kaynak bülten arşivi."""
import json
from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException
from .config import LEAGUES
from .db import session, get_meta
from . import ops, explain, bulletin_archive, quote_provenance
from .model import engine, extra_markets
from .model.blend import blend_prediction, calibration_stats, assess_prediction
from .scrapers.iddaa import load_constraints, match_event
from .model.coupons import TZ
from .model.small_leagues import bulletin_event

router = APIRouter()
LEAGUE_ORDER = ('tur.1', 'eng.1', 'esp.1', 'ger.1', 'ita.1', 'fra.1', 'tur.2',
                'ned.1', 'por.1', 'uefa.champions', 'uefa.europa')
# Liste kartı bunları çizmez. Tam hali /api/match/{id} içindedir.
# 663 maçlık günde markets+factors tek başına ~2.3 MB idi; Hobby gövde tavanı 4.5 MB.
_LIST_HEAVY = {"markets", "factors", "extra_markets", "scores", "no_news_markets",
               "lambda_home", "lambda_away", "calibration_by_market", "data_availability", "blend", "quote_provenance"}
_CARD_ODDS = {"1", "X", "2"}


def _list_item(item: dict) -> dict:
    """Bülten listesi: kart, 1-X-2 ve kupona eklenen tek seçim. Model matrisi yok."""
    needed = set()
    for key in ("pick", "alternative"):
        choice = item.get(key)
        market = choice.get("market") if isinstance(choice, dict) else None
        if market:
            needed.add(market)
    from .match_state import describe
    slim = {key: value for key, value in item.items() if key not in _LIST_HEAVY}
    slim["match_state"] = describe(item)
    markets = item.get("markets") if isinstance(item.get("markets"), dict) else {}
    extra = item.get("extra_markets") if isinstance(item.get("extra_markets"), dict) else {}
    kept = {market: markets[market] for market in needed if market in markets}
    kept_extra = {market: extra[market] for market in needed if market in extra and market not in kept}
    if kept:
        slim["markets"] = kept
    if kept_extra:
        slim["extra_markets"] = kept_extra
    odds = item.get("iddaa_odds") if isinstance(item.get("iddaa_odds"), dict) else {}
    slim["iddaa_odds"] = {market: odds[market] for market in (_CARD_ODDS | needed) if market in odds}
    mins = item.get("iddaa_min_matches") if isinstance(item.get("iddaa_min_matches"), dict) else {}
    slim["iddaa_min_matches"] = {market: mins[market] for market in needed if market in mins}
    return slim


def fixture_view(fixture, source=None):
    """Geçmişe yeni tahmin üretme; gelecek fikstürü gerçek oran yokken de görünür tut."""
    if source is None:
        with session() as conn:
            rows=conn.execute('SELECT market,odd,updated_at FROM odds_log WHERE match_id=?',(str(fixture['id']),)).fetchall()
        source={'odds':{r['market']:r['odd'] for r in rows},'updated_at':max((r['updated_at'] for r in rows),default=None)} if rows else {}
    now = datetime.now(timezone.utc)
    started = datetime.fromisoformat(fixture['kickoff'].replace('Z','+00:00')) <= now
    base = {**fixture, 'id':str(fixture['id']), 'model_match_id':str(fixture['id']), 'league_name':LEAGUES[fixture['league']]['name'],
            'country':LEAGUES[fixture['league']]['country'], 'iddaa_odds':source.get('odds',{}),
            'iddaa_event_id':source.get('id'), 'iddaa_updated_at':source.get('updated_at'),
            'iddaa_min_matches':source.get('min_matches',{}), 'odds_current':False,
            'fixture_only':not bool(source.get('id')), 'is_archived':bool(source), 'pick':None}
    if started or fixture.get('completed'):
        out = bulletin_event({**base, 'small_league':True, 'result_only':True})
        out['summary']=explain.summarize(out)
        return out
    prediction=engine.get_model(fixture['league']).predict(fixture['home'],fixture['away'],
                    datetime.fromisoformat(fixture['kickoff'].replace('Z','+00:00')))
    out={**base,**blend_prediction(prediction,{}),'model_only':True}
    out['summary']=explain.summarize(out)
    return out


def build_bulletin(day: date, category: str, league: str | None = None):
    start=datetime.combine(day,datetime.min.time(),TZ).astimezone(timezone.utc)
    end=start+timedelta(days=1)
    now=datetime.now(timezone.utc)
    with session() as conn:
        raw=conn.execute('SELECT e.*,o.market,o.odd FROM iddaa_events e LEFT JOIN iddaa_odds o ON o.event_id=e.id '
                         'WHERE e.kickoff>=? AND e.kickoff<? ORDER BY e.kickoff,e.id',
                         (start.isoformat(),end.isoformat())).fetchall()
        fixtures=[]
        if category!='other':
            keys=[league] if league else list(LEAGUES)
            marks=','.join('?' for _ in keys)
            fixtures=[dict(row) for row in conn.execute('SELECT * FROM matches WHERE kickoff>=? AND kickoff<? '
                      f'AND league IN ({marks}) ORDER BY kickoff,id',[start.isoformat(),end.isoformat(),*keys])]
        histories={}
        if fixtures:
            ids=[str(m['id']) for m in fixtures];marks=','.join('?' for _ in ids)
            for row in conn.execute(f'SELECT match_id,market,odd,updated_at FROM odds_log WHERE match_id IN ({marks})',ids):
                source=histories.setdefault(row['match_id'],{'odds':{},'updated_at':row['updated_at']})
                source['odds'][row['market']]=row['odd']
                source['updated_at']=max(source['updated_at'],row['updated_at'])
        observed=calibration_stats(conn)
        constraints=load_constraints(conn)
        provenance=quote_provenance.load(conn,[r['id'] for r in raw])
        archived=bulletin_archive.load_day(conn,day)
        updated=get_meta(conn,'iddaa_last_update')
        coverage=json.loads(get_meta(conn,'iddaa_stats') or 'null')
        day_rows=conn.execute('SELECT kickoff FROM iddaa_events') if category=='all' else conn.execute('SELECT kickoff FROM iddaa_events WHERE is_small=?',(int(category=='other'),))
        day_stamps=[r['kickoff'] for r in day_rows]
        if category!='other':
            keys=[league] if league else list(LEAGUES);marks=','.join('?' for _ in keys)
            day_stamps += [r['kickoff'] for r in conn.execute(f'SELECT kickoff FROM matches WHERE league IN ({marks}) AND kickoff>=? AND kickoff<?',
                            [*keys,(now-timedelta(days=30)).isoformat(),(now+timedelta(days=31)).isoformat()])]
        days=sorted(set(bulletin_archive.available_days(conn)) |
                    {datetime.fromisoformat(t.replace('Z','+00:00')).astimezone(TZ).date().isoformat() for t in day_stamps})
    def belongs(event):
        return (category=='all' or bool(event.get('is_small',str(event['league']).startswith('small:'))) == (category=='other')) and (not league or event['league']==league)
    events={}
    active=set()
    for row in raw:
        row=dict(row)
        if not belongs(row):continue
        event=events.setdefault(row['id'],{**dict(row),'odds':{},'min_matches':constraints.get(row['id'],{})})
        if row['market'] and row['odd']:event['odds'][row['market']]=row['odd']
        active.add(row['id'])
    for event_id,event in archived.items():
        if event_id not in events and belongs(event):events[event_id]=event
    by_id={str(m['id']):m for m in fixtures}
    covered=set();matches=[]
    for event_id,event in events.items():
        fixture=by_id.get(str(event.get('match_id')))
        if fixture is None and event['league'] in LEAGUES:fixture=match_event(event,fixtures)[0]
        if fixture and str(fixture['id']) in covered:continue
        if fixture:covered.add(str(fixture['id']))
        current=event_id in active and datetime.fromisoformat(event['kickoff'].replace('Z','+00:00'))>now and not (fixture or {}).get('completed')
        if fixture and not current:
            matches.append(fixture_view(fixture,event));continue
        base={k:event.get(k) for k in ('league','kickoff','home','away','country','competition_id')}
        base.update(id=str(fixture['id']) if fixture else 'iddaa:'+event_id,
                    league_name=LEAGUES.get(event['league'],{}).get('name') or event.get('league_name') or event['league'],
                    country=LEAGUES.get(event['league'],{}).get('country',event.get('country','')),
                    iddaa_event_id=event_id,iddaa_updated_at=event.get('updated_at'),
                    iddaa_min_matches=event.get('min_matches',{}),odds_current=current,
                    is_archived=event_id not in active,bulletin_present=event_id in active,iddaa_odds=event.get('odds',{}),
                    quote_provenance=quote_provenance.for_event(provenance,event_id) if current else None)
        if fixture and current:
            prediction=engine.get_model(event['league']).predict(fixture['home'],fixture['away'],
                          datetime.fromisoformat(event['kickoff'].replace('Z','+00:00')))
            out={**base,**blend_prediction(prediction,event['odds'])}
            out['extra_markets']=extra_markets.for_match(out['lambda_home'],out['lambda_away'],event['odds'])
            out=assess_prediction(out,observed)
            out['summary']=explain.summarize(out);matches.append(out)
        else:
            out=bulletin_event({**base,'small_league':True,'bulletin_only':True})
            out['summary']=explain.summarize(out)
            matches.append(out)
    for fixture in fixtures:
        if str(fixture['id']) not in covered:matches.append(fixture_view(fixture,histories.get(str(fixture["id"]),{})))
    from .recommendation_history import decorate
    decorate(matches)
    groups={}
    for item in matches:
        group=groups.setdefault(item['league'],{'league':item['league'],'name':item['league_name'],
                                                'country':item.get('country') or '','matches':[]})
        group['matches'].append(item)
    rank={key:i for i,key in enumerate(LEAGUE_ORDER)}
    ordered=sorted(groups.values(),key=lambda g:(rank.get(g['league'],999),g['country'],g['name']))
    for group in ordered:
        group['matches'].sort(key=lambda m:(-(m.get('confidence') or 0),m['kickoff'],m['id']))
        group['matches']=[_list_item(m) for m in group['matches']]
    past=day<now.astimezone(TZ).date()
    return {'day':day.isoformat(),'category':category,'groups':ordered,'total':len(matches),
            'modeled':sum(not m.get('small_league') for m in matches),'limited':sum(bool(m.get('small_league')) for m in matches),
            'fixture_only':sum(bool(m.get('fixture_only')) for m in matches),
            'archived':sum(bool(m.get('is_archived')) for m in matches),
            'coverage':coverage,'updated_at':updated,'available_days':days,
            'empty_reason':('Bu tarih için kaydedilmiş bülten arşivi bulunmuyor.' if past else
                            'Kaynakta bu gün için henüz fikstür veya bülten kaydı bulunmuyor.') if not matches else None}


@router.get('/api/bulletin')
def bulletin(day: date | None = None, category: str = 'main', league: str | None = None):
    if category not in ('all','main','other'):raise HTTPException(400,'Bilinmeyen bülten türü')
    if league and (category=='other' or league not in LEAGUES):raise HTTPException(400,'Bilinmeyen lig')
    target=day or datetime.now(TZ).date()
    if target>=datetime.now(TZ).date():
        from .bulletin_refresh import refresh_if_stale
        refresh_if_stale()
    return ops.cached(('bulletin',target,category,league),lambda:build_bulletin(target,category,league))
