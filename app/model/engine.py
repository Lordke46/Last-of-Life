"""Tüm parametreleri birleştirip maç tahmini üreten ana motor.

Akış (her maç için):
  1. Poisson güçleri  -> λ_ev, λ_dep (zaman ağırlıklı, ev/deplasman ayrı hücum-savunma)
  2. Elo farkı        -> beklenen gol üstünlüğü, Poisson ile log-uzayda harmanlanır
  3. Form (son 5 maç) -> küçük çarpan
  4. Haber            -> kural tabanlı sakatlık/ceza/teknik direktör vb. (oyuncu başı ~-%3, takım başı en çok -%12)
  5. Dinlenme süresi  -> 4 günden az dinlenen takıma küçük ceza
  6. Dixon-Coles düzeltmeli skor matrisi -> 1X2, Alt/Üst, KG, skor olasılıkları -> oranlar
"""
import math
import threading
import time
from datetime import datetime, timedelta, timezone

from .. import news_intel, odds_log
from ..config import LEAGUES, MODEL, NEWS_LOOKBACK_DAYS, is_cup
from ..db import get_meta, session, set_meta
from . import predict as P
from .blend import BlendEval
from ..scrapers.news import TR_ALIASES
from .ratings import (Elo, Strengths, form_score, new_season, last_match_before, parse_ts, season_start,
                      standings, team_form)

_cache: dict = {}
_lock = threading.Lock()
# Serverless'ta her örneğin kendi önbelleği vardır; başka bir örnek/cron güncellediyse (last_updated_at
# değiştiyse) önbellek atılır. Kontrol en çok STAMP_TTL saniyede bir yapılır.
STAMP_TTL = 15.0
_stamp = {"value": None, "checked": 0.0}


def invalidate():
    with _lock:
        _cache.clear()
        _stamp["checked"] = 0.0


def _refresh_if_stale():
    if time.monotonic() - _stamp["checked"] < STAMP_TTL:
        return
    with session() as conn:
        current = (get_meta(conn, "last_updated_at"), get_meta(conn, "update_data_version"))
    with _lock:
        _stamp["checked"] = time.monotonic()
        if current != _stamp["value"]:
            _cache.clear()
            _stamp["value"] = current


def _load(conn, league: str):
    rows = conn.execute("SELECT * FROM matches WHERE league=? ORDER BY kickoff", (league,)).fetchall()
    done, upcoming = [], []
    for r in rows:
        m = dict(r)
        m["ts"] = parse_ts(m["kickoff"])
        (done if m["completed"] else upcoming).append(m)
    return done, upcoming


def news_scores(conn, league: str, at: datetime) -> dict:
    """Takım başına kural tabanlı haber etkisi (log-λ): att = kendi gol beklentisi, opp = rakibe giden etki.
    Haber yoksa değerler 0'dır; eksik haber akışı güveni tek başına düşürmez."""
    since = (at - timedelta(days=NEWS_LOOKBACK_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT team, title, link, source, published FROM news "
        "WHERE league=? AND published>=? ORDER BY published DESC", (league, since)).fetchall()
    by_team: dict[str, list[dict]] = {}
    for r in rows:
        by_team.setdefault(r["team"], []).append(dict(r))
    return {team: news_intel.summarize(team, items, at, parse_ts, TR_ALIASES.get(team))
            for team, items in by_team.items()}


class LeagueModel:
    elo_weight = None  # None: MODEL["elo_weight"]; alt sınıflar (Avrupa kupaları) üzerine yazabilir

    def __init__(self, league: str, done: list[dict], upcoming: list[dict], news: dict):
        self.league = league
        self.done = done
        self.upcoming = upcoming
        self.news = news
        self.elo = Elo()
        for m in done:
            self.elo.update(m)
        now = datetime.now(timezone.utc)
        self.strengths = Strengths(done, now)
        self.season_start = season_start(done)
        self.table = standings(done, self.season_start)
        self.pos = {r["team"]: r for r in self.table}
        self.total_goals = self.strengths.mu_h + self.strengths.mu_a

    # --- tek maç tahmini ---------------------------------------------------
    def predict(self, home: str, away: str, kickoff: datetime, strengths=None, elo=None,
                history=None, news=None, detail=True) -> dict:
        st = strengths or self.strengths
        el = elo or self.elo
        hist = self.done if history is None else history
        news = self.news if news is None else news

        lh_p, la_p = st.lambdas(home, away)
        elo_h, elo_a = el.get(home), el.get(away)
        diff = elo_h + MODEL["elo_home_adv"] - elo_a
        total = st.mu_h + st.mu_a
        sup = MODEL["elo_goals_per_point"] * diff  # Elo farkını gol üstünlüğüne çevirir
        lh_e = max(0.15, (total + sup) / 2)
        la_e = max(0.15, (total - sup) / 2)
        w = MODEL["elo_weight"] if self.elo_weight is None else self.elo_weight
        log_h = (1 - w) * math.log(lh_p) + w * math.log(lh_e)
        log_a = (1 - w) * math.log(la_p) + w * math.log(la_e)

        form_h = team_form(hist, home, kickoff)
        form_a = team_form(hist, away, kickoff)
        fs_h, fs_a = form_score(form_h), form_score(form_a)
        f_adj = MODEL["form_coef"] * (fs_h - fs_a) / 2
        log_h += f_adj
        log_a -= f_adj

        nh, na = news.get(home, {}), news.get(away, {})
        # Kendi eksikleri/kadro durumu + rakibin durumunun bana etkisi
        n_h = nh.get("att", 0.0) + na.get("opp", 0.0)
        n_a = na.get("att", 0.0) + nh.get("opp", 0.0)
        log_h += n_h
        log_a += n_a
        n_adj = n_h - n_a

        rest = {}
        for side, team in (("home", home), ("away", away)):
            last = last_match_before(hist, team, kickoff)
            rest[side] = round((kickoff - last).total_seconds() / 86400, 1) if last else None
        r_adj_h = -MODEL["rest_coef"] if rest["home"] is not None and rest["home"] < 4 else 0.0
        r_adj_a = -MODEL["rest_coef"] if rest["away"] is not None and rest["away"] < 4 else 0.0
        log_h += r_adj_h
        log_a += r_adj_a

        lh, la = math.exp(log_h), math.exp(log_a)
        mat = P.score_matrix(lh, la)
        mk = P.markets(mat)
        out = {"lambda_home": round(lh, 3), "lambda_away": round(la, 3), **mk}
        if not detail:
            return out

        n_h = st.n_weighted.get(home, 0)
        n_a = st.n_weighted.get(away, 0)
        data_q = min(1.0, min(n_h, n_a) / 15)
        n_min = min(n_h, n_a)
        pick = P.best_pick(mk["markets"])
        news_teams = sum(1 for t in (home, away) if news.get(t, {}).get("relevant", 0) > 0)
        out.update({
            "pick": pick,
            **P.confidence(pick["p"] if pick else None, n_min),
            "news_teams": news_teams,
            "data_n": round(n_min, 2),
            "data_quality": round(data_q, 4),  # iddaa oranlarıyla harmanlarken güven yeniden hesaplanır
            "factors": {
                "poisson": {"home": round(lh_p, 3), "away": round(la_p, 3),
                            "league_home_avg": round(st.mu_h, 2), "league_away_avg": round(st.mu_a, 2),
                            "att_home": round(st.att_h.get(home, 1), 2), "def_home": round(st.def_h.get(home, 1), 2),
                            "att_away": round(st.att_a.get(away, 1), 2), "def_away": round(st.def_a.get(away, 1), 2)},
                "elo": {"home": round(elo_h), "away": round(elo_a), "diff_with_home_adv": round(diff),
                        "expected_home": round(el.expected(home, away), 3),
                        "lambda_home": round(lh_e, 3), "lambda_away": round(la_e, 3)},
                "form": {"home": form_h, "away": form_a, "home_ppg": round(fs_h, 2), "away_ppg": round(fs_a, 2),
                         "log_adj": round(f_adj, 3)},
                "news": {"home": _news_view(nh), "away": _news_view(na), "log_adj": round(n_adj, 3),
                         "home_log": round(n_h, 4), "away_log": round(n_a, 4)},
                "rest": {"home_days": rest["home"], "away_days": rest["away"],
                         "home_adj": r_adj_h, "away_adj": r_adj_a},
                "table": {"home": self.pos.get(home), "away": self.pos.get(away)},
                "h2h": self.h2h(home, away),
            },
        })
        return out

    def h2h(self, a: str, b: str, n: int = 6) -> list[dict]:
        res = [m for m in self.done if {m["home"], m["away"]} == {a, b}]
        return [{"date": m["kickoff"][:10], "home": m["home"], "away": m["away"],
                 "score": f'{m["home_goals"]}-{m["away_goals"]}'} for m in res[-n:]][::-1]

    def upcoming_predictions(self, days: int = 14) -> list[dict]:
        now = datetime.now(timezone.utc)
        out = []
        for m in self.upcoming:
            if m["ts"] < now - timedelta(hours=2) or m["ts"] > now + timedelta(days=days):
                continue
            p = self.predict(m["home"], m["away"], m["ts"])
            # İleriye dönük haber A/B: aynı maç için yalnızca haber etkisini sıfırlayıp
            # karşıt tahmini kaydet. Sonucu daha sonra öğrenince ikisini eşleştirebiliriz.
            no_news = self.predict(m["home"], m["away"], m["ts"], news={}, detail=False)
            out.append({"id": m["id"], "league": self.league, "kickoff": m["kickoff"],
                        "home": m["home"], "away": m["away"], "status": m["status"],
                        "home_logo": _logo(m["home_id"]), "away_logo": _logo(m["away_id"]),
                        "no_news_markets": {k: v["p"] for k, v in no_news["markets"].items()}, **p})
        return out


def _logo(team_id):
    # ESPN kimlikleri sayıdır; TFF gibi başka kaynakların kimliklerinin ESPN logosu yoktur
    return f"https://a.espncdn.com/i/teamlogos/soccer/500/{team_id}.png" if team_id and str(team_id).isdigit() else None


def _news_view(n: dict) -> dict:
    if not n:
        return {"att_pct": 0.0, "opp_pct": 0.0, "counts": {}, "events": [], "relevant": 0, "total": 0,
                "absences": 0, "items": []}
    return {k: n[k] for k in ("att_pct", "opp_pct", "counts", "events", "relevant", "total", "absences")} | {
        "items": n["items"][:8]}


def get_model(league: str) -> LeagueModel:
    _refresh_if_stale()
    with _lock:
        if league in _cache:
            return _cache[league]
    if is_cup(league):
        from . import european  # ligler arası güç modeli (yerel lig modellerine bağlıdır)
        model = european.build_cup_model(league)
    else:
        with session() as conn:
            done, upcoming = _load(conn, league)
            news = news_scores(conn, league, datetime.now(timezone.utc))
        model = LeagueModel(league, done, upcoming, news)
    with _lock:
        _cache[league] = model
    return model


def walkforward_samples(league, done, min_history, step=1):
    """Aynı anda başlayan / henüz bitmemiş maçın sonucu geçmişe sızmasın."""
    done = sorted(done, key=lambda m: m["ts"])
    base, elo, cursor = LeagueModel(league, [], [], {}), Elo(), 0
    for idx in range(min_history, len(done), step):
        match = done[idx]
        cutoff = match["ts"] - timedelta(hours=3)
        while cursor < idx and done[cursor]["ts"] <= cutoff:
            elo.update(done[cursor])
            cursor += 1
        if cursor < min_history:
            continue
        hist = done[:cursor]
        markets = base.predict(match["home"], match["away"], match["ts"],
                               strengths=Strengths(hist, match["ts"]), elo=elo,
                               history=hist, news={}, detail=False)["markets"]
        yield match, markets


# --- Geriye dönük test --------------------------------------------------------
def backtest(league: str, min_history: int | None = None, step: int = 1) -> dict:
    """Her maçı, sadece o maçtan önceki verilerle tahmin edip gerçek sonuçla karşılaştırır.
    Varsayılan olarak ilk sezon ısınma için kullanılır, test ikinci sezondan başlar.
    (Geçmiş haber verisi olmadığından haber etkisi bu testte yoktur.)
    Ana metrikler yalnız modeldir (harmansız). Kaydedilmiş iddaa oranı olan maçlar (odds_log) ayrıca
    `blend` altında model / harmanlı / yalnız piyasa olarak karşılaştırılır; kayıt yoksa `blend` boştur."""
    with session() as conn:
        done, _ = _load(conn, league)
        try:
            logged = odds_log.load_for_league(conn, league)
        except Exception:  # odds_log tablosu henüz yok (migration uygulanmadı)
            conn.rollback()
            logged = {}
    if min_history is None:
        min_history = next((i for i in range(1, len(done))
                            if new_season(done[i - 1]["ts"], done[i]["ts"])), 120)
    if len(done) <= min_history:
        return {"league": league, "n": 0}
    blend_eval = BlendEval()
    n = hit = 0
    brier = logloss = 0.0
    ou_hit = 0
    for m, p in walkforward_samples(league, done, min_history, step):
        probs = [p["1"]["p"], p["X"]["p"], p["2"]["p"]]
        hg, ag = m["home_goals"], m["away_goals"]
        actual = 0 if hg > ag else 1 if hg == ag else 2
        n += 1
        hit += int(max(range(3), key=lambda i: probs[i]) == actual)
        brier += sum((probs[i] - (1 if i == actual else 0)) ** 2 for i in range(3))
        logloss += -math.log(max(1e-9, probs[actual]))
        ou_hit += int((p["Ü2.5"]["p"] >= 0.5) == (hg + ag >= 3))
        if m["id"] in logged:
            blend_eval.add(p, logged[m["id"]], hg, ag)
    if not n:
        return {"league": league, "n": 0}
    return {"league": league, "n": n, "from": done[min_history]["kickoff"][:10],
            "accuracy_1x2": round(hit / n, 3), "brier": round(brier / n, 3),
            "logloss": round(logloss / n, 3), "accuracy_ou25": round(ou_hit / n, 3),
            "blend": blend_eval.result()}


def run_backtests(persist=True) -> dict:
    # Kupalar için geriye dönük test yok: ligler arası güç, o gün bilinen veriyle yeniden kurulamıyor
    res = {lg: backtest(lg) for lg in LEAGUES if not is_cup(lg)}
    if persist:
        with session() as conn:
            import json
            set_meta(conn, "backtest", json.dumps(res))
            set_meta(conn, "backtest_at", datetime.now(timezone.utc).isoformat())
    return res


def cached_backtest() -> dict | None:
    import json
    with session() as conn:
        v = get_meta(conn, "backtest")
        at = get_meta(conn, "backtest_at")
    return {"results": json.loads(v), "at": at} if v else None
