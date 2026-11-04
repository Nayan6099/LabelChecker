"""Company administration: users, company settings, API keys, audit log, data export."""
import json
import os
import tempfile
import zipfile
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .. import auth, db, mailer
from ..config import settings
from ..storage import store
from .common import audit, check_user_limit, paginate_response

router = APIRouter(prefix="/api", tags=["company admin"])
EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


# ---------------- users ----------------

class UserIn(BaseModel):
    email: str = Field(max_length=254, pattern=EMAIL)
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern="^(admin|reviewer|uploader)$")
    password: Optional[str] = Field(None, max_length=200)   # empty = send an invite e-mail


class UserPatch(BaseModel):
    role: Optional[str] = Field(None, pattern="^(admin|reviewer|uploader)$")
    active: Optional[bool] = None
    password: Optional[str] = Field(None, max_length=200)
    reset_2fa: Optional[bool] = None


@router.get("/users")
def users_list(page: Optional[int] = None, limit: int = 10, paginated: bool = False,
               user=Depends(auth.require("admin"))):
    if paginated or page is not None:
        p = max(1, page or 1)
        lim = max(1, min(limit, 100))
        offset = (p - 1) * lim
        items = db.list_users(user["org_id"], limit=lim, offset=offset)
        total = db.count_org_users(user["org_id"])
        return paginate_response(items, total, p, lim)
    return db.list_users(user["org_id"])


def invite_user(org_id, uid, inviter_name):
    token = auth.new_link_token(uid, "invite", 72)
    u = db.user_by_id(uid)
    url = auth.link_url("invite", token)
    mailer.invite(u["email"], u["name"], db.org_name(org_id), inviter_name, url)
    return url


@router.post("/users")
def users_create(body: UserIn, request: Request, user=Depends(auth.require("admin"))):
    if db.user_by_email(body.email):
        raise HTTPException(409, "A user with this email already exists.")
    check_user_limit(user["org_id"])
    if body.password:
        problem = auth.password_problem(body.password)
        if problem:
            raise HTTPException(400, problem)
    pw = body.password or auth.random_password()
    uid = db.create_user(user["org_id"], body.email, body.name, auth.hash_password(pw), body.role)
    out = {"id": uid}
    if not body.password:
        out["invite_url"] = invite_user(user["org_id"], uid, user["name"])
    audit(request, user, "user_created", "user", uid,
          {"email": body.email, "role": body.role, "invited": not body.password})
    return out


@router.post("/users/{uid}/invite")
def users_reinvite(uid: int, request: Request, user=Depends(auth.require("admin"))):
    target = db.user_by_id(uid)
    if not target or target["org_id"] != user["org_id"]:
        raise HTTPException(404, "User not found.")
    url = invite_user(user["org_id"], uid, user["name"])
    audit(request, user, "user_reinvited", "user", uid)
    return {"invite_url": url}


@router.patch("/users/{uid}")
def users_update(uid: int, body: UserPatch, request: Request, user=Depends(auth.require("admin"))):
    target = db.user_by_id(uid)
    if not target or target["org_id"] != user["org_id"]:
        raise HTTPException(404, "User not found.")
    if uid == user["id"] and (body.active is False or (body.role and body.role != "admin")):
        raise HTTPException(400, "You cannot remove your own admin access.")
    vals, detail = {}, {}
    if body.role:
        vals["role"] = detail["role"] = body.role
    if body.active is not None:
        if body.active and not target["active"]:
            check_user_limit(user["org_id"])
        vals["active"] = detail["active"] = body.active
    if body.password:
        problem = auth.password_problem(body.password)
        if problem:
            raise HTTPException(400, problem)
        vals["password_hash"] = auth.hash_password(body.password)
        detail["password"] = "reset"  # noqa: S105 (audit note, not a password)
    if body.reset_2fa:
        vals.update(totp_enabled=False, totp_secret=None)
        detail["2fa"] = "reset"
    if vals:
        db.update_user(uid, **vals)
        if body.active is False or body.password or body.reset_2fa:
            db.delete_user_sessions(uid)
        audit(request, user, "user_updated", "user", uid, detail)
    return {"ok": True}


@router.delete("/users/{uid}")
def users_delete(uid: int, request: Request, user=Depends(auth.require("admin"))):
    """Erase a person's personal data (name, e-mail) but keep their actions in the audit trail,
    which then shows 'Deleted user'. Their scans stay with the company."""
    target = db.user_by_id(uid)
    if not target or target["org_id"] != user["org_id"]:
        raise HTTPException(404, "User not found.")
    if uid == user["id"]:
        raise HTTPException(400, "You cannot delete yourself.")
    db.update_user(uid, name="Deleted user", email=f"deleted-{uid}@deleted.invalid", active=False,
                   password_hash=auth.hash_password(auth.random_password()), totp_enabled=False,
                   totp_secret=None, notify=False)
    db.delete_user_sessions(uid)
    audit(request, user, "user_deleted", "user", uid)
    return {"ok": True}


# ---------------- company settings + usage ----------------

class OrgPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    retention_days: Optional[int] = Field(None, ge=0, le=3650)   # 0 = keep forever
    require_2fa: Optional[bool] = None


@router.get("/org")
def org_info(user=Depends(auth.require("uploader"))):
    org = db.get_org(user["org_id"])
    return {"id": org["id"], "name": org["name"], "retention_days": org["retention_days"],
            "require_2fa": org["require_2fa"], **db.org_limits(org),
            "users": db.count_active_users(org["id"]), "scans_this_month": db.usage_this_month(org["id"]),
            "usage_history": db.usage_history(org["id"])}


@router.patch("/org")
def org_update(body: OrgPatch, request: Request, user=Depends(auth.require("admin"))):
    vals = {}
    if body.name:
        vals["name"] = body.name.strip()
    if body.retention_days is not None:
        vals["retention_days"] = body.retention_days or None
    if body.require_2fa is not None:
        if body.require_2fa and not db.user_by_id(user["id"])["totp_enabled"]:
            raise HTTPException(409, "Turn on two-step login for your own account first.")
        vals["require_2fa"] = body.require_2fa
    if vals:
        db.update_org(user["org_id"], **vals)
        audit(request, user, "org_updated", "org", user["org_id"], vals)
    return org_info(user)


@router.post("/org/test-email")
def test_email(request: Request, user=Depends(auth.require("admin"))):
    """Send a test email to the current admin to verify SMTP configuration."""
    if not settings.smtp_host:
        raise HTTPException(400, "SMTP server is not configured. Set LABELCHECK_SMTP_HOST in your environment.")
    try:
        mailer.send_test_email(user["email"], user["name"])
    except Exception as e:
        raise HTTPException(502, f"SMTP delivery failed: {e}")
    audit(request, user, "smtp_test_sent", "org", user["org_id"], {"to": user["email"]})
    return {"ok": True, "message": f"Test email sent to {user['email']} via {settings.smtp_host}:{settings.smtp_port}."}


# ---------------- API keys ----------------

class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(default_factory=lambda: ["read", "write"])


@router.get("/api-keys")
def keys_list(page: Optional[int] = None, limit: int = 10, paginated: bool = False,
              user=Depends(auth.require("admin"))):
    if paginated or page is not None:
        p = max(1, page or 1)
        lim = max(1, min(limit, 100))
        offset = (p - 1) * lim
        items = db.list_api_keys(user["org_id"], limit=lim, offset=offset)
        total = db.count_api_keys(user["org_id"])
        return paginate_response(items, total, p, lim)
    return db.list_api_keys(user["org_id"])


@router.post("/api-keys")
def keys_create(body: KeyIn, request: Request, user=Depends(auth.require("admin"))):
    if user.get("via") == "api_key":
        raise HTTPException(403, "API keys cannot create API keys.")
    scopes = sorted(set(body.scopes))
    if not scopes or any(s not in auth.API_SCOPES for s in scopes):
        raise HTTPException(400, f"Scopes must be from {list(auth.API_SCOPES)}.")
    full, prefix, h = auth.new_api_key()
    kid = db.create_api_key(user["org_id"], body.name.strip(), prefix, h, " ".join(scopes), user["id"])
    audit(request, user, "api_key_created", "api_key", kid, {"name": body.name, "scopes": scopes})
    return {"id": kid, "key": full, "prefix": prefix, "scopes": scopes,
            "note": "Copy this key now. It is shown only once."}


@router.delete("/api-keys/{kid}")
def keys_revoke(kid: int, request: Request, user=Depends(auth.require("admin"))):
    if not db.revoke_api_key(user["org_id"], kid):
        raise HTTPException(404, "API key not found or already revoked.")
    audit(request, user, "api_key_revoked", "api_key", kid)
    return {"ok": True}


# ---------------- audit + export ----------------

@router.get("/audit")
def audit_all(before_id: Optional[int] = None, page: Optional[int] = None, limit: int = 200,
              paginated: bool = False, user=Depends(auth.require("admin"))):
    if paginated or page is not None:
        p = max(1, page or 1)
        lim = 10 if (paginated and limit == 200 and page is None) else max(1, min(limit, 1000))
        offset = (p - 1) * lim
        items = db.audit_list(user["org_id"], limit=lim, before_id=before_id, offset=offset)
        total = db.count_audit(user["org_id"])
        return paginate_response(items, total, p, lim)
    return db.audit_list(user["org_id"], limit=max(1, min(limit, 1000)), before_id=before_id)


@router.get("/org/export")
def export(request: Request, files: bool = False, user=Depends(auth.require("admin"))):
    """Everything the company has stored, as a ZIP of JSON files (+ the uploaded files if files=true).
    Supports data-portability requests and leaving the service."""
    auth.rate_limit("export", user["org_id"], 5, 3600, "Export limit: 5 per hour.")
    oid = user["org_id"]
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        org = db.get_org(oid)
        z.writestr("company.json", json.dumps({k: org[k] for k in ("id", "name", "plan", "created_at",
                                                                    "retention_days")}, indent=2))
        z.writestr("users.json", json.dumps(db.list_users(oid), indent=2))
        z.writestr("api_keys.json", json.dumps(db.list_api_keys(oid), indent=2))
        before, file_keys = None, []
        with z.open("scans.jsonl", "w") as f:
            while True:
                page = db.list_scans(oid, limit=500, before_id=before)
                if not page:
                    break
                for row in page:
                    s = db.get_scan(row["id"])
                    s.pop("ocr", None)
                    s["decisions"] = db.finding_decisions(row["id"])
                    s["comments"] = db.list_comments(row["id"])
                    f.write((json.dumps(s, ensure_ascii=False) + "\n").encode())
                    if files and s["upload_path"]:
                        file_keys.append((f"files/{s['id']}-{os.path.basename(s['filename'] or 'upload')}",
                                          s["upload_path"]))
                before = page[-1]["id"]
        for name, key in file_keys:          # one file in memory at a time
            try:
                z.writestr(name, store().get(key))
            except Exception:
                z.writestr(name + ".missing.txt", "File not found in storage.")
        before = None
        with z.open("audit_log.jsonl", "w") as f:
            while True:
                page = db.audit_list(oid, limit=1000, before_id=before)
                if not page:
                    break
                for a in page:
                    f.write((json.dumps(a, ensure_ascii=False) + "\n").encode())
                before = page[-1]["id"]
    tmp.close()
    audit(request, user, "data_exported", "org", oid, {"files": files})
    return FileResponse(tmp.name, media_type="application/zip", filename=f"labelcheck-export-{oid}.zip",
                        background=BackgroundTask(os.unlink, tmp.name))

