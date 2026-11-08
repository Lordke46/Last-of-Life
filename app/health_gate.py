"""GitHub Actions sağlık adımının karar fonksiyonu.

Üretim `/api/health`:
- veritabanı kapalıysa HTTP 503 ve `db: false`
- veri eski ama servis ayaktaysa HTTP 200 ve `ok: false`
  (eski sürüm aynı durumda 503 de dönebilir)

Taşıma hatası, veritabanı hatası ve beklenmeyen yanıt çıkış 1 verir.
Yalnız bilinen tazelik sorunları uyarıdır ve çıkış 0 bırakır.
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# curl --fail HTTP hata kodunda 22 döner. Bu kapı o kodu da gövdeyle yorumlar;
# diğer curl çıkışları (DNS, TLS, zaman aşımı, bağlantı) taşıma hatasıdır.
_HTTP_ERROR_EXIT = 22
_STALE_PROBLEM = re.compile(r"^(?:update_stale|iddaa_stale|source_stale:\S+)$")


@dataclass(frozen=True)
class HealthVerdict:
    level: str  # ok, warning, error
    message: str


def _parse_status(value) -> int | None:
    if isinstance(value, int):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text.isdigit():
        return None
    return int(text)


def _excerpt(body: str, limit: int = 180) -> str:
    text = " ".join(body.split())
    if not text:
        return "<boş gövde>"
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def judge(curl_exit: int, http_status, body: str) -> HealthVerdict:
    """Durum kodu ve gövdeye göre sağlık adımının sonucunu seç."""
    status = _parse_status(http_status)
    shown = status if status is not None else "yok"
    if curl_exit not in (0, _HTTP_ERROR_EXIT):
        return HealthVerdict(
            "error",
            f"::error::Sağlık ucuna ulaşılamadı (curl çıkış {curl_exit}, HTTP {shown})",
        )
    if status not in (200, 503):
        return HealthVerdict(
            "error",
            f"::error::Beklenmeyen sağlık yanıtı (HTTP {shown}): {_excerpt(body)}",
        )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        return HealthVerdict(
            "error",
            f"::error::Beklenmeyen sağlık yanıtı (HTTP {status}): {_excerpt(body)}",
        )
    db = payload.get("db")
    ok = payload.get("ok")
    problems = payload.get("problems")
    if db is False:
        return HealthVerdict(
            "error",
            f"::error::API erişilebilir ama veritabanı sağlıklı değil (HTTP {status})",
        )
    if db is not True or not isinstance(problems, list) or any(not isinstance(item, str) for item in problems):
        return HealthVerdict(
            "error",
            f"::error::Beklenmeyen sağlık yanıtı (HTTP {status}): {_excerpt(body)}",
        )
    if ok is True and status == 200 and problems == []:
        return HealthVerdict("ok", "API ve veritabanı sağlıklı")
    if ok is False and problems and all(_STALE_PROBLEM.fullmatch(item) for item in problems):
        joined = ", ".join(item.replace("\n", " ") for item in problems)
        return HealthVerdict("warning", f"::warning::Veri tazeliği sorunu: {joined}")
    return HealthVerdict(
        "error",
        f"::error::Beklenmeyen sağlık yanıtı (HTTP {status}): {_excerpt(body)}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canlı /api/health yanıtını yorumla")
    parser.add_argument("--curl-exit", type=int, required=True)
    parser.add_argument("--http-status", required=True)
    parser.add_argument("--body", required=True)
    args = parser.parse_args(argv)
    path = Path(args.body)
    body = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    verdict = judge(args.curl_exit, args.http_status, body)
    print(verdict.message)
    return 0 if verdict.level != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
