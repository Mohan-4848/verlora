"""Shop-owner accounts: scrypt password hashes, cookie sessions, and the middleware that scopes each request
to the logged-in user's shop."""
import hashlib
import hmac
import secrets
from contextvars import ContextVar
from datetime import datetime, timedelta
from http.cookies import SimpleCookie

from . import db

COOKIE = "vyapar_session"
SESSION_DAYS = 30
_current_user: ContextVar[dict | None] = ContextVar("current_user", default=None)


# ---------------- passwords ----------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


# ---------------- sessions ----------------

def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    db.run("INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES(?,?,?,?)", (token, user_id, db.now(), expires))
    db.run("UPDATE users SET last_login_at=? WHERE id=?", (db.now(), user_id))
    return token


def end_session(token: str | None):
    if token:
        db.run("DELETE FROM sessions WHERE token=?", (token,))


def user_for_token(token: str | None) -> dict | None:
    if not token:
        return None
    row = db.one("""SELECT u.id, u.shop_id, u.name, u.email, u.phone, u.role, s.expires_at
                    FROM sessions s JOIN users u ON u.id = s.user_id
                    JOIN shops sh ON sh.id = u.shop_id AND sh.active = 1
                    WHERE s.token=?""", (token,))
    if not row or row["expires_at"] < db.now():
        return None
    return row


def current_user() -> dict | None:
    return _current_user.get()


def set_cookie(response, token: str, secure: bool):
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure, path="/")


def clear_cookie(response):
    response.delete_cookie(COOKIE, path="/")


# ---------------- middleware ----------------

class ShopSessionMiddleware:
    """Pure-ASGI middleware: reads the session cookie and selects that user's shop for the whole request.
    (Runs in the request's own task, so the context can't leak between requests.)"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = None
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                cookie = SimpleCookie()
                cookie.load(value.decode("latin-1"))
                if COOKIE in cookie:
                    token = cookie[COOKIE].value
        user = user_for_token(token)
        scope.setdefault("state", {})["session_token"] = token
        u_tok = _current_user.set(user)
        s_tok = db.use_shop(user["shop_id"] if user else None)
        try:
            await self.app(scope, receive, send)
        finally:
            db._current_shop.reset(s_tok)
            _current_user.reset(u_tok)
