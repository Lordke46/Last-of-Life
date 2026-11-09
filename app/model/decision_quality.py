"""Auditable eligibility checks around balanced_v2; probabilities/ranking are unchanged."""
import math
from datetime import datetime, timezone
from ..config import MODEL
from ..quote_provenance import fresh, timestamp
from . import predict as P


def comparison(item, source='iddaa', group=None, book=None):
    model, market = item.get('model_p'), item.get('market_p')
    valid = lambda p: isinstance(p,(int,float)) and not isinstance(p,bool) and math.isfinite(p) and 0<=p<=1
    gap = abs(model-market)*100 if valid(model) and valid(market) else None
    return {'model_market_gap_pp':round(gap,2) if gap is not None else None,
            'disagreement_review_required':gap is not None and gap >= MODEL.get('disagreement_review_pp',20),
            'market_probability_source':source if valid(market) else None,
            'devig_method':'multiplicative_normalization' if valid(market) else None,
            'devig_group':group, 'book_sum':round(book,6) if book is not None else None}


def reasons(prediction, market, now=None):
    now = now or datetime.now(timezone.utc)
    at = timestamp(prediction.get('kickoff'))
    item = prediction.get('markets',{}).get(market,{})
    integrity = prediction.get('data_integrity') or {}
    calibration = (prediction.get('calibration_by_market') or {}).get(market) or {}
    out = []
    if not at or at<=now or prediction.get('completed') or prediction.get('result_only'):
        out.append('not_prematch')
    if any(x in str(prediction.get('status','')).lower() for x in ('cancel','postpon','abandon','suspend')):
        out.append('inactive_fixture')
    if not fresh(prediction,market,now):out.append('quote_unverified_or_stale')
    if integrity.get('history_complete') is False:out.append('inconsistent_history')
    if integrity.get('source_checked_at') and integrity.get('source_fresh') is False:out.append('stale_team_context')
    if item.get('validation_status') == 'experimental':out.append('experimental_market')
    if item.get('disagreement_review_required') and not calibration.get('applied'):
        out.append('uncalibrated_model_market_disagreement')
    return out


def apply(prediction, now=None):
    """Apply to real API predictions, not bare probability calculators or historical simulations."""
    if 'kickoff' not in prediction:
        return prediction
    out = dict(prediction)
    eligible, rejected = {}, {}
    for market,item in prediction.get('markets',{}).items():
        if market not in P.PICK_MARKETS:continue
        why = reasons(prediction,market,now)
        if why:rejected[market]=why
        else:
            calib=(prediction.get('calibration_by_market') or {}).get(market) or {}
            eligible[market]=({**item,**P.odds(calib['p']),'raw_p':item['p'],
                              'calibration_applied':True,'calibration_scope':calib.get('scope','league'),
                              'calibration_n':calib.get('n',0)} if calib.get('applied') else dict(item))
    pick = P.best_pick(eligible, actual_odds=prediction.get('iddaa_odds',{}))
    if not pick:
        candidate=P.candidate(eligible,prediction.get('iddaa_odds',{}))
        if candidate:
            pick={**candidate,'policy':'best_available_v3','selection_tier':'best_available',
                  'target_met':False,'probability_source':'model_market_blend'}
    if pick:
        pick.setdefault('selection_tier','preferred')
        pick.setdefault('target_met',True)
    out['pick'] = pick
    if pick:out['alternative']=None
    strongest=P.best_pick(eligible,min_odds=1.15,actual_odds=prediction.get('iddaa_odds',{}))
    if strongest:
        strongest={**strongest,'estimated_edge':round(strongest['p']*strongest['odds']-1,4),'policy':'highest_probability_v1',**P.confidence(strongest['p'],prediction.get('data_n',0),data_integrity=prediction.get('data_integrity'))}
        if not strongest.get('calibration_applied'):
            strongest['confidence']=min(strongest['confidence'],.69)
            strongest['confidence_label']=P.confidence_label(strongest['confidence'])
            strongest['confidence_reason']+='; ileriye dönük kalibrasyon kanıtı yetersiz'
    out['strongest_pick']=strongest
    out['decision'] = {'status':'bet_candidate' if pick else 'no_bet', 'policy':P.PICK_POLICY,
                       'review_threshold_pp':MODEL.get('disagreement_review_pp',20),
                       'rejected_markets':rejected,
                       'reason':'eligible_market' if pick else 'no_eligible_market',
                       'estimated_edge':round(pick['p']*pick['odds']-1,4) if pick else None}
    out['recommendation_status'] = 'recommended' if pick else 'no_bet'
    if not pick:
        out['alternative'] = P.candidate(prediction.get('markets',{}),prediction.get('iddaa_odds',{})) or prediction.get('alternative')
        if out['alternative'] and out['alternative'].get('odds_source')=='iddaa':
            out['alternative'] = {**out['alternative'],'candidate_status':'review_only'}
        out.update(P.confidence(None,prediction.get('data_n',0)))
    else:
        calib = (prediction.get('calibration_by_market') or {}).get(pick['market']) or {}
        status = 'available' if calib.get('applied') else 'insufficient_data'
        out['calibration_status'] = status
        conf = P.confidence(pick['p'],prediction.get('data_n',0),data_integrity=prediction.get('data_integrity'))
        if status == 'insufficient_data':
            conf['confidence'] = min(conf['confidence'],.69)
            conf['confidence_label'] = P.confidence_label(conf['confidence'])
            conf['confidence_reason'] += '; ileriye dönük kalibrasyon kanıtı yetersiz'
        out.update(conf)
    out.setdefault('calibration_status','insufficient_data')
    out['uncertainty'] = {'calibration_status':out['calibration_status'],
                          'data_coverage':prediction.get('data_coverage_score',prediction.get('sample_coverage')),
                          'data_freshness':(prediction.get('data_integrity') or {}).get('source_fresh'),
                          'data_integrity':(prediction.get('data_integrity') or {}).get('history_complete'),
                          'empirical_accuracy_claim':False}
    return out
