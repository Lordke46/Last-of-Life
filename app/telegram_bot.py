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

from .db import init_db, session
from .model.coupons import TZ, build_coupons
from .model.settlement import outcome, selection_state, VOID_AFTER_DAYS
from .model.coupon_history import daily_coupons

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
COUPON_TITLES = {"banko": "💎 BANKO", "surprise": "🎲 SÜRPRİZ"}


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
            "picks": [{**{k: p[k] for k in fields}, **{k: p[k] for k in extras if k in p}}
                      for p in coupon["picks"]]}


def format_coupon_message(day: str, coupons: list[dict], banko_note: str | None = None) -> str:
    lines = [f"🎯 <b>Günlük Kupon</b> · {_day_label(day)}"]
    for c in coupons:
        lines += ["", f"{COUPON_TITLES[c['type']]} · toplam oran <b>{c['total_odds']:.2f}</b>"
                      f" · tutma %{c['total_probability'] * 100:.0f}"]
        for p in sorted(c["picks"], key=lambda x: x["kickoff"]):
            lines.append(f"• {_kickoff_tr(p['kickoff'])} {_esc(p['league_name'])} — "
                         f"{_esc(p['home'])} - {_esc(p['away'])} → <b>{_esc(_label(p['market']))}</b> "
                         f"({p['odds']:.2f} · tahmini %{p['p'] * 100:.0f})")
            if c["type"] == "banko":
                parts = []
                if isinstance(p.get("model_probability"), (int, float)):
                    parts.append(f"model %{p['model_probability']*100:.0f}")
                if isinstance(p.get("market_probability"), (int, float)):
                    parts.append(f"piyasa %{p['market_probability']*100:.0f}")
                if p.get("calibration_applied"):
                    parts.append(f"kalibrasyon {p['calibration_n']} geçmiş seçim")
                if isinstance(p.get("data_n"), (int, float)):
                    parts.append(f"veri ~{p['data_n']:.0f} maç")
                if isinstance(p.get("value"), (int, float)):
                    parts.append(f"tahmini değer %{p['value']*100:+.1f}")
                if parts:
                    lines.append("  ↳ " + " · ".join(parts))
    if banko_note and not any(c["type"] == "banko" for c in coupons):
        lines += ["", "💎 <b>Banko paylaşılmadı:</b> " + _esc(banko_note)]
    lines += ["", "<i>Yüzdeler istatistiksel tahmindir; kalibrasyon geçmiş veriyle sınırlıdır. Garanti değildir.</i>"]
    return "\n".join(lines)


def pick_outcome(market: str, home_goals: int, away_goals: int) -> bool | None:
    """Pazarın tuttuğu mu? Tanınmayan pazar için None."""
    return outcome(market, home_goals, away_goals)


def settle_coupon(coupon: dict, results: dict[str, dict], now: datetime) -> dict | None:
    """Kuponu değerlendirir. Bitmemiş (ve henüz sonuçsuz sayılmayan) maç varsa None döner.
    Dönüş: {"status": won|lost|void, "odds": etkin oran, "picks": [{...pick, state, score}]}."""
    settled, odds = [], 1.0
    for p in coupon["picks"]:
        res = results.get(str(p["id"])) or {}
        state = selection_state(p["market"], p["kickoff"], now, completed=res.get("completed"),
                                home_goals=res.get("home_goals"), away_goals=res.get("away_goals"), status=res.get("status"))
        if state == "pending":
            return None
        score = f"{res['home_goals']}-{res['away_goals']}" if state in ("won", "lost") else None
        if state == "won":
            odds *= p["odds"]
        settled.append({**p, "state": state, "score": score})
    states = {s["state"] for s in settled}
    status = "lost" if "lost" in states else ("void" if states == {"void"} else "won")
    return {"status": status, "odds": round(odds, 2), "picks": settled}


def format_result_message(day: str, settled: dict[str, dict]) -> str:
    lines = [f"📊 <b>Kupon Sonuçları</b> · {_day_label(day)}"]
    verdict = {"won": "✅ KAZANDI", "lost": "❌ KAYBETTİ", "void": "⚪ İPTAL"}
    icons = {"won": "✅", "lost": "❌", "void": "⚪"}
    for kind in ("banko", "surprise"):
        if kind not in settled:
            continue
        s = settled[kind]
        head = f"{COUPON_TITLES[kind]} · {verdict[s['status']]}"
        if s["status"] == "won":
            head += f" · <b>{s['odds']:.2f}</b>"
        lines += ["", head]
        for p in s["picks"]:
            score = p["score"] or "sonuç yok"
            lines.append(f"{icons[p['state']]} {_esc(p['home'])} - {_esc(p['away'])} <b>{score}</b> "
                         f"→ {_esc(_label(p['market']))} ({p['odds']:.2f})")
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
    coupons = []
    for kind in ("banko", "surprise"):
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
    note = "Yeterli kalibre edilmiş, pozitif tahmini değerli 2 maç bulunamadı."
    sent = _post_once(key, "coupon", now, json.dumps(coupons, ensure_ascii=False),
                      format_coupon_message(key, coupons, banko_note=note), send)
    return "kupon: gönderildi" if sent else "kupon: başka çalıştırma gönderiyor/gönderdi"


def post_results(now: datetime, send) -> list[str]:
    since = (now.astimezone(TZ).date() - timedelta(days=RESULT_LOOKBACK_DAYS)).isoformat()
    with session() as conn:
        pending = conn.execute(
            "SELECT c.day, c.payload, c.message_id FROM telegram_posts c WHERE c.kind='coupon' "
            "AND c.status='sent' AND c.day >= ? AND NOT EXISTS (SELECT 1 FROM telegram_posts r "
            "WHERE r.day=c.day AND r.kind='result') ORDER BY c.day", (since,)).fetchall()
    out = []
    for row in pending:
        coupons = json.loads(row["payload"])
        ids = sorted({str(p["id"]) for c in coupons for p in c["picks"]})
        with session() as conn:
            marks = ",".join("?" for _ in ids)
            results = {r["id"]: dict(r) for r in conn.execute(
                f"SELECT id, completed, home_goals, away_goals, status FROM matches WHERE id IN ({marks})", ids)}
        settled = {}
        for c in coupons:
            s = settle_coupon(c, results, now)
            if s is None:
                break
            settled[c["type"]] = s
        else:
            sent = _post_once(row["day"], "result", now, None, format_result_message(row["day"], settled),
                              send, reply_to=row["message_id"])
            out.append(f"sonuç {row['day']}: " + ("gönderildi" if sent else "zaten gönderiliyor/gönderildi"))
            continue
        out.append(f"sonuç {row['day']}: maçlar bitmedi")
    return out


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
             lambda: post_results(now, send)]
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
