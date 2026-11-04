"""Login, logout, password reset, invites, 2FA, account settings."""
import io

import segno
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import auth, db, mailer
from ..config import settings
from ..observability import LOGINS
from .common import public_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    totp: str = Field("", max_length=10)


def _session_response(user, token):
    resp = JSONResponse({"token": token, "user": public_user(user)})
    resp.set_cookie(auth.COOKIE, token, httponly=True, secure=settings.cookie_secure, samesite="strict",
                    max_age=settings.session_hours * 3600, path="/")
    return resp


@router.post("/login")
def login(body: LoginIn, request: Request):
    ip = auth.client_ip(request)
    if auth.locked(body.email, ip):
        LOGINS.labels("locked").inc()
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    auth.rate_limit("login-ip", ip, 60, 60 * 15, "Too many login attempts from this network. Try later.")
    u = db.user_by_email(body.email)
    org = db.get_org(u["org_id"]) if u else None
    if not u or not u["active"] or not org["active"] or not auth.check_password(u["password_hash"], body.password):
        auth.record_fail(body.email, ip)
        LOGINS.labels("failed").inc()
        if u:
            db.log_event(u["org_id"], u["id"], "login_failed", "user", u["id"], ip=ip)
        raise HTTPException(401, "Wrong email or password.")
    if u["totp_enabled"]:
        if not body.totp:
            raise HTTPException(401, {"code": "totp_required", "message": "Enter the 6-digit code from your app."})
        if not auth.verify_totp(u, body.totp):
            auth.record_fail(body.email, ip)
            LOGINS.labels("failed_2fa").inc()
            db.log_event(u["org_id"], u["id"], "login_failed_2fa", "user", u["id"], ip=ip)
            raise HTTPException(401, {"code": "totp_invalid", "message": "Wrong or expired code."})
    auth.clear_fails(body.email, ip)
    token = auth.new_session(u["id"])
    db.update_user(u["id"], last_login_at=db.now())
    db.log_event(u["org_id"], u["id"], "login", "user", u["id"], ip=ip)
    LOGINS.labels("ok").inc()
    return _session_response(u, token)


class RegisterIn(BaseModel):
    company_name: str = Field(min_length=2, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(max_length=254)
    password: str = Field(min_length=10, max_length=200)
    plan: str = Field("trial", pattern="^(trial|standard|business)$")


@router.post("/register")
def register(body: RegisterIn, request: Request):
    ip = auth.client_ip(request)
    auth.rate_limit("register-ip", ip, 10, 3600, "Too many registration attempts. Try again later.")
    clean_email = body.email.strip().lower()
    if db.user_by_email(clean_email):
        raise HTTPException(409, "An account with this email already exists. Please log in.")
    problem = auth.password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)
    oid = db.create_org(body.company_name.strip(), body.plan)
    uid = db.create_user(oid, clean_email, body.name.strip(), auth.hash_password(body.password), "admin")
    db.update_user(uid, terms_accepted_at=db.now(), last_login_at=db.now())
    token = auth.new_session(uid)
    u = db.user_by_id(uid)
    db.log_event(oid, uid, "register", "user", uid, ip=ip)
    try:
        mailer.invite(clean_email, body.name.strip(), body.company_name.strip(), "LabelCheck", f"{settings.public_url}/#/")
    except Exception:
        pass
    return _session_response(u, token)


@router.post("/logout")
def logout(request: Request, user=Depends(auth.session_only)):
    token, _ = auth._credentials(request)
    auth.end_session(token)
    db.log_event(user["org_id"], user["id"], "logout", "user", user["id"])
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
        raise HTTPException(400, "Current password is wrong.")
    problem = auth.password_problem(body.new)
    if problem:
        raise HTTPException(400, problem)
    db.update_user(user["id"], password_hash=auth.hash_password(body.new))
    db.delete_user_sessions(user["id"])  # log out everywhere
    db.log_event(user["org_id"], user["id"], "password_changed", "user", user["id"], ip=auth.client_ip(request))
    return {"ok": True}


class PrefsIn(BaseModel):
    notify: bool


@router.post("/preferences")
def preferences(body: PrefsIn, user=Depends(auth.session_only)):
    db.update_user(user["id"], notify=body.notify)
    return {"ok": True}


# ---------------- forgot / reset password ----------------

class ForgotIn(BaseModel):
    email: str = Field(max_length=254)


@router.post("/forgot")
def forgot(body: ForgotIn, request: Request):
    """Always answers the same way, so it cannot be used to find out which e-mails have accounts."""
    ip = auth.client_ip(request)
    auth.rate_limit("forgot-ip", ip, 20, 3600)
    ok, _ = db.hit("forgot-email", body.email.lower().strip(), 3, 3600)
    u = db.user_by_email(body.email)
    if ok and u and u["active"]:
        token = auth.new_link_token(u["id"], "reset", 1)
        mailer.password_reset(u["email"], u["name"], auth.link_url("reset", token))
        db.log_event(u["org_id"], u["id"], "password_reset_requested", "user", u["id"], ip=ip)
    return {"ok": True, "message": "If the e-mail belongs to an account, a reset link has been sent."}


class ResetIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(max_length=200)


def _set_password_with_token(body: ResetIn, kind, request):
    problem = auth.password_problem(body.password)
    if problem:
        raise HTTPException(400, problem)
    uid = db.use_token(auth.hash_token(body.token), kind)
    if not uid:
        raise HTTPException(400, "This link is invalid, already used or expired. Ask for a new one.")
    u = db.user_by_id(uid)
    db.update_user(uid, password_hash=auth.hash_password(body.password), active=True)
    db.delete_user_sessions(uid)
    db.log_event(u["org_id"], uid, f"password_set_by_{kind}", "user", uid, ip=auth.client_ip(request))
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


class CodeIn(BaseModel):
    code: str = Field(max_length=10)
    password: str = Field("", max_length=200)


@router.post("/2fa/enable")
def totp_enable(body: CodeIn, user=Depends(auth.session_only)):
    full = db.user_by_id(user["id"])
    if not full["totp_secret"]:
        raise HTTPException(400, "Start setup first.")
    if not auth.verify_totp(full, body.code):
        raise HTTPException(400, "Wrong code. Check the time on your phone and try again.")
    db.update_user(user["id"], totp_enabled=True)
    db.log_event(user["org_id"], user["id"], "2fa_enabled", "user", user["id"])
    return {"ok": True}


@router.post("/2fa/disable")
def totp_disable(body: CodeIn, user=Depends(auth.session_only)):
    full = db.user_by_id(user["id"])
    if not auth.check_password(full["password_hash"], body.password):
        raise HTTPException(400, "Password is wrong.")
    if full["totp_enabled"] and not auth.verify_totp(full, body.code):
        raise HTTPException(400, "Wrong code.")
    if db.get_org(user["org_id"])["require_2fa"]:
        raise HTTPException(409, "Your company requires two-step login.")
    db.update_user(user["id"], totp_enabled=False, totp_secret=None)
    db.log_event(user["org_id"], user["id"], "2fa_disabled", "user", user["id"])
    return {"ok": True}
