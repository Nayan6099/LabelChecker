"""Login, logout, password reset, email verification, invites, 2FA, account settings."""
import io
import logging
import re
from typing import Optional

import segno
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from .. import auth, db, mailer
from ..config import settings
from ..observability import LOGINS
from .common import public_user

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger("labelcheck.auth")

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
_DUMMY_HASH = auth.hash_password("DummySecureConstantTimeVerification#2026")


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    totp: str = Field("", max_length=10)


def _session_response(user, token):
    resp = JSONResponse({"token": token, "user": public_user(user)})
    resp.set_cookie(auth.COOKIE, token, httponly=True, secure=settings.cookie_secure, samesite="lax",
                    max_age=settings.session_hours * 3600, path="/")
    return resp


@router.post("/login")
def login(body: LoginIn, request: Request):
    ip = auth.client_ip(request)
    clean_email = body.email.strip().lower()

    # 1. Dual-Key Brute-Force Protection (counts only failed attempts)
    if auth.is_account_locked(clean_email):
        LOGINS.labels("locked").inc()
        log.warning("Login locked out: too many failed attempts for account", extra={"ip": ip, "email": clean_email})
        raise HTTPException(429, "Too many failed attempts on this account. Try again in 15 minutes.")
    if not auth.is_rate_limit_exempt(ip) and auth.is_ip_locked(ip):
        LOGINS.labels("locked").inc()
        log.warning("Login locked out: too many aggregate failed attempts for IP", extra={"ip": ip})
        raise HTTPException(429, "Too many failed login attempts from this network. Try later.")

    # 2. Look up user
    u = db.user_by_email(clean_email)

    # 3. Constant-time password check (prevents account enumeration via timing differences)
    stored_hash = u["password_hash"] if u else _DUMMY_HASH
    valid_password = auth.check_password(stored_hash, body.password)

    org = db.get_org(u["org_id"]) if u else None
    if not u or not u["active"] or not org or not org["active"] or not valid_password:
        auth.record_login_fail(clean_email, ip)
        LOGINS.labels("failed").inc()
        log.warning("Login failed: invalid credentials", extra={"ip": ip, "reason": "invalid_credentials"})
        if u:
            db.log_event(u["org_id"], u["id"], "login_failed", "user", u["id"], ip=ip)
        raise HTTPException(401, "Invalid credentials.")

    # 4. Check email verification requirement
    if not u.get("email_verified", True):
        log.warning("Login attempt for unverified email address", extra={"ip": ip, "user_id": u["id"]})
        raise HTTPException(403, "Please verify your email address to continue. Check your inbox.")

    # 5. Two-factor authentication (TOTP)
    if u["totp_enabled"]:
        if not body.totp:
            raise HTTPException(401, {"code": "totp_required", "message": "Enter the 6-digit code from your app."})
        if not auth.verify_totp(u, body.totp):
            auth.record_login_fail(clean_email, ip)
            LOGINS.labels("failed_2fa").inc()
            log.warning("Login 2FA verification failed", extra={"ip": ip, "user_id": u["id"]})
            db.log_event(u["org_id"], u["id"], "login_failed_2fa", "user", u["id"], ip=ip)
            raise HTTPException(401, {"code": "totp_invalid", "message": "Wrong or expired code."})

    # 6. Success: Clear account failure counter. Successful logins NEVER count against rate limiter.
    auth.clear_login_fails(clean_email, ip)
    token = auth.new_session(u["id"])
    db.update_user(u["id"], last_login_at=db.now())
    db.log_event(u["org_id"], u["id"], "login", "user", u["id"], ip=ip)
    LOGINS.labels("ok").inc()
    log.info("User logged in successfully", extra={"ip": ip, "user_id": u["id"], "org_id": u["org_id"]})
    return _session_response(u, token)


class RegisterIn(BaseModel):
    model_config = {"extra": "ignore"}
    company_name: str = Field(min_length=2, max_length=100)
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(max_length=254, pattern=EMAIL_RE)
    password: str = Field(min_length=12, max_length=200)
    website: Optional[str] = Field(None, max_length=100)  # Bot honeypot

    @field_validator("name", "company_name")
    @classmethod
    def sanitize_text(cls, v: str) -> str:
        cleaned = v.strip()
        if re.search(r"[<>]", cleaned):
            raise ValueError("HTML tags or angle brackets (<, >) are not allowed.")
        return cleaned


@router.post("/register")
def register(body: RegisterIn, request: Request):
    ip = auth.client_ip(request)
    auth.rate_limit("register-ip", ip, 10, 3600, "Too many registration attempts. Try again later.")

    # Bot honeypot detection
    if body.website:
        log.warning("Bot registration honeypot triggered", extra={"ip": ip})
        raise HTTPException(400, "Registration rejected.")

    clean_email = body.email.strip().lower()
    if db.user_by_email(clean_email):
        raise HTTPException(409, "An account with this email already exists. Please log in.")

    # Strict password validation with contextual user data
    problem = auth.password_problem(body.password, {"email": clean_email, "name": body.name})
    if problem:
        raise HTTPException(400, problem)

    # Force all self-registrations strictly to the free tier ('trial').
    # Strip any user-supplied plan/tier/role/quota to prevent privilege escalation.
    oid = db.create_org(body.company_name.strip(), plan="trial")
    # Require email verification on self-registration
    uid = db.create_user(oid, clean_email, body.name.strip(), auth.hash_password(body.password),
                         "admin", email_verified=False)
    db.update_user(uid, terms_accepted_at=db.now(), last_login_at=db.now())
    token = auth.new_session(uid)
    u = db.user_by_id(uid)
    db.log_event(oid, uid, "register", "user", uid, ip=ip)
    log.info("New user registered", extra={"ip": ip, "user_id": uid, "org_id": oid})

    # Generate single-use email verification token (valid 24h)
    vtok = auth.new_link_token(uid, "verify", hours=24)
    vurl = f"{settings.public_url}/#/verify/{vtok}"

    if settings.production and not settings.smtp_host:
        log.error("SMTP not configured in production. Cannot send verification email.")
        raise HTTPException(500, "Email delivery service unavailable. Registration could not be completed.")

    try:
        mailer.verify_email(clean_email, body.name.strip(), vurl)
    except Exception as e:
        log.warning("Could not send verification email: %s", e)

    if not settings.smtp_host:
        # In Development/Staging: Log verification links to backend server stdout/console exclusively.
        print(f"[AUTH DEV] Verification link for {clean_email}: {vurl}")
        log.info("Email verification link (dev mode): %s", vurl)

    # Strictly return non-sensitive metadata only. Never leak verification tokens or activation links in response.
    return {
        "success": True,
        "message": "Account created. Please check your email to verify your address."
    }


class VerifyIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


@router.post("/verify-email")
def verify_email(body: VerifyIn, request: Request):
    ip = auth.client_ip(request)
    auth.rate_limit("verify-ip", ip, 30, 3600)
    uid = db.use_token(auth.hash_token(body.token), "verify")
    if not uid:
        raise HTTPException(400, "Verification link is invalid, already used or expired. Please request a new one.")
    u = db.user_by_id(uid)
    db.update_user(uid, email_verified=True)
    db.log_event(u["org_id"], uid, "email_verified", "user", uid, ip=ip)
    log.info("Email address verified successfully", extra={"ip": ip, "user_id": uid, "org_id": u["org_id"]})
    return {"ok": True, "message": "Email verified successfully! You can now access your account."}


class ResendVerifyIn(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL_RE)


@router.post("/resend-verification")
def resend_verification(body: ResendVerifyIn, request: Request):
    ip = auth.client_ip(request)
    auth.rate_limit("resend-verify", ip, 5, 3600)
    clean_email = body.email.strip().lower()
    u = db.user_by_email(clean_email)
    if u and not u.get("email_verified", True):
        vtok = auth.new_link_token(u["id"], "verify", hours=24)
        vurl = f"{settings.public_url}/#/verify/{vtok}"
        try:
            mailer.verify_email(clean_email, u["name"], vurl)
            log.info("Verification email resent", extra={"ip": ip, "user_id": u["id"]})
        except Exception as e:
            log.warning("Could not resend verification email: %s", e)
    return {"ok": True, "message": "If an unverified account exists for this email, a verification link has been sent."}


@router.post("/logout")
def logout(request: Request, user=Depends(auth.session_only)):
    token, _ = auth._credentials(request)
    auth.end_session(token)
    db.log_event(user["org_id"], user["id"], "logout", "user", user["id"])
    log.info("User logged out and session revoked", extra={"user_id": user["id"], "org_id": user["org_id"]})
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@router.get("/me")
def me(user=Depends(auth.current_user)):
    return public_user(user)


@router.post("/accept-terms")
def accept_terms(user=Depends(auth.session_only)):
    db.update_user(user["id"], terms_accepted_at=db.now())
    db.log_event(user["org_id"], user["id"], "accept_terms", "user", user["id"])
    return {"ok": True}


class PasswordIn(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(max_length=200)


@router.post("/password")
def change_password(body: PasswordIn, request: Request, user=Depends(auth.session_only)):
    full = db.user_by_id(user["id"])
    if not auth.check_password(full["password_hash"], body.current):
        log.warning("Password change rejected: wrong current password", extra={"user_id": user["id"]})
        raise HTTPException(400, "Current password is wrong.")

    problem = auth.password_problem(body.new, {"email": full["email"], "name": full["name"]})
    if problem:
        raise HTTPException(400, problem)

    db.update_user(user["id"], password_hash=auth.hash_password(body.new))
    db.delete_user_sessions(user["id"])  # Invalidate all existing sessions immediately
    db.log_event(user["org_id"], user["id"], "password_changed", "user", user["id"], ip=auth.client_ip(request))
    log.info("Password changed and all active sessions revoked", extra={"user_id": user["id"]})
    return {"ok": True}


class PrefsIn(BaseModel):
    notify: bool


@router.post("/preferences")
def preferences(body: PrefsIn, user=Depends(auth.session_only)):
    db.update_user(user["id"], notify=body.notify)
    return {"ok": True}


# ---------------- forgot / reset password ----------------

class ForgotIn(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL_RE)


@router.post("/forgot")
def forgot(body: ForgotIn, request: Request):
    """Always answers identically to prevent account/email enumeration."""
    ip = auth.client_ip(request)
    auth.rate_limit("forgot-ip", ip, 20, 3600)
    clean_email = body.email.lower().strip()
    ok, _ = db.hit("forgot-email", clean_email, 3, 3600)
    u = db.user_by_email(clean_email)
    if ok and u and u["active"]:
        # Cryptographically secure, single-use reset token with TTL <= 15 minutes
        token = auth.new_link_token(u["id"], "reset", minutes=15)
        mailer.password_reset(u["email"], u["name"], auth.link_url("reset", token))
        db.log_event(u["org_id"], u["id"], "password_reset_requested", "user", u["id"], ip=ip)
        log.info("Password reset requested", extra={"ip": ip, "user_id": u["id"]})
    return {"ok": True, "message": "If the e-mail belongs to an account, a reset link has been sent."}


class ResetIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    password: str = Field(max_length=200)


def _set_password_with_token(body: ResetIn, kind, request):
    uid = db.peek_token(auth.hash_token(body.token), kind)
    if not uid:
        raise HTTPException(400, "This link is invalid, already used or expired. Ask for a new one.")
    u = db.user_by_id(uid)

    problem = auth.password_problem(body.password, {"email": u["email"], "name": u["name"]})
    if problem:
        raise HTTPException(400, problem)

    # Atomically mark token as used
    used_uid = db.use_token(auth.hash_token(body.token), kind)
    if not used_uid:
        raise HTTPException(400, "This link is invalid, already used or expired. Ask for a new one.")

    # Update password, activate account, and verify email
    db.update_user(uid, password_hash=auth.hash_password(body.password), active=True, email_verified=True)
    db.delete_user_sessions(uid)  # Invalidate all active sessions
    db.log_event(u["org_id"], uid, f"password_set_by_{kind}", "user", uid, ip=auth.client_ip(request))
    log.info(f"Password set via {kind} and existing sessions revoked", extra={"user_id": uid})
    return u


@router.post("/reset")
def reset(body: ResetIn, request: Request):
    auth.rate_limit("reset-ip", auth.client_ip(request), 30, 3600)
    _set_password_with_token(body, "reset", request)
    return {"ok": True}


@router.get("/invite/{token}")
def invite_info(token: str, request: Request):
    auth.rate_limit("invite-ip", auth.client_ip(request), 60, 3600)
    uid = db.peek_token(auth.hash_token(token), "invite")
    if not uid:
        raise HTTPException(404, "This invitation is invalid, already used or expired.")
    u = db.user_by_id(uid)
    return {"email": u["email"], "name": u["name"], "org_name": db.org_name(u["org_id"])}


@router.post("/accept-invite")
def accept_invite(body: ResetIn, request: Request):
    auth.rate_limit("invite-ip", auth.client_ip(request), 60, 3600)
    u = _set_password_with_token(body, "invite", request)
    u = db.user_by_id(u["id"])
    return _session_response(u, auth.new_session(u["id"]))


# ---------------- 2FA ----------------

@router.post("/2fa/setup")
def totp_setup(user=Depends(auth.session_only)):
    """Start setup: a new secret is stored (not yet active) and returned as a QR code."""
    if user["totp_enabled"]:
        raise HTTPException(409, "Two-step login is already on. Turn it off first to set up a new device.")
    secret = auth.new_totp_secret()
    db.update_user(user["id"], totp_secret=auth.encrypt(secret))
    uri = auth.totp_uri(secret, user["email"])
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="svg", border=4, dark="#000", light="#fff", omitsize=True, xmldecl=False)
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": buf.getvalue().decode()}


class TotpCodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


@router.post("/2fa/enable")
def totp_enable(body: TotpCodeIn, request: Request, user=Depends(auth.session_only)):
    full = db.user_by_id(user["id"])
    if not full.get("totp_secret"):
        raise HTTPException(400, "Start setup first.")
    if not auth.verify_totp(full, body.code, auth.decrypt(full["totp_secret"])):
        raise HTTPException(400, "Wrong or expired code. Enter the 6-digit number currently on your phone.")
    db.update_user(user["id"], totp_enabled=True)
    db.log_event(user["org_id"], user["id"], "2fa_enabled", "user", user["id"], ip=auth.client_ip(request))
    log.info("2FA enabled for user", extra={"user_id": user["id"]})
    return {"ok": True}


class Disable2faIn(BaseModel):
    password: str = Field(max_length=200)
    code: str = Field(min_length=6, max_length=10, description="Current valid 6-digit TOTP code")


@router.post("/2fa/disable")
def totp_disable(body: Disable2faIn, request: Request, user=Depends(auth.session_only)):
    full = db.user_by_id(user["id"])
    if not auth.check_password(full["password_hash"], body.password):
        raise HTTPException(401, "Password is wrong.")
    if not full.get("totp_enabled") or not full.get("totp_secret"):
        raise HTTPException(400, "Two-factor authentication is not currently enabled.")
    decrypted_secret = auth.decrypt(full["totp_secret"])
    if not auth.verify_totp(full, body.code, decrypted_secret):
        raise HTTPException(403, "Invalid or expired two-factor authentication code.")
    org = db.get_org(user["org_id"])
    if org["require_2fa"]:
        raise HTTPException(409, "Your company requires two-step login. You cannot turn it off.")
    db.update_user(user["id"], totp_enabled=False, totp_secret=None)
    db.log_event(user["org_id"], user["id"], "2fa_disabled", "user", user["id"], ip=auth.client_ip(request))
    log.info("2FA disabled for user", extra={"user_id": user["id"]})
    return {"ok": True}
