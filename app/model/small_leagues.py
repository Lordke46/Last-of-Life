"""Small-league event details from the bulletin, without invented team data."""
from .blend import devig

MARKET_KEYS = (
    "1", "X", "2", "1X", "12", "X2", "Ü1.5", "A1.5", "Ü2.5", "A2.5",
    "Ü3.5", "A3.5", "KG Var", "KG Yok", "Ev 0.5Ü", "Dep 0.5Ü",
)
ODDS_GROUPS = {
    "1x2": ("1", "X", "2"),
    "ou1.5": ("Ü1.5", "A1.5"),
    "ou2.5": ("Ü2.5", "A2.5"),
    "ou3.5": ("Ü3.5", "A3.5"),
    "btts": ("KG Var", "KG Yok"),
}


def _market(probability: float | None = None) -> dict:
    return {
        "p": round(probability, 4) if probability is not None else None,
        "fair": round(1 / probability, 2) if probability else None,
        "suggested": None,
        "model_p": None,
        "market_p": round(probability, 4) if probability is not None else None,
        "model_available": False,
    }


def bulletin_event(event: dict) -> dict:
    odds = event.get("iddaa_odds") or {}
    market_keys = list(MARKET_KEYS) + [key for key in odds if key not in MARKET_KEYS]
    markets = {key: _market() for key in market_keys}
    available_groups = []

    for group, keys in ODDS_GROUPS.items():
        probabilities = devig([odds.get(key) for key in keys])
        if probabilities is None:
            continue
        available_groups.append(group)
        for key, probability in zip(keys, probabilities):
            markets[key] = _market(probability)

    # Çifte şans olasılıkları yalnızca tam ve geçerli 1X2 bülten oranlarından türetilir.
    if "1x2" in available_groups:
        p1, px, p2 = (markets[key]["p"] for key in ("1", "X", "2"))
        for key, probability in (("1X", p1 + px), ("12", p1 + p2), ("X2", px + p2)):
            markets[key] = _market(probability)

    event_id = str(event.get("iddaa_event_id") or event.get("event_id") or event.get("id", ""))
    result = {
        **event,
        "id": str(event.get("id", event_id)),
        "event_id": event_id,
        "iddaa_event_id": event_id,
        "markets": markets,
        "iddaa_odds": odds,
        "lambda_home": None,
        "lambda_away": None,
        "pick": None,
        "confidence": None,
        "confidence_label": "Model güveni yok",
        "data_quality": None,
        "data_availability": {
            "market_odds": bool(odds),
            "market_probabilities": bool(available_groups),
            "xg": False,
            "form": False,
            "standings": False,
            "scores": False,
            "team_news": False,
        },
        "blend": {"applied": bool(available_groups), "weight": 1 if available_groups else 0,
                  "groups": available_groups},
    }

    from .licensed_history import for_event
    result['independent_team_analysis']=for_event(event)
    from ..match_state import describe
    result["match_state"]=describe(result)
    from ..quote_provenance import fresh
    from datetime import datetime,timezone
    from ..quote_provenance import timestamp
    at=timestamp(event.get('kickoff'))
    if (event.get('odds_current') is False or event.get('completed') or (at and at<=datetime.now(timezone.utc))
            or (event.get('quote_provenance') is not None and not any(fresh(event,k) for k,v in markets.items() if v.get('p') is not None and k in odds))):
        result['decision']={'status':'no_bet','reason':'no_current_prematch_quotes'}
        return result
    from .predict import candidate, PICK_MIN_P, PICK_MIN_ODDS, PICK_MAX_ODDS
    eligible={k:v for k,v in markets.items() if event.get('quote_provenance') is None or fresh(event,k)}
    selection = candidate(eligible, odds)
    if selection:
        selection.update(candidate_type='market_candidate', probability_source='iddaa_market',
                         policy='market_candidate_v1', model_available=False)
        # Keep a below-threshold choice available without presenting it as a preferred pick.
        target_met = (selection['p'] >= PICK_MIN_P
                      and PICK_MIN_ODDS <= selection['odds'] <= PICK_MAX_ODDS)
        selection.update(target_met=target_met,
                         selection_tier='preferred' if target_met else 'best_available')
        if target_met:
            selection['candidate_status']='market_candidate'
        result['pick']=selection
    return result


def market_favorite(prediction: dict) -> dict | None:
    """Return a plainly labeled 1X2 market favorite when the bulletin supports one.

    This is an informational market candidate, not a model prediction or a value bet.
    It requires the complete, validated source 1X2 odds used by ``bulletin_event``.
    """
    odds = prediction.get("iddaa_odds", {})
    keys = ("1", "X", "2")
    probabilities = devig([odds.get(key) for key in keys])
    if probabilities is None:
        return None

    choices = [(key, probability, odds[key])
               for key, probability in zip(keys, probabilities)]
    market, probability, odd = max(choices, key=lambda choice: choice[1])
    if probability < 0.5:
        return None

    return {
        "market": market,
        "p": probability,
        "suggested": None,
        "market_source": "iddaa",
        "market_source_label": "İddaa piyasa oranları",
        "probability_source": "iddaa_market",
        "odds": odd,
        "odds_source": "iddaa",
        "candidate_type": "market_favorite",
        "confidence": None,
        "confidence_label": "Düşük · model güveni yok",
        "model_available": False,
    }


def predict_event(event: dict) -> dict:
    """Kuponlarda da yalnız doğrulanmış bülten olasılıklarını kullan."""
    return bulletin_event(event)
