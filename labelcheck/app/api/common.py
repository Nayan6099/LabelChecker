"""Helpers shared by the route modules."""
import math
from fastapi import HTTPException, Request

from .. import auth, db


def num(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def flag(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y")


def scan_or_404(sid, user):
    s = db.get_scan(sid, user["org_id"])
    if not s:
        raise HTTPException(404, "Scan not found.")
    return s


def audit(request: Request, user, action, target_type=None, target_id=None, detail=None):
    """Audit entry that also records the API key used, if any."""
    if user.get("via") == "api_key":
        detail = dict(detail or {}, api_key=user.get("api_key_name"))
    db.log_event(user["org_id"], user["id"], action, target_type, target_id, detail, auth.client_ip(request))


def check_user_limit(org_id):
    org = db.get_org(org_id)
    limit = db.org_limits(org)["user_limit"]
    if limit is not None and db.count_active_users(org_id) >= limit:
        raise HTTPException(409, f"Your plan allows {limit} active users. Disable a user or upgrade the plan.")


def public_user(u):
    org = db.get_org(u["org_id"])
    return {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"],
            "org_id": u["org_id"], "org_name": org["name"],
            "terms_accepted": bool(u.get("terms_accepted_at")),
            "email_verified": bool(u.get("email_verified", True)),
            "totp_enabled": bool(u.get("totp_enabled")), "notify": bool(u.get("notify", True)),
            "is_platform_admin": bool(u.get("is_platform_admin")),
            "org_requires_2fa": bool(org["require_2fa"]), "via": u.get("via")}


def paginate_response(items, total, page, limit):
    p = max(1, page or 1)
    lim = max(1, limit)
    total_pages = math.ceil(total / lim) if total > 0 else 1
    return {
        "items": items,
        "total": total,
        "page": p,
        "limit": lim,
        "total_pages": total_pages,
    }

