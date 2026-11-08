"""ESPN'in herkese açık fikstür sayfalarından maç sonuçlarını ve fikstürü çeker.

API anahtarı kullanılmaz: sayfa HTML'inin içine gömülü JSON (window['__espnfitt__'])
okunur. Her istek, verilen tarihin çevresindeki ~1 haftalık pencereyi döndürür.
"""
import gzip
import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from ..config import (FIXTURE_DAYS_AHEAD, HISTORY_START, LEGACY_HISTORY_START, REQUEST_DELAY_SEC,
                      USER_AGENT)
from ..db import get_meta, set_meta

URL = "https://www.espn.com/soccer/fixtures/_/date/{day}/league/{league}"
_EMBED = re.compile(r"window\['__espnfitt__'\]=(\{.*?\});</script>", re.S)


def _goals(value) -> int | None:
    if isinstance(value, dict):
        value = value.get("value", value.get("displayValue"))
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return int(number) if number.is_integer() else None
    except (TypeError, ValueError):
        return None


# Kısa süreli sunucu yanıtları. 400 gibi kalıcı istemci hataları bu kümede değildir.
_TRANSIENT_HTTP = frozenset({408, 429, 500, 502, 503, 504})


def _transient_fetch_error(exc: BaseException) -> bool:
    """Ağ, TLS, zaman aşımı ve kısa süreli HTTP hataları yeniden denenebilir.

    Sertifika doğrulaması kapatılmaz; urlopen varsayılan bağlamıyla çağrılır.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _TRANSIENT_HTTP
    return isinstance(exc, (
        urllib.error.URLError,
        TimeoutError,
        ConnectionError,
        ssl.SSLError,
        gzip.BadGzipFile,
        http.client.IncompleteRead,
    ))


def _fetch_bytes(req: urllib.request.Request, timeout: int, retries: int) -> bytes:
    """HTTP 200 gövdesi. Geçici hatalarda sınırlı, artan beklemeyle yeniden dener.

    Denemeler tükenince son hata mesajın içinde kalır. HTTP 400 gibi kalıcı
    hatalar yeniden denenmez.
    """
    last_error: BaseException | None = None
    attempts = max(1, retries)
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                if resp.status == 200 and raw:
                    return raw
                last_error = RuntimeError(f"HTTP {resp.status} / boş yanıt")
        except Exception as exc:
            if not _transient_fetch_error(exc):
                raise
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(2 * (attempt + 1))  # 2s, 4s, ... artan bekleme
    detail = "Boş yanıt" if isinstance(last_error, RuntimeError) else last_error
    raise RuntimeError(
        f"ESPN isteği {attempts} denemede başarısız ({detail}): {req.full_url}"
    ) from last_error


def fetch_html(url: str, timeout: int = 25, retries: int = 3) -> str:
    # gzip ile sayfa ~540 KB yerine ~75 KB iner
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    return _fetch_bytes(req, timeout, retries).decode("utf-8", errors="ignore")



# ESPN'nin HTML fikstür sayfası bot korumasında boş dönebilir. Aynı ESPN
# verisinin JSON scoreboard uç noktası yedek kaynaktır; maç ID'leri değişmez.
API_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard?dates={day}"


def normalize_api_event(event):
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    competitors = competitions[0].get("competitors") or []
    typ = (event.get("status") or competitions[0].get("status") or {}).get("type") or {}
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
    return normalized


def fetch_api_window(league: str, day: date, timeout: int = 25, retries: int = 3) -> dict[str, list[dict]]:
    # ESPN 2026'dan beri tarihler arası sorgulara HTTP 400 verebiliyor.
    # Scoreboard uç noktası tek günü destekler; bir haftalık veri varsayılmaz.
    url = API_URL.format(league=league, day=day.strftime("%Y%m%d"))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    raw = _fetch_bytes(req, timeout, retries)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ESPN JSON çözülemedi: {url} ({exc})") from exc
    if not isinstance(data.get("events"), list):
        raise RuntimeError(f"ESPN JSON events alanı yok: {url}")
    result: dict[str, list[dict]] = {}
    for event in data["events"]:
        normalized = normalize_api_event(event)
        if normalized is None:
            continue
        if normalized["date"]:
            key = normalized["date"][:10].replace("-", "")
            result.setdefault(key, []).append(normalized)
    return result


def fetch_window(league: str, day: date, **kw) -> dict[str, list[dict]]:
    # JSON tek günlük endpoint ana kaynaktır. HTML bot korumasından boş dönebildiği
    # için her sorguda önce HTML'yi beklemek gereksiz gecikme ve hata üretir.
    timeout = kw.get("timeout", 25)
    retries = kw.get("retries", 3)
    try:
        return fetch_api_window(league, day, timeout=timeout, retries=retries)
    except Exception as api_error:
        try:
            html = fetch_html(URL.format(day=day.strftime("%Y%m%d"), league=league),
                              timeout=timeout, retries=retries)
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
    # Bitmiş statüsü tek başına yeterli değil: eksik skor verisi, 0-0 gibi
    # yanlış bir kesin sonuç olarak modele veya sonuçlandırmaya girmesin.
    hg, ag = _goals(home.get("score")), _goals(away.get("score"))
    if completed and (hg is None or ag is None or hg < 0 or ag < 0):
        completed = False
    return {
        "id": str(ev["id"]),
        "league": league,
        "kickoff": ev.get("date"),
        "home": home.get("displayName"),
        "away": away.get("displayName"),
        "home_id": str(home["id"]) if home.get("id") is not None else None,
        "away_id": str(away["id"]) if away.get("id") is not None else None,
        "home_goals": hg if completed else None,
        "away_goals": ag if completed else None,
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
               away=excluded.away,
               home_id=COALESCE(excluded.home_id, matches.home_id), away_id=COALESCE(excluded.away_id, matches.away_id),
               home_goals=CASE WHEN matches.completed=1 AND excluded.completed=0 AND :preserve_final=1 THEN matches.home_goals ELSE excluded.home_goals END,
               away_goals=CASE WHEN matches.completed=1 AND excluded.completed=0 AND :preserve_final=1 THEN matches.away_goals ELSE excluded.away_goals END,
               completed=CASE WHEN matches.completed=1 AND :preserve_final=1 THEN 1 ELSE excluded.completed END,
               status=CASE WHEN matches.completed=1 AND excluded.completed=0 AND :preserve_final=1 THEN matches.status ELSE excluded.status END,
               updated_at=excluded.updated_at""",
        {**m, "now": datetime.now(timezone.utc).isoformat(),
         "preserve_final": int(not any(word in str(m.get('status') or '').lower() for word in ('cancel','postpon','abandon','suspend')))},
    )


def _walk(conn, league: str, day: date, end: date, progress=None, checkpoint=None, strict=False) -> int:
    """day..end aralığını günlük pencerelerle çeker; her adımda checkpoint(day) çağrılır."""
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
            day += timedelta(days=1)
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
    # Tam güncellemede sezon içi delikleri ve diğer resmi kulvarları uzlaştır.
    from .team_context import reconcile_league
    count += reconcile_league(conn, league, progress=progress)
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
