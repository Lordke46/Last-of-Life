"""FastAPI uygulaması. Çalıştırma: ./run.sh  (veya .venv/bin/uvicorn app.main:app)"""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, explain, odds_history, odds_log, ops, pwa, updater
from . import live, backtest_jobs
from .api_odds_history import router as odds_history_router
from .config import LEAGUES, MODEL, STATIC_DIR, league_aliases, league_slug, resolve_league
from .db import get_meta, init_db, is_postgres, session
from .coupon_history_api import router as coupon_history_router
from .match_insights import router as match_insights_router
from .model_report_api import router as model_report_router
from .model import coupon_history, engine
from .model.blend import blend_predictions
from .model.coupons import TZ, build_coupons
from .model.ratings import parse_ts
from .model.small_leagues import market_favorite
from .value_bets import router as value_bets_router
from .seo import router as seo_router
from .results_api import router as results_router
from .bulletin_api import router as bulletin_router

AUTO_UPDATE_HOURS = 6
UPDATE_PER_MINUTE = 5


@asynccontextmanager
async def lifespan(_app):
    init_db()
    if not is_postgres():  # yerel geliştirme: veri eskiyse açılışta arka planda güncelle
        with session() as conn:
            last = get_meta(conn, "last_updated_at") or get_meta(conn, "last_update")
        if not last or datetime.now(timezone.utc) - parse_ts(last) > timedelta(hours=AUTO_UPDATE_HOURS):
            updater.run_in_background()
    yield  # Postgres (serverless): arka plan işi yok; güncelleme istek içinde veya cron'da


app = FastAPI(title="İddia Oran Öneri", lifespan=lifespan)
ops.install(app)
auth.install(app)  # /api/auth/*, /giris; AUTH_REQUIRED=1 iken sayfa/veri kapısı
app.include_router(pwa.router)  # /manifest.webmanifest, /sw.js
app.include_router(seo_router)
app.include_router(odds_history_router)
app.include_router(match_insights_router)
app.include_router(coupon_history_router)
app.include_router(model_report_router)
app.include_router(value_bets_router)
app.include_router(results_router)
app.include_router(bulletin_router)


def _check_league(league: str):
    canonical = resolve_league(league)
    if canonical is None:
        raise HTTPException(404, "Bilinmeyen lig")
    return canonical


def _attach_iddaa_odds(items: list[dict]) -> list[dict]:
    """Tahmin yanıtlarına aynı ESPN maçıyla eşleşen güncel iddaa oranlarını ekler ve (`markets` içeren
    tahminlerde) olasılıkları marjı temizlenmiş iddaa olasılıklarıyla harmanlar (model/blend.py)."""
    if not items:
        return items
    with session() as conn:
        blended = blend_predictions(conn, items)
        movements = odds_history.movements_for_matches(conn, [item["id"] for item in items])
    for item in blended:
        item["iddaa_movement"] = movements.get(str(item["id"]), {})
        if "pick" in item:
            item["summary"] = explain.summarize(item)
    return blended


@app.get("/api/leagues")
def leagues():
    return ops.cached(("leagues",), _leagues)


def _leagues():
    with session() as conn:
        rows = {r["league"]: dict(r) for r in conn.execute(
            "SELECT league, COUNT(*) total, SUM(completed) played FROM matches GROUP BY league")}
        last = get_meta(conn, "last_updated_at") or get_meta(conn, "last_update")
        iddaa_last = get_meta(conn, "iddaa_last_update")
        iddaa_error = get_meta(conn, "iddaa_last_error")
        iddaa_stats = json.loads(get_meta(conn, "iddaa_stats", "null"))
        small = [dict(r) for r in conn.execute(
            "SELECT league, MAX(league_name) league_name, MAX(country) country, COUNT(*) total "
            "FROM iddaa_events WHERE is_small=1 GROUP BY league ORDER BY country, league_name")]
    return {"last_update": last, "model": MODEL,
            "iddaa": {"last_update": iddaa_last, "error": iddaa_error, "stats": iddaa_stats},
            "leagues": [{"id": k, **v, "slug": league_slug(k), "aliases": list(league_aliases(k)),
                         "total": rows.get(k, {}).get("total", 0),
                         "played": rows.get(k, {}).get("played", 0), "category": "main"} for k, v in LEAGUES.items()],
            "small_leagues": [{"id": r["league"], "name": r["league_name"], "country": r["country"],
                               "total": r["total"], "played": 0, "category": "small"} for r in small]}


@app.get("/api/live")
def live_scores():
    return live.get_live_snapshot()


@app.get("/api/upcoming")
def upcoming(league: str, days: int = Query(14, ge=1, le=30)):
    return ops.cached(("upcoming", league, days), lambda: _upcoming(league, days))


def _upcoming(league: str, days: int):
    if league.startswith("small:"):
        now = datetime.now(timezone.utc)
        with session() as conn:
            rows = conn.execute(
                """SELECT e.id, e.competition_id, e.updated_at, e.league, e.league_name,
                          e.country, e.kickoff, e.home, e.away, o.market, o.odd
                   FROM iddaa_events e LEFT JOIN iddaa_odds o ON o.event_id=e.id
                   WHERE e.is_small=1 AND e.league=? AND e.kickoff>=? AND e.kickoff<=?
                   ORDER BY e.kickoff""",
                (league, now.isoformat(), (now + timedelta(days=days)).isoformat())).fetchall()
        grouped = {}
        for r in rows:
            item = grouped.setdefault(r["id"], {"id": r["id"], "event_id": r["id"],
                "iddaa_event_id": r["id"], "competition_id": r["competition_id"],
                "iddaa_updated_at": r["updated_at"], "league": league,
                "league_name": r["league_name"], "country": r["country"], "kickoff": r["kickoff"],
                "home": r["home"], "away": r["away"], "iddaa_odds": {}, "small_league": True})
            if r["market"] and r["odd"]:
                item["iddaa_odds"][r["market"]] = r["odd"]
        return [_small_prediction(item) for item in grouped.values()]
    league = _check_league(league)
    items = _attach_iddaa_odds(engine.get_model(league).upcoming_predictions(days))
    for item in items:
        item["league_name"] = LEAGUES[league]["name"]
    return items


def _small_prediction(event: dict) -> dict:
    """Return actual small-league bulletin data and only market-derived probabilities."""
    from .model.small_leagues import bulletin_event
    from .scrapers.iddaa import load_constraints
    if 'iddaa_min_matches' not in event:
        with session() as conn:
            constraints = load_constraints(conn)
        event = {**event, 'iddaa_min_matches':constraints.get(str(event.get('iddaa_event_id',event['id'])), {})}
    if 'odds_current' not in event:
        event={**event,'odds_current':parse_ts(event['kickoff'])>datetime.now(timezone.utc)}
    result=bulletin_event(event)
    result['summary']=explain.summarize(result)
    return result


@app.get("/api/match/{match_id}")
def match(match_id: str, competition_id: Annotated[int | None, Query(ge=1)] = None):
    with session() as conn:
        event_id = match_id.removeprefix("iddaa:")
        small = conn.execute("SELECT * FROM iddaa_events WHERE id=? AND (is_small=1 OR ?=1)",
                             (event_id, int(match_id.startswith("iddaa:")))).fetchone()
        if small:
            if competition_id is not None and small["competition_id"] != competition_id:
                raise HTTPException(404, "Maç bulunamadı")
            odds = {r["market"]: r["odd"] for r in conn.execute(
                "SELECT market, odd FROM iddaa_odds WHERE event_id=?", (small["id"],)).fetchall()}
            event = {"id": "iddaa:"+small["id"] if match_id.startswith("iddaa:") else small["id"], "event_id": small["id"], "iddaa_event_id": small["id"],
                "competition_id": small["competition_id"], "iddaa_updated_at": small["updated_at"],
                "league": small["league"], "league_name": small["league_name"],
                "country": small["country"], "kickoff": small["kickoff"], "home": small["home"],
                "away": small["away"], "iddaa_odds": odds, "small_league": True}
            result = _small_prediction(event)
            result["league_name"] = f"{small['country']} · {small['league_name']}"
            from .model.recommendation_tools import analyse
            result["recommendation_tools"] = analyse(result)
            return result
    with session() as conn:
        if match_id.startswith('iddaa:'):
            from . import bulletin_archive
            archived=bulletin_archive.find_event(conn,event_id)
            if archived:
                if competition_id is not None and archived.get('competition_id') != competition_id:
                    raise HTTPException(404,'Maç bulunamadı')
                if archived.get('match_id'):
                    archived_fixture=conn.execute('SELECT * FROM matches WHERE id=?',(archived['match_id'],)).fetchone()
                    if archived_fixture and archived_fixture['league'] in LEAGUES:
                        from .bulletin_api import fixture_view
                        return {**fixture_view(dict(archived_fixture),archived),'id':match_id}
                return _small_prediction({**archived,'id':match_id,'iddaa_event_id':event_id,
                                          'iddaa_odds':archived.get('odds',{}),'small_league':True,
                                          'odds_current':False,'is_archived':True})
        fixture=conn.execute('SELECT * FROM matches WHERE id=?',(match_id,)).fetchone()
    if fixture and fixture['league'] in LEAGUES and (fixture['completed'] or parse_ts(fixture['kickoff'])<=datetime.now(timezone.utc)):
        from .bulletin_api import fixture_view
        return fixture_view(dict(fixture))
    for lg in LEAGUES:
        m = next((x for x in engine.get_model(lg).upcoming if x["id"] == match_id), None)
        if m:
            model = engine.get_model(lg)
            p = model.predict(m["home"], m["away"], m["ts"])
            result = {"id": m["id"], "league": lg, "league_name": LEAGUES[lg]["name"], "kickoff": m["kickoff"],
                      "home": m["home"], "away": m["away"],
                      "home_logo": engine._logo(m["home_id"]), "away_logo": engine._logo(m["away_id"]), **p}
            from .model.recommendation_tools import analyse
            result = _attach_iddaa_odds([result])[0]
            result["recommendation_tools"] = analyse(result)
            return result
    raise HTTPException(404, "Maç bulunamadı")


@app.get("/api/standings")
def standings(league: str):
    league = _check_league(league)
    return ops.cached(("standings", league), lambda: _standings(league))


def _standings(league: str):
    m = engine.get_model(league)
    elo = m.elo
    return [{**r, "elo": round(elo.get(r["team"]))} for r in m.table]


def _small_league_name(country: str | None, league_name: str | None) -> str:
    name = league_name or ""
    if not country:
        return name
    prefix = f"{country} · "
    if name.startswith(prefix):
        return name
    return f"{prefix}{name}" if name else country


def _small_league_picks(days: int, min_conf: float = 0.0) -> list[dict]:
    # Market probability is informational, not model confidence, so min_conf does not apply.
    now = datetime.now(timezone.utc)
    with session() as conn:
        rows = conn.execute(
            """SELECT e.id, e.competition_id, e.updated_at, e.league, e.league_name,
                      e.country, e.kickoff, e.home, e.away, o.market, o.odd
               FROM iddaa_events e LEFT JOIN iddaa_odds o ON o.event_id=e.id
               WHERE e.is_small=1 AND e.kickoff>=? AND e.kickoff<=?
               ORDER BY e.kickoff""",
            (now.isoformat(), (now + timedelta(days=days)).isoformat())).fetchall()
    grouped = {}
    for row in rows:
        event = grouped.setdefault(row["id"], {
            "id": row["id"], "event_id": row["id"], "iddaa_event_id": row["id"],
            "competition_id": row["competition_id"], "iddaa_updated_at": row["updated_at"],
            "league": row["league"], "league_name": row["league_name"],
            "country": row["country"], "kickoff": row["kickoff"], "home": row["home"],
            "away": row["away"], "iddaa_odds": {}, "small_league": True,
        })
        if row["market"] and row["odd"]:
            event["iddaa_odds"][row["market"]] = row["odd"]

    picks = []
    for event in grouped.values():
        prediction = _small_prediction(event)
        pick = market_favorite(prediction)
        if not pick:
            continue
        picks.append({"id": prediction["id"], "event_id": prediction["event_id"],
                      "iddaa_event_id": prediction["iddaa_event_id"],
                      "competition_id": prediction["competition_id"],
                      "iddaa_updated_at": prediction["iddaa_updated_at"],
                      "league": prediction["league"],
                      "league_name": _small_league_name(prediction.get("country"), prediction.get("league_name")),
                      "country": prediction["country"],
                      "kickoff": prediction["kickoff"], "home": prediction["home"],
                      "away": prediction["away"], "pick": pick,
                      "recommendation_source": "iddaa_market",
                      "recommendation_label": "İddaa piyasa favorisi",
                      "confidence": None, "confidence_label": pick["confidence_label"],
                      "confidence_filter_applied": False, "small_league": True,
                      "iddaa_odds": prediction["iddaa_odds"], "score": pick["p"]})
    picks.sort(key=lambda item: (-item["score"], item["kickoff"]))
    return picks


@app.get("/api/picks")
def picks(days: int = Query(7, ge=1, le=21), min_conf: float = 0.0, category: str = "main"):
    """Ana lig veya küçük lig önerilerini döndürür."""
    if category not in ("main", "small"):
        raise HTTPException(400, "Bilinmeyen öneri kategorisi")
    return ops.cached(("picks", days, min_conf, category), lambda: _picks(days, min_conf, category))


def _picks(days: int, min_conf: float, category: str):
    if category == "small":
        return _small_league_picks(days, min_conf)
    if category != "main":
        raise HTTPException(400, "Bilinmeyen öneri kategorisi")
    predictions = [p for lg in LEAGUES for p in engine.get_model(lg).upcoming_predictions(days)
                   if not p.get("small_league") and parse_ts(p["kickoff"]) > datetime.now(timezone.utc)]
    out = []
    for p in _attach_iddaa_odds(predictions):  # öneri/güven harmanlı olasılıktan hesaplanır
        if p.get("pick") and p["confidence"] >= min_conf:
            out.append({"id": p["id"], "league": p["league"], "league_name": LEAGUES[p["league"]]["name"],
                        "kickoff": p["kickoff"], "home": p["home"], "away": p["away"],
                        "pick": p["pick"], "confidence": p["confidence"],
                        "confidence_label": p["confidence_label"], "blend": p["blend"],
                        "confidence_reason": p.get("confidence_reason"),
                        "iddaa_odds": p["iddaa_odds"], "iddaa_event_id": p["iddaa_event_id"],
                        "iddaa_updated_at": p["iddaa_updated_at"], "iddaa_movement": p["iddaa_movement"],
                        "score": p["pick"]["p"] * (0.5 + p["confidence"])})
    out.sort(key=lambda x: -x["score"])
    return out


@app.get("/api/coupons")
def coupons(day: str | None = Query(None, description="YYYY-MM-DD (Türkiye saati); varsayılan bugün")):
    try:
        target = date.fromisoformat(day) if day else datetime.now(TZ).date()
    except ValueError as exc:
        raise HTTPException(400, "Gün YYYY-AA-GG biçiminde olmalı") from exc
    return ops.cached(("coupons", target), lambda: _coupons(target))


def _coupons(target):
    return coupon_history.daily_coupons(target, build=build_coupons)


@app.post("/api/update", dependencies=[Depends(auth.require_admin)])
def start_update(request: Request):
    """Hızlı artımlı güncelleme (iddaa oranları + ESPN son günler), istek içinde senkron çalışır.
    Son güncellemeden sonra 10 dk bekleme ve kilit vardır; ağır tam güncelleme cron'dadır."""
    if ops.rate_limited(f"update:{ops.client_ip(request)}", UPDATE_PER_MINUTE):
        raise HTTPException(429, "Çok fazla istek; bir dakika sonra tekrar deneyin",
                            headers={"Retry-After": "60"})
    return updater.run_quick_update()


@app.get("/api/health")
def health_check():
    result = ops.health()
    # 503 yalnız veritabanı kapalıyken. Veri eskiliği ok=false ile 200 döner; servis
    # ayaktadır ve izleme işi gövdeyi okuyup uyarı verir. 200 CDN'de tutulmasın.
    status = 200 if result.get("db") else 503
    return JSONResponse(result, status_code=status, headers={
        "Cache-Control": "no-store",
        "CDN-Cache-Control": "no-store",
        "Vercel-CDN-Cache-Control": "no-store",
    })


@app.get("/api/update/status")
def update_status():
    return updater.status()


@app.get("/api/backtest")
def get_backtest():
    job = backtest_jobs.status()
    return {"running": job["status"] in ("queued", "running"), "job": job,
            "data": engine.cached_backtest(), "odds_log": odds_log.current_summary()}


@app.post("/api/backtest", dependencies=[Depends(auth.require_admin)])
def run_backtest():
    job = backtest_jobs.request()
    return {"running": job["status"] in ("queued", "running"), "job": job}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
