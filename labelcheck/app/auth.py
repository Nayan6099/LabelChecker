"""Who is calling, and what may they do.

Three ways to authenticate:
1. Browser session: random token in an HttpOnly, Secure, SameSite=Strict cookie. Requests that change
   data must also send the header `X-LabelCheck: 1` (CSRF protection).
2. Session token as `Authorization: Bearer <token>` (scripts, from /api/auth/login).
3. API key for integrations: `X-API-Key: lc_<prefix>_<secret>` (or `Authorization: Bearer lc_...`).
   API keys act with the uploader role, limited by their scopes ("read", "write").
Only SHA-256 hashes of session tokens, reset/invite tokens and API keys are stored.

Passwords: Argon2id. 2FA: TOTP (RFC 6238), secret encrypted with LABELCHECK_SECRET_KEY.
Rate limits and the login lockout are counted in the database, so they hold across all servers.

Roles (each includes the ones below): admin > reviewer > uploader. Platform admins (the SaaS
operator) can also manage companies and plans.
"""
import base64
import hashlib
import hmac
import logging
import re
import secrets

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request

from . import db
from .config import settings

log = logging.getLogger("labelcheck.auth")
COOKIE = "lc_session"
ROLES = {"uploader": 1, "reviewer": 2, "admin": 3}
MIN_PASSWORD = 12
LOGIN_FAILS, LOGIN_WINDOW = 5, 15 * 60
# Argon2id with RFC 9106 recommended production settings:
_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)
_DEV_KEY = "labelcheck-development-only-key-do-not-use-in-production"

COMMON_PASSWORDS = {
    "password1234", "password@123", "123456789012", "admin@123456", "labelcheck123",
    "qwerty123456", "welcome@1234", "letmein12345", "changeme1234", "iloveyou1234",
    "administrator", "password2026", "secret123456", "adminadmin12", "rootroot1234"
}

# paths a user who must set up 2FA or verify email can still use
_2FA_SETUP_PATHS = {"/api/auth/me", "/api/auth/logout", "/api/auth/2fa/setup", "/api/auth/2fa/enable",
                    "/api/auth/accept-terms", "/api/auth/password", "/api/auth/verify-email",
                    "/api/auth/resend-verification"}
_VERIFY_SETUP_PATHS = {"/api/auth/me", "/api/auth/logout", "/api/auth/verify-email",
                       "/api/auth/resend-verification"}


# ---------------- passwords ----------------

def hash_password(pw):
    return _ph.hash(pw)


def check_password(stored, pw):
    try:
        return _ph.verify(stored, pw)
    except (VerificationError, InvalidHashError):
        return False


def password_problem(pw: str, user_context: dict = None) -> str | None:
    if len(pw) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters long."
    if not re.search(r"[A-Z]", pw):
        return "Password must contain at least one uppercase letter (A-Z)."
    if not re.search(r"[a-z]", pw):
        return "Password must contain at least one lowercase letter (a-z)."
    if not re.search(r"\d", pw):
        return "Password must contain at least one number (0-9)."
    if not re.search(r"[^A-Za-z0-9]", pw):
        return "Password must contain at least one special symbol (e.g. !@#$%^&*)."
    COMMON_PATTERNS = ("password", "qwerty", "admin123", "welcome123", "letmein", "12345678")
    if any(pat in pw.lower() for pat in COMMON_PATTERNS) or pw.lower() in COMMON_PASSWORDS:
        return "This password is too common or easily guessable. Please choose a stronger password."
    if user_context:
        pw_lower = pw.lower()
        email = (user_context.get("email") or "").lower()
        if email and "@" in email:
            for ep in re.split(r"[@.]", email):
                if len(ep) >= 3 and ep not in ("com", "org", "net", "edu", "test", "local", "mail") and ep in pw_lower:
                    return "Password must not contain parts of your email address."
        name = (user_context.get("name") or "").lower()
        for part in re.split(r"[\s._-]+", name):
            if len(part) >= 3 and part in pw_lower:
                return "Password must not contain parts of your name."
    return None


def random_password():
    alphabet = "abcdefghjkmnpqrstuvwxyz"
    caps = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "23456789"
    symbols = "!@#$%^&*-_=+"
    pwd = [
        secrets.choice(alphabet),
        secrets.choice(caps),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    all_chars = alphabet + caps + digits + symbols
    pwd += [secrets.choice(all_chars) for _ in range(16)]
    secrets.SystemRandom().shuffle(pwd)
    return "".join(pwd)


# ---------------- tokens ----------------

def hash_token(t):
    return hashlib.sha256(t.encode()).hexdigest()


def new_session(user_id):
    token = secrets.token_urlsafe(32)
    db.create_session(hash_token(token), user_id, settings.session_hours)
    return token


def end_session(token):
    db.delete_session(hash_token(token))


def new_link_token(user_id, kind, hours=None, minutes=None):
    """One-time token for password reset, email verification, or invite. Returns the token."""
    token = secrets.token_urlsafe(32)
    db.create_token(hash_token(token), user_id, kind, hours=hours, minutes=minutes)
    return token


def link_url(kind, token):
    return f"{settings.public_url}/#/{kind}/{token}"


# ---------------- 2FA (TOTP) ----------------

def _fernet():
    raw = settings.secret_key or _DEV_KEY
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


def encrypt(s):
    return _fernet().encrypt(s.encode()).decode()


def decrypt(s):
    try:
        return _fernet().decrypt(s.encode()).decode()
    except InvalidToken:
        log.error("cannot decrypt a 2FA secret: was LABELCHECK_SECRET_KEY changed?")
        return None


def new_totp_secret():
    return pyotp.random_base32()


def totp_uri(secret, email):
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="LabelCheck")


def verify_totp(user, code, secret=None):
    """Check a 6-digit code (±30 s clock drift). A code can be used only once (replay protection)."""
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return False
    secret = secret or (decrypt(user["totp_secret"]) if user.get("totp_secret") else None)
    if not secret or not pyotp.TOTP(secret).verify(code, valid_window=1):
        return False
    ok, _ = db.hit("totp-used", f"{user['id']}:{code}", 1, 120)
    return ok


# ---------------- login lockout (shared through the database) ----------------

ACCOUNT_LOGIN_FAILS, ACCOUNT_LOGIN_WINDOW = 5, 15 * 60
IP_LOGIN_FAILS, IP_LOGIN_WINDOW = 100, 15 * 60
LOGIN_FAILS, LOGIN_WINDOW = 5, 15 * 60


def is_rate_limit_exempt(ip: str) -> bool:
    """Exempt local loopback, CI, and test environments from aggregate IP rate limits."""
    return ip in ("127.0.0.1", "::1", "localhost", "testclient")


def is_account_locked(email: str) -> bool:
    return db.count_hits("login-fail-user", email.lower().strip(), ACCOUNT_LOGIN_WINDOW) >= ACCOUNT_LOGIN_FAILS


def is_ip_locked(ip: str) -> bool:
    if is_rate_limit_exempt(ip):
        return False
    return db.count_hits("login-fail-ip", ip, IP_LOGIN_WINDOW) >= IP_LOGIN_FAILS


def record_login_fail(email: str, ip: str):
    clean_email = email.lower().strip()
    db.hit("login-fail-user", clean_email, ACCOUNT_LOGIN_FAILS, ACCOUNT_LOGIN_WINDOW)
    if not is_rate_limit_exempt(ip):
        db.hit("login-fail-ip", ip, IP_LOGIN_FAILS, IP_LOGIN_WINDOW)


def clear_login_fails(email: str, ip: str):
    clean_email = email.lower().strip()
    db.clear_hits("login-fail-user", clean_email, ACCOUNT_LOGIN_WINDOW)


def locked(email, ip):
    return is_account_locked(email) or is_ip_locked(ip)


def record_fail(email, ip):
    record_login_fail(email, ip)


def clear_fails(email, ip):
    clear_login_fails(email, ip)


def rate_limit(name, subject, limit, window, message="Too many requests. Please slow down."):
    ok, _ = db.hit(name, subject, limit, window)
    if not ok:
        raise HTTPException(429, message, headers={"Retry-After": str(window)})


def client_ip(request: Request):
    if settings.trust_proxy:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


# ---------------- API keys ----------------

API_SCOPES = ("read", "write")


def new_api_key():
    """Returns (full_key, prefix, hash). The full key is shown to the user ONCE."""
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    full = f"lc_{prefix}_{secret}"
    return full, prefix, hash_token(full)


def _api_key_principal(raw, request):
    m = re.fullmatch(r"lc_([0-9a-f]{8})_[A-Za-z0-9_\-]{20,}", raw.strip())
    if not m:
        raise HTTPException(401, "Invalid API key.")
    k = db.api_key_by_prefix(m.group(1))
    if not k or k["revoked_at"] or not hmac.compare_digest(k["key_hash"], hash_token(raw.strip())):
        raise HTTPException(401, "Invalid or revoked API key.")
    creator = db.user_by_id(k["created_by"])
    org = db.get_org(k["org_id"])
    if not creator or not creator["active"] or not org["active"]:
        raise HTTPException(401, "API key belongs to a disabled user or company.")
    rate_limit("api", k["id"], settings.rate_api_per_min, 60, "API rate limit reached for this key.")
    scopes = set(k["scopes"].split())
    need = "read" if request.method in ("GET", "HEAD") else "write"
    if need not in scopes:
        raise HTTPException(403, f"This API key does not have the '{need}' scope.")
    db.touch_api_key(k["id"])
    p = {k2: creator[k2] for k2 in ("id", "org_id", "email", "name")}
    p.update(role="uploader", is_platform_admin=False, via="api_key", api_key_id=k["id"],
             api_key_name=k["name"], scopes=sorted(scopes), totp_enabled=False,
             terms_accepted_at=creator["terms_accepted_at"])
    return p


# ---------------- dependencies ----------------

def _credentials(request: Request):
    if "session_token" in request.query_params:
        raise HTTPException(400, "Authentication tokens in URL query parameters are forbidden.")
    key = request.headers.get("x-api-key")
    if key:
        return key, "api_key"
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        t = h[7:].strip()
        return t, "api_key" if t.startswith("lc_") else "bearer"
    return request.cookies.get(COOKIE), "cookie"


def current_user(request: Request):
    token, kind = _credentials(request)
    if not token:
        raise HTTPException(401, "Please log in.")
    if kind == "api_key":
        p = _api_key_principal(token, request)
        request.state.principal = p
        return p
    user = db.session_user(hash_token(token))
    if not user:
        raise HTTPException(401, "Session expired. Please log in again.")
    if kind == "cookie" and request.method not in ("GET", "HEAD", "OPTIONS") \
            and request.headers.get("x-labelcheck") != "1":
        raise HTTPException(403, "Missing X-LabelCheck header (CSRF protection).")
    for k in ("password_hash", "totp_secret", "expires_at", "org_active"):
        user.pop(k, None)
    user["via"] = kind
    org = db.get_org(user["org_id"])
    if not user.get("email_verified", True) and request.url.path not in _VERIFY_SETUP_PATHS:
        raise HTTPException(403, "Please verify your email address to continue.")
    if org["require_2fa"] and not user["totp_enabled"] and request.url.path not in _2FA_SETUP_PATHS:
        raise HTTPException(403, "Your company requires two-step login. Set it up under Account first.")
    request.state.principal = user
    return user


def session_only(user=Depends(current_user)):
    """Account actions (password, 2FA, ...) need a real login, not an API key."""
    if user["via"] == "api_key":
        raise HTTPException(403, "Not available with an API key.")
    return user


def require(role):
    def dep(user=Depends(current_user)):
        if ROLES[user["role"]] < ROLES[role]:
            raise HTTPException(403, f"This needs the {role} role.")
        return user
    return dep


def platform_admin(user=Depends(session_only)):
    if not user.get("is_platform_admin"):
        raise HTTPException(403, "Platform administrators only.")
    return user
