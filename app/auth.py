"""Kimlik doğrulama: kayıt, giriş, oturum çerezi, şifre sıfırlama, roller ve hız sınırı.

Her şey DB tabanlıdır (serverless uyumlu; Postgres ve SQLite). Şifreler argon2id; oturum ve sıfırlama
token'ları yalnızca SHA-256 özetiyle saklanır. Hız sınırı `ops.rate_limited` (rate_limits tablosu).

Yönetici oluşturmak için (ilk kurulum):  python -m app.auth create-admin E-POSTA
Ortam: AUTH_REQUIRED=1 (sayfa/veri girişi zorunlu; varsayılan kapalı), APP_URL, SMTP_HOST/PORT/USER/PASSWORD/FROM,
AI_FEATURE_ENABLED=1.
"""
import getpass
import hashlib
import os
import re
import secrets
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import ops
from .config import STATIC_DIR
from .db import init_db, session

SESSION_COOKIE = "iddia_session"
SESSION_DAYS = 14
RESET_MINUTES = 60
MIN_PASSWORD = 10
MAX_PASSWORD = 128
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Hız sınırları: (adet, saniye). Anahtarlar rate_limits tablosunda; e-postalar özetlenerek saklanır.
LIMIT_LOGIN_IP = (30, 900)
LIMIT_LOGIN_EMAIL = (5, 900)
LIMIT_RESET_IP = (20, 3600)
LIMIT_RESET_EMAIL = (3, 3600)
LIMIT_REGISTER_IP = (10, 3600)
LIMIT_REGISTER_EMAIL = (3, 3600)

# Girişsiz kalacak API yolları (AUTH_REQUIRED=1 iken de): sağlık kontrolü ve kimlik uçları.
PUBLIC_API = ("/api/health", "/api/auth/")

_hasher = PasswordHasher()  # argon2id, kütüphane varsayılanları
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))  # kullanıcı yokken de aynı sürede doğrula


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """Sabit biçim: sözlük sırası = zaman sırası, iki veritabanında da aynı."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _expired(stamp: str) -> bool:
    return stamp <= _iso(_now())


def _digest(token: str) -> str:
    """Token'lar veritabanında düz metin tutulmaz; yalnızca SHA-256 özeti saklanır."""
    return hashlib.sha256(token.encode()).hexdigest()


def auth_required() -> bool:
    return os.environ.get("AUTH_REQUIRED") == "1"


# ---------- hız sınırı ----------

def _limited(request: Request, scope: str, ip_limit: tuple, email: str | None = None, email_limit: tuple | None = None):
    ip = ops.client_ip(request)
    hit = ops.rate_limited(f"auth:{scope}:ip:{ip}", *ip_limit)
    if email is not None and email_limit:
        hit = ops.rate_limited(f"auth:{scope}:email:{_digest(email)[:32]}", *email_limit) or hit
    if hit:
        raise HTTPException(429, "Çok fazla deneme. Biraz sonra tekrar deneyin.", headers={"Retry-After": "900"})


# ---------- şifre ve e-posta ----------

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise HTTPException(400, "Geçerli bir e-posta girin")
    return email


def check_password_policy(password: str):
    if len(password) < MIN_PASSWORD:
        raise HTTPException(400, f"Şifre en az {MIN_PASSWORD} karakter olmalı")
    if len(password) > MAX_PASSWORD:
        raise HTTPException(400, f"Şifre en fazla {MAX_PASSWORD} karakter olabilir")


# ---------- kullanıcılar ----------

def create_user(email: str, password: str, role: str = "user") -> int:
    email = normalize_email(email)
    check_password_policy(password)
    with session() as conn:
        created = conn.execute(
            "INSERT INTO users(email, password_hash, role, created_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(email) DO NOTHING",
            (email, hash_password(password), role, _iso(_now()))).rowcount
        if created != 1:
            raise HTTPException(409, "Bu e-posta ile zaten bir hesap var")
        return conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


def authenticate(email: str, password: str) -> dict | None:
    with session() as conn:
        row = conn.execute("SELECT id, email, role, password_hash FROM users WHERE email=?", (email,)).fetchone()
    ok = verify_password(row["password_hash"] if row else _DUMMY_HASH, password)
    if not row or not ok:
        return None
    return {"id": row["id"], "email": row["email"], "role": row["role"]}


def user_by_id(user_id: int) -> dict:
    with session() as conn:
        row = conn.execute("SELECT id, email, role FROM users WHERE id=?", (user_id,)).fetchone()
    return {"id": row["id"], "email": row["email"], "role": row["role"]}


# ---------- oturumlar ----------

def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with session() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(_now()),))  # süresi dolanları temizle
        conn.execute("INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?, ?, ?)",
                     (_digest(token), user_id, _iso(_now() + timedelta(days=SESSION_DAYS))))
    return token


def end_session(token: str | None):
    if token:
        with session() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_digest(token),))


def set_session_cookie(response: Response, token: str):
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400,
                        httponly=True, secure=True, samesite="strict", path="/")


def clear_session_cookie(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="strict")


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with session() as conn:
        row = conn.execute(
            "SELECT u.id, u.email, u.role, s.expires_at FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash=?", (_digest(token),)).fetchone()
        if not row:
            return None
        if _expired(row["expires_at"]):
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_digest(token),))
            return None
    return {"id": row["id"], "email": row["email"], "role": row["role"]}


# ---------- FastAPI bağımlılıkları ----------

def require_user(user: dict | None = Depends(current_user)) -> dict:
    if not user:
        raise HTTPException(401, "Giriş gerekli")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Yönetici yetkisi gerekli")
    return user


def require_ai_admin(user: dict = Depends(require_admin)) -> dict:
    """Yapay zeka uç noktaları için kapı: yalnız yönetici ve yalnız AI_FEATURE_ENABLED=1 iken."""
    if user.get("role") != "admin":  # doğrudan çağrıda da kontrol şart
        raise HTTPException(403, "Yönetici yetkisi gerekli")
    if os.environ.get("AI_FEATURE_ENABLED") != "1":
        raise HTTPException(503, "Yapay zeka özelliği kapalı")
    return user


# ---------- şifre sıfırlama ----------

def app_url() -> str:
    """Bağlantılar yalnızca APP_URL'den kurulur (Host başlığına güvenilmez)."""
    return os.environ.get("APP_URL", "http://127.0.0.1:8000").rstrip("/")


def request_reset(email: str) -> str | None:
    """Hesap varsa tek kullanımlık bağlantı üretir ve URL'sini döndürür; yoksa None."""
    with session() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not row:
            return None
        token = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM password_resets WHERE user_id=?", (row["id"],))  # eski bağlantılar geçersiz
        conn.execute("INSERT INTO password_resets(token_hash, user_id, expires_at, used) VALUES(?, ?, ?, 0)",
                     (_digest(token), row["id"], _iso(_now() + timedelta(minutes=RESET_MINUTES))))
    return f"{app_url()}/giris?reset={token}"


def send_reset_email(to: str, link: str):
    """SMTP yoksa token veya hesap bilgisi loglanmadan gönderim atlanır."""
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("[auth] SMTP yapılandırılmamış; sıfırlama e-postası gönderilemedi.", flush=True)
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = "İddia Oran şifre sıfırlama"
        msg["From"] = os.environ.get("SMTP_FROM", "noreply@localhost")
        msg["To"] = to
        msg.set_content(f"Şifrenizi sıfırlamak için bağlantı ({RESET_MINUTES} dakika geçerli, tek kullanımlık):\n{link}\n\n"
                        "Bu isteği siz yapmadıysanız bu e-postayı yok sayabilirsiniz.")
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=15) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if os.environ.get("SMTP_USER"):
                smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            smtp.send_message(msg)
    except Exception as exc:  # yanıt hesap varlığını sızdırmasın; hata yalnız günlüğe (bağlantı yazılmaz)
        print(f"[auth] sıfırlama e-postası gönderilemedi ({type(exc).__name__}).", flush=True)


def consume_reset(token: str, password: str) -> bool:
    check_password_policy(password)
    digest = _digest(token)
    with session() as conn:
        row = conn.execute("SELECT user_id, expires_at, used FROM password_resets WHERE token_hash=?",
                           (digest,)).fetchone()
        if not row or row["used"] or _expired(row["expires_at"]):
            return False
        # used=0 koşulu iki eş zamanlı istekte yalnızca birinin geçmesini sağlar
        if conn.execute("UPDATE password_resets SET used=1 WHERE token_hash=? AND used=0", (digest,)).rowcount != 1:
            return False
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), row["user_id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))  # tüm oturumları kapat
    return True


# ---------- HTTP uçları ----------

router = APIRouter()


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=MAX_PASSWORD)


class ForgotBody(BaseModel):
    email: str = Field(max_length=254)


class ResetBody(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(max_length=MAX_PASSWORD)


def _no_store(response: Response):
    response.headers["Cache-Control"] = "private, no-store"


@router.post("/api/auth/register")
def register(body: Credentials, request: Request, response: Response):
    email = normalize_email(body.email)
    _limited(request, "register", LIMIT_REGISTER_IP, email, LIMIT_REGISTER_EMAIL)
    user_id = create_user(email, body.password)
    set_session_cookie(response, start_session(user_id))
    _no_store(response)
    return {"user": user_by_id(user_id)}


@router.post("/api/auth/login")
def login(body: Credentials, request: Request, response: Response):
    email = body.email.strip().lower()
    _limited(request, "login", LIMIT_LOGIN_IP, email, LIMIT_LOGIN_EMAIL)
    user = authenticate(email, body.password)
    if not user:
        raise HTTPException(401, "E-posta veya şifre hatalı")
    set_session_cookie(response, start_session(user["id"]))
    _no_store(response)
    return {"user": user}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    end_session(request.cookies.get(SESSION_COOKIE))
    clear_session_cookie(response)
    _no_store(response)
    return {"ok": True}


@router.get("/api/auth/me")
def me(response: Response, user: dict | None = Depends(current_user)):
    _no_store(response)
    return {"user": user}


@router.post("/api/auth/forgot")
def forgot(body: ForgotBody, request: Request, response: Response, background: BackgroundTasks):
    email = body.email.strip().lower()
    _limited(request, "reset", LIMIT_RESET_IP, email, LIMIT_RESET_EMAIL)
    link = request_reset(email)  # hesap yoksa None; yanıt yine de aynı
    if link:
        background.add_task(send_reset_email, email, link)  # gönderim süresi yanıttan ayrı (zamanlama sızıntısı yok)
    _no_store(response)
    return {"message": "E-posta kayıtlıysa şifre sıfırlama bağlantısı gönderildi."}


@router.post("/api/auth/reset")
def reset(body: ResetBody, request: Request, response: Response):
    _limited(request, "resetuse", LIMIT_RESET_IP)
    if not consume_reset(body.token, body.password):
        raise HTTPException(400, "Bağlantı geçersiz veya süresi dolmuş")
    _no_store(response)
    return {"ok": True}


@router.get("/giris", include_in_schema=False)
def login_page():
    # Referrer göndermez: sıfırlama token'ı (?reset=) üçüncü taraflara sızmasın.
    return FileResponse(STATIC_DIR / "auth.html", headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})


def install(app):
    """AUTH_REQUIRED=1 iken veri API'leri ve ana sayfa girişe bağlanır. /mac/*, sitemap, robots, /api/health,
    /api/auth/*, /giris ve statik dosyalar hiçbir zaman kapatılmaz (GitHub Actions cron'u HTTP kullanmaz)."""
    app.include_router(router)

    @app.middleware("http")
    async def _gate(request: Request, call_next):
        path = request.url.path
        gated = auth_required() and (path == "/" or (path.startswith("/api/") and not path.startswith(PUBLIC_API)))
        if gated and not await run_in_threadpool(current_user, request):
            if path == "/":
                return RedirectResponse("/giris", status_code=302, headers={"Cache-Control": "private, no-store"})
            return JSONResponse({"detail": "Giriş gerekli"}, status_code=401, headers={"Cache-Control": "private, no-store"})
        response = await call_next(request)
        if gated:  # CDN, girişli kullanıcıya özel yanıtı herkese sunmasın
            response.headers["Cache-Control"] = "private, no-store"
        return response


# ---------- komut satırı: ilk yönetici ----------

def _create_admin(email: str) -> str:
    email = normalize_email(email)
    with session() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            conn.execute("UPDATE users SET role='admin' WHERE id=?", (row["id"],))
            return f"{email} yönetici yapıldı"
    password = getpass.getpass("Şifre: ")
    if password != getpass.getpass("Şifre (tekrar): "):
        raise SystemExit("Şifreler eşleşmiyor")
    create_user(email, password, role="admin")
    return f"{email} yönetici hesabı oluşturuldu"


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "create-admin":
        raise SystemExit("Kullanım: python -m app.auth create-admin E-POSTA")
    init_db()
    try:
        print(_create_admin(sys.argv[2]))
    except HTTPException as exc:
        raise SystemExit(exc.detail)
