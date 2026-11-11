"""A separate live trio, built only from the existing priced model candidates."""
import math
from datetime import datetime, timezone
from itertools import combinations, product

from .quote_provenance import timestamp
from .model.coupons import _coupon, _match_view


def select(entries, now=None):
    now = now or datetime.now(timezone.utc)
    pool, seen = [], set()
    for prediction, choices in entries:
        mid = str(prediction.get('id') or '')
        kickoff = timestamp(prediction.get('kickoff'))
        if not mid or mid in seen or not kickoff or kickoff <= now or prediction.get('completed') or prediction.get('result_only'):
            continue
        valid = []
        for choice in choices:
            p, odds = choice.get('p'), choice.get('odds')
            if (choice.get('odds_source') != 'iddaa' or isinstance(p, bool) or isinstance(odds, bool)
                    or not isinstance(p, (int, float)) or not isinstance(odds, (int, float))
                    or not math.isfinite(p) or not math.isfinite(odds) or not 0 < p < 1 or odds <= 1):
                continue
            try:
                minimum = int(choice.get('min_matches', 1) or 1)
            except (TypeError, ValueError):
                continue
            if minimum > 3:
                continue
            valid.append(choice)
        if valid:
            # Retain the existing candidate prices; cap combinations for predictable cost.
            valid.sort(key=lambda c: c['p'], reverse=True)
            pool.append((prediction, valid[:8]))
            seen.add(mid)
    pool.sort(key=lambda item: max(c['p'] for c in item[1]), reverse=True)
    best = None
    for matches in combinations(pool[:14], 3):
        for choices in product(*(item[1] for item in matches)):
            total = math.prod(c['odds'] for c in choices)
            if not 2.5 <= total <= 4.0:
                continue
            score = math.prod(c['p'] for c in choices)
            if best is None or score > best[0]:
                best = (score, matches, choices)
    picks = []
    if best:
        for (prediction, _), choice in zip(best[1], best[2]):
            view = _match_view(prediction, choice)
            if prediction.get('iddaa_event_id'):
                view['iddaa_event_id'] = prediction['iddaa_event_id']
            picks.append(view)
    result = _coupon('trio', picks, 'Üç farklı maç, gerçek fiyat ve 2,50–4,00 toplam oran; olasılık maçların bağımsızlığı varsayımıyla hesaplanır. Grok bulguları olasılıkları değiştirmez.')
    result.update(target_met=bool(picks), selection_tier='trio' if picks else 'unavailable', publication='live')
    return result
