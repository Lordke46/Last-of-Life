"""Günlük, ligler arası kupon seçimi (Banko + Sürpriz).

Olasılıklar iddaa.com oranlarıyla harmanlıdır (model/blend.py; oran yoksa model aynen).
Otomatik seçimler yalnız gerçek iddaa oranlarını kullanır. Eski kayıt/aday yardımcıları
model fiyatını ayrı kaynak etiketiyle koruyabilir; bunlar otomatik kupona alınmaz.
Gün sınırı Türkiye saatidir (arayüzdeki saatler de yerel gösterilir).
"""
import math
import heapq
from datetime import date, datetime, timezone, timedelta
from itertools import combinations, product
from zoneinfo import ZoneInfo

from ..config import LEAGUES
from ..db import session
from . import engine, calibration
from . import predict as P
from .blend import blend_predictions
from .settlement import SUPPORTED_MARKETS
from .. import quote_provenance
from . import decision_quality
from ..scrapers.iddaa import load_constraints

TZ = ZoneInfo("Europe/Istanbul")
LOOKAHEAD_DAYS = 60

# Kupon hedefleri. Amaç: toplam oran tabanının üstünde kalan kuponlar arasında ortak tutma
# olasılığını en yükseğe çıkarmak. iddaa her pazara ~%20 marj bastığından (Σ1/oran ≈ 1,20)
# aynı toplam orana daha az bacakla ulaşmak her zaman daha olasıdır; bu yüzden sağlayıcının
# MBS 1–2 verdiği maçlar az bacaklı kuponda doğal olarak öne çıkar.
TIERS = {
    "banko": {
        "picks": (1, 3),            # tek maç yalnız MBS 1 verilen seçimde mümkündür
        "leg_odds": (1.10, 2.00),
        "leg_min_p": .45,
        "total_odds": (1.80, 4.00),
        "min_total_p": .25,
        "pool": 14, "alternatives": 3,
        "fallback": "in_band_first",   # hedef tutmazsa önce yüksek olasılıklı bacaklar
    },
    "surprise": {
        "picks": (2, 4),
        "leg_odds": (1.40, 3.50),
        "leg_min_p": .33,           # şansa değil, hâlâ makul olasılığa dayanan yüksek oran
        "total_odds": (4.50, 15.0),
        "min_total_p": .07,
        "pool": 14, "alternatives": 3,
        "fallback": "total_first",     # hedef tutmazsa önce toplam oran aralığı
    },
}
LOW_MBS_BONUS = 4  # MBS ≤ 2 maçlardan havuza ek alınacak en iyi aday sayısı
BANKO_MIN_P, BANKO_MIN_ODDS, BANKO_MAX_ODDS = TIERS["banko"]["leg_min_p"], *TIERS["banko"]["leg_odds"]
BANKO_MIN_TOTAL_P = TIERS["banko"]["min_total_p"]
BANKO_MIN_TOTAL_ODDS, BANKO_MAX_TOTAL_ODDS = TIERS["banko"]["total_odds"]
BANKO_MIN_EDGE = None  # İsabet önceliğinde pozitif beklenen değer bir eleme koşulu değildir.
BANKO_MIN_DATA_N = 12
BANKO_PICKS, BANKO_POOL, BANKO_ALTERNATIVES = TIERS["banko"]["picks"], TIERS["banko"]["pool"], TIERS["banko"]["alternatives"]
SURPRISE_MIN_P, SURPRISE_MAX_P = TIERS["surprise"]["leg_min_p"], 1.0
SURPRISE_MIN_ODDS, SURPRISE_MAX_ODDS = TIERS["surprise"]["leg_odds"]
SURPRISE_MIN_TOTAL_ODDS, SURPRISE_MAX_TOTAL_ODDS = TIERS["surprise"]["total_odds"]
SURPRISE_PICKS, SURPRISE_POOL, SURPRISE_ALTERNATIVES = TIERS["surprise"]["picks"], TIERS["surprise"]["pool"], TIERS["surprise"]["alternatives"]
SURPRISE_MARKETS = SUPPORTED_MARKETS
SELECTION_POLICY = 'hit_probability_v4'

MAX_COUPON_ODDS_AGE = timedelta(hours=2)


def _odds_age_minutes(stamp) -> int | None:
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - when).total_seconds() // 60))


def _odds_fresh(prediction: dict) -> bool:
    """Kupon için yalnız son 2 saat içinde yenilenmiş bülten kabul edilir.

    Test/elle kurulan prediction'larda iddaa kaynak alanı yoktur. Gerçek
    blend_predictions çıktısında iddaa_event_id ve iddaa_updated_at bulunur.
    """
    if prediction.get('quote_provenance') is not None:
        return any(quote_provenance.fresh(prediction,key) for key in prediction.get('iddaa_odds',{}))
    if "iddaa_event_id" not in prediction:
        return True
    stamp = prediction.get("iddaa_updated_at")
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return now - MAX_COUPON_ODDS_AGE <= when <= now + timedelta(minutes=5)
    except (TypeError, ValueError):
        return False



def _choices(prediction: dict, iddaa_odds: dict) -> list[dict]:
    """Bir maçın seçilebilir pazarları; oran = iddaa varsa iddaa, yoksa Claude oranı."""
    out = []
    if iddaa_odds and not _odds_fresh(prediction):
        return out  # bayat/kimliği belirsiz fiyatla otomatik kupon yok
    available = {**prediction.get("markets", {}), **prediction.get("extra_markets", {})}
    for market, item in available.items():
        if market not in SUPPORTED_MARKETS:
            continue  # ilk yarı/handikap için ortak sonuç verisi henüz tutulmuyor
        if item.get("p") is None:  # korner/kart için geçmiş veri yok; oranı kupona model gibi sokma
            continue
        claude = item.get("suggested")
        theirs = iddaa_odds.get(market)
        if prediction.get("small_league"):
            # Small-league probabilities must come from a complete devigged group
            # in small_leagues.bulletin_event, and the selected market needs its own quote.
            if (not prediction.get("data_availability", {}).get("market_probabilities")
                    or item.get("market_p") is None or not theirs or theirs < 1.01):
                continue
        elif not claude or claude < 1.01:
            continue
        if prediction.get('quote_provenance') is not None and not quote_provenance.fresh(prediction, market):
            continue
        odds, source = (round(theirs, 2), "iddaa") if theirs and theirs >= 1.01 else (claude, "claude")
        if prediction.get('quote_provenance') is not None and decision_quality.reasons(prediction,market):
            continue
        if item.get('disagreement_review_required') and not ((prediction.get('calibration_by_market') or {}).get(market) or {}).get('applied'):
            continue
        calib = (prediction.get("calibration_by_market") or {}).get(market) or {}
        calibrated = bool(calib.get("applied") and calib.get("n", 0) >= calibration.MIN_SAMPLES)
        out.append({"market": market, "p": calib["p"] if calibrated else item["p"], "raw_p": item["p"],
                    "calibration_n": calib.get("n", 0), "calibration_applied": calibrated,
                    "odds": odds, "odds_source": source,
                    "iddaa_odds": theirs if source == "iddaa" else None, "claude_odds": claude,
                    "market_p": item.get("market_p"), "market_only":item.get("market_only",prediction.get("small_league",False)),
                    "probability_source":item.get("probability_source"),
                    "fair": item.get("fair"), "min_matches": prediction.get("iddaa_min_matches", {}).get(market, 1)})
    return out


def _match_view(prediction: dict, choice: dict) -> dict:
    view = {"id": prediction["id"], "league": prediction["league"],
            "league_name": prediction.get("league_name") or LEAGUES.get(prediction["league"], {}).get("name", prediction["league"]),
            "kickoff": prediction["kickoff"],
            "home": prediction["home"], "away": prediction["away"], **choice,
            "model_probability": prediction.get("markets", {}).get(choice["market"], {}).get("model_p", choice.get("raw_p", choice["p"])),
            "market_probability": prediction.get("markets", {}).get(choice["market"], {}).get("market_p"),
            "data_n": prediction.get("data_n", 0),
            "news_effect": (prediction.get("factors") or {}).get("news", {}).get("log_adj"),
            "probability_source": choice.get("probability_source") or ("iddaa_market" if prediction.get("small_league") else
                                  "calibrated" if choice.get("calibration_applied") else
                                  "blend" if prediction.get("blend", {}).get("applied") else "model")}
    if prediction.get("small_league") or choice.get("market_only"):
        view["small_league"] = True
    return view


def small_league_recommendation(prediction: dict, choices: list[dict] | None = None) -> dict | None:
    """Küçük ligdeki maç için mevcut düşük güven seçim kuralına uyan en olası pazarı bul."""
    if prediction.get("small_league") and not prediction.get("data_availability", {}).get("market_probabilities"):
        return None
    choices = choices if choices is not None else _choices(prediction, prediction.get("iddaa_odds", {}))
    valid = [c for c in choices if c['market'] in SUPPORTED_MARKETS and c['p'] >= .50
             and c['odds_source']=='iddaa' and 1.40 <= c['odds'] <= 2.30]
    return max(valid, key=lambda c: (c["p"], -c["odds"])) if valid else None


def _coupon(kind: str, picks: list[dict], note: str) -> dict:
    sources = {p["odds_source"] for p in picks}
    source = sources.pop() if len(sources) == 1 else ("karışık" if sources else None)
    model_picks = [p for p in picks if not (p.get("small_league") or p.get("market_only"))]
    calibrated = sum(bool(p.get("calibration_applied")) for p in model_picks)
    calibration_status = ("not_applicable" if picks and not model_picks else
                          "complete" if model_picks and calibrated == len(model_picks) else
                          "limited" if model_picks else "unavailable")
    calibration_note = None
    if kind == "banko" and model_picks:
        calibration_note = (f"{calibrated}/{len(model_picks)} model seçiminde geçmiş sonuçlarla kalibrasyon uygulandı. "
            + (f"{len(picks)-len(model_picks)} seçim yalnız piyasa olasılığıdır; takım modeli yok. "
               if len(model_picks) < len(picks) else "")
            + ("Kalibrasyonu olmayan model olasılıkları henüz doğrulanmadı."
               if calibrated < len(model_picks) else "Olasılıklar tahmindir; garanti değildir."))
    return {"type": kind, "selection_policy": "market_only_v2" if kind == "small" else SELECTION_POLICY,
            "selection_tier": picks[0].get("selection_tier") if picks else "unavailable",
            "target_met": bool(picks and picks[0].get("target_met")),
            "calibration_status": calibration_status, "calibration_note": calibration_note,
            "market_only_picks": sum(bool(p.get("small_league") or p.get("market_only")) for p in picks),
            "picks": picks, "stake_note": note, "odds_source": source,
            "iddaa_picks": sum(p["odds_source"] == "iddaa" for p in picks),
            "total_odds": round(math.prod(p["odds"] for p in picks), 2) if picks else 0,
            "probability_assumption":"independent_matches_not_a_guarantee",
            "total_probability": round(math.prod(p["p"] for p in picks), 4) if picks else 0}


def _efficiency(p: float, odds: float) -> float:
    """Oran tabanına ulaşmanın olasılık maliyeti: −ln p / ln oran. Adil fiyatta 1; düşük = iyi.

    Sabit toplam oran altında ortak olasılığı en yükseğe çıkaran kupon, bu maliyeti en düşük
    bacaklardan kurulur; bu yüzden hem havuz sıralaması hem bacak seçimi bu ölçüyü kullanır.
    """
    return -math.log(p) / math.log(odds)


def _in_band(choice: dict, tier: dict) -> bool:
    lo, hi = tier["leg_odds"]
    return lo <= choice["odds"] <= hi and choice["p"] >= tier["leg_min_p"]


def _shortlist(candidates: list[dict], tier: dict) -> list[dict]:
    """Aynı maç için her MBS sınıfında en fazla üç seçenek: en olası, bant içinde en verimli,
    bant içinde en yüksek oranlı. Yüksek MBS'li bir pazar, aynı maçın daha düşük MBS'li
    kullanılabilir seçeneğini gizleyemez."""
    selected = []
    for required in sorted({c["min_matches"] for c in candidates}):
        group = [c for c in candidates if c["min_matches"] == required]
        in_band = [c for c in group if _in_band(c, tier)]
        for pick in (max(group, key=lambda c: (c["p"], c["odds"])),
                     min(in_band, key=lambda c: (_efficiency(c["p"], c["odds"]), -c["p"])) if in_band else None,
                     max(in_band, key=lambda c: (c["odds"], c["p"])) if in_band else None):
            if pick is not None and pick not in selected:
                selected.append(pick)
    return selected


def _pool(matches: list[tuple[dict, list[dict]]], tier: dict) -> list[tuple[dict, list[dict]]]:
    """Havuz: bant içi verimliliğe göre en iyiler + en olasılar + MBS ≤ 2 veren en iyi maçlar."""
    def best_eff(item):
        in_band = [c for c in item[1] if _in_band(c, tier)]
        return min((_efficiency(c["p"], c["odds"]) for c in in_band), default=math.inf)
    def best_p(item):
        return max(c["p"] for c in item[1])
    size = tier["pool"]
    by_eff = sorted(matches, key=lambda m: (best_eff(m), -best_p(m), m[0]["kickoff"], str(m[0]["id"])))
    by_p = sorted(matches, key=lambda m: (-best_p(m), m[0]["kickoff"], str(m[0]["id"])))
    low_mbs = [m for m in by_eff if any(c["min_matches"] <= 2 for c in m[1])]
    pool, seen = [], set()
    for item in [*by_eff[:size // 2], *by_p[:size - size // 2], *low_mbs[:LOW_MBS_BONUS]]:
        key = str(item[0]["id"])
        if key not in seen:
            pool.append(item); seen.add(key)
    return pool


def _select_balanced(entries, kind, limit, diagnostics=None):
    """Hedef kuponu, yoksa gerçek fiyatlı en iyi alternatifi seç.

    Puanlama: hedefi sağlayan kombinasyonlar arasında en yüksek ortak olasılık; eşitlikte daha az
    maç, sonra daha yüksek toplam oran. Hedef yoksa türün `fallback` sırasına göre bant içi
    bacaklar veya toplam oran aralığı önceliklidir; seçim `target_met=False` ile etiketlenir.
    """
    if limit <= 0:
        return []
    tier = TIERS[kind]
    banko = kind == 'banko'
    sizes = tier["picks"]
    t_lo, t_hi = tier["total_odds"]
    pool, seen = [], set()
    counts = {"matches": len(entries), "insufficient_data": 0, "stale_odds": 0, "no_market_quote": 0,
              "low_probability": 0, "odds_out_of_range": 0,
              "calibration_insufficient": 0, "calibration_best_n": 0,
              "calibration_min_samples": calibration.MIN_SAMPLES,
              "selection_policy": SELECTION_POLICY, "objective": "max_joint_probability_above_total_odds_floor",
              "total_odds_floor": t_lo,
              "eligible_matches": 0, "low_mbs_matches": 0,
              "combinations_rejected": 0, "valid_combinations": 0, "quality_rejected":0, "not_prematch":0}
    for prediction, choices in entries:
        mid = str(prediction.get('id',''))
        if not mid or mid in seen:
            continue
        if prediction.get('completed') or prediction.get('result_only'):
            counts['not_prematch'] += 1
            continue
        kickoff = quote_provenance.timestamp(prediction.get('kickoff'))
        if kickoff is None or kickoff <= datetime.now(timezone.utc):
            counts['not_prematch'] += 1
            continue
        n = prediction.get('data_n', 0)
        if prediction.get("iddaa_odds") and not _odds_fresh(prediction):
            counts["stale_odds"] += 1
            age = _odds_age_minutes(prediction.get("iddaa_updated_at"))
            if age is not None:
                counts["odds_age_minutes"] = max(counts.get("odds_age_minutes", 0), age)
            continue
        candidates = []
        for choice in choices:
            market_only=bool(prediction.get("small_league") or choice.get("market_only"))
            if choice['odds_source'] != 'iddaa' or choice['market'] not in SUPPORTED_MARKETS:
                counts["no_market_quote"] += 1
                continue
            if prediction.get("small_league") and (not prediction.get("data_availability", {}).get("market_probabilities")
                                                     or choice.get("market_p") is None):
                counts["no_market_quote"] += 1
                continue
            if (isinstance(choice.get('p'), bool) or not isinstance(choice.get('p'), (int, float))
                    or not math.isfinite(choice['p']) or not 0 < choice['p'] < 1):
                counts["low_probability"] += 1
                continue
            if (isinstance(choice.get('odds'), bool) or not isinstance(choice.get('odds'), (int, float))
                    or not math.isfinite(choice['odds']) or choice['odds'] <= 1):
                counts["odds_out_of_range"] += 1
                continue
            try:
                required_matches = max(1, int(choice.get("min_matches", 1) or 1))
            except (TypeError, ValueError):
                required_matches = 1
            if required_matches > sizes[1]:
                counts["combinations_rejected"] += 1
                continue
            choice = {**choice, "min_matches": required_matches}
            if banko and not market_only and not choice.get("calibration_applied"):
                # Az ileriye dönük kayıt, takım modelinin hiç çalışmaması demek değildir.
                # Tahmini düzeltmeden kullan; eksik kanıtı kuponda açıkça göster.
                counts["calibration_insufficient"] += 1
                counts["calibration_best_n"] = max(counts["calibration_best_n"], int(choice.get("calibration_n") or 0))
            conf = (P.confidence(choice['p'], n) if not market_only
                    else {"confidence": None, "confidence_label": "Model güveni yok", "confidence_reason": None})
            if not market_only and not choice.get('calibration_applied'):
                conf['confidence']=min(conf['confidence'],.69)
                conf['confidence_label']=P.confidence_label(conf['confidence'])
                conf['confidence_reason']+='; kalibrasyon kanıtı yetersiz'
            candidates.append({**choice, **conf, 'value': round(choice['p']*choice['odds']-1,4),
                               'efficiency': round(_efficiency(choice['p'], choice['odds']), 4)})
        if candidates:
            counts["eligible_matches"] += 1
            if any(c["min_matches"] <= 2 for c in candidates):
                counts["low_mbs_matches"] += 1
            pool.append((prediction, _shortlist(candidates, tier))); seen.add(mid)
    pool = _pool(pool, tier)
    target_candidates, fallback_in_band, fallback_total, fallback_any = [], [], [], []
    for size in range(1, min(sizes[1], len(pool))+1):
        for match_combo in combinations(pool,size):
            for choices in product(*(cs for _,cs in match_combo)):
                combo = [(pair[0],choice) for pair,choice in zip(match_combo,choices)]
                if size < max(c['min_matches'] for _,c in combo):
                    counts["combinations_rejected"] += 1
                    continue
                probability = math.prod(c['p'] for _,c in combo)
                total = math.prod(c['odds'] for _,c in combo)
                in_band = all(_in_band(c, tier) for _, c in combo)
                total_ok = t_lo <= total <= t_hi
                target_met = (sizes[0] <= size <= sizes[1] and in_band and total_ok
                              and probability >= tier["min_total_p"])
                signature = frozenset((str(p['id']),c['market']) for p,c in combo)
                if target_met:
                    score = (probability, -size, total)
                elif total_ok:
                    score = (probability, -size, total)
                elif in_band:
                    # Bant içi ama toplam oran tabanının altı: önce tabana yakınlık, sonra olasılık.
                    score = (-abs(total - t_lo), probability, -size)
                else:
                    distance = min(abs(total-t_lo), abs(total-t_hi))
                    score = (-distance, probability, -size)
                record = (score, combo, signature, target_met, probability, total)
                if target_met:
                    target_candidates.append(record)
                elif total_ok:
                    fallback_total.append(record)
                elif in_band:
                    fallback_in_band.append(record)
                else:
                    fallback_any.append(record)
    counts["valid_combinations"] = len(target_candidates)
    order = ((fallback_in_band, fallback_total) if tier["fallback"] == "in_band_first"
             else (fallback_total, fallback_in_band))
    candidates = target_candidates or order[0] or order[1] or fallback_any
    candidates = heapq.nlargest(max(1, limit * 12), candidates, key=lambda item: item[0])
    # Alternatifler önce ortak maçı olmayan kuponlardan seçilir ki kullanıcı gerçekten farklı
    # kuponlar görsün; yetmezse alt/üst küme olmayan örtüşen kombinasyonlar eklenir.
    out, used, used_matches = [], set(), set()
    matches_of = lambda signature: {match for match, _ in signature}
    for disjoint_only in (True, False):
        for _, combo, signature, target_met, probability, total in candidates:
            if len(out) >= limit:
                break
            if signature in used or any(signature <= other or other <= signature for other in used):
                continue
            if disjoint_only and matches_of(signature) & used_matches:
                continue
            picks = []
            for prediction, choice in sorted(combo,key=lambda x:x[0]['kickoff']):
                pick = _match_view(prediction, choice)
                pick.update(selection_tier="preferred" if target_met else "available_fallback",
                            target_met=target_met)
                if prediction.get("small_league"):
                    pick.update(confidence=None, confidence_label="Model güveni yok",
                                probability_source="iddaa_market", market_only=True)
                picks.append(pick)
            out.append(picks); used.add(signature); used_matches |= matches_of(signature)
    if diagnostics is not None:
        diagnostics.update(counts)
        diagnostics["selected_coupons"] = len(out)
        diagnostics["selection_tier"] = out[0][0]["selection_tier"] if out else "unavailable"
        diagnostics["target_met"] = bool(out and out[0][0]["target_met"])
    return out


def select_banko(entries):
    return next(iter(_select_balanced(entries,'banko',1)),[])


def select_banko_alternatives(entries, limit=BANKO_ALTERNATIVES):
    return _select_balanced(entries,'banko',limit)


def select_surprise(entries):
    return next(iter(_select_balanced(entries,'surprise',1)),[])


def select_surprise_alternatives(entries, limit=SURPRISE_ALTERNATIVES):
    return _select_balanced(entries,'surprise',limit)


def _day_entries(target: date) -> list[tuple[dict, list[dict]]]:
    now = datetime.now(timezone.utc)
    predictions = []
    for league in LEAGUES:
        for p in engine.get_model(league).upcoming_predictions(LOOKAHEAD_DAYS):
            kickoff = datetime.fromisoformat(p["kickoff"].replace("Z", "+00:00"))
            if kickoff > now and kickoff.astimezone(TZ).date() == target and not p.get("small_league"):
                predictions.append(p)
    with session() as conn:
        predictions = blend_predictions(conn, predictions)  # olasılıklar iddaa ile harmanlı
    return [(p, _choices(p, p["iddaa_odds"])) for p in predictions]


def select_small_league(entries: list[tuple[dict, list[dict]]]) -> list[dict]:
    """Primary market-only coupon, retained for existing callers."""
    return next(iter(select_small_league_alternatives(entries, limit=1)), [])


def select_small_league_alternatives(entries: list[tuple[dict, list[dict]]], limit=3) -> list[list[dict]]:
    """Distinct small-league market-only coupons, ranked by combined market probability."""
    best = []
    seen_matches = set()
    for prediction, choices in entries:
        if not prediction.get("small_league") or not prediction.get("data_availability", {}).get("market_probabilities"):
            continue
        mid = str(prediction.get("id", ""))
        if not mid or mid in seen_matches:
            continue
        choice = small_league_recommendation(prediction, choices)
        if choice:
            best.append((prediction, choice))
            seen_matches.add(mid)

    # Bound combinatorics independently of bulletin size. Keeping the strongest
    # few candidates in each min_matches bucket preserves options with distinct
    # eligibility constraints while avoiding O(n^4) generation on busy days.
    frontier_per_bucket = max(4, max(0, limit) + 3)
    buckets = {}
    for item in best:
        required = max(1, int(item[1].get("min_matches", 1) or 1))
        if required <= 4:
            buckets.setdefault(required, []).append(item)
    frontier = []
    for items in buckets.values():
        items.sort(key=lambda item: (-item[1]["p"], str(item[0].get("id", ""))))
        frontier.extend(items[:frontier_per_bucket])

    ranked = heapq.nlargest(max(0, limit) * 8, (
        (math.prod(choice["p"] for _, choice in combo), size,
         tuple(str(prediction.get("id", "")) for prediction, _ in combo), combo)
        for size in range(2, min(4, len(frontier)) + 1)
        for combo in combinations(frontier, size)
        if size >= max(int(choice.get("min_matches", 1) or 1) for _, choice in combo)
    ), key=lambda item: (item[0], -item[1], tuple(reversed(item[2]))))

    out, used = [], []
    for _, _, ids, combo in ranked:
        signature = frozenset(ids)
        if any(signature <= previous or previous <= signature for previous in used):
            continue
        picks = []
        for prediction, choice in sorted(combo, key=lambda item: item[0]["kickoff"]):
            pick = _match_view(prediction, choice)
            pick.update({"confidence": None, "confidence_label": "Model güveni yok",
                         "small_league": True, "probability_source": "iddaa_market"})
            picks.append(pick)
        out.append(picks)
        used.append(signature)
        if len(out) >= limit:
            break
    return out


def build_coupons(target: date) -> dict:
    entries = _day_entries(target)
    with session() as conn:
        constraints = load_constraints(conn)
        provenance = quote_provenance.load(conn)
        rows = conn.execute(
            """SELECT e.id, e.league, e.league_name, e.country, e.kickoff, e.home, e.away, e.updated_at,
                      o.market, o.odd
               FROM iddaa_events e LEFT JOIN iddaa_odds o ON o.event_id=e.id
                WHERE e.is_small=1 AND e.kickoff>=? AND e.kickoff<? AND e.kickoff>? ORDER BY e.kickoff""",
            (datetime.combine(target, datetime.min.time(), TZ).astimezone(timezone.utc).isoformat(),
             datetime.combine(target + timedelta(days=1), datetime.min.time(), TZ)
             .astimezone(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat())).fetchall()
    grouped = {}
    for r in rows:
        p = grouped.setdefault(r["id"], {"id": r["id"], "league": r["league"], "country": r["country"],
            "league_name": r["league_name"], "kickoff": r["kickoff"],
            "home": r["home"], "away": r["away"], "iddaa_odds": {}, "small_league": True,
            "iddaa_updated_at": r["updated_at"],
            "quote_provenance": quote_provenance.for_event(provenance,r["id"]),
            "iddaa_min_matches": constraints.get(r["id"], {})})
        if r["market"] and r["odd"]:
            p["iddaa_odds"][r["market"]] = r["odd"]
    small_entries = []
    for event in grouped.values():
        p = _small_prediction_for_coupon(event)
        small_entries.append((p, _choices(p, p["iddaa_odds"])))
    small_alternatives = select_small_league_alternatives(small_entries)
    small_picks = small_alternatives[0] if small_alternatives else []
    small_coupon = _coupon("small", small_picks,
        "Bu kupon iddaa piyasa olasılıklarına dayanır; takım modeli güveni hesaplanmaz.")
    for pick in small_coupon["picks"]:
        pick["league_name"] = grouped[pick["id"]]["league_name"]
        pick["country"] = grouped[pick["id"]]["country"]
    for coupon in small_alternatives:
        for pick in coupon:
            pick["league_name"] = grouped[pick["id"]]["league_name"]
            pick["country"] = grouped[pick["id"]]["country"]
    daily_entries = entries + small_entries
    banko_note = _tier_note("banko")
    surprise_note = _tier_note("surprise")
    banko_diagnostics = {}
    banko_alternatives = [_coupon("banko", picks, banko_note)
                          for picks in _select_balanced(daily_entries, "banko", BANKO_ALTERNATIVES, banko_diagnostics)]
    surprise_diagnostics = {}
    surprise_alternatives = [_coupon("surprise", picks, surprise_note)
                             for picks in _select_balanced(daily_entries, "surprise", SURPRISE_ALTERNATIVES, surprise_diagnostics)]
    from ..research_trio import select as select_research_trio
    trio = select_research_trio(daily_entries)
    return {
        "date": target.isoformat(),
        "matches": len(daily_entries),
        "research_trio": trio,
        # Kupon geçmişi ve Telegram mevcut tek-kupon alanlarını kullanmayı sürdürür.
        "banko": banko_alternatives[0] if banko_alternatives else _coupon("banko", [], banko_note),
        "surprise": surprise_alternatives[0] if surprise_alternatives else _coupon("surprise", [], surprise_note),
        "banko_alternatives": banko_alternatives,
        "banko_diagnostics": banko_diagnostics,
        "surprise_diagnostics": surprise_diagnostics,
        "surprise_alternatives": surprise_alternatives,
        "small_leagues": small_coupon,
        "small_league_alternatives": [_coupon("small", picks,
            "Bu kupon iddaa piyasa olasılıklarına dayanır; takım modeli güveni hesaplanmaz.")
            for picks in small_alternatives],
        "rules": {"selection_policy": SELECTION_POLICY,
                  "objective": "max_joint_probability_above_total_odds_floor",
                  "banko_min_probability": BANKO_MIN_P, "banko_min_total_probability": BANKO_MIN_TOTAL_P,
                  "banko_min_edge": BANKO_MIN_EDGE, "banko_min_data_n": BANKO_MIN_DATA_N,
                  "banko_min_calibration_samples": calibration.MIN_SAMPLES,
                  "banko_calibration_required": False, "banko_objective": "hit_probability",
                  "banko_min_odds": BANKO_MIN_ODDS, "banko_total_odds": list(TIERS["banko"]["total_odds"]),
                  "surprise_probability_range": [SURPRISE_MIN_P, SURPRISE_MAX_P],
                  "surprise_min_total_odds": SURPRISE_MIN_TOTAL_ODDS,
                  "tiers": {kind: {k: (list(v) if isinstance(v, tuple) else v) for k, v in tier.items()}
                            for kind, tier in TIERS.items()}},
    }


def _tier_note(kind: str) -> str:
    tier = TIERS[kind]
    fmt = lambda x: f"{x:.2f}".replace(".", ",").rstrip("0").rstrip(",")
    legs = f"{tier['picks'][0]}–{tier['picks'][1]}"
    common = (f"gerçek oran {fmt(tier['leg_odds'][0])}–{fmt(tier['leg_odds'][1])}, tek seçim olasılığı en az "
              f"%{tier['leg_min_p']*100:.0f}, kupon toplam oranı {fmt(tier['total_odds'][0])}–{fmt(tier['total_odds'][1])}. "
              "Bu sınırlar içinde ortak tutma olasılığı en yüksek kupon seçilir; eşitlikte daha az maç tercih edilir. "
              "iddaa her pazara yaklaşık %20 marj bastığından aynı toplam orana daha az maçla ulaşmak daha olasıdır; "
              "sağlayıcının minimum bahis adedi (MBS) 1–2 verdiği maçlar bu yüzden öne çıkabilir. ")
    if kind == "banko":
        return (f"Banko hedefi: {legs} farklı maç, " + common +
                "Hedef tutmazsa önce bant içi seçimlerden tabana en yakın, sonra en olası alternatif sunulur; "
                "hedef durumu ayrıca belirtilir. Küçük lig olasılıkları yalnız piyasadan gelir ve takım modeli güveni "
                "taşımaz. Toplam olasılık maçların bağımsızlığı varsayımıyla hesaplanır; garanti değildir.")
    return (f"Sürpriz hedefi: {legs} farklı maç, " + common +
            "Sürpriz şansa değil, hâlâ makul olasılığı olan yüksek oranlı seçimlere dayanır. Hedef mümkün değilse "
            "önce toplam oran aralığını sağlayan, sonra bant içi en olası alternatif seçilir; hedef durumu ayrıca "
            "belirtilir. Daha yüksek risk taşır.")


def _small_prediction_for_coupon(event):
    from .small_leagues import predict_event
    return predict_event(event)
