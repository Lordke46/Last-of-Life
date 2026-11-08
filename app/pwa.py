"""PWA: web app manifest ve service worker. Vercel'de tüm yollar FastAPI'ye gittiği için ikisi de burada
doğru Content-Type / kapsam başlıklarıyla sunulur (sw.js kökten, kapsam "/")."""
import hashlib
import json
import os

from fastapi import APIRouter, Response

from .config import BASE_DIR, STATIC_DIR

router = APIRouter()

THEME_COLOR = "#1F3D2B"
SW_TEMPLATE = BASE_DIR / "app" / "sw.js"
NO_CACHE = {"Cache-Control": "no-cache"}  # her zaman yeniden doğrulanır (güncelleme algılama için şart)

MANIFEST = {
    "id": "/",
    "name": "İddia Oran",
    "short_name": "İddia Oran",
    "description": "Maç sonucu olasılıkları, önerilen oranlar ve günlük kupon önerileri.",
    "lang": "tr",
    "dir": "ltr",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": THEME_COLOR,
    "theme_color": THEME_COLOR,
    "categories": ["sports"],
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
        {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}


def _static_files() -> list:
    # index.html "/" olarak önbelleğe alınır; sw.js/manifest ayrı yollardan sunulur
    return sorted(p for p in STATIC_DIR.rglob("*")
                  if p.is_file() and not p.name.startswith(".") and p.relative_to(STATIC_DIR).as_posix() != "index.html")


def precache_urls() -> list[str]:
    shell = [] if os.environ.get("AUTH_REQUIRED") == "1" else ["/"]
    return shell + ["/manifest.webmanifest"] + ["/static/" + p.relative_to(STATIC_DIR).as_posix() for p in _static_files()]


def cache_version() -> str:
    """Statik dosyalar, manifest veya sw.js değişince değişir → yeni service worker + yeni önbellek."""
    h = hashlib.sha1(SW_TEMPLATE.read_bytes())
    h.update(json.dumps(MANIFEST, sort_keys=True).encode())
    h.update(str(os.environ.get("AUTH_REQUIRED") == "1").encode())
    for path in [STATIC_DIR / "index.html", *_static_files()]:
        h.update(path.relative_to(STATIC_DIR).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


def render_sw() -> str:
    return (SW_TEMPLATE.read_text(encoding="utf-8")
            .replace("__VERSION__", cache_version())
            .replace("__AUTH_REQUIRED__", json.dumps(os.environ.get("AUTH_REQUIRED") == "1"))
            .replace("__PRECACHE__", json.dumps(precache_urls())))


@router.api_route("/manifest.webmanifest", methods=["GET", "HEAD"], include_in_schema=False)
def manifest():
    return Response(json.dumps(MANIFEST, ensure_ascii=False), media_type="application/manifest+json", headers=NO_CACHE)


@router.api_route("/sw.js", methods=["GET", "HEAD"], include_in_schema=False)
def service_worker():
    return Response(render_sw(), media_type="text/javascript", headers={**NO_CACHE, "Service-Worker-Allowed": "/"})
