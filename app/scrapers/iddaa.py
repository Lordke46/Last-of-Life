"""iddaa.com'un herkese açık maç öncesi bülteninden oranları çeker.

Site bu uç noktayı kendi program sayfasında XHR ile kullanıyor; API anahtarı veya
oturum gerektirmiyor. Alan adları kısa tutulduğu için market sözleşmesi burada
tek yerde çevrilir. Site sözleşmesi değişirse uygulamanın geri kalanı etkilenmez.
"""
import gzip
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
import re
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher

from .. import odds_log
from ..config import USER_AGENT
from .. import odds_history
from ..db import set_meta, get_meta

EVENTS_URL = "https://sportsbookv2.iddaa.com/sportsbook/events?st=1&type=0&version=0"
COMPETITIONS_URL = "https://sportsbookv2.iddaa.com/sportsbook/competitions"

# iddaa competition id -> ESPN lig anahtarı. Kimlikler /sportsbook/competitions
# yanıtındaki kalıcı futbol organizasyonu kimlikleridir.
COMPETITION_IDS = {
    584: "tur.1",   # Türkiye Süper Lig
    43: "eng.1",    # İngiltere Premier Lig
    129: "esp.1",   # İspanya La Liga
    143: "ita.1",   # İtalya Serie A
    45: "ger.1",    # Almanya Bundesliga
    381: "fra.1",   # Fransa Ligue 1
    1980: "tur.2",  # Türkiye 1. Lig (TFF)
    322: "ned.1",   # Hollanda Eredivisie
    566: "por.1",   # Portekiz Premier Lig (Liga Portugal)
    # UEFA kupaları: katalogda her biri için iki kayıt var (eski kalıcı kimlik + güncel kimlik); hangisinin maç
    # bülteninde kullanıldığı UEFA maç günü olmadan gözlenemediği için ikisi de eşlenir. Maç olmayan
    # (turnuva/şampiyon bahisleri) kayıtların takım adı olmadığı için parse_event bunları eler.
    126: "uefa.champions", 46194: "uefa.champions",   # UEFA Şampiyonlar Ligi
    46195: "uefa.europa", 588: "uefa.europa",         # UEFA Avrupa Ligi (588: eleme turları)
}

# İddaa bülteninde ülke/organizasyon adı her sürümde aynı alanda olmayabiliyor.
_COUNTRY_KEYS = ("countryName", "country", "cn", "country_name", "cname")
_LEAGUE_KEYS = ("competitionName", "competition", "leagueName", "league", "tournamentName", "n")


def _label(raw: dict, keys: tuple[str, ...], fallback: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for name in ("name", "n", "title"):
                if isinstance(value.get(name), str) and value[name].strip():
                    return value[name].strip()
    return fallback


def event_labels(raw: dict) -> tuple[str, str]:
    return (_label(raw, _COUNTRY_KEYS, "Diğer"),
            _label(raw, _LEAGUE_KEYS, f"Lig {raw.get('ci', 'diğer')}"))

_CHAR_MAP = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
                           "ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "æ": "ae"})
_NON_WORD = re.compile(r"[^a-z0-9]+")

# Kaynaklar arasında düzenli görülen kısa/ad değişiklikleri. Değerler yalnızca
# eşleştirme anahtarıdır; arayüzde takım adını değiştirmez.
_ALIAS_GROUPS = {
    "manchester city": ["man city"],
    "manchester united": ["man utd"],
    "nottingham forest": ["nottm forest"],
    "wolverhampton": ["wolverhampton wanderers", "wolves"],
    "tottenham": ["tottenham hotspur", "spurs"],
    "newcastle": ["newcastle united"],
    "west ham": ["west ham united"],
    "brighton": ["brighton hove albion", "brighton and hove albion"],
    "psg": ["paris saint germain", "paris sg"],
    "marseille": ["olympique marseille", "olympique de marseille"],
    "lyon": ["olympique lyon", "olympique lyonnais"],
    "inter": ["inter milan", "internazionale"],
    "ac milan": ["milan"],
    "hellas verona": ["verona"],
    "atletico madrid": ["atletico de madrid"],
    "athletic bilbao": ["athletic club"],
    "celta vigo": ["celta de vigo"],
    "bayern munich": ["bayern munchen", "bayern munih", "fc bayern munchen"],
    "borussia monchengladbach": ["bor monchengladbach", "gladbach"],
    "mainz": ["mainz 05", "1 fsv mainz 05"],
    "rb leipzig": ["rasenballsport leipzig"],
    "basaksehir": ["istanbul basaksehir", "rams basaksehir"],
    "gaziantep": ["gaziantep fk", "gazisehir gaziantep"],
    "rizespor": ["caykur rizespor"],
    "goztepe": ["goztepe sk"],
    "kasimpasa": ["kasimpasa sk"],
    "eyupspor": ["ikas eyupspor"],
    "fenerbahce": ["fenerbahce sk"],
    "galatasaray": ["galatasaray sk"],
    "besiktas": ["besiktas jk"],
    "trabzonspor": ["trabzonspor sk"],
    "amed": ["amed sfk", "amed sportif faaliyetler", "amedspor"],
    "genclerbirligi": ["gencler birligi", "genclerbirligi sk"],
    "erzurum": ["erzurum bb", "erzurumspor", "bb erzurumspor"],
    "corum": ["corum fk", "corumspor"],
    "karagumruk": ["fatih karagumruk", "istanbul fatih karagumruk"],
    "kocaelispor": ["kocaeli", "kocaelispor sk"],
    "alanyaspor": ["corendon alanyaspor"],
    "antalyaspor": ["bitexen antalyaspor"],
    "kayserispor": ["mondihome kayserispor"],
    "konyaspor": ["tumosan konyaspor"],
    "samsunspor": ["yilport samsunspor"],
    "koln": ["fc koln", "fc cologne", "cologne", "1 fc koln"],
    "monchengladbach": ["borussia monchengladbach", "bor monchengladbach", "gladbach", "b monchengladbach"],
    # Hollanda Eredivisie
    "ajax": ["ajax amsterdam"],
    "feyenoord": ["feyenoord rotterdam"],
    "psv": ["psv eindhoven"],
    "zwolle": ["pec zwolle"],
    "twente": ["fc twente", "twente enschede"],
    "utrecht": ["fc utrecht"],
    "groningen": ["fc groningen"],
    "heracles": ["heracles almelo"],
    "cambuur": ["sc cambuur"],
    "den haag": ["ado den haag"],
    "volendam": ["fc volendam"],
    "nac breda": ["nac"],
    "go ahead eagles": ["ga eagles"],
    # Portekiz Liga
    "braga": ["sporting braga", "sporting de braga", "sc braga"],
    "nacional": ["c d nacional", "cd nacional", "nacional da madeira"],
    "estoril": ["estoril praia", "gd estoril praia"],
    "sporting cp": ["sporting lizbon", "sporting lisbon"],
    "vitoria guimaraes": ["vitoria de guimaraes", "vitoria sc", "guimaraes"],
    "estrela": ["cf estrela", "estrela amadora", "cf estrela da amadora"],
    "famalicao": ["fc famalicao"],
    "arouca": ["fc arouca"],
    "alverca": ["fc alverca"],
    "moreirense": ["moreirense fc"],
    "tondela": ["cd tondela"],
    "santa clara": ["cd santa clara"],
    # Avrupa kupaları (ligleri kapsama dışı takımlar dahil)
    "salzburg": ["rb salzburg", "red bull salzburg", "fc red bull salzburg"],
    "slavia prague": ["slavia prag"],
    "sparta prague": ["sparta prag"],
    "viktoria plzen": ["plzen", "fc viktoria plzen"],
    "union saint gilloise": ["union st gilloise", "union sg", "royale union saint gilloise"],
    "borussia dortmund": ["b dortmund", "dortmund", "bor dortmund"],
    "bayer leverkusen": ["leverkusen", "bayer 04 leverkusen"],
    "eintracht frankfurt": ["frankfurt", "ein frankfurt"],
    "vfb stuttgart": ["stuttgart"],
    "sc freiburg": ["freiburg"],
    "hoffenheim": ["tsg hoffenheim", "1899 hoffenheim"],
    "real betis": ["betis"],
    "stade rennais": ["rennes"],
    "lille": ["lille osc", "losc lille"],
    "lens": ["rc lens"],
    "nice": ["ogc nice"],
    "red star belgrade": ["crvena zvezda", "red star", "kizil yildiz"],
    "shakhtar donetsk": ["shakhtar", "sahtar donetsk"],
    "dinamo zagreb": ["gnk dinamo zagreb"],
    "ludogorets": ["ludogorets razgrad"],
    "levski sofia": ["levski"],
    "jagiellonia": ["jagiellonia bialystok"],
    "lech poznan": ["lech"],
    "kairat": ["kairat almaty"],
    "copenhagen": ["f c kobenhavn", "fc kobenhavn", "kobenhavn", "kopenhag"],
    "omonia": ["omonia nicosia", "omonoia"],
    "paok": ["paok salonika", "paok selanik"],
    "aek athens": ["aek atina"],
    "olympiacos": ["olympiakos", "olimpiyakos"],
    "rangers": ["glasgow rangers"],
    "young boys": ["bsc young boys"],
    "basel": ["fc basel 1893"],
    "lask": ["lask linz"],
    "sturm graz": ["sk sturm graz"],
    "ferencvaros": ["ferencvarosi tc", "ferencvaros tc"],
    "celje": ["nk celje"],
    "ofi crete": ["ofi", "ofi girit"],
    "maccabi tel aviv": ["maccabi tel-aviv"],
    "hapoel beer sheva": ["hapoel be er sheva", "hapoel beer-sheva"],
    "malmo": ["malmo ff"],
}


def _basic_name(value: str) -> str:
    value = (value or "").translate(_CHAR_MAP).casefold()
    value = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))
    value = _NON_WORD.sub(" ", value).strip()
    tokens = [t for t in value.split() if t not in {"fc", "cf", "fk", "sk", "as", "ssc", "afc"}]
    return " ".join(tokens)


TEAM_ALIASES = {}
for _canonical, _variants in _ALIAS_GROUPS.items():
    for _name in [_canonical, *_variants]:
        TEAM_ALIASES[_basic_name(_name)] = _canonical


def normalize_team(value: str) -> str:
    normalized = _basic_name(value)
    return TEAM_ALIASES.get(normalized, normalized)


def team_similarity(a: str, b: str) -> float:
    a, b = normalize_team(a), normalize_team(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    token_score = 2 * len(ta & tb) / (len(ta) + len(tb))
    return max(token_score, SequenceMatcher(None, a, b).ratio())


def fetch_json(url: str = EVENTS_URL, timeout: int = 25, retries: int = 3) -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Origin": "https://www.iddaa.com",
        "Referer": "https://www.iddaa.com/program/futbol",
    }
    req = urllib.request.Request(url, headers=headers)
    last_error = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                data = json.loads(raw.decode("utf-8"))
            if not data.get("isSuccess"):
                raise RuntimeError(data.get("message") or "iddaa başarısız yanıt verdi")
            return data
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"iddaa bülteni alınamadı: {last_error}")


def fetch_competitions(timeout: int = 10) -> dict[int, dict]:
    """Load the public catalog used by the events API to resolve competition labels."""
    req = urllib.request.Request(COMPETITIONS_URL, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json",
        "Origin": "https://www.iddaa.com", "Referer": "https://www.iddaa.com/program/futbol",
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("data", []) if payload.get("isSuccess") else []
    return {int(row["i"]): row for row in rows if row.get("i") is not None}


def _market_odds(markets: list[dict]) -> dict[str, float]:
    odds: dict[str, float] = {}
    for market in markets or []:
        if market.get("s") != 1:
            continue
        key = (market.get("t"), market.get("st"))
        outcomes = market.get("o") or []
        mapped: dict[int, str] = {}
        if key == (1, 1):
            mapped = {1: "1", 2: "X", 3: "2"}
        elif key == (2, 92):
            mapped = {1: "1X", 2: "12", 3: "X2"}
        elif key == (2, 89):
            mapped = {1: "KG Var", 2: "KG Yok"}
        elif key == (2, 101) and str(market.get("sov")) in {"1.5", "2.5", "3.5"}:
            line = str(market["sov"])
            mapped = {1: f"A{line}", 2: f"Ü{line}"}
        elif key == (2, 88):
            mapped = {1: "İY 1", 2: "İY X", 3: "İY 2"}
        elif key == (2, 90):
            mapped = {i: f"İY/MS {name}" for i, name in enumerate(
                ("1/1", "1/X", "1/2", "X/1", "X/X", "X/2", "2/1", "2/X", "2/2"), 1)}
        elif key == (2, 100) and market.get("sov") is not None:
            line = str(market["sov"])
            mapped = {1: f"Handikap {line} 1", 2: f"Handikap {line} X", 3: f"Handikap {line} 2"}
        elif key == (2, 60) and str(market.get("sov")) in {"0.5", "1.5", "2.5", "3.5"}:
            line = str(market["sov"])
            mapped = {1: f"İY A{line}", 2: f"İY Ü{line}"}
        elif key == (2, 48) and market.get("sov") is not None:
            line = str(market["sov"])
            mapped = {1: f"Korner A{line}", 2: f"Korner Ü{line}"}
        elif key == (2, 658) and market.get("sov") is not None:
            line = str(market["sov"])
            mapped = {1: f"Kart A{line}", 2: f"Kart Ü{line}"}
        if not mapped:
            continue
        for outcome in outcomes:
            market_name = mapped.get(outcome.get("no"))
            odd = outcome.get("odd")  # iddaa web arayüzü odd gösterir; wodd üzeri çizili eski orandır.
            if market_name and not isinstance(odd, bool) and isinstance(odd, (int, float)) and math.isfinite(odd) and odd > 1:
                odds[market_name] = round(float(odd), 2)
    return odds


def parse_event(raw: dict) -> dict | None:
    if raw.get("sid", 1) != 1 or not raw.get("d") or not raw.get("hn") or not raw.get("an"):
        return None  # takım adı olmayan kayıtlar maç değil (turnuva/şampiyon bahsi)
    try:
        competition_id = int(raw.get("ci"))
    except (TypeError, ValueError):
        return None
    country, league_name = event_labels(raw)
    league = COMPETITION_IDS.get(competition_id) or small_league_key(country, league_name)
    constraints = {}
    for market in raw.get('m') or []:
        try:
            minimum = max(1, min(30, int(market.get('mbc', raw.get('mbc', 1)))))
        except (TypeError, ValueError):
            minimum = 1
        for name in _market_odds([market]):
            constraints[name] = minimum
    return {
        "id": str(raw["i"]),
        "league": league,
        "competition_id": competition_id,
        "league_name": league_name,
        "country": country,
        "kickoff": datetime.fromtimestamp(raw["d"], timezone.utc).isoformat(),
        "home": raw.get("hn") or "",
        "away": raw.get("an") or "",
        "odds": _market_odds(raw.get("m") or []),
        "min_matches": constraints,
    }


def small_league_key(country: str, competition: str) -> str:
    """Bültendeki ana lig dışı organizasyon için kararlı ve güvenli sınıflandırma anahtarı."""
    def slug(value):
        value = _basic_name(str(value))
        return re.sub(r"\s+", "-", value)[:60] or "diger"
    return f"small:{slug(country)}:{slug(competition)}"


# Benzer isim / yakın başlama saatinden yanlış maç eşleşmesini önle.
MATCH_AMBIGUITY_GAP = 0.03


def match_event(event: dict, fixtures: list[dict]) -> tuple[dict | None, float]:
    kickoff = datetime.fromisoformat(event["kickoff"].replace("Z", "+00:00"))
    candidates = []
    for fixture in fixtures:
        if fixture["league"] != event["league"]:
            continue
        fixture_time = datetime.fromisoformat(fixture["kickoff"].replace("Z", "+00:00"))
        hours = abs((fixture_time - kickoff).total_seconds()) / 3600
        if hours > 6:
            continue
        home_score = team_similarity(event["home"], fixture["home"])
        away_score = team_similarity(event["away"], fixture["away"])
        if min(home_score, away_score) < 0.72 or (home_score + away_score) / 2 < 0.84:
            continue
        score = 0.45 * home_score + 0.45 * away_score + 0.10 * (1 - hours / 6)
        candidates.append((score, fixture))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if not candidates:
        return None, 0.0
    # İki farklı maç eşit derecede uyuyorsa oranları hiçbirine bağlama.
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < MATCH_AMBIGUITY_GAP:
        return None, 0.0
    return candidates[0][1], round(candidates[0][0], 3)


def enrich_events(raw_events: list[dict], budget_seconds: float = 20) -> dict:
    """Özet bültende olmayan pazarları tam maç detayından al; DB yalnız ana iş parçacığında yazılır."""
    candidates = sorted(raw_events, key=lambda e: (e.get('ci') not in COMPETITION_IDS, e.get('d', 0)))
    # Büyük bültenlerde hızlı güncelleme süresi sınırlı kalır; kapsam yanıt istatistiğinde görünür.
    candidates = candidates[:400]
    def fetch(event):
        data = fetch_json(f"https://sportsbookv2.iddaa.com/sportsbook/event/{event['i']}", timeout=6, retries=1)['data']
        if str(data.get('i')) != str(event['i']) or not isinstance(data.get('m'), list):
            raise ValueError('Maç detay kimliği uyuşmuyor')
        return data
    pool = ThreadPoolExecutor(max_workers=6)
    jobs = {pool.submit(fetch, e): e for e in candidates}
    loaded = failed = 0
    try:
        for job in as_completed(jobs, timeout=budget_seconds):
            event = jobs[job]
            try:
                detail = job.result()
                markets = {m['i']: m for m in event.get('m', []) if 'i' in m}
                for m in detail['m']:
                    if 'i' not in m:
                        continue
                    previous = markets.get(m['i'])
                    if previous is None or m.get('v', 0) >= previous.get('v', 0):
                        markets[m['i']] = m
                event['m'] = list(markets.values())
                loaded += 1
            except Exception:
                failed += 1
    except FuturesTimeout:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return {'detail_requested': len(candidates), 'detail_loaded': loaded,
            'detail_failed': failed, 'detail_skipped': len(raw_events)-loaded-failed}


def sync_odds(conn, progress=None, retries: int = 3) -> dict:
    payload = fetch_json(retries=retries)
    if payload.get('data', {}).get('isdiff'):
        raise RuntimeError('Tam bülten yerine artımlı yanıt alındı; önceki oranlar korundu')
    raw_events = payload["data"].get("events", [])
    if not isinstance(raw_events, list) or not raw_events:
        raise RuntimeError('Boş/geçersiz bülten; önceki oranlar korundu')
    detail_stats = enrich_events(raw_events)
    try:
        catalog = fetch_competitions()
    except Exception:
        catalog = {}
    for raw in raw_events:
        competition = catalog.get(int(raw["ci"])) if raw.get("ci") is not None else None
        if competition:
            raw.setdefault("competitionName", competition.get("n") or competition.get("sn"))
            raw.setdefault("countryName", competition.get("cid"))
    events = [parsed for raw in raw_events if (parsed := parse_event(raw))]
    if not events:
        raise RuntimeError('Bültenden geçerli futbol maçı ayrıştırılamadı; önceki oranlar korundu')
    fixtures = [dict(row) for row in conn.execute(
        "SELECT id, league, kickoff, home, away FROM matches WHERE completed=0"
    ).fetchall()]
    now = datetime.now(timezone.utc).isoformat()

    # Yalnız başarılı ve parse edilebilir bir yanıttan sonra eski bülteni değiştir.
    from .. import bulletin_archive
    bulletin_archive.preserve_current(conn)
    conn.execute("DELETE FROM iddaa_odds")
    conn.execute("DELETE FROM iddaa_events")
    # Bir ESPN maçına en fazla bir iddaa etkinliği bağlanır; aksi halde iki etkinliğin
    # (ör. aynı maçın özel/kopya kaydı) oranları tek maçta karışırdı.
    best_for_fixture: dict[str, tuple[float, str]] = {}
    pairs = []
    for event in events:
        if event["league"].startswith("small:"):
            pairs.append((event, None, 0.0))  # küçük ligler kendi bülten kimliğinde kalır
            continue
        fixture, score = match_event(event, fixtures)
        pairs.append((event, fixture, score))
        if fixture and score > best_for_fixture.get(fixture["id"], (0.0, ""))[0]:
            best_for_fixture[fixture["id"]] = (score, event["id"])
    # Aynı ESPN maçına neredeyse eşit puanla birden çok bülten etkinliği
    # bağlanıyorsa yanlış oran seçmek yerine hepsini eşleşmemiş bırak.
    competing = {}
    for event, fixture, score in pairs:
        if fixture:
            competing.setdefault(fixture["id"], []).append((score, event["id"]))
    ambiguous_ids = {mid for mid, choices in competing.items()
                     if len(choices) > 1
                     and sorted((s for s, _ in choices), reverse=True)[0]
                     - sorted((s for s, _ in choices), reverse=True)[1] < MATCH_AMBIGUITY_GAP}
    matched = odds_count = 0
    event_rows, odds_rows = [], []
    log_rows = []  # maç öncesi oranlar ileriye dönük kayda (odds_log) da yazılır
    for event, fixture, score in pairs:
        if fixture and (fixture["id"] in ambiguous_ids
                        or best_for_fixture[fixture["id"]][1] != event["id"]):
            fixture, score = None, 0.0
        match_id = fixture["id"] if fixture else None
        event.update(match_id=match_id, updated_at=now, is_small=int(event["league"].startswith("small:")))
        matched += int(fixture is not None)
        event_rows.append(
            (event["id"], event["league"], event["competition_id"], event["kickoff"],
             event["home"], event["away"], match_id, score or None, event["league_name"],
             event["country"], int(event["league"].startswith("small:")), now),
        )
        for market, odd in event["odds"].items():
            odds_rows.append((event["id"], market, odd))
            odds_count += 1
            if match_id and event["kickoff"] > now:
                log_rows.append((match_id, market, odd))

    conn.executemany(
        """INSERT INTO iddaa_events
           (id, league, competition_id, kickoff, home, away, match_id, match_score,
            league_name, country, is_small, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", event_rows)
    conn.executemany("INSERT INTO iddaa_odds(event_id, market, odd) VALUES (?, ?, ?)", odds_rows)
    bulletin_archive.record(conn, events)
    odds_history.record_snapshot(conn, events, now)
    odds_history.prune(conn)

    stats = {"bulletin_events": len(raw_events), "supported_events": len(events),
             "small_league_events": sum(e["league"].startswith("small:") for e in events),
             "matched": matched, "odds": odds_count,
             "ambiguous_fixtures": len(ambiguous_ids),
             "rate": round(matched / max(1, sum(not e['league'].startswith('small:') for e in events)), 3),
             "unmatched_main": sum(not e['league'].startswith('small:') for e in events)-matched,
             "markets_received": sum(len(e.get('m', [])) for e in raw_events), **detail_stats}
    set_meta(conn, "iddaa_market_constraints", json.dumps({e["id"]:e["min_matches"] for e in events}))
    set_meta(conn, "iddaa_last_update", now)
    set_meta(conn, "iddaa_last_error", "")
    set_meta(conn, "iddaa_stats", json.dumps(stats, ensure_ascii=False))
    if progress:
        pct = f"%{stats['rate'] * 100:.0f}" if stats["rate"] is not None else "-"
        progress(f"iddaa.com: bültende {len(raw_events)} futbol maçı; desteklenen liglerde "
                 f"{len(events)} maç, {matched} eşleşme ({pct}), {odds_count} oran")
    odds_log.record_safely(conn, log_rows, now)
    return stats


def odds_for_matches(conn, match_ids) -> dict[str, dict]:
    """ESPN maç kimliği -> {"event_id", "updated_at", "odds": {pazar: oran}} (yalnız eşleşenler)."""
    match_ids = [str(m) for m in match_ids]
    if not match_ids:
        return {}
    marks = ",".join("?" for _ in match_ids)
    rows = conn.execute(
        f"""SELECT e.match_id, e.id event_id, e.updated_at, o.market, o.odd
            FROM iddaa_events e JOIN iddaa_odds o ON o.event_id=e.id
            WHERE e.match_id IN ({marks})""", match_ids).fetchall()
    out: dict[str, dict] = {}
    constraints = load_constraints(conn)
    for row in rows:
        entry = out.setdefault(row["match_id"], {
            "event_id": row["event_id"], "updated_at": row["updated_at"], "odds": {},
            "min_matches": constraints.get(row['event_id'], {})})
        entry["odds"][row["market"]] = row["odd"]
    return out


def load_constraints(conn):
    try:
        return json.loads(get_meta(conn, 'iddaa_market_constraints') or '{}')
    except (TypeError, ValueError):
        return {}
