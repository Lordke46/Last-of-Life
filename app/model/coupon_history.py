"""Kupon geçmişi: günün Banko / Sürpriz kuponunu dondurur, sonuçlara göre işaretler, kâr/zararı hesaplar.

* freeze(): kupon (gün, tür) başına bir kez, ilk üretildiği haliyle `saved_coupons` / `saved_coupon_picks`
  tablolarına yazılır; sonradan oranlar veya tahminler değişse de kayıt değişmez. Seçimi olmayan (boş)
  kupon kaydedilmez; gün içinde uygun kupon çıkarsa sonraki çalışmada kaydedilir.
* settle(): biten maçlara göre her seçimi won / lost / void, kuponu da won / lost / pending / void yapar;
  ESPN eşleşmesi olmayan bülten bacaklarının skoru iddaa canlı sonuç servisinden alınır.
  Tek seçim yattıysa kupon hemen yatmıştır; ertelenen / iptal edilen maç oranı 1.00 sayılır (void).
* history(): tutma oranı, "her gün KUPON_BASINA 100 TL" kâr/zarar + ROI, CLV ve günlük liste.
* CLV (closing line value): kaydedilen oran / maçtan önce görülen son iddaa oranı − 1. Pozitifse piyasa
  bizim yönümüze hareket etmiştir; uzun vadede kârın en erken ve en güvenilir öncü göstergesidir.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from ..db import get_meta, set_meta

from .coupons import TZ, build_coupons
from .settlement import outcome, selection_state, SUPPORTED_MARKETS

log = logging.getLogger(__name__)

STAKE = 100.0  # simülasyon: her gün, her kupona yatırılan TL
KINDS = ("banko", "surprise")
STAKE_MIN_PCT, STAKE_MAX_PCT = 0.5, 3.0  # kasanın yüzdesi
STAKE_RECENT_DAYS = 7
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
                  "calibration_applied", "data_n", "probability_source", "value", "news_effect", "iddaa_event_id")
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


def stake_plan(coupon: dict, recent_profit: float | None = None) -> dict | None:
    """Çeyrek Kelly, kasanın %0.5–3'ü ile sınırlı; son 7 gün zarardaysa yarıya iner."""
    p, odds = coupon.get("total_probability"), coupon.get("total_odds")
    if not isinstance(p, (int, float)) or not isinstance(odds, (int, float)) or odds <= 1:
        return None
    b = odds - 1
    kelly = (p * b - (1 - p)) / b
    pct = min(STAKE_MAX_PCT, max(STAKE_MIN_PCT, kelly / 4 * 100))
    note = "çeyrek Kelly" if kelly > 0 else "değer pozitif değil, en düşük miktar"
    if recent_profit is not None and recent_profit < 0:
        pct = max(STAKE_MIN_PCT, pct / 2)
        note += "; son 7 gün zararda, yarıya indirildi"
    return {"percent": round(pct, 1), "note": note}


def recent_profits(conn, today, days: int = STAKE_RECENT_DAYS) -> dict:
    """Türe göre son günlerin yaklaşık kâr birimi (1 birim yatırım; iptal bacaklar yok sayılır)."""
    since = (today - timedelta(days=days)).isoformat()
    out = {}
    for r in conn.execute("SELECT kind, status, total_odds FROM saved_coupons WHERE day>=? AND day<?",
                          (since, today.isoformat())):
        gain = (r["total_odds"] - 1) if r["status"] == "won" else -1 if r["status"] == "lost" else 0
        out[r["kind"]] = out.get(r["kind"], 0) + gain
    return out


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
        try:
            recent = recent_profits(conn, target)
        except Exception:  # noqa: BLE001 - stake önerisi kuponu engellememeli
            log.warning("stake geçmişi okunamadı", exc_info=True)
            recent = {}
    for kind in KINDS:
        for key in (kind, f"current_{kind}"):
            if built.get(key) and built[key].get("picks"):
                built[key] = {**built[key], "stake_plan": stake_plan(built[key], recent.get(kind))}
    return built


# --- Sonuçlandırma -------------------------------------------------------------------
def _pick_outcome(row) -> tuple[str, int | None, int | None]:
    """Bekleyen bir seçimin (durum, ev golü, dep golü) bilgisi; maç bitmediyse ('pending', None, None)."""
    state = selection_state(row["market"], row["kickoff"], _now(), completed=row["completed"],
                            home_goals=row["home_goals"], away_goals=row["away_goals"], status=row["match_status"])
    return state, row["home_goals"] if row["completed"] else None, row["away_goals"] if row["completed"] else None


CLOSING_LOOKBACK = timedelta(days=35)  # iddaa oran geçmişi 30 gün tutulur; daha eski seçim için kapanış aranmaz


def _event_ids(conn, cache: dict, day: str, kind: str) -> dict[tuple[str, str], str]:
    """Dondurma anında seçimle kaydedilen iddaa etkinlik kimlikleri: {(match_id, market): event_id}."""
    if (day, kind) not in cache:
        metadata = json.loads(get_meta(conn, f"coupon_metadata:{day}:{kind}") or "{}")
        ids = {}
        for key, fields in (metadata.get("picks") or {}).items():
            if fields.get("iddaa_event_id"):
                ids[tuple(json.loads(key))] = str(fields["iddaa_event_id"])
        cache[(day, kind)] = ids
    return cache[(day, kind)]


def closing_odds(conn, match_id: str, market: str, kickoff: str, event_id: str | None = None) -> float | None:
    """Maçtan önce görülen son iddaa oranı: önce etkinlik oran geçmişi, yoksa ESPN eşleşmeli odds_log."""
    for eid in dict.fromkeys(x for x in (event_id, match_id) if x):
        row = conn.execute("SELECT odd FROM iddaa_odds_history WHERE event_id=? AND market=? AND captured_at<? "
                           "ORDER BY captured_at DESC LIMIT 1", (eid, market, kickoff)).fetchone()
        if row and row["odd"] and row["odd"] > 1:
            return float(row["odd"])
    row = conn.execute("SELECT odd FROM odds_log WHERE match_id=? AND market=? AND updated_at<?",
                       (match_id, market, kickoff)).fetchone()
    return float(row["odd"]) if row and row["odd"] and row["odd"] > 1 else None


def record_closing(conn, now: datetime | None = None) -> int:
    """Başlamış ve kapanış oranı henüz yazılmamış seçimlere kapanış oranını işler; bulunamayan sonraki turda denenir."""
    now = now or _now()
    rows = conn.execute(
        "SELECT day, kind, match_id, market, kickoff FROM saved_coupon_picks "
        "WHERE closing_odds IS NULL AND kickoff<? AND kickoff>=?",
        (now.isoformat(), (now - CLOSING_LOOKBACK).isoformat())).fetchall()
    cache, done = {}, 0
    for r in rows:
        event_id = _event_ids(conn, cache, r["day"], r["kind"]).get((r["match_id"], r["market"]))
        close = closing_odds(conn, r["match_id"], r["market"], r["kickoff"], event_id)
        if close is None:
            continue
        conn.execute("UPDATE saved_coupon_picks SET closing_odds=? WHERE day=? AND kind=? AND match_id=?",
                     (close, r["day"], r["kind"], r["match_id"]))
        done += 1
    return done


RESULT_DELAY = timedelta(hours=2)  # maç bitmiş olmalı; erken sorgu bir sonraki turda yinelenir
RESULT_WINDOW = timedelta(days=3)  # iddaa canlı sonuç servisi yalnız son ~3 günü verir (öncesi HTTP 400)


def iddaa_results(rows, fetch, now: datetime) -> dict[str, tuple[int, int]]:
    """ESPN skoru olmayan, başlayalı en az RESULT_DELAY geçmiş seçimlerin TSİ günleri için iddaa skorları.
    fetch(gün) → {etkinlik_id: (ev, dep)}; bir günün hatası diğerlerini durdurmaz."""
    if fetch is None:
        return {}
    days = set()
    for r in rows:
        kickoff = datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00"))
        if not r["completed"] and now - RESULT_WINDOW <= kickoff <= now - RESULT_DELAY:
            days.add(kickoff.astimezone(TZ).date().isoformat())
    results = {}
    for day in sorted(days):
        try:
            results.update(fetch(day))
        except Exception as exc:
            log.warning("iddaa sonuçları alınamadı (%s): %s", day, exc)
    return results


def settle(conn, fetch_results=None) -> dict:
    """Biten maçlara göre bekleyen seçimleri ve kuponları işaretler, başlamış seçimlere kapanış oranını yazar.
    fetch_results verilirse ESPN eşleşmesi olmayan bacaklar iddaa skoruyla sonuçlanır (bkz. iddaa_results).
    {"picks": n, "coupons": n, "closing": n} döner."""
    rows = [dict(r) for r in conn.execute(
        "SELECT p.day, p.kind, p.match_id, p.market, p.kickoff, m.completed, m.home_goals, m.away_goals, "
        "m.status AS match_status FROM saved_coupon_picks p LEFT JOIN matches m ON m.id = p.match_id "
        "WHERE p.status = 'pending'").fetchall()]
    results, cache = iddaa_results(rows, fetch_results, _now()), {}
    touched, picks_done = set(), 0
    for r in rows:
        if results and not r["completed"]:
            event_id = _event_ids(conn, cache, r["day"], r["kind"]).get((r["match_id"], r["market"])) or r["match_id"]
            if score := results.get(str(event_id)):
                r.update(completed=1, home_goals=score[0], away_goals=score[1])
        status, hg, ag = _pick_outcome(r)
        if status == "pending":
            continue
        conn.execute("UPDATE saved_coupon_picks SET status=?, home_goals=?, away_goals=? "
                     "WHERE day=? AND kind=? AND match_id=?", (status, hg, ag, r["day"], r["kind"], r["match_id"]))
        touched.add((r["day"], r["kind"]))
        picks_done += 1
    closing_done = record_closing(conn)
    now, coupons_done = _now().isoformat(timespec="seconds"), 0
    for day, kind in touched:
        statuses = [x["status"] for x in conn.execute(
            "SELECT status FROM saved_coupon_picks WHERE day=? AND kind=?", (day, kind))]
        new = coupon_status(statuses)
        conn.execute("UPDATE saved_coupons SET status=?, settled_at=? WHERE day=? AND kind=?",
                     (new, None if new == "pending" else now, day, kind))
        coupons_done += new != "pending"
    return {"picks": picks_done, "coupons": coupons_done, "closing": closing_done}


# --- Geçmiş / istatistik -------------------------------------------------------------
def effective_odds(picks: list[dict]) -> float:
    """Kupon oranı; iptal (void) seçimler 1.00 sayılır."""
    total = 1.0
    for p in picks:
        if p["status"] != "void":
            total *= p["odds"]
    return round(total, 2)


def pick_clv(pick: dict) -> float | None:
    """Kapanış oranına göre değer: kaydedilen oran / kapanış − 1 (iptal seçimler hariç)."""
    close, odds = pick.get("closing_odds"), pick.get("odds")
    if pick.get("status") == "void" or not close or not odds or close <= 1 or odds <= 1:
        return None
    return round(odds / close - 1, 4)


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
    clvs = [p["clv"] for c in coupons for p in c["picks"] if p.get("clv") is not None]
    return {"coupons": len(coupons), "won": won, "lost": lost, "pending": count("pending"), "void": count("void"),
            "hit_rate": won / (won + lost) if won + lost else None,
            "picks_settled": len(picks), "picks_won": picks_won,
            "pick_hit_rate": picks_won / len(picks) if picks else None,
            "stake": stake, "returned": returned, "profit": round(returned - stake, 2),
            "roi": (returned - stake) / stake if stake else None,
            "clv_picks": len(clvs), "clv": round(sum(clvs) / len(clvs), 4) if clvs else None,
            "clv_positive": sum(c > 0 for c in clvs)}


def history(conn, days: int = 60) -> dict:
    """Tüm kayıtlı kuponların özeti ve en yeni `days` günün listesi."""
    picks_by = {}
    for r in conn.execute("SELECT * FROM saved_coupon_picks ORDER BY kickoff, match_id"):
        pick = {k: r[k] for k in ("match_id", "league", "league_name", "kickoff", "home", "away", "market", "odds",
                                  "odds_source", "probability", "status", "home_goals", "away_goals", "closing_odds")}
        pick["clv"] = pick_clv(pick)
        picks_by.setdefault((r["day"], r["kind"]), []).append(pick)
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
