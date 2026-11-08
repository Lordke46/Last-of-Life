"""Altyapı yardımcıları: sonuç önbelleği, IP hız sınırı, sağlık kontrolü, yapılandırılmış hata günlüğü."""
import json
import logging
import os
import time
from datetime import datetime, timezone

from fastapi import Request
from fastapi.responses import JSONResponse

from .db import get_meta, session

log = logging.getLogger("iddiaoran")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_limits (
    key TEXT NOT NULL,
    win BIGINT NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key, win)
);
"""

_cache: dict = {}
_MAX_ENTRIES = 200
CACHE_TTL_SECONDS = 30


def _next_kickoff(value):
    if isinstance(value, dict):
        stamp = value.get("kickoff")
        if stamp:
            try:
                yield datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                pass
        for child in value.values():
            yield from _next_kickoff(child)
    elif isinstance(value, list):
        for child in value:
            yield from _next_kickoff(child)


def data_version() -> str:
    """Önbellek anahtarı: veri güncellemeleri (ESPN + iddaa) değişince önbellek kendiliğinden geçersiz olur."""
    with session() as conn:
        return "|".join(str(get_meta(conn, k) or "") for k in ("last_updated_at", "last_update", "iddaa_last_update", "update_data_version"))


def cached(key: tuple, build):
    """Süreç-içi önbellek (veri sürümü anahtarlı) + Vercel CDN için Cache-Control."""
    try:
        version = data_version()
    except Exception:  # sürüm okunamıyorsa önbelleği atla
        return build()
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[0] == version and now < hit[2]:
        return hit[1]
    value = build()
    if len(_cache) >= _MAX_ENTRIES:
        _cache.clear()
    remaining = min((t - time.time() for t in _next_kickoff(value) if t > time.time()), default=CACHE_TTL_SECONDS)
    _cache[key] = (version, value, time.monotonic() + min(CACHE_TTL_SECONDS, remaining))
    return value


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def rate_limited(key: str, limit: int, window_seconds: int = 60) -> bool:
    """DB tabanlı sabit pencere sayacı (serverless uyumlu). Sınır aşıldıysa True.

    `win` pencerenin başlangıç dakikasıdır; böylece farklı pencere boyutları aynı temizlik ölçüsünü
    paylaşır. En uzun pencere 1 saattir (temizlik 65 dk'dan eskiyi siler).
    """
    now = int(time.time())
    win = (now // window_seconds) * window_seconds // 60
    with session() as conn:
        conn.execute("DELETE FROM rate_limits WHERE win < ?", (now // 60 - 65,))
        conn.execute(
            "INSERT INTO rate_limits(key, win, hits) VALUES(?, ?, 1) "
            "ON CONFLICT(key, win) DO UPDATE SET hits = rate_limits.hits + 1", (key, win))
        row = conn.execute("SELECT hits FROM rate_limits WHERE key=? AND win=?", (key, win)).fetchone()
    return row["hits"] > limit


def _age(ts: str | None):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except ValueError:
        return None


# Hızlı güncelleme saatlik (quick.yml, dakika 17). 75 dk: bir pencere kaçtıysa uyarı.
# Sağlık işi (dakika 45) bu uyarıda hızlı güncellemeyi tetikler.
MAX_UPDATE_AGE = 75 * 60
MAX_IDDAA_AGE = 75 * 60


def _iddaa_job_age(fetch_age, sources):
    """İddaa bayatlığı işin bitişine bakar.

    `iddaa_last_update` oranlar yazılınca konur. Ortak başarı damgası haber
    bittikten sonradır. Aynı eşiği fetch saatine uygularsak iddaa, güncelleme
    daha taze görünürken birkaç dakika erken bayat sayılır. Kaynak başarılıysa
    `last_success_at` (işin sonu) esas alınır. `iddaa_age_seconds` yine gerçek
    çekim yaşı olarak kalır; kupon tazeliği de etkinlik damgasını kullanır.
    """
    source = sources.get("iddaa") if isinstance(sources, dict) else None
    if isinstance(source, dict) and source.get("status") == "success":
        job_age = _age(source.get("last_success_at"))
        if job_age is not None:
            return job_age
    return fetch_age


def health() -> dict:
    out = {"db": False, "update_age_seconds": None, "iddaa_age_seconds": None, "problems": []}
    try:
        with session() as conn:
            conn.execute("SELECT 1").fetchone()
            out["db"] = True
            out["update_age_seconds"] = _age(get_meta(conn, "last_updated_at") or get_meta(conn, "last_update"))
            out["iddaa_age_seconds"] = _age(get_meta(conn, "iddaa_last_update"))
            out["sources"] = json.loads(get_meta(conn, "update_sources", "{}"))
    except Exception as exc:
        log.error(json.dumps({"event": "health_db_error", "error": repr(exc)}))
        out["problems"].append("db")
        out["ok"] = False
        return out
    iddaa_age = _iddaa_job_age(out["iddaa_age_seconds"], out["sources"])
    for name, age, limit in (("update_stale", out["update_age_seconds"], MAX_UPDATE_AGE),
                             ("iddaa_stale", iddaa_age, MAX_IDDAA_AGE)):
        if age is None or age > limit:
            out["problems"].append(name)
    for key, source in out["sources"].items():
        age = _age(source.get("last_success_at"))
        if source.get("status") != "success" or age is None or age > (24 * 3600 if key == "news" else MAX_UPDATE_AGE):
            out["problems"].append(f"source_stale:{key}")
    out["ok"] = not out["problems"]
    return out


# Vercel CDN: yalnız başarılı GET yanıtları önbelleğe alınır.
CDN_TTL = {"/api/leagues": 60, "/api/upcoming": 60, "/api/picks": 60, "/api/coupons": 60,
           "/api/value-bets": 60, "/api/standings": 120, "/api/live": 15, "/api/model-report": 300,
           "/api/coupon-history": 120}


def install(app):
    @app.middleware("http")
    async def _cdn_cache(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in {"/api/leagues", "/api/update/status", "/api/results", "/api/bulletin"}:
            response.headers["Cache-Control"] = "no-store"
            response.headers["CDN-Cache-Control"] = "no-store"
            response.headers["Vercel-CDN-Cache-Control"] = "no-store"
            return response
        ttl = CDN_TTL.get(request.url.path)
        public = os.environ.get("AUTH_REQUIRED") != "1"  # girişe bağlıysa CDN herkese açık önbellek tutmasın
        if ttl and public and request.method == "GET" and response.status_code == 200 and "cache-control" not in response.headers:
            sensitive = request.url.path in {"/api/upcoming", "/api/picks", "/api/coupons", "/api/value-bets", "/api/live"}
            response.headers["Cache-Control"] = ("public, max-age=0, s-maxage=0, must-revalidate" if sensitive
                                                  else f"public, s-maxage={ttl}, stale-while-revalidate={ttl * 10}")
        return response

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.error(json.dumps({"event": "unhandled_exception", "path": request.url.path,
                              "method": request.method, "error": repr(exc)}), exc_info=exc)
        return JSONResponse({"detail": "Sunucu hatası"}, status_code=500)
