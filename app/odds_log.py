"""İddaa oranlarının ileriye dönük kaydı (harmanlı/harmansız backtest için).

`iddaa_odds` her senkronda silinip yeniden yazıldığı için geçmiş oran tutmaz. Bu tablo ESPN maç kimliği
başına her pazarın son (kapanışa en yakın) oranını ve ilk görülen oranını saklar; maç başladıktan sonra
güncellenmez. Backtest, sonuçlanmış maçlarda bu oranları walk-forward model olasılıklarıyla karşılaştırır.
Postgres şeması: supabase/migrations/*_odds_log.sql (SQLite şeması aşağıdaki SCHEMA'dır).
"""
from .db import session, set_meta

SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_log (
    match_id TEXT NOT NULL,         -- ESPN matches.id
    market TEXT NOT NULL,
    odd REAL NOT NULL,              -- maçtan önce görülen son oran
    first_odd REAL NOT NULL,        -- ilk görülen oran
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (match_id, market)
);
"""

CHUNK = 100  # tek INSERT'te satır sayısı (uzak veritabanında istek sayısını düşük tutar)


def record(conn, rows: list[tuple[str, str, float]], now: str):
    """rows: (match_id, market, oran). Var olan satırda yalnız son oran ve zamanı güncellenir."""
    for i in range(0, len(rows), CHUNK):
        part = rows[i:i + CHUNK]
        values = ",".join("(?, ?, ?, ?, ?, ?)" for _ in part)
        params = [x for match_id, market, odd in part for x in (match_id, market, odd, odd, now, now)]
        conn.execute(
            f"INSERT INTO odds_log(match_id, market, odd, first_odd, first_seen, updated_at) VALUES {values} "
            "ON CONFLICT(match_id, market) DO UPDATE SET odd=excluded.odd, updated_at=excluded.updated_at",
            params)


def record_safely(conn, rows: list[tuple[str, str, float]], now: str) -> bool:
    """Kayıt hatası (ör. tablo henüz oluşturulmamış) oran senkronunu bozmasın: önce bülten işlemi
    commit edilir, kayıt ayrı işlemde denenir, hata `odds_log_error` meta anahtarına yazılır."""
    conn.commit()
    try:
        record(conn, rows, now)
        set_meta(conn, "odds_log_error", "")
        conn.commit()
        return True
    except Exception as exc:
        conn.rollback()
        try:
            set_meta(conn, "odds_log_error", str(exc)[:300])
            conn.commit()
        except Exception:
            conn.rollback()
        return False


def load_for_league(conn, league: str) -> dict[str, dict[str, float]]:
    """Lig maçları için kaydedilmiş oranlar: {match_id: {pazar: oran}}."""
    rows = conn.execute(
        "SELECT l.match_id, l.market, l.odd, l.first_seen, l.updated_at, m.kickoff FROM odds_log l JOIN matches m ON m.id=l.match_id "
        "WHERE m.league=?", (league,)).fetchall()
    out: dict[str, dict[str, float]] = {}
    from .quote_provenance import timestamp
    for r in rows:
        first, last, kickoff = (timestamp(r[k]) for k in ('first_seen','updated_at','kickoff'))
        if not first or not last or not kickoff or not first<=last<kickoff:
            continue
        out.setdefault(r["match_id"], {})[r["market"]] = r["odd"]
    return out


def summary(conn) -> dict:
    """Kayıt durumu: kaç maç kaydedildi, kaçı sonuçlandı (backtest'e girebilir), ilk kayıt zamanı."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT l.match_id) matches, MIN(l.first_seen) since FROM odds_log l").fetchone()
    done = conn.execute(
        "SELECT COUNT(DISTINCT l.match_id) n FROM odds_log l JOIN matches m ON m.id=l.match_id "
        "WHERE m.completed=1").fetchone()
    return {"available": True, "matches": row["matches"] or 0, "completed": done["n"] or 0,
            "since": row["since"]}


def current_summary() -> dict:
    """`summary` kendi bağlantısıyla; tablo yoksa (migration uygulanmadı) {"available": False}."""
    try:
        with session() as conn:
            return summary(conn)
    except Exception:
        return {"available": False}
