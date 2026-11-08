"""iddaa.com oranlarından marjı temizlenmiş (implied) olasılıklar ve modelle ağırlıklı harmanlama.

Akış: iddaa oranı -> 1/oran -> aynı pazar grubunun toplamına bölünerek marj temizlenir (1X2: üçlü,
Alt/Üst ve KG: ikili) -> model olasılığıyla doğrusal harman: p = (1-w)·p_model + w·p_piyasa.
w = MODEL["market_weight"]. Bir pazar grubunun oranı yoksa (veya tutarsızsa) o pazarda model aynen kalır.
Çifte şans (1X, 12, X2) iddaa'nın kendi çifte şans oranından değil, harmanlı 1X2'den türetilir; böylece
pazarlar birbiriyle tutarlı kalır. Beklenen goller (xG) ve skor olasılıkları model değeridir, harmanlanmaz.
"""
import math

from ..config import MODEL
from ..scrapers import iddaa
from . import predict as P
from . import extra_markets

# Marjı temizlenen pazar grupları: grup adı -> pazarlar (olasılıkları toplamı 1)
GROUPS = {
    "1x2": ["1", "X", "2"],
    "ou1.5": ["Ü1.5", "A1.5"],
    "ou2.5": ["Ü2.5", "A2.5"],
    "ou3.5": ["Ü3.5", "A3.5"],
    "btts": ["KG Var", "KG Yok"],
}
# Σ(1/oran) bu aralığın dışındaysa oran seti güvenilmez sayılır (hatalı/eksik veri); model kullanılır.
BOOK_MIN, BOOK_MAX = 0.98, 1.50


def devig(odds: list[float]) -> list[float] | None:
    """Oranlardan marjı temizlenmiş olasılıklar (toplam 1); oranlar geçersizse None."""
    if not odds or any(isinstance(o, bool) or not isinstance(o, (int, float)) or not math.isfinite(o) or o <= 1.0 for o in odds):
        return None
    book = sum(1 / o for o in odds)
    if not BOOK_MIN <= book <= BOOK_MAX:
        return None
    return [1 / o / book for o in odds]


def implied_groups(iddaa_odds: dict) -> dict[str, dict[str, float]]:
    """Oranı tam olan her pazar grubu için {pazar: marjı temizlenmiş olasılık}."""
    out = {}
    for name, markets in GROUPS.items():
        probs = devig([iddaa_odds.get(m) for m in markets])
        if probs:
            out[name] = dict(zip(markets, probs))
    return out


def blend_probs(p_model: list[float], p_market: list[float], weight: float) -> list[float]:
    """İki olasılık dağılımının doğrusal harmanı (toplam 1 korunur)."""
    return [(1 - weight) * a + weight * b for a, b in zip(p_model, p_market)]


def _clamp(weight) -> float:
    return min(1.0, max(0.0, float(weight)))


def blend_markets(model_markets: dict, iddaa_odds: dict, weight: float | None = None):
    """(harmanlı pazarlar, harmanlanan grup adları). Her pazar: model `p`/`fair`/`suggested` alanları
    harmanlı olasılıktan yeniden hesaplanır; `model_p` ve `market_p` (piyasa yoksa None) eklenir.
    Hiçbir grup harmanlanmadıysa `model_markets` aynen döner."""
    w = _clamp(MODEL["market_weight"] if weight is None else weight)
    groups = implied_groups(iddaa_odds) if w > 0 else {}
    if not groups:
        return model_markets, []

    model_p = {m: item["p"] for m, item in model_markets.items()}
    market_p = {m: q for g in groups.values() for m, q in g.items()}
    final = dict(model_p)
    for markets in groups.values():
        keys = list(markets)
        for k, p in zip(keys, blend_probs([model_p[k] for k in keys], [markets[k] for k in keys], w)):
            final[k] = p
    if "1x2" in groups:
        p1, px, p2 = (final[k] for k in GROUPS["1x2"])
        total = p1 + px + p2  # model olasılıkları 4 hane yuvarlandığından toplam tam 1 olmayabilir
        p1, px, p2 = p1 / total, px / total, p2 / total
        final.update({"1": p1, "X": px, "2": p2, "1X": p1 + px, "12": p1 + p2, "X2": px + p2})
        q1, qx, q2 = (market_p[k] for k in GROUPS["1x2"])
        market_p.update({"1X": q1 + qx, "12": q1 + q2, "X2": qx + q2})

    out = {}
    for m, item in model_markets.items():
        out[m] = ({**P.odds(final[m]), "model_p": model_p[m], "market_p": round(market_p[m], 4)}
                  if m in market_p else {**item, "model_p": model_p[m], "market_p": None})
    return out, list(groups)


def blend_prediction(pred: dict, iddaa_odds: dict, weight: float | None = None) -> dict:
    """Tek maç tahminini iddaa oranlarıyla harmanlar (yeni sözlük döner). Oran yoksa model aynen kalır;
    Gerçek oran varsa, harman grubu eksik olsa da öneri mevcut pazarlardan yeniden seçilir."""
    w = _clamp(MODEL["market_weight"] if weight is None else weight)
    markets, groups = blend_markets(pred["markets"], iddaa_odds, w)
    out = {**pred, "markets": markets, "blend": {"applied": bool(groups), "weight": w, "groups": groups}}
    if "pick" in pred:
        out["pick"] = P.best_pick(markets, actual_odds=iddaa_odds)
        out["alternative"] = None if out['pick'] else P.candidate(markets,iddaa_odds) if iddaa_odds else P.model_preview(markets)
        out['recommendation_status'] = 'recommended' if out['pick'] else (out['alternative'] or {}).get('candidate_status','missing_data')
    if "data_quality" in pred:
        pick = out.get("pick")
        n_min = pred.get("data_n", pred["data_quality"] * 15)
        out.update(P.confidence(pick["p"] if pick else None, n_min))
    return out


def blend_predictions(conn, items: list[dict]) -> list[dict]:
    """Tahmin listesine eşleşen güncel iddaa oranlarını ekler (`iddaa_odds`, `iddaa_event_id`,
    `iddaa_updated_at`) ve `markets` içeren her tahmini harmanlar. Sıra korunur."""
    if not items:
        return items
    by_match = iddaa.odds_for_matches(conn, [item["id"] for item in items])
    out = []
    for item in items:
        source = by_match.get(str(item["id"]))
        odds = source["odds"] if source else {}
        blended = blend_prediction(item, odds) if "markets" in item else dict(item)
        blended["iddaa_odds"] = odds
        blended["iddaa_min_matches"] = source.get("min_matches", {}) if source else {}
        if blended.get("pick"):
            blended["pick"]["min_matches"] = blended["iddaa_min_matches"].get(blended["pick"]["market"], 1)
        if "lambda_home" in blended and "lambda_away" in blended:
            blended["extra_markets"] = extra_markets.for_match(
                blended["lambda_home"], blended["lambda_away"], odds)
        blended["iddaa_event_id"] = source["event_id"] if source else None
        blended["iddaa_updated_at"] = source["updated_at"] if source else None
        out.append(blended)
    return out


# --- Harmanlı / harmansız karşılaştırma (backtest) ---------------------------------------
# Değerlendirilen gruplar: ad -> (grup, gerçekleşen sonucun indeksi)
EVAL_GROUPS = {
    "1x2": ("1x2", lambda hg, ag: 0 if hg > ag else 1 if hg == ag else 2),
    "ou25": ("ou2.5", lambda hg, ag: 0 if hg + ag >= 3 else 1),
    "btts": ("btts", lambda hg, ag: 0 if hg > 0 and ag > 0 else 1),
}
SWEEP_WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)
VARIANTS = ("model", "blend", "market")


class BlendEval:
    """Aynı maçlar üzerinde model / harmanlı / yalnız piyasa olasılıklarını karşılaştırır.
    Brier = sonuçlar üzerinden toplanan kare hata (1X2 için rastgele ≈ 0.667, ikili pazarlar için 0.5).
    `sweep`: harman ağırlığına göre log-loss; en düşüğü, `market_weight` için en iyi adaydır."""

    def __init__(self, weight: float | None = None):
        self.weight = _clamp(MODEL["market_weight"] if weight is None else weight)
        self.acc = {g: {"n": 0, **{v: {"brier": 0.0, "logloss": 0.0, "hit": 0} for v in VARIANTS},
                        "sweep": {w: 0.0 for w in SWEEP_WEIGHTS}} for g in EVAL_GROUPS}
        self.matches = 0

    def add(self, model_markets: dict, iddaa_odds: dict, home_goals: int, away_goals: int) -> bool:
        """Maçı değerlendirmeye ekler; hiçbir grupta oran yoksa False."""
        implied = implied_groups(iddaa_odds)
        blended, _ = blend_markets(model_markets, iddaa_odds, self.weight)
        used = False
        for name, (group, outcome) in EVAL_GROUPS.items():
            if group not in implied:
                continue
            used = True
            keys = GROUPS[group]
            actual = outcome(home_goals, away_goals)
            pm = [model_markets[k]["p"] for k in keys]
            pq = [implied[group][k] for k in keys]
            a = self.acc[name]
            a["n"] += 1
            for variant, probs in zip(VARIANTS, (pm, [blended[k]["p"] for k in keys], pq)):
                v = a[variant]
                v["brier"] += sum((p - (i == actual)) ** 2 for i, p in enumerate(probs))
                v["logloss"] += -math.log(max(1e-9, probs[actual]))
                v["hit"] += int(max(range(len(probs)), key=probs.__getitem__) == actual)
            for w in SWEEP_WEIGHTS:
                a["sweep"][w] += -math.log(max(1e-9, blend_probs(pm, pq, w)[actual]))
        self.matches += int(used)
        return used

    def result(self) -> dict:
        groups = {}
        for name, a in self.acc.items():
            n = a["n"]
            if not n:
                continue
            groups[name] = {"n": n,
                            **{v: {"brier": round(a[v]["brier"] / n, 4), "logloss": round(a[v]["logloss"] / n, 4),
                                   "accuracy": round(a[v]["hit"] / n, 3)} for v in VARIANTS},
                            "sweep": {f"{w:g}": round(ll / n, 4) for w, ll in a["sweep"].items()}}
        return {"weight": self.weight, "matches": self.matches, "groups": groups}
