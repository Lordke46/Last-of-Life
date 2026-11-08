"""Skor dağılımı (Dixon-Coles düzeltmeli Poisson) ve bahis pazarları."""
import math

from ..config import MODEL

MAX_GOALS = 10


def _pois(k: int, lam: float) -> float:
    return math.exp(-lam) * lam ** k / math.factorial(k)


def score_matrix(lh: float, la: float, rho: float | None = None) -> list[list[float]]:
    rho = MODEL["dc_rho"] if rho is None else rho
    ph = [_pois(i, lh) for i in range(MAX_GOALS + 1)]
    pa = [_pois(j, la) for j in range(MAX_GOALS + 1)]
    mat = [[ph[i] * pa[j] for j in range(MAX_GOALS + 1)] for i in range(MAX_GOALS + 1)]
    # Dixon-Coles: düşük skorlu sonuçlar arasındaki bağımlılık düzeltmesi
    mat[0][0] *= 1 - lh * la * rho
    mat[0][1] *= 1 + lh * rho
    mat[1][0] *= 1 + la * rho
    mat[1][1] *= 1 - rho
    tot = sum(map(sum, mat))
    return [[v / tot for v in row] for row in mat]


def odds(p: float, margin: float | None = None) -> dict:
    margin = MODEL["margin"] if margin is None else margin
    if p <= 0:
        return {"p": 0.0, "fair": None, "suggested": None}
    fair = 1 / p
    sug = max(1.01, 1 / min(0.99, p * (1 + margin)))
    return {"p": round(p, 4), "fair": round(fair, 2), "suggested": round(sug, 2)}


def markets(mat: list[list[float]]) -> dict:
    n = len(mat)
    p1 = sum(mat[i][j] for i in range(n) for j in range(n) if i > j)
    px = sum(mat[i][i] for i in range(n))
    p2 = 1 - p1 - px
    tot = lambda f: sum(mat[i][j] for i in range(n) for j in range(n) if f(i, j))
    out = {
        "1": odds(p1), "X": odds(px), "2": odds(p2),
        "1X": odds(p1 + px), "12": odds(p1 + p2), "X2": odds(px + p2),
        "Ü1.5": odds(tot(lambda i, j: i + j >= 2)), "A1.5": odds(tot(lambda i, j: i + j <= 1)),
        "Ü2.5": odds(tot(lambda i, j: i + j >= 3)), "A2.5": odds(tot(lambda i, j: i + j <= 2)),
        "Ü3.5": odds(tot(lambda i, j: i + j >= 4)), "A3.5": odds(tot(lambda i, j: i + j <= 3)),
        "KG Var": odds(tot(lambda i, j: i > 0 and j > 0)), "KG Yok": odds(tot(lambda i, j: i == 0 or j == 0)),
        "Ev 0.5Ü": odds(tot(lambda i, j: i >= 1)), "Dep 0.5Ü": odds(tot(lambda i, j: j >= 1)),
    }
    scores = sorted(((mat[i][j], f"{i}-{j}") for i in range(n) for j in range(n)), reverse=True)[:6]
    out_scores = [{"score": s, **odds(p)} for p, s in scores]
    return {"markets": out, "scores": out_scores}


# --- Güven ----------------------------------------------------------------------
# Kaynak: sürekli 0-1 skor. Skor = min(önerinin olasılığından, veri kapsamından gelen skor).
# Etiket ve açıklama bu skordan türetilir (eşikler: 0.45 Orta, 0.70 Yüksek), böylece
#   olasılık >= %62 Yüksek, %56-62 Orta, altı Düşük; ağırlıklı maç < 12 en fazla Orta, < 8 Düşük.
# Haber akışının bulunmaması güveni sınırlamaz: haber yokluğu, model için kanıt değildir.
CONF_LABELS = ["Düşük", "Orta", "Yüksek"]
CONF_CUTS = (0.45, 0.70)
CONF_P = (0.33, 0.56, 0.62, 0.80)      # olasılık düğümleri -> skor 0, 0.45, 0.70, 1
CONF_DATA = (0.0, 8.0, 12.0, 20.0)     # ağırlıklı maç düğümleri -> skor 0, 0.45, 0.70, 1
CONF_RULE = ("Güven, önerinin olasılığı ve zaman ağırlıklı takım maç verisine göre verilir (%62 ve üstü Yüksek, "
             "%56–62 Orta, altı Düşük); takım verisi azsa bir ya da iki kademe sınır uygulanır. Haber akışının "
             "bulunmaması tek başına güveni azaltmaz.")


def _ramp(x: float, knots: tuple) -> float:
    ys = (0.0, CONF_CUTS[0], CONF_CUTS[1], 1.0)
    if x <= knots[0]:
        return 0.0
    for i in range(1, 4):
        if x <= knots[i]:
            return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - knots[i - 1]) / (knots[i] - knots[i - 1])
    return 1.0


def confidence_label(level: float) -> str:
    return CONF_LABELS[(level >= CONF_CUTS[0]) + (level >= CONF_CUTS[1])]


def confidence(p: float | None, n_min: float, news_teams: int | None = None, data_integrity=None) -> dict:
    """p: önerilen pazarın olasılığı (öneri yoksa None), n_min: iki takımdan az olanın (zaman ağırlıklı) maç sayısı.
    news_teams eski çağrı noktalarıyla uyumluluk için alınır; haber bulunmaması güveni etkilemez."""
    if p is None:
        return {"confidence": 0.0, "confidence_label": "Düşük",
                "confidence_reason": "belirgin bir öneri yok"}
    by_p, by_n = _ramp(p, CONF_P), _ramp(n_min, CONF_DATA)
    level = math.floor(min(by_p, by_n) * 100 + 1e-9) / 100  # aşağı yuvarla: etiket eşiği geçmeden Orta/Yüksek olmasın
    incomplete = (data_integrity or {}).get('history_complete') is False
    if incomplete:
        level = min(level, CONF_CUTS[0])
    news_note = ""
    label = confidence_label(level)
    if by_n < by_p and confidence_label(by_n) != confidence_label(by_p):
        reason = f"olasılık %{p * 100:.0f}, ama veri az (~{n_min:.0f} maç) olduğu için sınırlandı"
    elif n_min >= CONF_DATA[2]:
        reason = f"olasılık %{p * 100:.0f}, veri yeterli (~{n_min:.0f} maç)"
    else:
        reason = f"olasılık %{p * 100:.0f} (~{n_min:.0f} maç veri)"
    if incomplete:
        reason += "; sezon kayıtlarında doğrulanmış eksik/tutarsızlık var"
    return {"confidence": level, "confidence_label": label, "confidence_reason": reason + news_note}


# İsabet önceliği: tam maç skoru ile sonuçlandırılabilen standart pazarlar.
PICK_MARKETS = ["1", "X", "2", "1X", "X2", "12", "Ü1.5", "A1.5", "Ü2.5", "A2.5", "Ü3.5", "A3.5", "KG Var", "KG Yok"]
PICK_POLICY = "balanced_v2"
PICK_MIN_ODDS, PICK_MAX_ODDS = 1.40, 2.30
PICK_MIN_P = .56


def best_pick(mk: dict, min_odds: float = PICK_MIN_ODDS, actual_odds: dict | None = None) -> dict | None:
    """Orta oran bandında en yüksek tahmini olasılık. Bandın dışında öneri zorlanmaz.

    Gerçek oran sağlanmışsa yalnız o listede geçerli oranı olan pazar seçilir.
    Oran yoksa model önerisi açıkça ayrı kaynak olarak işaretlenir.
    """
    candidates = []
    for key in PICK_MARKETS:
        item = mk.get(key, {})
        probability = item.get("p")
        quote = actual_odds.get(key) if actual_odds is not None else item.get("suggested")
        if (isinstance(probability, bool) or not isinstance(probability, (int, float))
                or not math.isfinite(probability) or not 0 < probability <= 1
                or (actual_odds is not None and probability < PICK_MIN_P)
                or isinstance(quote, bool) or not isinstance(quote, (int, float))
                or not math.isfinite(quote) or quote <= 1 or not min_odds <= quote <= PICK_MAX_ODDS):
            continue
        candidates.append({"market": key, **item, "odds": quote,
                           "odds_source": "iddaa" if actual_odds is not None else "model",
                           "policy": PICK_POLICY})
    return max(candidates, key=lambda item: item["p"], default=None)


def candidate(mk: dict, actual_odds: dict) -> dict | None:
    """Güçlü öneri yoksa gerçek fiyatlı en olası adayı açık statüyle göster."""
    options=[]
    for market in PICK_MARKETS:
        item=mk.get(market,{})
        p,quote=item.get('p'),actual_odds.get(market)
        if (isinstance(p,bool) or not isinstance(p,(int,float)) or not math.isfinite(p) or not 0<p<=1
                or isinstance(quote,bool) or not isinstance(quote,(int,float)) or not math.isfinite(quote) or quote<=1):
            continue
        options.append({'market':market,**item,'odds':quote,'odds_source':'iddaa','policy':'candidate_v1'})
    in_band=[item for item in options if PICK_MIN_ODDS<=item['odds']<=PICK_MAX_ODDS]
    if in_band:
        out=max(in_band,key=lambda item:item['p'])
        return {**out,'candidate_status':'below_threshold'}
    if options:
        # Bandın dışındaki adayı en yakın gerçek fiyattan göster; uygun öneri gibi etiketleme.
        out=min(options,key=lambda item:(min(abs(item['odds']-PICK_MIN_ODDS),abs(item['odds']-PICK_MAX_ODDS)),-item['p']))
        return {**out,'candidate_status':'outside_band'}
    return None


def model_preview(mk: dict) -> dict | None:
    choices=[{'market':key,**mk[key],'odds':None,'odds_source':None,'policy':'model_preview',
              'candidate_status':'odds_missing'} for key in ('1','X','2') if mk.get(key,{}).get('p') is not None]
    return max(choices,key=lambda item:item['p'],default=None)
