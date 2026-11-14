"""Telegram kanalına günlük kupon ve sonuç paylaşımı (Bot API, kütüphanesiz HTTP).

Komut satırı / GitHub Actions (.github/workflows/telegram.yml):
    python -m app.telegram_bot [--dry-run] [--force] [--day YYYY-MM-DD]

Her çalıştırma iki şeyi idempotent yapar:
  1. Sabah penceresinde (Türkiye saati) o günün banko + sürpriz kuponunu kanala gönderir.
  2. Daha önce gönderilmiş kuponlardaki tüm maçlar bitince sonucu kuponun altına yanıt olarak yazar.
Aynı gün/tür ikinci kez gönderilmez: `telegram_posts` tablosundaki (day, kind) kaydı önce alınır, sonra
gönderilir; gönderim başarısız olursa kayıt silinip sonraki çalıştırmada tekrar denenir.

TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID yoksa (dry-run hariç) sessizce atlanır.
"""
import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from .db import get_meta, init_db, session
from .model.coupons import TZ, build_coupons
from .model.settlement import outcome, selection_state, VOID_AFTER_DAYS
from .model.coupon_history import daily_coupons, iddaa_results, _event_ids, stake_plan
from .scrapers import iddaa

API_URL = "https://api.telegram.org/bot{token}/{method}"
MORNING_START_HOUR = 9   # Türkiye saati: kupon bu saatten önce gönderilmez
MORNING_END_HOUR = 15    # ... ve bu saatten sonra gönderilmez (başlamış maçlarla boş kalmasın)
STALE_CLAIM_MINUTES = 30  # çöken çalıştırmanın bıraktığı 'sending' kaydı bu süreden sonra yeniden alınır
RESULT_LOOKBACK_DAYS = VOID_AFTER_DAYS + 2

MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim",
          "Kasım", "Aralık"]
MARKET_LABELS = {
    "1": "MS 1", "X": "MS X", "2": "MS 2", "1X": "Çifte Şans 1-X", "12": "Çifte Şans 1-2",
    "X2": "Çifte Şans X-2", "Ü1.5": "1.5 Üst", "A1.5": "1.5 Alt", "Ü2.5": "2.5 Üst", "A2.5": "2.5 Alt",
    "Ü3.5": "3.5 Üst", "A3.5": "3.5 Alt", "KG Var": "KG Var", "KG Yok": "KG Yok",
}
COUPON_TITLES = {"banko": "💎 BANKO", "surprise": "🎲 SÜRPRİZ",
                 "banko_grok": "💎 BANKO (Grok değişimi)", "surprise_grok": "🎲 SÜRPRİZ (Grok değişimi)"}
WEEKLY_WEEKDAY, WEEKLY_HOUR = 0, 10  # Pazartesi 10:00 TSİ'den sonra geçen haftanın özeti


class TelegramError(RuntimeError):
    pass


# --- Telegram HTTP ---------------------------------------------------------------
def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def send_message(token: str, chat_id: str, text: str, reply_to: str | None = None) -> str:
    """sendMessage çağırır ve mesaj kimliğini döndürür. 429'da bir kez bekleyip yeniden dener."""
    body = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_to:
        body.update(reply_to_message_id=int(reply_to), allow_sending_without_reply=True)
    data = json.dumps(body).encode()
    for attempt in (1, 2):
        req = urllib.request.Request(API_URL.format(token=token, method="sendMessage"), data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.load(resp)
            break
        except urllib.error.HTTPError as exc:
            try:
                err = json.load(exc)
            except Exception:  # noqa: BLE001
                err = {}
            retry = (err.get("parameters") or {}).get("retry_after")
            if exc.code == 429 and retry and attempt == 1:
                time.sleep(min(int(retry), 30))
                continue
            raise TelegramError(_scrub(f"Telegram HTTP {exc.code}: {err.get('description', exc.reason)}",
                                       token)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramError(_scrub(f"Telegram'a ulaşılamadı: {exc}", token)) from None
    if not payload.get("ok"):
        raise TelegramError(_scrub(f"Telegram hata: {payload.get('description')}", token))
    return str(payload["result"]["message_id"])


# --- Mesaj metinleri ---------------------------------------------------------------
def _esc(value) -> str:
    return html.escape(str(value), quote=False)


def _day_label(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _kickoff_tr(kickoff: str) -> str:
    return datetime.fromisoformat(kickoff.replace("Z", "+00:00")).astimezone(TZ).strftime("%H:%M")


def _label(market: str) -> str:
    return MARKET_LABELS.get(market, market)


def _compact_coupon(coupon: dict) -> dict:
    """Veritabanında saklanacak (sonucu değerlendirmeye yetecek) kupon özeti."""
    fields = ("id", "league_name", "kickoff", "home", "away", "market", "odds", "p")
    extras = ("model_probability", "market_probability", "raw_p", "calibration_n",
              "calibration_applied", "data_n", "probability_source", "value", "news_effect")
    return {"type": coupon["type"], "total_odds": coupon["total_odds"],
            "total_probability": coupon["total_probability"],
            "coupon_id": coupon.get("coupon_id"), "published_at": coupon.get("published_at"),
            **{k: coupon[k] for k in ("target_met", "stake_note", "stake_plan", "grok_note") if k in coupon},
            "picks": [{**{k: p[k] for k in fields}, **{k: p[k] for k in extras if k in p}}
                      for p in coupon["picks"]]}


def _stake_line(plan: dict | None) -> str | None:
    return f"Önerilen miktar: kasanın %{plan['percent']:g}'i" if plan else None


def format_coupon_message(day: str, coupons: list[dict], banko_note: str | None = None) -> str:
    """Sade mesaj: her kupon için maç → oynanacak seçim (oran), altta toplam oran ve önerilen miktar."""
    lines = [f"🎯 <b>Günlük Kupon</b> · {_day_label(day)}"]
    for c in coupons:
        lines += ["", f"<b>{COUPON_TITLES.get(c['type'], c['type'])}</b>"]
        if c.get("grok_note"):
            lines.append(f"<i>♻️ {_esc(c['grok_note'])}</i>")
        for p in sorted(c["picks"], key=lambda x: x["kickoff"]):
            lines.append(f"• {_kickoff_tr(p['kickoff'])} {_esc(p['home'])} – {_esc(p['away'])} → "
                         f"<b>{_esc(_label(p['market']))}</b> ({p['odds']:.2f})")
        total = f"Toplam oran <b>{c['total_odds']:.2f}</b> · tutma ihtimali %{c['total_probability'] * 100:.0f}"
        if not c.get("target_met", True):
            total += " · <i>yedek kupon</i>"
        lines.append(total)
        if stake := _stake_line(c.get("stake_plan")):
            lines.append(stake)
    if banko_note and not any(c["type"].startswith("banko") for c in coupons):
        lines += ["", "💎 <b>Banko yok:</b> " + _esc(banko_note)]
    lines += ["", "<i>İstatistiksel tahmindir, garanti değildir.</i>"]
    return "\n".join(lines)


def pick_outcome(market: str, home_goals: int, away_goals: int) -> bool | None:
    """Pazarın tuttuğu mu? Tanınmayan pazar için None."""
    return outcome(market, home_goals, away_goals)


def leg_states(coupon: dict, results: dict[str, dict], now: datetime) -> list[dict]:
    """Her bacağın durumu (won|lost|void|pending) ve skoru."""
    out = []
    for p in coupon["picks"]:
        res = results.get(str(p["id"])) or {}
        state = selection_state(p["market"], p["kickoff"], now, completed=res.get("completed"),
                                home_goals=res.get("home_goals"), away_goals=res.get("away_goals"), status=res.get("status"))
        score = f"{res['home_goals']}-{res['away_goals']}" if state in ("won", "lost") else None
        out.append({**p, "state": state, "score": score})
    return out


def settle_coupon(coupon: dict, results: dict[str, dict], now: datetime) -> dict | None:
    """Kuponu değerlendirir. Kaybeden bacak yoksa ve bitmemiş (henüz sonuçsuz sayılmayan) maç varsa None döner;
    bir bacak kaybettiyse kupon kalan maçları beklemeden kaybetmiş sayılır.
    Dönüş: {"status": won|lost|void, "odds": etkin oran, "picks": [{...pick, state, score}]}."""
    settled = leg_states(coupon, results, now)
    odds = 1.0
    for p in settled:
        if p["state"] == "won":
            odds *= p["odds"]
    states = {s["state"] for s in settled}
    if "pending" in states and "lost" not in states:
        return None
    status = "lost" if "lost" in states else ("void" if states == {"void"} else "won")
    return {"status": status, "odds": round(odds, 2), "picks": settled}


def format_result_message(day: str, settled: dict[str, dict]) -> str:
    lines = [f"📊 <b>Kupon Sonuçları</b> · {_day_label(day)}"]
    verdict = {"won": "✅ KAZANDI", "lost": "❌ KAYBETTİ", "void": "⚪ İPTAL"}
    icons = {"won": "✅", "lost": "❌", "void": "⚪", "pending": "⏳"}
    for kind in COUPON_TITLES:
        if kind not in settled:
            continue
        s = settled[kind]
        head = f"{COUPON_TITLES[kind]} · {verdict[s['status']]}"
        if s["status"] == "won":
            head += f" · <b>{s['odds']:.2f}</b>"
        lines += ["", head]
        for p in s["picks"]:
            score = p["score"] or ("bekleniyor" if p["state"] == "pending" else "sonuç yok")
            lines.append(f"{icons[p['state']]} {_esc(p['home'])} - {_esc(p['away'])} <b>{score}</b> "
                         f"→ {_esc(_label(p['market']))} ({p['odds']:.2f})")
    return "\n".join(lines)


def format_leg_message(coupon_type: str, leg: dict, coupon_lost: bool) -> str:
    icon = "✅" if leg["state"] == "won" else "❌"
    text = (f"{icon} {_esc(leg['home'])} – {_esc(leg['away'])} <b>{leg['score']}</b> → "
            f"{_esc(_label(leg['market']))} ({leg['odds']:.2f}) · {COUPON_TITLES.get(coupon_type, coupon_type)}")
    if coupon_lost:
        text += "\n<i>Bu kupon kaybetti.</i>"
    return text


def format_weekly_message(start: date, end: date, summary: dict[str, dict]) -> str:
    lines = [f"📅 <b>Haftalık Özet</b> · {start.day} {MONTHS[start.month - 1]} – {end.day} {MONTHS[end.month - 1]}"]
    for kind in ("banko", "surprise", "total"):
        s = summary.get(kind)
        if not s or not s["coupons"]:
            continue
        title = "📊 Toplam" if kind == "total" else COUPON_TITLES[kind]
        line = f"{title}: {s['won']} kazandı / {s['lost']} kaybetti"
        if s["pending"]:
            line += f" / {s['pending']} bekliyor"
        if s["roi"] is not None:
            line += f" · kâr <b>{s['profit']:+.0f} TL</b> (ROI %{s['roi'] * 100:+.0f})"
        lines.append(line)
    total = summary.get("total") or {}
    if total.get("clv") is not None:
        lines.append(f"📈 Kapanış oranı farkı (CLV): %{total['clv'] * 100:+.1f} ({total['clv_picks']} seçim)")
    if len(lines) == 1:
        lines.append("Bu hafta kayıtlı kupon yok.")
    lines += ["", "<i>Her kupona 100 TL yatırılmış gibi hesaplanır.</i>"]
    return "\n".join(lines)


# --- Kayıt (aynı gün iki kez gönderme engeli) ---------------------------------------
def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def claim(day: str, kind: str, now: datetime, payload: str | None = None) -> bool:
    """(gün, tür) için gönderim hakkını atomik alır. Başkası aldıysa (veya gönderildiyse) False."""
    with session() as conn:
        cur = conn.execute(
            "INSERT INTO telegram_posts(day, kind, status, payload, created_at) VALUES(?, ?, 'sending', ?, ?) "
            "ON CONFLICT(day, kind) DO NOTHING", (day, kind, payload, _iso(now)))
        if cur.rowcount == 1:
            return True
        stale = _iso(now - timedelta(minutes=STALE_CLAIM_MINUTES))
        cur = conn.execute(
            "UPDATE telegram_posts SET created_at=?, payload=? WHERE day=? AND kind=? "
            "AND status='sending' AND created_at < ?", (_iso(now), payload, day, kind, stale))
        return cur.rowcount == 1


def mark_sent(day: str, kind: str, message_id: str, now: datetime):
    with session() as conn:
        conn.execute("UPDATE telegram_posts SET status='sent', message_id=?, sent_at=? WHERE day=? AND kind=?",
                     (message_id, _iso(now), day, kind))


def release(day: str, kind: str):
    with session() as conn:
        conn.execute("DELETE FROM telegram_posts WHERE day=? AND kind=? AND status='sending'", (day, kind))


def _post_once(day, kind, now, payload, text, send, reply_to=None) -> bool:
    """Kaydı alır, gönderir, işaretler. Gönderim hatasında kaydı bırakır ve hatayı yükseltir."""
    if not claim(day, kind, now, payload):
        return False
    try:
        message_id = send(text, reply_to)
    except BaseException:
        release(day, kind)
        raise
    mark_sent(day, kind, message_id, now)
    return True


# --- Akışlar ---------------------------------------------------------------------
def post_daily_coupons(now: datetime, send, force: bool = False, day: date | None = None,
                       build=build_coupons) -> str:
    target = day or now.astimezone(TZ).date()
    key = target.isoformat()
    if not force and day is None and not MORNING_START_HOUR <= now.astimezone(TZ).hour < MORNING_END_HOUR:
        return "kupon: sabah penceresi dışında"
    with session() as conn:
        if conn.execute("SELECT 1 FROM telegram_posts WHERE day=? AND kind='coupon' AND status='sent'",
                        (key,)).fetchone():
            return "kupon: bugün zaten gönderildi"
    built = daily_coupons(target, build=build)
    diagnostics = built.get("banko_diagnostics") or {}
    if diagnostics:
        print("Banko aday elemesi: " + json.dumps(diagnostics, ensure_ascii=False), flush=True)
    try:
        from . import grok_audit
        built = grok_audit.decorate(built, now=now)
    except Exception as exc:  # noqa: BLE001 - Grok/araştırma okunamazsa kupon yine gönderilir
        print(f"Grok kararları uygulanamadı: {type(exc).__name__}", flush=True)
    coupons = []
    for kind in ("banko", "surprise"):
        replacement = built.get(f"{kind}_grok")
        if replacement and replacement.get("picks"):
            # Doğrulanmış erteleme/iptal veya elle veto: dondurulmuş Grok alternatifi gönderilir.
            coupons.append(_compact_coupon({**replacement, "stake_plan": replacement.get("stake_plan")
                                            or stake_plan(replacement)}))
            continue
        published = built[kind]
        if not published["picks"]:
            continue
        # Dondurulmuş tahminin değerlerini ASLA canlı başka bir seçimle değiştirme.
        # Sadece aynı maç/pazar, oran ve yüzde ise açıklama alanlarını tamamla.
        current = built.get("current_" + kind) or {}
        live = {(str(p["id"]), p["market"]): p for p in current.get("picks", [])}
        copy = {**published, "picks": []}
        for p in published["picks"]:
            old = dict(p)
            candidate = live.get((str(p["id"]), p["market"]))
            if (candidate and abs(candidate["odds"] - p["odds"]) < 0.001
                    and abs(candidate["p"] - p["p"]) < 0.0001):
                old.update({k: candidate[k] for k in ("model_probability", "market_probability",
                            "raw_p", "calibration_n", "calibration_applied", "data_n",
                            "probability_source", "value", "news_effect") if k in candidate})
            copy["picks"].append(old)
        coupons.append(_compact_coupon(copy))
    if not coupons:
        return "kupon: uygun kupon yok"
    note = ("Güncel gerçek oranı 2 saatten taze, başlamamış ve bülten MBS kuralına uyan hiçbir maç bulunamadı; "
            "oranlar yenilenince tekrar denenecek.")
    sent = _post_once(key, "coupon", now, json.dumps(coupons, ensure_ascii=False),
                      format_coupon_message(key, coupons, banko_note=note), send)
    return "kupon: gönderildi" if sent else "kupon: başka çalıştırma gönderiyor/gönderdi"


def _match_results(conn, day: str, coupons: list[dict], now: datetime, fetch_results) -> dict[str, dict]:
    """Kupon bacaklarının skorları: önce ESPN (matches); eşleşmesi olmayan bülten bacakları iddaa canlı
    sonuçlarıyla tamamlanır (bkz. coupon_history.iddaa_results). Dönüş {seçim_id: matches satırı}."""
    ids = sorted({str(p["id"]) for c in coupons for p in c["picks"]})
    marks = ",".join("?" for _ in ids)
    results = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT id, completed, home_goals, away_goals, status FROM matches WHERE id IN ({marks})", ids)}
    rows = [{"kickoff": p["kickoff"], "completed": results.get(str(p["id"]), {}).get("completed")}
            for c in coupons for p in c["picks"]]
    scores = iddaa_results(rows, fetch_results, now)
    cache = {}
    for c in coupons:
        for p in c["picks"]:
            pid = str(p["id"])
            if not scores or results.get(pid, {}).get("completed"):
                continue
            event_id = _event_ids(conn, cache, day, c["type"]).get((pid, p["market"])) or pid
            if score := scores.get(str(event_id)):
                results[pid] = {"id": pid, "completed": 1, "home_goals": score[0], "away_goals": score[1],
                                "status": "FT"}
    return results


def post_results(now: datetime, send, fetch_results=None) -> list[str]:
    """Gönderilmiş kuponların sonuçlarını yanıt olarak yazar. fetch_results (gün → {etkinlik: skor}) verilirse
    ESPN'de olmayan bülten bacakları iddaa skoruyla değerlendirilir; aynı gün bir kez çekilir."""
    fetched = {}

    def fetch(day):
        if day not in fetched:
            fetched[day] = fetch_results(day)
        return fetched[day]

    since = (now.astimezone(TZ).date() - timedelta(days=RESULT_LOOKBACK_DAYS)).isoformat()
    with session() as conn:
        pending = conn.execute(
            "SELECT c.day, c.payload, c.message_id FROM telegram_posts c WHERE c.kind='coupon' "
            "AND c.status='sent' AND c.day >= ? AND NOT EXISTS (SELECT 1 FROM telegram_posts r "
            "WHERE r.day=c.day AND r.kind='result') ORDER BY c.day", (since,)).fetchall()
    out = []
    for row in pending:
        coupons = json.loads(row["payload"])
        with session() as conn:
            results = _match_results(conn, row["day"], coupons, now, fetch if fetch_results else None)
        settled = {}
        for c in coupons:
            s = settle_coupon(c, results, now)
            if s is None:
                break
            settled[c["type"]] = s
        if len(settled) < len(coupons):
            out += _post_leg_updates(row["day"], coupons, results, now, send, row["message_id"])
        else:
            sent = _post_once(row["day"], "result", now, None, format_result_message(row["day"], settled),
                              send, reply_to=row["message_id"])
            out.append(f"sonuç {row['day']}: " + ("gönderildi" if sent else "zaten gönderiliyor/gönderildi"))
            continue
        out.append(f"sonuç {row['day']}: maçlar bitmedi")
    return out


def _post_leg_updates(day, coupons, results, now, send, reply_to) -> list[str]:
    """Kupon sonucu henüz kesinleşmemişken biten her bacağı (✅/❌) bir kez yanıt olarak yazar."""
    with session() as conn:
        done = {r["kind"] for r in conn.execute(
            "SELECT kind FROM telegram_posts WHERE day=? AND kind LIKE 'leg:%'", (day,))}
    out = []
    for c in coupons:
        legs = leg_states(c, results, now)
        lost = any(p["state"] == "lost" for p in legs)
        for leg in legs:
            kind = f"leg:{c['type']}:{leg['id']}"
            if leg["state"] not in ("won", "lost") or kind in done:
                continue
            if _post_once(day, kind, now, None, format_leg_message(c["type"], leg, lost), send, reply_to):
                out.append(f"bacak {day}: {leg['home']} - {leg['away']} {leg['state']}")
    return out


def post_weekly_summary(now: datetime, send) -> str:
    """Pazartesi 10:00 TSİ'den sonra geçen haftanın (Pzt–Paz) kupon özetini bir kez gönderir."""
    from .model import coupon_history
    local = now.astimezone(TZ)
    if local.weekday() != WEEKLY_WEEKDAY or local.hour < WEEKLY_HOUR:
        return "haftalık: zamanı değil"
    end = local.date() - timedelta(days=1)
    start = end - timedelta(days=6)
    with session() as conn:
        if conn.execute("SELECT 1 FROM telegram_posts WHERE day=? AND kind='weekly'",
                        (local.date().isoformat(),)).fetchone():
            return "haftalık: zaten gönderildi"
        days = coupon_history.history(conn, days=14)["days"]
    week = [d for d in days if start.isoformat() <= d["date"] <= end.isoformat()]
    picked = {k: [d[k] for d in week if d.get(k)] for k in ("banko", "surprise")}
    summary = {k: coupon_history._summary(v) for k, v in picked.items()}
    summary["total"] = coupon_history._summary(picked["banko"] + picked["surprise"])
    sent = _post_once(local.date().isoformat(), "weekly", now, None, format_weekly_message(start, end, summary), send)
    return "haftalık: gönderildi" if sent else "haftalık: başka çalıştırma gönderiyor/gönderdi"


def post_grok_audits(now: datetime, send) -> list[str]:
    """Panelden kaydedilmiş ama Telegram'a düşmemiş Grok denetimlerini gönderir (sunucuda sır yoksa buradan çıkar)."""
    from . import grok_audit
    return grok_audit.post_pending(now, send)


def saved_and_iddaa_results(day: str) -> dict[str, tuple[int, int]]:
    """Canlı skor akışının kalıcılaştırdığı kesin skorlar (verified_scores) + iddaa sonuç servisi.
    iddaa akışı birkaç gün sonra eski maçları düşürdüğü için kayıtlı skor yedek olarak kullanılır."""
    with session() as conn:
        saved = json.loads(get_meta(conn, "verified_scores:" + day) or "{}")
    scores = {key.removeprefix("iddaa:"): (row["home_goals"], row["away_goals"]) for key, row in saved.items()
              if row.get("state") == "post" and row.get("home_goals") is not None and row.get("away_goals") is not None}
    try:
        scores.update(iddaa.fetch_results(day))
    except Exception:
        if not scores:
            raise
    return scores


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Telegram kupon/sonuç paylaşımı")
    parser.add_argument("--dry-run", action="store_true", help="Mesajı gönderme/kaydetme, ekrana yaz")
    parser.add_argument("--force", action="store_true", help="Sabah penceresini yok say")
    parser.add_argument("--test", action="store_true",
                        help="--day için kuponu '🧪 TEST' başlığıyla gönder, kayıt tutma (gerçek günü bloklamaz)")
    parser.add_argument("--day", help="YYYY-MM-DD (Türkiye saati); verilirse pencere denetlenmez")
    args = parser.parse_args(argv)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    now = datetime.now(timezone.utc)
    if args.dry_run:
        init_db()
        built = build_coupons(date.fromisoformat(args.day) if args.day else now.astimezone(TZ).date())
        coupons = [_compact_coupon(built[k]) for k in ("banko", "surprise") if built[k]["picks"]]
        print(format_coupon_message(built["date"], coupons) if coupons else "Uygun kupon yok.")
        return 0
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID tanımlı değil; Telegram paylaşımı atlandı.")
        return 0

    init_db()
    if args.test:
        if not args.day:
            parser.error("--test için --day gerekli")
        built = build_coupons(date.fromisoformat(args.day))
        coupons = [_compact_coupon(built[k]) for k in ("banko", "surprise") if built[k]["picks"]]
        if not coupons:
            print("Uygun kupon yok.")
            return 0
        send_message(token, chat_id, "🧪 <b>TEST</b>\n" + format_coupon_message(built["date"], coupons))
        print("test mesajı gönderildi (kayıt tutulmadı)")
        return 0

    def send(text, reply_to=None):
        return send_message(token, chat_id, text, reply_to)

    lines, failed = [], False
    steps = [lambda: [post_daily_coupons(now, send, force=args.force,
                                         day=date.fromisoformat(args.day) if args.day else None)],
             lambda: post_results(now, send, fetch_results=saved_and_iddaa_results),
             lambda: post_grok_audits(now, send),
             lambda: [post_weekly_summary(now, send)]]
    for step in steps:  # biri hata verse de diğeri denenir
        try:
            lines += step()
        except TelegramError as exc:
            print(f"HATA: {exc}", file=sys.stderr)
            failed = True
    print("\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
