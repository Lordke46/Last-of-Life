"""Veri güncelleme.

İki yol vardır ve ikisi de `meta` tablosundaki kilidi (update_lock) paylaşır:
  * run_quick_update(): web isteği içinde çalışan hızlı artımlı güncelleme (iddaa oranları + ESPN son
    günler). Serverless uyumlu: arka plan thread'i yok, son güncellemeden sonra COOLDOWN_MINUTES bekleme var.
  * run_update(): ağır tam güncelleme (tüm ligler + haberler). Komut satırı / GitHub Actions cron'u:
        python -m app.updater [--no-news] [--league tur.1]

Herkes `last_updated_at` değerini okur ("X dk önce güncellendi").
"""
import argparse
from contextlib import contextmanager
from functools import partial
import json
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from datetime import datetime, timedelta, timezone

from .config import LEAGUES
from .db import get_meta, init_db, session, set_meta
from .model.ratings import parse_ts
from .scrapers import espn, iddaa, news, tff

COOLDOWN_MINUTES = 10  # herkese açık sitede iki güncelleme arası en az bekleme
QUICK_LOCK_MINUTES = 3  # hızlı güncelleme kilidi zaman aşımı (çöken istek kilidi kalıcı bırakmasın)
FULL_LOCK_MINUTES = 45  # tam güncelleme kilidi zaman aşımı
RECENT_DAYS_BACK = 3  # hızlı güncellemede ESPN'den geriye bakılan gün
IDDAA_MAX_STALE_HOURS = 6  # iddaa erişilemezse eski oranlar en çok bu kadar süre gösterilir
# Serverless istek süresi (300 sn) bütçesi: hızlı güncellemede lig çekimleri en çok bu kadar bekler; ardından
# iddaa (en kötü ~55 sn) çalışır. Yavaş kalan lig atlanır, bir sonraki güncelleme tamamlar.
QUICK_MAX_WORKERS = 8
QUICK_LEAGUES_DEADLINE_SEC = 120
QUICK_IDDAA_RETRIES = 1  # hızlı endpoint bülten denemesini sınırlar; tam cron daha dayanıklıdır

_progress_state = {"message": ""}


def source_for(league: str):
    """Ligin veri kaynağı modülü (espn | tff); ikisi de sync_league / sync_recent sunar."""
    return tff if LEAGUES[league].get("source") == "tff" else espn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _progress(msg: str):
    _progress_state["message"] = msg
    print(msg, flush=True)


# --- Kilit ---------------------------------------------------------------------
def acquire_lock(conn, minutes: int) -> str | None:
    """Atomik kilit alır. Değer kilidin bitiş zamanıdır; süresi geçmiş kilit tekrar alınabilir."""
    now = _now()
    cur = conn.execute(
        "INSERT INTO meta(key, value) VALUES('update_lock', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value "
        "WHERE meta.value IS NULL OR meta.value = '' OR meta.value < ?",
        (_iso(now + timedelta(minutes=minutes)), _iso(now)),
    )
    if cur.rowcount != 1:
        return None
    token = secrets.token_hex(24)
    set_meta(conn, "update_lock_owner", token)
    return token


class LostLease(RuntimeError):
    pass


def renew_lock(conn, token, minutes):
    now = _now()
    cur = conn.execute(
        "UPDATE meta SET value=? WHERE key='update_lock' AND value>? "
        "AND EXISTS (SELECT 1 FROM meta owner WHERE owner.key='update_lock_owner' AND owner.value=?)",
        (_iso(now + timedelta(minutes=minutes)), _iso(now), token))
    if cur.rowcount != 1:
        raise LostLease("Güncelleme kilidi artık bu işleme ait değil")


def release_lock(conn, token):
    cur = conn.execute(
        "UPDATE meta SET value='' WHERE key='update_lock' "
        "AND EXISTS (SELECT 1 FROM meta owner WHERE owner.key='update_lock_owner' AND owner.value=?)", (token,))
    return cur.rowcount == 1


@contextmanager
def leased_session(token, minutes, cancelled=None):
    with session() as conn:
        class GuardedConnection:
            def __getattr__(self, name):
                return getattr(conn, name)

            def commit(self):
                if cancelled is not None and cancelled.is_set():
                    conn.rollback()
                    raise LostLease("Lig çekimi süre sınırını aştı")
                try:
                    renew_lock(conn, token, minutes)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        guarded = GuardedConnection()
        yield guarded
        guarded.commit()


def _lock_active(conn) -> bool:
    value = get_meta(conn, "update_lock", "")
    return bool(value) and value >= _iso(_now())


def _take_lock(minutes: int, wait_seconds: int = 0) -> str | None:
    deadline = time.monotonic() + wait_seconds
    while True:
        with session() as conn:
            token = acquire_lock(conn, minutes)
            if token:
                return token
        if time.monotonic() >= deadline:
            return None
        time.sleep(3)


# --- Durum ---------------------------------------------------------------------
def cooldown_left(last_updated_at: str | None) -> int:
    """Bir sonraki güncellemeye kalan saniye (0 = şimdi güncellenebilir)."""
    if not last_updated_at:
        return 0
    elapsed = (_now() - parse_ts(last_updated_at)).total_seconds()
    return max(0, int(COOLDOWN_MINUTES * 60 - elapsed + 0.999))


def status() -> dict:
    """Herkes için ortak durum (veritabanından okunur; sunucu örneğine bağlı değildir)."""
    with session() as conn:
        last = get_meta(conn, "last_updated_at") or get_meta(conn, "last_update")
        running = _lock_active(conn)
        error = get_meta(conn, "update_last_error") or None
        sources = json.loads(get_meta(conn, "update_sources", "{}"))
        outcome = get_meta(conn, "update_outcome")
    return {"running": running, "last_updated_at": last, "error": error,
            "outcome": outcome, "sources": sources,
            "cooldown_seconds": cooldown_left(last), "cooldown_minutes": COOLDOWN_MINUTES,
            "message": _progress_state["message"] if running else ""}


# --- iddaa adımı (hızlı ve tam güncelleme ortak) ----------------------------------
def _sync_iddaa(conn, retries: int = 3) -> bool:
    try:
        iddaa.sync_odds(conn, progress=_progress, retries=retries)
        return True
    except LostLease:
        raise
    except Exception as exc:
        conn.rollback()
        set_meta(conn, "iddaa_last_error", str(exc))
        last = get_meta(conn, "iddaa_last_update")
        stale = not last or _now() - parse_ts(last) > timedelta(hours=IDDAA_MAX_STALE_HOURS)
        if stale:
            # Çok eski oranı güncelmiş gibi göstermemek için temizle; yakın tarihli oran tutulur.
            conn.execute("DELETE FROM iddaa_odds")
            conn.execute("DELETE FROM iddaa_events")
            set_meta(conn, "iddaa_stats", "null")
        conn.commit()
        _progress(f"iddaa.com oranları alınamadı ({exc}); "
                  + ("model oranları kullanılacak" if stale else "son alınan oranlar korunuyor"))
        return False


def _source_result(count=0, error=None):
    return {"status": "error" if error else "success", "count": count,
            "error": str(error)[:300] if error else None}


def _collect_league(league, quick=False, token=None, cancelled=None):
    try:
        with leased_session(token, QUICK_LOCK_MINUTES if quick else FULL_LOCK_MINUTES, cancelled) as conn:
            source = source_for(league)
            if quick:
                count, ok = source.sync_recent(conn, league, days_back=RECENT_DAYS_BACK, strict=True)
                if not ok:
                    raise RuntimeError("Kaynak isteği başarısız")
            else:
                count = source.sync_league(conn, league, _progress, strict=True)
        return _source_result(count)
    except Exception as exc:
        _progress(f"{league}: {exc}")
        return _source_result(error=exc)


def _finish(conn, error: str | None, sources=None, token=None):
    """Yalnız bütün istenen kaynaklar başarılıysa ortak başarı zamanını ilerlet."""
    renew_lock(conn, token, QUICK_LOCK_MINUTES)
    if sources is not None:
        previous = json.loads(get_meta(conn, "update_sources", "{}"))
        attempted_at = _iso(_now())
        for key, result in sources.items():
            last_success = previous.get(key, {}).get("last_success_at")
            previous[key] = {**result, "attempted_at": attempted_at,
                             "last_success_at": attempted_at if result["status"] == "success" else last_success}
        set_meta(conn, "update_sources", json.dumps(previous))
        successes = sum(r["status"] == "success" for r in sources.values())
        failures = [key for key, r in sources.items() if r["status"] != "success"]
        outcome = "success" if not failures and not error else "partial" if successes else "failed"
        error = error or ("Güncellenemeyen kaynaklar: " + ", ".join(failures) if failures else None)
        set_meta(conn, "update_outcome", outcome)
        set_meta(conn, "update_last_attempt_at", attempted_at)
        # Başarısız kaynak da önceki commit'lerde kısmi veri yazmış olabilir.
        set_meta(conn, "update_data_version", secrets.token_hex(16))
    set_meta(conn, "update_last_error", error or "")
    if error is None:
        set_meta(conn, "last_updated_at", _iso(_now()))
    release_lock(conn, token)
    return error


def _invalidate_models():
    from .model import engine
    engine.invalidate()


def _capture_snapshots() -> int:
    """İlk maç öncesi tahmini güncelleme işinde dondur. Bülten okuması yazmaz."""
    try:
        from .model import engine
        from .model.blend import blend_predictions
        from .model.snapshots import record_safely
        upcoming = []
        for league in LEAGUES:
            upcoming.extend(engine.get_model(league).upcoming_predictions(2))
        if not upcoming:
            _progress("Maç öncesi tahmin kaydı: yaklaşan maç yok")
            return 0
        with session() as conn:
            saved = record_safely(conn, blend_predictions(conn, upcoming))
        _progress(f"Maç öncesi tahmin kaydı: {saved}")
        return saved
    except Exception as exc:
        _progress(f"Tahmin kaydı yazılamadı: {exc}")
        return 0


def _track_coupons(freeze: bool):
    """Kupon geçmişi: (tam güncellemede) bugünün kuponunu dondurur, biten maçlara göre işaretler.
    Hata güncellemeyi bozmaz."""
    try:
        from .model import coupon_history
        if freeze:
            saved = coupon_history.freeze_today()
            if saved:
                _progress(f"Kupon geçmişine kaydedildi: {', '.join(saved)}")
        with session() as conn:
            done = coupon_history.settle(conn)
        if done["picks"]:
            _progress(f"Kupon sonuçları: {done['picks']} seçim, {done['coupons']} kupon sonuçlandı")
    except Exception as exc:
        _progress(f"Kupon geçmişi güncellenemedi: {exc}")


# --- Hızlı güncelleme (web isteği içinde) -----------------------------------------
def run_quick_update() -> dict:
    """Senkron güncelleme. status: updated | partial | cooldown | busy | error."""
    init_db()
    with session() as conn:
        last = get_meta(conn, "last_updated_at") or get_meta(conn, "last_update")
    wait = cooldown_left(last)
    if wait:
        return {"status": "cooldown", **status()}
    token = _take_lock(QUICK_LOCK_MINUTES)
    if not token:
        return {"status": "busy", **status()}
    error, skipped, sources = None, False, {}
    try:
        with session() as conn:  # kilidi beklerken başka biri güncellemiş olabilir
            last = get_meta(conn, "last_updated_at") or get_meta(conn, "last_update")
        skipped = cooldown_left(last) > 0
        if not skipped:
            pool = ThreadPoolExecutor(max_workers=min(len(LEAGUES), QUICK_MAX_WORKERS))
            cancellations = {lg: threading.Event() for lg in LEAGUES}
            futures = {pool.submit(_collect_league, lg, quick=True, token=token, cancelled=cancellations[lg]): lg for lg in LEAGUES}
            finished, late = wait_futures(futures, timeout=QUICK_LEAGUES_DEADLINE_SEC)
            for future in late:
                cancellations[futures[future]].set()
            pool.shutdown(wait=False, cancel_futures=True)  # geç kalanlar kısa zaman aşımlarıyla kendiliğinden biter
            sources = {futures[f]: f.result() for f in finished}
            sources.update({futures[f]: _source_result(error="Lig çekimi süre sınırını aştı") for f in late})
            with leased_session(token, QUICK_LOCK_MINUTES) as conn:
                iddaa_ok = _sync_iddaa(conn, retries=QUICK_IDDAA_RETRIES)
                sources["iddaa"] = _source_result(error=None if iddaa_ok else get_meta(conn, "iddaa_last_error") or "Oranlar alınamadı")
    except Exception as exc:
        error = str(exc)
    finally:
        with session() as conn:
            if skipped:
                release_lock(conn, token)
            else:
                try:
                    error = _finish(conn, error, sources, token)
                except LostLease as exc:
                    error = str(exc)
    if not skipped:
        _invalidate_models()
        if sources.get("iddaa", {}).get("status") == "success":
            _capture_snapshots()
        _track_coupons(freeze=False)  # hızlı yol yalnız sonuçlandırır; dondurma ağır model kurar
    state = status()
    result_status = "error" if error and not sources else "partial" if state["outcome"] == "partial" else "error" if error else "updated"
    return {"status": "cooldown" if skipped else result_status, **state}


# --- Tam güncelleme (cron / komut satırı) -----------------------------------------
def run_update(leagues=None, with_news=True, wait_lock_seconds: int = 0) -> str:
    """Ağır tam güncelleme. Dönen değer: "ok" | "error" | "skipped" (kilit alınamadı)."""
    init_db()
    token = _take_lock(FULL_LOCK_MINUTES, wait_lock_seconds)
    if not token:
        _progress("Başka bir güncelleme sürüyor; atlandı")
        return "skipped"
    error, sources = None, {}
    try:
        # Ligler paralel çekilir (her biri kendi bağlantısıyla); site başına istekler yine sıralı
        selected = list(leagues or LEAGUES)
        with ThreadPoolExecutor(max_workers=6) as pool:
            for lg, result in zip(selected, pool.map(partial(_collect_league, token=token), selected)):
                sources[lg] = result
                _progress(f"{lg}: {result['count']} maç işlendi ({result['status']})")
        with leased_session(token, FULL_LOCK_MINUTES) as conn:
            ok = _sync_iddaa(conn)
            sources["iddaa"] = _source_result(error=None if ok else get_meta(conn, "iddaa_last_error") or "Oranlar alınamadı")
        if with_news:
            try:
                with leased_session(token, FULL_LOCK_MINUTES) as conn:
                    n = news.sync_news(conn, progress=_progress, strict=True)
                sources["news"] = _source_result(n)
                _progress(f"{n} haber işlendi")
            except Exception as exc:
                sources["news"] = _source_result(error=exc)
    except Exception as exc:  # arayüzde göstermek için
        error = str(exc)
        _progress(f"Hata: {exc}")
    finally:
        with session() as conn:
            try:
                error = _finish(conn, error, sources, token)
            except LostLease as exc:
                error = str(exc)
    _invalidate_models()
    if sources.get("iddaa", {}).get("status") == "success":
        _capture_snapshots()
    if error is None:
        _track_coupons(freeze=True)
    else:
        _track_coupons(freeze=False)
    _progress("Güncelleme tamamlandı" if not error else error)
    return "error" if error else "ok"


def run_in_background(**kw) -> bool:
    """Yalnız yerel SQLite geliştirmesi için (açılışta eski veriyi tazele); serverless'ta kullanılmaz."""
    threading.Thread(target=run_update, kwargs=kw, daemon=True).start()
    return True


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Maç ve haber verilerini güncelle")
    p.add_argument("--league", action="append", choices=list(LEAGUES), help="Sadece bu lig(ler)")
    p.add_argument("--no-news", action="store_true", help="Haberleri atla")
    p.add_argument("--quick", action="store_true", help="Yalnız hızlı artımlı güncelleme (iddaa + son günler)")
    p.add_argument("--wait-lock", type=int, default=120, help="Kilit doluysa en çok kaç sn beklensin")
    a = p.parse_args()
    if a.quick:
        import faulthandler
        faulthandler.dump_traceback_later(270, exit=True)
        res = run_quick_update()
        print(res.get("status"))
        sys.exit(1 if res.get("status") in ("error", "partial") else 0)
    sys.exit(1 if run_update(a.league, with_news=not a.no_news, wait_lock_seconds=a.wait_lock) == "error" else 0)
