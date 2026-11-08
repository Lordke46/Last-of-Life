"""Sağlık yanıtı hızlı güncellemeyi tetiklemeli mi?

GitHub Actions `health.yml` bunu çağırır. Ağ yalnız `--dispatch` ile vardır.
Çıkış 0: tetik gerekmedi, tetiklendi veya iş zaten kuyrukta.
Çıkış 1: GitHub API hatası.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Yalnız oran/güncelleme tazeliği hızlı işi düzeltir. Tek lig hatası veya
# haber bayatlığı haber içermeyen hızlı işe yeni bir tur açtırmaz.
REFRESH_PROBLEMS = frozenset({"update_stale", "iddaa_stale"})
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")
BLOCKING = ("quick.yml", "update.yml")


def should_refresh(problems) -> bool:
    if not isinstance(problems, list):
        return False
    return any(item in REFRESH_PROBLEMS for item in problems)


def _request(url: str, token: str, data: bytes | None = None, method: str | None = None):
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "health-dispatch",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, resp.read()


def dispatch_quick(repo: str, token: str, opener=_request) -> str:
    """Kuyrukta hızlı veya tam güncelleme varsa yeni iş açma."""
    for workflow in BLOCKING:
        for status in ACTIVE:
            _status, raw = opener(
                f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs"
                f"?status={status}&per_page=1",
                token,
            )
            runs = json.loads(raw.decode() or "{}").get("workflow_runs") or []
            if runs:
                return f"skipped:{workflow}:{status}"
    status, _raw = opener(
        f"https://api.github.com/repos/{repo}/actions/workflows/quick.yml/dispatches",
        token, json.dumps({"ref": "main"}).encode(), "POST",
    )
    if status not in (200, 204):
        raise RuntimeError(f"GitHub tetik HTTP {status}")
    return "dispatched"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bayat sağlık yanıtında hızlı güncelleme iste")
    parser.add_argument("--body", required=True)
    parser.add_argument("--dispatch", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.body)
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except json.JSONDecodeError:
        payload = None
    problems = payload.get("problems") if isinstance(payload, dict) else None
    if not should_refresh(problems):
        print("Tazelik eşiği aşılmadı; güncelleme tetiklenmedi")
        return 0
    if not args.dispatch:
        print("Hızlı güncelleme tetiklenecek")
        return 0
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not repo or not token:
        print("::error::GITHUB_REPOSITORY veya GH_TOKEN yok", file=sys.stderr)
        return 1
    try:
        result = dispatch_quick(repo, token)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"::error::Hızlı güncelleme tetiklenemedi ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
