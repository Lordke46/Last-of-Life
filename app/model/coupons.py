"""Günlük, ligler arası kupon seçimi (Banko + Sürpriz).

Olasılıklar iddaa.com oranlarıyla harmanlıdır (model/blend.py; oran yoksa model aynen).
Otomatik seçimler yalnız gerçek iddaa oranlarını kullanır. Eski kayıt/aday yardımcıları
model fiyatını ayrı kaynak etiketiyle koruyabilir; bunlar otomatik kupona alınmaz.
Gün sınırı Türkiye saatidir (arayüzdeki saatler de yerel gösterilir).
"""
import math
from datetime import date, datetime, timezone, timedelta
from itertools import combinations, product
from zoneinfo import ZoneInfo

from ..config import LEAGUES
from ..db import session
from . import engine, calibration
from . import predict as P
from .blend import blend_predictions, devig
from .settlement import SUPPORTED_MARKETS
from ..scrapers.iddaa import load_constraints

TZ = ZoneInfo("Europe/Istanbul")
LOOKAHEAD_DAYS = 60

BANKO_MIN_P, BANKO_MIN_ODDS, BANKO_MAX_ODDS = .62, 1.40, 2.30
BANKO_MIN_TOTAL_P = .25
BANKO_MIN_EDGE = .03  # en az %3 tahmini beklenen değer; geçmiş kalibrasyon zorunlu
BANKO_MIN_DATA_N = 12
BANKO_PICKS, BANKO_POOL, BANKO_ALTERNATIVES = (2, 3), 14, 3
SURPRISE_MIN_P, SURPRISE_MAX_P = .38, .70
SURPRISE_MIN_ODDS, SURPRISE_MAX_ODDS = 1.80, 3.50
SURPRISE_MIN_TOTAL_ODDS, SURPRISE_MAX_TOTAL_ODDS = 8.0, 25.0
SURPRISE_PICKS, SURPRISE_POOL, SURPRISE_ALTERNATIVES = (3, 4), 14, 3
SURPRISE_MARKETS = SUPPORTED_MARKETS
SELECTION_POLICY = 'balanced_v2'

MAX_COUPON_ODDS_AGE = timedelta(hours=2)


def _odds_fresh(prediction: dict) -> bool:
    """Kupon için yalnız son 2 saat içinde yenilenmiş bülten kabul edilir.

    Test/elle kurulan prediction'larda iddaa kaynak alanı yoktur. Gerçek
    blend_predictions çıktısında iddaa_event_id ve iddaa_updated_at bulunur.
    """
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
    if prediction.get("small_league") and devig([iddaa_odds.get(k) for k in ("1", "X", "2")]) is None:
        return out
    available = {**prediction.get("markets", {}), **prediction.get("extra_markets", {})}
    for market, item in available.items():
        if market not in SUPPORTED_MARKETS:
            continue  # ilk yarı/handikap için ortak sonuç verisi henüz tutulmuyor
        if item.get("p") is None:  # korner/kart için geçmiş veri yok; oranı kupona model gibi sokma
            continue
        claude = item.get("suggested")
        theirs = iddaa_odds.get(market)
        if prediction.get("small_league"):
            if not prediction.get("data_availability", {}).get("market_probabilities") or not theirs or theirs < 1.01:
                continue
        elif not claude or claude < 1.01:
            continue
        odds, source = (round(theirs, 2), "iddaa") if theirs and theirs >= 1.01 else (claude, "claude")
        calib = (prediction.get("calibration_by_market") or {}).get(market) or {}
        out.append({"market": market, "p": calib.get("p", item["p"]), "raw_p": item["p"],
                    "calibration_n": calib.get("n", 0), "calibration_applied": calib.get("applied", False),
                    "odds": odds, "odds_source": source,
                    "iddaa_odds": theirs if source == "iddaa" else None, "claude_odds": claude,
                    "fair": item.get("fair"), "min_matches": prediction.get("iddaa_min_matches", {}).get(market, 1)})
    return out


def _match_view(prediction: dict, choice: dict) -> dict:
    return {"id": prediction["id"], "league": prediction["league"],
            "league_name": prediction.get("league_name") or LEAGUES.get(prediction["league"], {}).get("name", prediction["league"]),
            "kickoff": prediction["kickoff"],
            "home": prediction["home"], "away": prediction["away"], **choice,
            "model_probability": prediction.get("markets", {}).get(choice["market"], {}).get("model_p", choice["p"]),
            "market_probability": prediction.get("markets", {}).get(choice["market"], {}).get("market_p"),
            "data_n": prediction.get("data_n", 0),
            "news_effect": (prediction.get("factors") or {}).get("news", {}).get("log_adj"),
            "probability_source": "iddaa_market" if prediction.get("small_league") else
                                  "calibrated" if choice.get("calibration_applied") else
                                  "blend" if prediction.get("blend", {}).get("applied") else "model"}


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
    return {"type": kind, "selection_policy": SELECTION_POLICY, "picks": picks, "stake_note": note, "odds_source": source,
            "iddaa_picks": sum(p["odds_source"] == "iddaa" for p in picks),
            "total_odds": round(math.prod(p["odds"] for p in picks), 2) if picks else 0,
            "total_probability": round(math.prod(p["p"] for p in picks), 4) if picks else 0}


def _select_balanced(entries, kind, limit, diagnostics=None):
    """Gerçek fiyatlı, veri kapsamı yeterli adaylarda en olası, sınırlı büyüklükte kombinasyon."""
    if limit <= 0:
        return []
    banko = kind == 'banko'
    p_min, p_max = (BANKO_MIN_P, 1) if banko else (SURPRISE_MIN_P, SURPRISE_MAX_P)
    o_min, o_max = (BANKO_MIN_ODDS, BANKO_MAX_ODDS) if banko else (SURPRISE_MIN_ODDS, SURPRISE_MAX_ODDS)
    pool, seen = [], set()
    counts = {"matches": len(entries), "insufficient_data": 0, "no_market_quote": 0,
              "low_probability": 0, "odds_out_of_range": 0,
              "calibration_insufficient": 0, "negative_or_small_edge": 0,
              "eligible_matches": 0, "combinations_rejected": 0, "valid_combinations": 0}
    for prediction, choices in entries:
        mid = str(prediction.get('id',''))
        if not mid or mid in seen or prediction.get('small_league') or str(prediction.get('league','')).startswith('small:'):
            continue
        n = prediction.get('data_n', 0)
        if n < (BANKO_MIN_DATA_N if banko else 8):
            counts["insufficient_data"] += 1
            continue
        candidates = []
        for choice in choices:
            if choice['odds_source'] != 'iddaa' or choice['market'] not in SUPPORTED_MARKETS:
                counts["no_market_quote"] += 1
                continue
            if not p_min <= choice['p'] <= p_max:
                counts["low_probability"] += 1
                continue
            if not o_min <= choice['odds'] <= o_max:
                counts["odds_out_of_range"] += 1
                continue
            if banko and choice.get("calibration_n", 0) < calibration.MIN_SAMPLES:
                counts["calibration_insufficient"] += 1
                continue
            if banko and choice["p"] * choice["odds"] - 1 < BANKO_MIN_EDGE:
                counts["negative_or_small_edge"] += 1
                continue
            conf = P.confidence(choice['p'], n)
            candidates.append({**choice, **conf, 'value': round(choice['p']*choice['odds']-1,4)})
        if candidates:
            counts["eligible_matches"] += 1
            # Sabit oran bandında önce olasılık, sonra veri güveni. Küçük oran için bütün pazarlar bastırılmaz.
            candidates.sort(key=lambda c:(c['confidence'],c['p'],c['odds']),reverse=True)
            selected=[candidates[0]]
            higher=max(candidates,key=lambda c:c['odds'])
            if higher['market'] != selected[0]['market']: selected.append(higher)
            pool.append((prediction,selected)); seen.add(mid)
    pool.sort(key=lambda x:(-x[1][0]['confidence'],-x[1][0]['p'],x[0]['kickoff']))
    pool = pool[:BANKO_POOL if banko else SURPRISE_POOL]
    sizes = BANKO_PICKS if banko else SURPRISE_PICKS
    candidates = []
    for size in range(sizes[0],sizes[1]+1):
        for match_combo in combinations(pool,size):
            for choices in product(*(cs for _,cs in match_combo)):
                combo = [(pair[0],choice) for pair,choice in zip(match_combo,choices)]
                if size < max(c.get('min_matches',1) for _,c in combo):
                    counts["combinations_rejected"] += 1
                    continue
                probability = math.prod(c['p'] for _,c in combo)
                total = math.prod(c['odds'] for _,c in combo)
                if banko and (probability < BANKO_MIN_TOTAL_P or not 1.9 <= total <= 8):
                    counts["combinations_rejected"] += 1
                    continue
                if not banko and (probability < .04 or not SURPRISE_MIN_TOTAL_ODDS <= total <= SURPRISE_MAX_TOTAL_ODDS):
                    counts["combinations_rejected"] += 1
                    continue
                counts["valid_combinations"] += 1
                signature = frozenset((str(p['id']),c['market']) for p,c in combo)
                tier = min({'Düşük':0,'Orta':1,'Yüksek':2}[c['confidence_label']] for _,c in combo)
                candidates.append(((tier,probability,sum(c['confidence'] for _,c in combo)/size,-size),combo,signature))
    candidates.sort(key=lambda x:x[0],reverse=True)
    out, used = [], set()
    for _, combo, signature in candidates:
        if signature in used or any(signature <= other or other <= signature for other in used):
            continue
        out.append([_match_view(p,c) for p,c in sorted(combo,key=lambda x:x[0]['kickoff'])]);used.add(signature)
        if len(out)>=limit:break
    if diagnostics is not None:
        diagnostics.update(counts)
        diagnostics["selected_coupons"] = len(out)
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
        # Eksik/bozuk snapshot tablosu PostgreSQL işlemini abort etmesin;
        # önceki snapshot yazımlarını koruyarak yalnız kalibrasyonu atla.
        conn.execute("SAVEPOINT calibration_read")
        try:
            observed = calibration.load(conn)
            conn.execute("RELEASE SAVEPOINT calibration_read")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT calibration_read")
            conn.execute("RELEASE SAVEPOINT calibration_read")
            observed = {}
    for prediction in predictions:
        prediction["calibration_by_market"] = {
            market: calibration.calibrate(item["p"], prediction["league"], market, observed)
            for market, item in prediction.get("markets", {}).items()
            if isinstance(item.get("p"), (int, float))
        }
    return [(p, _choices(p, p["iddaa_odds"])) for p in predictions]


def select_small_league(entries: list[tuple[dict, list[dict]]]) -> list[dict]:
    """Küçük liglerde 2-4 en olası ayrı maç; oranla tahmini harmanla ve güveni düşük tut."""
    best = []
    for prediction, choices in entries:
        if not prediction.get("small_league") or not prediction.get("data_availability", {}).get("market_probabilities"):
            continue
        choice = small_league_recommendation(prediction, choices)
        if choice:
            best.append((prediction, choice))
    best.sort(key=lambda item: (-item[1]["p"], item[0]["kickoff"]))
    picks = []
    for prediction, choice in best[:4]:
        pick = _match_view(prediction, choice)
        pick.update({"confidence": None, "confidence_label":"Model güveni yok", "small_league": True})
        picks.append(pick)
    return picks if len(picks) >= max([2]+[p.get('min_matches',1) for p in picks]) else []


def build_coupons(target: date) -> dict:
    entries = _day_entries(target)
    with session() as conn:
        constraints = load_constraints(conn)
        rows = conn.execute(
            """SELECT e.id, e.league, e.league_name, e.country, e.kickoff, e.home, e.away,
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
            "iddaa_min_matches": constraints.get(r["id"], {})})
        if r["market"] and r["odd"]:
            p["iddaa_odds"][r["market"]] = r["odd"]
    small_entries = []
    for event in grouped.values():
        p = _small_prediction_for_coupon(event)
        small_entries.append((p, _choices(p, p["iddaa_odds"])))
    small_picks = select_small_league(small_entries)
    small_coupon = _coupon("small", small_picks,
        "Bu kupon iddaa piyasa olasılıklarına dayanır; takım modeli güveni hesaplanmaz.")
    for pick in small_coupon["picks"]:
        pick["league_name"] = grouped[pick["id"]]["league_name"]
        pick["country"] = grouped[pick["id"]]["country"]
    banko_note = "Kalibrasyonu yeterli, en az %3 tahmini değer avantajlı, 12 ağırlıklı maç geçmişli 2–3 seçim. Banko garanti değildir."
    surprise_note = "Sürpriz: gerçek oran 1,80–3,50; olasılık %38–70; 3–4 farklı maç; toplam oran 8–25. Daha yüksek risk taşır."
    banko_diagnostics = {}
    banko_alternatives = [_coupon("banko", picks, banko_note)
                          for picks in _select_balanced(entries, "banko", BANKO_ALTERNATIVES, banko_diagnostics)]
    surprise_alternatives = [_coupon("surprise", picks, surprise_note)
                             for picks in select_surprise_alternatives(entries)]
    return {
        "date": target.isoformat(),
        "matches": len(entries),
        # Kupon geçmişi ve Telegram mevcut tek-kupon alanlarını kullanmayı sürdürür.
        "banko": banko_alternatives[0] if banko_alternatives else _coupon("banko", [], banko_note),
        "surprise": surprise_alternatives[0] if surprise_alternatives else _coupon("surprise", [], surprise_note),
        "banko_alternatives": banko_alternatives,
        "banko_diagnostics": banko_diagnostics,
        "surprise_alternatives": surprise_alternatives,
        "small_leagues": small_coupon,
        "rules": {"banko_min_probability": BANKO_MIN_P, "banko_min_total_probability": BANKO_MIN_TOTAL_P,
                  "banko_min_edge": BANKO_MIN_EDGE, "banko_min_data_n": BANKO_MIN_DATA_N,
                  "banko_min_calibration_samples": calibration.MIN_SAMPLES,
                  "banko_min_odds": BANKO_MIN_ODDS,
                  "surprise_probability_range": [SURPRISE_MIN_P, SURPRISE_MAX_P],
                  "surprise_min_total_odds": SURPRISE_MIN_TOTAL_ODDS},
    }


def _small_prediction_for_coupon(event):
    from .small_leagues import predict_event
    return predict_event(event)
