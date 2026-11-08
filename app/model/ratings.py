"""Takım güç hesapları: Elo, zaman ağırlıklı Poisson güçleri, form, puan durumu."""
import math
from collections import defaultdict
from datetime import datetime

from ..config import MODEL

SEASON_GAP_DAYS = 40  # yaz aylarında bu kadar maçsız gün = yeni sezon


def new_season(prev: datetime | None, t: datetime) -> bool:
    """Uzun ara + yaz/sonbahar başlangıcı = yeni sezon. Ay şartı, 2022 Dünya Kupası gibi
    sezon ortası uzun araların yeni sezon sayılmasını engeller."""
    return prev is None or ((t - prev).days > SEASON_GAP_DAYS and t.month in (6, 7, 8, 9))


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def points(gf: int, ga: int) -> int:
    return 3 if gf > ga else 1 if gf == ga else 0


def season_start(matches: list[dict]) -> datetime | None:
    """Tamamlanmış maçlar listesinde son sezonun başlangıcını bulur."""
    start = None
    prev = None
    for m in matches:
        t = m["ts"]
        if new_season(prev, t):
            start = t
        prev = t
    return start


class Elo:
    def __init__(self):
        self.r: dict[str, float] = {}
        self.last_ts = None
        self.seasons = 1
        self.history: dict[str, list[tuple[str, float]]] = defaultdict(list)

    def get(self, team: str) -> float:
        return self.r.get(team, 1500.0)

    def expected(self, home: str, away: str) -> float:
        d = self.get(home) + MODEL["elo_home_adv"] - self.get(away)
        return 1 / (1 + 10 ** (-d / 400))

    def update(self, m: dict):
        if self.last_ts and new_season(self.last_ts, m["ts"]):
            # sezon arası: herkesi biraz ortalamaya çek
            k = MODEL["elo_season_regress"]
            self.r = {t: v + (1500 - v) * k for t, v in self.r.items()}
            self.seasons += 1
        for t in (m["home"], m["away"]):
            if t not in self.r:
                # ilk sezon herkes 1500; sonradan gelen (yükselen) takım daha düşük başlar
                self.r[t] = MODEL["elo_new_team"] if self.seasons > 1 else 1500.0
        e = self.expected(m["home"], m["away"])
        gf, ga = m["home_goals"], m["away_goals"]
        s = 1.0 if gf > ga else 0.5 if gf == ga else 0.0
        mult = math.log(abs(gf - ga) + 1) + 1 if gf != ga else 1.0
        delta = MODEL["elo_k"] * mult * (s - e)
        self.r[m["home"]] += delta
        self.r[m["away"]] -= delta
        day = m["kickoff"][:10]
        self.history[m["home"]].append((day, round(self.r[m["home"]], 1)))
        self.history[m["away"]].append((day, round(self.r[m["away"]], 1)))
        self.last_ts = m["ts"]


class Strengths:
    """Zaman ağırlıklı, lig ortalamasına büzülmüş hücum/savunma güçleri (ev & deplasman ayrı)."""

    def __init__(self, matches: list[dict], at: datetime):
        hl = MODEL["half_life_days"]
        k = MODEL["prior_matches"]
        acc = defaultdict(lambda: [0.0] * 6)  # w_home, gs_home, gc_home, w_away, gs_away, gc_away
        sw = sh = sa = 0.0
        for m in matches:
            age = (at - m["ts"]).total_seconds() / 86400
            if age < 0:
                continue
            w = 0.5 ** (age / hl)
            hg, ag = m["home_goals"], m["away_goals"]
            sw += w
            sh += w * hg
            sa += w * ag
            h = acc[m["home"]]
            h[0] += w; h[1] += w * hg; h[2] += w * ag
            a = acc[m["away"]]
            a[3] += w; a[4] += w * ag; a[5] += w * hg
        # Bir önsel maç: küçük/henüz golsüz örneklemde sıfıra bölünmeyi önler.
        self.mu_h = (sh + 1.45) / (sw + 1.0)
        self.mu_a = (sa + 1.15) / (sw + 1.0)
        self.n_weighted = {t: v[0] + v[3] for t, v in acc.items()}
        self.att_h, self.def_h, self.att_a, self.def_a = {}, {}, {}, {}
        mh, ma = self.mu_h, self.mu_a
        for t, v in acc.items():
            wh, gsh, gch, wa, gsa, gca = v[:6]
            # genel güç (ev+deplasman birlikte), ev/deplasman özel güçlerin önsel değeri olur
            att_all = ((gsh / mh if mh else 0) + (gsa / ma if ma else 0) + 2 * k) / (wh + wa + 2 * k)
            def_all = ((gch / ma if ma else 0) + (gca / mh if mh else 0) + 2 * k) / (wh + wa + 2 * k)
            self.att_h[t] = (gsh + k * mh * att_all) / ((wh + k) * mh)
            self.def_h[t] = (gch + k * ma * def_all) / ((wh + k) * ma)
            self.att_a[t] = (gsa + k * ma * att_all) / ((wa + k) * ma)
            self.def_a[t] = (gca + k * mh * def_all) / ((wa + k) * mh)

    def lambdas(self, home: str, away: str) -> tuple[float, float]:
        lh = self.mu_h * self.att_h.get(home, 0.9) * self.def_a.get(away, 1.1)
        la = self.mu_a * self.att_a.get(away, 0.9) * self.def_h.get(home, 1.1)
        return lh, la


def _team_side(match, team, team_id=None):
    if team_id is not None and (match.get('home_id') or match.get('away_id')):
        if str(match.get('home_id')) == str(team_id): return True
        if str(match.get('away_id')) == str(team_id): return False
        return None
    if match['home'] == team: return True
    if match['away'] == team: return False
    return None


def played_before(match, before):
    """Only completed competitive matches with conservative result-availability lag."""
    if match.get('completed') is not None and not match.get('completed'):return False
    if any(v in str(match.get('status','')).lower() for v in ('cancel','postpon','abandon','suspend')):return False
    if 'friendly' in str(match.get('league','')).lower():return False
    return (match.get('home_goals') is not None and match.get('away_goals') is not None
            and match['ts'].timestamp()+3*3600 <= before.timestamp())


def team_form(matches: list[dict], team: str, before: datetime, n: int = 5, team_id=None) -> list[dict]:
    out = []
    for m in reversed(matches):
        if not played_before(m, before):
            continue
        home = _team_side(m, team, team_id)
        if home is not None:
            gf, ga = (m["home_goals"], m["away_goals"]) if home else (m["away_goals"], m["home_goals"])
            out.append({
                "date": m["kickoff"][:10],
                "opponent": m["away"] if home else m["home"],
                "opponent_id": m.get("away_id" if home else "home_id"),
                "competition": m.get("league"),
                "venue": "E" if home else "D",
                "gf": gf, "ga": ga,
                "result": "G" if gf > ga else "B" if gf == ga else "M",
            })
            if len(out) >= n:
                break
    return out


def form_score(form: list[dict]) -> float:
    """Son maçlardan ağırlıklı maç başı puan (0-3), yeni maçlar daha ağır."""
    if not form:
        return 1.35
    tot = wsum = 0.0
    for i, f in enumerate(form):
        w = len(form) - i
        tot += w * points(f["gf"], f["ga"])
        wsum += w
    return tot / wsum


def standings(matches: list[dict], since: datetime | None) -> list[dict]:
    table = defaultdict(lambda: {"p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0,
                                 "hp": 0, "hpts": 0, "ap": 0, "apts": 0, "form": []})
    for m in matches:
        if since and m["ts"] < since:
            continue
        for team, gf, ga, home in ((m["home"], m["home_goals"], m["away_goals"], True),
                                   (m["away"], m["away_goals"], m["home_goals"], False)):
            r = table[team]
            p = points(gf, ga)
            r["p"] += 1; r["gf"] += gf; r["ga"] += ga; r["pts"] += p
            r["w" if p == 3 else "d" if p == 1 else "l"] += 1
            if home:
                r["hp"] += 1; r["hpts"] += p
            else:
                r["ap"] += 1; r["apts"] += p
            r["form"].append("G" if p == 3 else "B" if p == 1 else "M")
    rows = []
    for team, r in table.items():
        rows.append({"team": team, **{k: v for k, v in r.items() if k != "form"},
                     "gd": r["gf"] - r["ga"], "form": "".join(r["form"][-5:])})
    rows.sort(key=lambda r: (-r["pts"], -r["gd"], -r["gf"], r["team"]))
    for i, r in enumerate(rows, 1):
        r["pos"] = i
    return rows


def last_match_before(matches: list[dict], team: str, before: datetime, team_id=None) -> datetime | None:
    for m in reversed(matches):
        if played_before(m, before) and _team_side(m, team, team_id) is not None:
            return m["ts"]
    return None

