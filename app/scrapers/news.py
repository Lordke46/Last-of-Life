"""Google Haberler RSS'inden takım haberlerini çeker (anahtar gerekmez)."""
import hashlib
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from ..config import LEAGUES, NEWS_LOOKBACK_DAYS, REQUEST_DELAY_SEC, USER_AGENT, is_cup
from ..db import get_meta, set_meta
from ..news_intel import classify
from ..sentiment import analyze

# ESPN İngilizce/ASCII isimlerini Türkçe haber aramasında daha iyi sonuç veren hallere çevirir
TR_ALIASES = {
    "Besiktas": "Beşiktaş",
    "Fenerbahce": "Fenerbahçe",
    "Galatasaray": "Galatasaray",
    "Caykur Rizespor": "Rizespor",
    "Goztepe": "Göztepe",
    "Kasimpasa": "Kasımpaşa",
    "Istanbul Basaksehir": "Başakşehir",
    "Basaksehir": "Başakşehir",
    "Genclerbirligi": "Gençlerbirliği",
    "Eyupspor": "Eyüpspor",
    "Fatih Karagumruk": "Karagümrük",
    "Karagumruk": "Karagümrük",
    "Kocaelispor": "Kocaelispor",
    "Gaziantep FK": "Gaziantep FK",
    "Samsunspor": "Samsunspor",
    "Kayserispor": "Kayserispor",
    "Konyaspor": "Konyaspor",
    "Antalyaspor": "Antalyaspor",
    "Alanyaspor": "Alanyaspor",
    "Trabzonspor": "Trabzonspor",
    "Corum FK": "Çorum FK",
    "Erzurumspor": "Erzurumspor",
    "Erzurum BB": "Erzurumspor",
    "Amed SFK": "Amedspor",
    "Bodrum FK": "Bodrumspor",
    "Sivasspor": "Sivasspor",
    "Hatayspor": "Hatayspor",
}

# Erkek A takımı dışındaki branş/takım haberlerini ele
EXCLUDE = ["kadın", "kadin", "voleybol", "basketbol", "basket", "hentbol", "u19", "u21", "u17", "akademi",
           "women", "wsl", "ladies", "girls", "u23", "netball", "esports", "e-spor", "yüzme", "atletizm"]

_STOP = {"fc", "fk", "sk", "afc", "cf", "ac", "as", "ss", "sc", "club", "de", "the", "united", "city", "real"}


def fold(text: str) -> str:
    text = text.replace("ı", "i").replace("İ", "i")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


def key_tokens(team: str) -> list[str]:
    toks = [t for t in fold(team).replace("-", " ").split() if len(t) > 2 and t not in _STOP]
    return toks or [fold(team)]


def search_term(team: str, lang: str) -> str:
    return TR_ALIASES.get(team, team) if lang == "tr" else team


def rss_url(team: str, lang: str) -> str:
    if lang == "tr":
        q = f'"{search_term(team, lang)}" when:{NEWS_LOOKBACK_DAYS}d'
        params = {"q": q, "hl": "tr", "gl": "TR", "ceid": "TR:tr"}
    else:
        q = f'"{team}" football when:{NEWS_LOOKBACK_DAYS}d'
        params = {"q": q, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def fetch_team_news(team: str, lang: str, limit: int = 25) -> list[dict]:
    req = urllib.request.Request(rss_url(team, lang), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        root = ET.fromstring(resp.read())
    tokens = key_tokens(search_term(team, lang)) + key_tokens(team)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        source = it.findtext("source") or ""
        # Google başlığın sonuna " - Kaynak" ekler
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        ft = fold(title)
        if not any(t in ft for t in tokens):
            continue  # takım adı başlıkta geçmiyorsa alakasız olabilir
        if any(fold(x) in ft for x in EXCLUDE):
            continue
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc)
        except Exception:
            pub = datetime.now(timezone.utc)
        link = it.findtext("link") or title
        sentiment, injury = analyze(title)
        c = classify(title, team, TR_ALIASES.get(team))
        if c["category"] in ("out", "suspended", "doubt"):
            injury = 1
        elif c["category"] in ("return", "ignored"):
            injury = 0  # "sakatlıktan döndü" / rakip haberi / söylenti sakatlık sayılmaz
        items.append({
            "id": hashlib.sha1((team + link).encode()).hexdigest(),
            "title": title,
            "link": link,
            "source": source,
            "published": pub.isoformat(),
            "sentiment": sentiment,
            "injury": injury,
            "category": c["category"],
            "reason": c["rule"],
        })
        if len(items) >= limit:
            break
    return items


def sync_news(conn, days_ahead: int = 10, progress=None, budget_sec: float | None = 900, strict=False) -> int:
    """Önümüzdeki maçları olan takımlar için haberleri günceller."""
    now = datetime.now(timezone.utc)
    # Bir takım için en yakın maç bilinir; 24 saat içindeki maça 1 saat,
    # daha uzaktakine 6 saat haber TTL'i uygulanır.
    rows = conn.execute(
        "SELECT league,team,MIN(kickoff) AS nearest FROM ("
        "SELECT league,home AS team,kickoff FROM matches WHERE completed=0 AND kickoff BETWEEN ? AND ? "
        "UNION ALL SELECT league,away AS team,kickoff FROM matches WHERE completed=0 AND kickoff BETWEEN ? AND ?"
        ") q GROUP BY league,team ORDER BY nearest",
        ((now - timedelta(hours=3)).isoformat(), (now + timedelta(days=days_ahead)).isoformat()) * 2,
    ).fetchall()
    # Kupa takımlarının haberi kendi liglerinden gelir (kupa modeli onları birleştirir); ayrıca çekilmez
    rows = [r for r in rows if not is_cup(r["league"])]
    total = skipped = fetched = 0
    started = time.monotonic()
    for i, r in enumerate(rows, 1):
        key = "news_poll_" + hashlib.sha1(
            (r["league"] + "|" + r["team"]).encode("utf-8")).hexdigest()[:24]
        previous = get_meta(conn, key)
        near = datetime.fromisoformat(r["nearest"].replace("Z", "+00:00"))
        ttl = timedelta(hours=1 if near < now + timedelta(hours=24) else 6)
        if previous:
            try:
                if now - datetime.fromisoformat(previous.replace("Z", "+00:00")) < ttl:
                    skipped += 1
                    continue
            except ValueError:
                pass
        if budget_sec and time.monotonic() - started > budget_sec:
            if strict:
                raise RuntimeError(f"Haber süre sınırı doldu ({i - 1}/{len(rows)})")
            if progress:
                progress(f"Haber süre sınırı doldu ({i - 1}/{len(rows)}); kalanlar sonraki turda")
            break
        lang = LEAGUES[r["league"]]["lang"]
        if progress:
            progress(f"Haberler ({i}/{len(rows)}): {r['team']}")
        try:
            items = fetch_team_news(r["team"], lang)
        except Exception as exc:
            if strict:
                raise RuntimeError(f"Haber alınamadı: {r['team']}: {exc}") from exc
            if progress:
                progress(f"Haber alınamadı: {r['team']} ({exc})")
            continue
        for n in items:
            conn.execute(
                """INSERT INTO news(id, league, team, title, link, source, published, sentiment, injury, category, reason, fetched_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                   sentiment=excluded.sentiment, injury=excluded.injury,
                   category=excluded.category, reason=excluded.reason""",
                (n["id"], r["league"], r["team"], n["title"], n["link"], n["source"], n["published"],
                 n["sentiment"], n["injury"], n["category"], n["reason"], now.isoformat()),
            )
        total += len(items)
        fetched += 1
        set_meta(conn, key, now.isoformat())
        conn.commit()
        time.sleep(REQUEST_DELAY_SEC)
    if progress:
        progress(f"Haberler: {fetched} takım sorgulandı, {skipped} önbellekten atlandı, {total} haber işlendi")
    # Eski haberleri temizle
    conn.execute("DELETE FROM news WHERE published < ?", ((now - timedelta(days=30)).isoformat(),))
    return total
