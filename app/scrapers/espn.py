"""ESPN'in herkese açık fikstür sayfalarından maç sonuçlarını ve fikstürü çeker.

API anahtarı kullanılmaz: sayfa HTML'inin içine gömülü JSON (window['__espnfitt__'])
okunur. Her istek, verilen tarihin çevresindeki ~1 haftalık pencereyi döndürür.
"""
import gzip
import json
import re
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

from ..config import (FIXTURE_DAYS_AHEAD, HISTORY_START, LEGACY_HISTORY_START, REQUEST_DELAY_SEC,
                      USER_AGENT)
from ..db import get_meta, set_meta

URL = "https://www.espn.com/soccer/fixtures/_/date/{day}/league/{league}"
_EMBED = re.compile(r"window\['__espnfitt__'\]=(\{.*?\});</script>", re.S)


def _goals(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_html(url: str, timeout: int = 25, retries: int = 3) -> str:
    # gzip ile sayfa ~540 KB yerine ~75 KB iner
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    for attempt in range(retries):
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            body = raw.decode("utf-8", errors="ignore")
        if resp.status == 200 and body:
            return body
        time.sleep(2 * (attempt + 1))  # 202/boş gövde: kısa bekleyip tekrar dene
    raise RuntimeError(f"Boş yanıt: {url}")



# ESPN'nin HTML fikstür sayfası bot korumasında boş dönebilir. Aynı ESPN
# verisinin JSON scoreboard uç noktası yedek kaynaktır; maç ID'leri değişmez.
API_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard?dates={day}"


def fetch_api_window(league: str, day: date, timeout: int = 25) -> dict[str, list[dict]]:
    # ESPN 2026'dan beri tarihler arası sorgulara HTTP 400 verebiliyor.
    # Scoreboard uç noktası tek günü destekler; bir haftalık veri varsayılmaz.
    url = API_URL.format(league=league, day=day.strftime("%Y%m%d"))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        if resp.status != 200 or not raw:
            raise RuntimeError(f"ESPN JSON HTTP {resp.status} / boş yanıt: {url}")
    data = json.loads(raw)
    if not isinstance(data.get("events"), list):
        raise RuntimeError(f"ESPN JSON events alanı yok: {url}")
    result: dict[str, list[dict]] = {}
    for event in data["events"]:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competitors = competitions[0].get("competitors") or []
        typ = (event.get("status") or {}).get("type") or {}
        normalized = {
            "id": event["id"], "date": event.get("date"),
            "completed": bool(typ.get("completed")),
            "status": {"state": typ.get("state"), "detail": typ.get("shortDetail") or typ.get("name")},
            "competitors": [
                {"isHome": c.get("homeAway") == "home",
                 "id": (c.get("team") or {}).get("id") or c.get("id"),
                 "displayName": (c.get("team") or {}).get("displayName") or c.get("displayName"),
                 "score": c.get("score")}
                for c in competitors
            ],
        }
        if normalized["date"]:
            key = normalized["date"][:10].replace("-", "")
            result.setdefault(key, []).append(normalized)
    return result


def fetch_window(league: str, day: date, **kw) -> dict[str, list[dict]]:
    # JSON tek günlük endpoint ana kaynaktır. HTML bot korumasından boş dönebildiği
    # için her sorguda önce HTML'yi beklemek gereksiz gecikme ve hata üretir.
    try:
        return fetch_api_window(league, day, timeout=kw.get("timeout", 25))
    except Exception as api_error:
        try:
            html = fetch_html(URL.format(day=day.strftime("%Y%m%d"), league=league), **kw)
            m = _EMBED.search(html)
            if not m:
                raise RuntimeError("HTML içerisinde ESPN fikstür verisi yok")
            data = json.loads(m.group(1))
            events = data.get("page", {}).get("content", {}).get("events")
            if not isinstance(events, dict):
                raise RuntimeError("HTML fikstür events alanı yok")
            return events
        except Exception as html_error:
            raise RuntimeError(
                f"ESPN JSON ve HTML erişimi başarısız: JSON={api_error}; HTML={html_error}"
            ) from html_error


def parse_event(ev: dict, league: str) -> dict | None:
    comps = ev.get("competitors") or []
    home = next((c for c in comps if c.get("isHome")), None)
    away = next((c for c in comps if not c.get("isHome")), None)
    if not home or not away:
        return None
    status = ev.get("status") or {}
    detail = status.get("detail") or ""
    completed = bool(ev.get("completed")) and status.get("state") == "post" and detail in ("FT", "AET", "FT-Pens", "Final", "STATUS_FINAL")
    return {
        "id": str(ev["id"]),
        "league": league,
        "kickoff": ev.get("date"),
        "home": home.get("displayName"),
        "away": away.get("displayName"),
        "home_id": str(home.get("id")),
        "away_id": str(away.get("id")),
        "home_goals": _goals(home.get("score")) if completed else None,
        "away_goals": _goals(away.get("score")) if completed else None,
        "completed": 1 if completed else 0,
        "status": detail or status.get("state"),
    }


def upsert(conn, m: dict):
    conn.execute(
        """INSERT INTO matches(id, league, kickoff, home, away, home_id, away_id,
               home_goals, away_goals, completed, status, updated_at)
           VALUES(:id, :league, :kickoff, :home, :away, :home_id, :away_id,
               :home_goals, :away_goals, :completed, :status, :now)
           ON CONFLICT(id) DO UPDATE SET kickoff=excluded.kickoff, home=excluded.home,
               away=excluded.away, home_goals=excluded.home_goals, away_goals=excluded.away_goals,
               completed=excluded.completed, status=excluded.status, updated_at=excluded.updated_at""",
        {**m, "now": datetime.now(timezone.utc).isoformat()},
    )


def _walk(conn, league: str, day: date, end: date, progress=None, checkpoint=None, strict=False) -> int:
    """day..end aralığını haftalık pencerelerle çeker; her adımda checkpoint(day) çağrılır."""
    count = 0
    while day <= end:
        if progress:
            progress(f"{league}: {day.isoformat()} haftası çekiliyor")
        try:
            window = fetch_window(league, day)
        except Exception as exc:  # ağ hatası: bir sonraki pencereye geç
            if strict:
                raise RuntimeError(f"{league}: {day} alınamadı: {exc}") from exc
            if progress:
                progress(f"{league}: {day} alınamadı ({exc})")
            day += timedelta(days=3)
            time.sleep(REQUEST_DELAY_SEC)
            continue
        for events in window.values():
            for ev in events:
                parsed = parse_event(ev, league)
                if parsed:
                    upsert(conn, parsed)
                    count += 1
        # JSON API tek günü döndürür. Maç olmayan günler dahil, hiçbir
        # tarihi atlamadan ilerle (eski +3 gün davranışı fikstür kaçırıyordu).
        day += timedelta(days=1)
        if checkpoint:
            checkpoint(day)
        conn.commit()
        time.sleep(REQUEST_DELAY_SEC)
    return count


def backfill_league(conn, league: str, progress=None, strict=False) -> int:
    """HISTORY_START ile veritabanının kapsadığı ilk gün arasındaki eksik geçmişi doldurur."""
    covered = get_meta(conn, f"espn_from_{league}")
    if covered is None:
        if get_meta(conn, f"espn_done_{league}") is None:
            return 0  # yeni kurulum: sync_league zaten HISTORY_START'tan başlar
        covered = LEGACY_HISTORY_START.isoformat()
    covered = date.fromisoformat(covered)
    if covered <= HISTORY_START:
        return 0
    key = f"espn_backfill_{league}"
    resume = get_meta(conn, key)
    day = max(HISTORY_START, date.fromisoformat(resume)) if resume else HISTORY_START
    n = _walk(conn, league, day, covered, progress,
              lambda d: set_meta(conn, key, min(d, covered).isoformat()), strict=strict)
    set_meta(conn, f"espn_from_{league}", HISTORY_START.isoformat())
    conn.execute("DELETE FROM meta WHERE key=?", (key,))
    return n


def sync_league(conn, league: str, progress=None, strict=False) -> int:
    """Ligi kaldığı yerden günceller. Dönen değer: işlenen maç sayısı."""
    today = date.today()
    end = today + timedelta(days=FIXTURE_DAYS_AHEAD)
    # Günlük cron geçmiş sezonları tekrar taramaz. Tarihsel backfill ayrı bakım işidir.
    # Son 7 gün tekrar taranarak geciken skor/düzeltmeler yakalanır.
    count = 0
    day = today - timedelta(days=7)
    # Son 7 gün ve ileri fikstür her çalışmada tazelenir.
    count += _walk(conn, league, day, end, progress,
                   lambda d: set_meta(conn, f"espn_done_{league}",
                                      min(d, today - timedelta(days=5)).isoformat()), strict=strict)
    # Bilgi amaçlı son başarılı kontrol tarihini kaydet.
    set_meta(conn, f"espn_done_{league}", (today - timedelta(days=5)).isoformat())
    return count


def sync_recent(conn, league: str, days_back: int = 3, max_requests: int = 2, progress=None, strict=False) -> tuple[int, bool]:
    """Hızlı artımlı güncelleme: yalnız son `days_back` gün ve yakın fikstür (istek başına ~1 haftalık pencere).

    Dönen değer: (işlenen maç sayısı, en az bir istek başarılı mı). Serverless istek süresine sığması için
    istek sayısı sınırlı, deneme/zaman aşımı kısadır; tam geçmiş `sync_league` ile (cron) çekilir.
    """
    today = date.today()
    # Günlük endpoint kullanıldığı için son iki günü kontrol et. Daha eski
    # düzeltmeler üç saatlik tam taramada (son 7 gün) yakalanır.
    days = [today - timedelta(days=offset) for offset in range(max_requests)]
    count = 0
    ok = False
    for day in days:
        if progress:
            progress(f"{league}: {day.isoformat()} çevresi çekiliyor")
        try:
            window = fetch_window(league, day, timeout=12, retries=2)
        except Exception as exc:
            if strict:
                raise RuntimeError(f"{league}: {day} alınamadı: {exc}") from exc
            if progress:
                progress(f"{league}: {day} alınamadı ({exc})")
            continue
        ok = True
        for events in window.values():
            for ev in events:
                parsed = parse_event(ev, league)
                if parsed:
                    upsert(conn, parsed)
                    count += 1
        conn.commit()
        # Tek günlük scoreboard çağrısında ileri gün çıkarımı yapılmaz.
    return count, ok
