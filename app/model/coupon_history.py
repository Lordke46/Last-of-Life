"""Kupon geçmişi: günün Banko / Sürpriz kuponunu dondurur, sonuçlara göre işaretler, kâr/zararı hesaplar.

* freeze(): kupon (gün, tür) başına bir kez, ilk üretildiği haliyle `saved_coupons` / `saved_coupon_picks`
  tablolarına yazılır; sonradan oranlar veya tahminler değişse de kayıt değişmez. Seçimi olmayan (boş)
  kupon kaydedilmez; gün içinde uygun kupon çıkarsa sonraki çalışmada kaydedilir.
* settle(): biten maçlara göre her seçimi won / lost / void, kuponu da won / lost / pending / void yapar.
  Tek seçim yattıysa kupon hemen yatmıştır; ertelenen / iptal edilen maç oranı 1.00 sayılır (void).
* history(): tutma oranı, "her gün KUPON_BASINA 100 TL" kâr/zarar + ROI ve günlük liste.
"""
import json
from datetime import datetime, timezone

from ..db import get_meta, set_meta

from .coupons import TZ, build_coupons
from .settlement import outcome, selection_state, SUPPORTED_MARKETS

STAKE = 100.0  # simülasyon: her gün, her kupona yatırılan TL
KINDS = ("banko", "surprise")
# Grok veto'su sonrası geçilen alternatifler; yayımlanan kupondan ayrı ölçülür (matematik ve yorum izi ayrı kalır).
GROK_KINDS = tuple(f"{k}_grok" for k in KINDS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def pick_result(market: str, home_goals: int, away_goals: int) -> str | None:
    """Skora göre 'won' / 'lost'; tanınmayan pazar için None (seçim bekliyor kalır)."""
    won = outcome(market, home_goals, away_goals)
    if won is None:
        return None
    return "won" if won else "lost"


def coupon_status(pick_statuses: list[str]) -> str:
    if "lost" in pick_statuses:
        return "lost"
    if "pending" in pick_statuses:
        return "pending"
    return "void" if all(s == "void" for s in pick_statuses) else "won"


# --- Dondurma ----------------------------------------------------------------------
def freeze(conn, coupons: dict, kinds: tuple[str, ...] = KINDS) -> list[str]:
    """`build_coupons` çıktısındaki boş olmayan kuponları, daha önce kaydedilmemişse yazar.
    Yarışta (iki istek aynı anda) birincil anahtar tek kaydı garanti eder. Yeni kaydedilen türleri döner."""
    saved, now = [], _now().isoformat(timespec="seconds")
    for kind in kinds:
        c = coupons.get(kind) or {}
        picks = c.get("picks") or []
        if not picks:
            continue
        cur = conn.execute(
            "INSERT INTO saved_coupons(day, kind, total_odds, total_probability, odds_source, status, created_at) "
            "VALUES(?, ?, ?, ?, ?, 'pending', ?) ON CONFLICT(day, kind) DO NOTHING",
            (coupons["date"], kind, c["total_odds"], c["total_probability"], c.get("odds_source"), now))
        if cur.rowcount != 1:
            continue
        for p in picks:
            conn.execute(
                "INSERT INTO saved_coupon_picks(day, kind, match_id, league, league_name, kickoff, home, away, "
                "market, odds, odds_source, probability, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending')",
                (coupons["date"], kind, str(p["id"]), p["league"], p.get("league_name"), p["kickoff"], p["home"],
                 p["away"], p["market"], p["odds"], p.get("odds_source"), p["p"]))
        # Seçimle birlikte kanıt durumunu da dondur; sonraki kalibrasyon geçmişi değiştirmesin.
        fields = ("model_probability", "market_probability", "raw_p", "calibration_n",
                  "calibration_applied", "data_n", "probability_source", "value", "news_effect")
        metadata = {k: c[k] for k in ("selection_policy", "calibration_status", "calibration_note", "stake_note",
                                      "target_met", "selection_tier", "grok_replaces", "grok_note") if k in c}
        metadata["picks"] = {json.dumps([str(p["id"]), p["market"]]):
                             {k: p[k] for k in fields if k in p} for p in picks}
        set_meta(conn, f"coupon_metadata:{coupons['date']}:{kind}", json.dumps(metadata))
        saved.append(kind)
    return saved


def freeze_today() -> list[str]:
    """Bugünün (Türkiye saati) kuponlarını üretip dondurur (güncelleme akışından çağrılır)."""
    from ..db import session
    coupons = build_coupons(datetime.now(TZ).date())
    with session() as conn:
        return freeze(conn, coupons)


def freeze_if_today(coupons: dict) -> list[str]:
    """/api/coupons yanıtı bugüne aitse dondurur. Kayıt hatası kupon yanıtını asla bozmaz."""
    if coupons.get("date") != datetime.now(TZ).date().isoformat():
        return []
    try:
        from ..db import session
        with session() as conn:
            return freeze(conn, coupons)
    except Exception as exc:
        print(f"Kupon geçmişi kaydedilemedi: {exc}", flush=True)
        return []


def load_saved(conn, day: str, kind: str) -> dict | None:
    """Dondurulmuş (gün, tür) kuponu `build_coupons` biçiminde döner; kayıt yoksa None."""
    row = conn.execute("SELECT * FROM saved_coupons WHERE day=? AND kind=?", (day, kind)).fetchone()
    if not row:
        return None
    try:
        metadata = json.loads(get_meta(conn, f"coupon_metadata:{day}:{kind}", "{}"))
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict) or not isinstance(metadata.get("picks", {}), dict):
        metadata = {}
    picks = []
    for p in conn.execute("SELECT * FROM saved_coupon_picks WHERE day=? AND kind=? ORDER BY kickoff, match_id",
                          (day, kind)):
        picks.append({"id": p["match_id"], "league": p["league"], "league_name": p["league_name"],
                      "home": p["home"], "away": p["away"], "kickoff": p["kickoff"],
                      "market": p["market"], "odds": p["odds"], "odds_source": p["odds_source"],
                      "p": p["probability"],
                      **metadata.get("picks", {}).get(json.dumps([str(p["match_id"]), p["market"]]), {})})
    return {"type": kind, "picks": picks, "total_odds": row["total_odds"],
            "total_probability": row["total_probability"], "odds_source": row["odds_source"],
            "iddaa_picks": sum(p["odds_source"] == "iddaa" for p in picks),
            "coupon_id": f"{day}:{kind}:v1", "publication": "published",
            "published_at": row["created_at"], "stake_note": "Yayımlandığı andaki seçim ve oranlar.",
            **{k: metadata[k] for k in ("selection_policy", "calibration_status", "calibration_note", "stake_note",
                                        "target_met", "selection_tier", "grok_replaces", "grok_note") if k in metadata}}


def daily_coupons(target, build=build_coupons) -> dict:
    """Web ve Telegram aynı (gün, tür) kaydını yayımlar; alternatifler canlı kalır."""
    from ..db import session
    built = build(target)
    # Güncel seçimler ayrı sunulur; daha önce yayımlanmış kayıt geçmişte değişmez.
    for kind in KINDS:
        built[f'current_{kind}'] = {**built.get(kind, {}), 'publication':'live'}
        built[f'current_{kind}_alternatives'] = [{**c, 'publication':'live'} for c in built.get(f'{kind}_alternatives', [])]
    with session() as conn:
        if target == _now().astimezone(TZ).date():
            freeze(conn, built)
        for kind in KINDS:
            published = load_saved(conn, target.isoformat(), kind)
            if not published:
                continue
            signature = lambda c: frozenset((str(p["id"]), p["market"]) for p in c.get("picks", []))
            live = [{**c, "publication": "live"} for c in built.get(f"{kind}_alternatives", [])
                    if signature(c) != signature(published)]
            built[kind] = published
            built[f"{kind}_alternatives"] = [published, *live]
    return built


# --- Sonuçlandırma -------------------------------------------------------------------
def _pick_outcome(row) -> tuple[str, int | None, int | None]:
    """Bekleyen bir seçimin (durum, ev golü, dep golü) bilgisi; maç bitmediyse ('pending', None, None)."""
    state = selection_state(row["market"], row["kickoff"], _now(), completed=row["completed"],
                            home_goals=row["home_goals"], away_goals=row["away_goals"], status=row["match_status"])
    return state, row["home_goals"] if row["completed"] else None, row["away_goals"] if row["completed"] else None


def settle(conn) -> dict:
    """Biten maçlara göre bekleyen seçimleri ve kuponları işaretler. {"picks": n, "coupons": n} döner."""
    rows = conn.execute(
        "SELECT p.day, p.kind, p.match_id, p.market, p.kickoff, m.completed, m.home_goals, m.away_goals, "
        "m.status AS match_status FROM saved_coupon_picks p LEFT JOIN matches m ON m.id = p.match_id "
        "WHERE p.status = 'pending'").fetchall()
    touched, picks_done = set(), 0
    for r in rows:
        status, hg, ag = _pick_outcome(r)
        if status == "pending":
            continue
        conn.execute("UPDATE saved_coupon_picks SET status=?, home_goals=?, away_goals=? "
                     "WHERE day=? AND kind=? AND match_id=?", (status, hg, ag, r["day"], r["kind"], r["match_id"]))
        touched.add((r["day"], r["kind"]))
        picks_done += 1
    now, coupons_done = _now().isoformat(timespec="seconds"), 0
    for day, kind in touched:
        statuses = [x["status"] for x in conn.execute(
            "SELECT status FROM saved_coupon_picks WHERE day=? AND kind=?", (day, kind))]
        new = coupon_status(statuses)
        conn.execute("UPDATE saved_coupons SET status=?, settled_at=? WHERE day=? AND kind=?",
                     (new, None if new == "pending" else now, day, kind))
        coupons_done += new != "pending"
    return {"picks": picks_done, "coupons": coupons_done}


# --- Geçmiş / istatistik -------------------------------------------------------------
def effective_odds(picks: list[dict]) -> float:
    """Kupon oranı; iptal (void) seçimler 1.00 sayılır."""
    total = 1.0
    for p in picks:
        if p["status"] != "void":
            total *= p["odds"]
    return round(total, 2)


def coupon_profit(coupon: dict) -> float | None:
    """STAKE TL'lik kuponun kâr/zararı; bekleyen kupon için None, iptal için 0."""
    if coupon["status"] == "won":
        return round(STAKE * (coupon["effective_odds"] - 1), 2)
    if coupon["status"] == "lost":
        return -STAKE
    return 0.0 if coupon["status"] == "void" else None


def _summary(coupons: list[dict]) -> dict:
    count = lambda s: sum(c["status"] == s for c in coupons)
    won, lost = count("won"), count("lost")
    stake = STAKE * (won + lost)
    returned = round(sum(STAKE * c["effective_odds"] for c in coupons if c["status"] == "won"), 2)
    picks = [p for c in coupons for p in c["picks"] if p["status"] in ("won", "lost")]
    picks_won = sum(p["status"] == "won" for p in picks)
    return {"coupons": len(coupons), "won": won, "lost": lost, "pending": count("pending"), "void": count("void"),
            "hit_rate": won / (won + lost) if won + lost else None,
            "picks_settled": len(picks), "picks_won": picks_won,
            "pick_hit_rate": picks_won / len(picks) if picks else None,
            "stake": stake, "returned": returned, "profit": round(returned - stake, 2),
            "roi": (returned - stake) / stake if stake else None}


def history(conn, days: int = 60) -> dict:
    """Tüm kayıtlı kuponların özeti ve en yeni `days` günün listesi."""
    picks_by = {}
    for r in conn.execute("SELECT * FROM saved_coupon_picks ORDER BY kickoff, match_id"):
        picks_by.setdefault((r["day"], r["kind"]), []).append(
            {k: r[k] for k in ("match_id", "league", "league_name", "kickoff", "home", "away", "market", "odds",
                               "odds_source", "probability", "status", "home_goals", "away_goals")})
    coupons = []
    for r in conn.execute("SELECT * FROM saved_coupons ORDER BY day DESC, kind"):
        c = {k: r[k] for k in ("day", "kind", "total_odds", "total_probability", "odds_source", "status",
                               "created_at", "settled_at")}
        c["picks"] = picks_by.get((c["day"], c["kind"]), [])
        c["coupon_id"] = f"{c['day']}:{c['kind']}:v1"
        c["settlement_warning"] = "Desteklenmeyen eski pazar: sonuç elle doğrulanmalı" if any(
            p["market"] not in SUPPORTED_MARKETS for p in c["picks"]) else None
        c["effective_odds"] = effective_odds(c["picks"])
        c["profit"] = coupon_profit(c)
        coupons.append(c)
    by_day = {}
    for c in coupons:
        by_day.setdefault(c["day"], {"date": c["day"]})[c["kind"]] = c
    # Günlük kâr ve "Toplam" yalnız yayımlanan Banko/Sürpriz'i sayar; Grok türleri ayrı satırlarda ölçülür.
    day_list = [{**{k: None for k in (*KINDS, *GROK_KINDS)}, **d,
                 "profit": round(sum(c["profit"] or 0 for k, c in d.items() if k in KINDS), 2)}
                for d in by_day.values()]
    day_list.sort(key=lambda d: d["date"], reverse=True)
    summary = {kind: _summary([c for c in coupons if c["kind"] == kind]) for kind in (*KINDS, *GROK_KINDS)}
    summary["total"] = _summary([c for c in coupons if c["kind"] in KINDS])
    return {"stake": STAKE, "summary": summary, "days": day_list[:days], "total_days": len(day_list)}
