"""Gerçek fiyatlı seçenekleri isabet / fiyat avantajı için karşılaştıran açıklama katmanı.

Olasılıkları değiştirmez, üçüncü taraf tahmini çekmez. Fiyat hareketi ve model/piyasa
uyumu gözlenen bilgidir; başarı garantisi veya olasılığa eklenen bir bonus değildir.
"""
import math
from datetime import datetime, timedelta, timezone

from ..quote_provenance import fresh as quotes_fresh
from .decision_quality import reasons
from ..config import MODEL
from .predict import PICK_MIN_ODDS, PICK_MAX_ODDS, PICK_MIN_P, PICK_MARKETS


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _probability(value):
    return _number(value) and 0 < value < 1


def _ts(value):
    try:
        at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return at if at.tzinfo else None
    except (TypeError, ValueError):
        return None


def analyse(prediction, now=None):
    now = now or datetime.now(timezone.utc)
    kickoff = _ts(prediction.get('kickoff'))
    updated = _ts(prediction.get('iddaa_updated_at'))
    fresh = any(quotes_fresh(prediction,market,now) for market in prediction.get('markets',{}) if market in prediction.get('iddaa_odds',{}))
    actionable = bool(fresh and kickoff and kickoff > now and prediction.get('odds_current') is not False)
    small = bool(prediction.get('small_league'))
    n = prediction.get('data_n')
    warnings = []
    if not fresh:
        warnings.append('Oran zamanı güncel veya doğrulanmış değil; seçenekler bilgi amaçlıdır.')
    if not kickoff or kickoff <= now:
        warnings.append('Maç başlamış veya saati doğrulanamamış; yeni seçim önerilmez.')
    if small:
        warnings.append('Takım modeli yok; yüzdeler marjı temizlenmiş piyasa olasılıklarıdır.')
    elif not _number(n) or n < 12:
        warnings.append('Takım geçmişi sınırlı; modelin hata payı daha yüksek olabilir.')
    if (prediction.get('data_integrity') or {}).get('history_complete') is False:
        warnings.append('Sezon maç kayıtları kaynak puan tablosuyla uyuşmuyor; veri tamlığı doğrulanamadı.')
    if any(v.get('disagreement_review_required') for v in prediction.get('markets',{}).values()):
        warnings.append('Büyük model–piyasa ayrışması var; bu fark doğrulanmış fiyat avantajı değildir.')
    options = []
    for market, item in prediction.get('markets', {}).items():
        raw_p = item.get('p')
        calibrated=(prediction.get('calibration_by_market') or {}).get(market) or {}
        p=calibrated['p'] if calibrated.get('applied') else raw_p
        quote = prediction.get('iddaa_odds', {}).get(market)
        if market not in PICK_MARKETS or not _probability(p) or not _number(quote) or quote <= 1:
            continue
        model_p = item.get('model_p', p if not small else None)
        market_p = item.get('market_p')
        gap = abs(model_p - market_p) if _probability(model_p) and _probability(market_p) else None
        move = prediction.get('iddaa_movement', {}).get(market) or {}
        opening = move.get('open')
        movement = None
        if (_number(opening) and opening > 1 and _number(move.get('current'))
                and abs(move['current'] - quote) < .011 and _ts(move.get('opened_at'))
                and _ts(move['opened_at']) <= now):
            movement = {'first_observed': opening, 'current': quote,
                        'change_pct': round((quote / opening - 1) * 100, 1)}
        options.append({'market': market, 'p': p, 'odds': quote,
                        'fair_odds': round(1 / p, 2), 'break_even_probability': round(1 / quote, 4),
                        'estimated_value': round(p * quote - 1, 4),
                        'model_p': model_p if _probability(model_p) else None,
                        'market_p': market_p if _probability(market_p) else None,
                        'disagreement_pp': round(gap * 100, 1) if gap is not None else None,
                        'model_market_disagree': gap is not None and gap*100 >= MODEL.get('disagreement_review_pp',20),
                        'movement': movement,
                        'in_preferred_band': PICK_MIN_ODDS <= quote <= PICK_MAX_ODDS,
                        'raw_p':raw_p, 'calibration_applied':bool(calibrated.get('applied')),
                        'meets_probability_threshold': None if small else p >= PICK_MIN_P,
                        'actionable': actionable and not reasons(prediction,market,now)})
    in_band = [c for c in options if c['in_preferred_band']]
    # Fiyat avantajı ayrı sıralamadır; varsayılan isabet listesini veya Banko'yu kapatmaz.
    by_probability = sorted(in_band, key=lambda c: (-c['p'], -c['estimated_value'], c['market']))[:3]
    by_value = sorted(in_band, key=lambda c: (-c['estimated_value'], -c['p'], c['market']))[:3]
    return {'version': 'comparison_v1', 'preferred_odds': [PICK_MIN_ODDS, PICK_MAX_ODDS],
            'probability_source': 'iddaa_market' if small else 'model_market_blend' if prediction.get('blend', {}).get('applied') else 'model',
            'quotes_fresh': fresh, 'actionable': actionable, 'warnings': warnings,
            'by_probability': by_probability, 'by_value': by_value,
            'note': 'Adil oran 1/olasılık; fiyat farkı tahmine bağlıdır. Oran düşüşü tek başına başarı kanıtı değildir.'}
