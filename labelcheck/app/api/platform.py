"""Platform administration (the company running LabelCheck as a service): customer companies,
plans and quotas. Only users with is_platform_admin can call these."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import auth, db
from .common import audit, paginate_response
from .users import EMAIL, invite_user

router = APIRouter(prefix="/api/platform", tags=["platform admin"])
PLAN_RE = "^(" + "|".join(db.PLANS) + ")$"


@router.get("/plans")
def plans(user=Depends(auth.platform_admin)):
    return db.PLANS


@router.get("/orgs")
def orgs(page: Optional[int] = None, limit: int = 10, paginated: bool = False,
         user=Depends(auth.platform_admin)):
    if paginated or page is not None:
        p = max(1, page or 1)
        lim = max(1, min(limit, 100))
        offset = (p - 1) * lim
        items = db.list_orgs(limit=lim, offset=offset)
        total = db.count_orgs()
        return paginate_response(items, total, p, lim)
    return db.list_orgs()


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    plan: str = Field("trial", pattern=PLAN_RE)
    admin_email: str = Field(max_length=254, pattern=EMAIL)
    admin_name: str = Field(min_length=1, max_length=200)


@router.post("/orgs")
def org_create(body: OrgIn, request: Request, user=Depends(auth.platform_admin)):
    """New customer company + its first admin, who gets an invite e-mail to set a password."""
    if db.user_by_email(body.admin_email):
        raise HTTPException(409, "A user with this email already exists.")
    oid = db.create_org(body.name.strip(), body.plan)
    uid = db.create_user(oid, body.admin_email, body.admin_name, auth.hash_password(auth.random_password()), "admin")
    url = invite_user(oid, uid, user["name"])
    audit(request, user, "platform_org_created", "org", oid, {"name": body.name, "plan": body.plan})
    db.log_event(oid, user["id"], "org_created_by_platform", "org", oid, {"plan": body.plan})
    return {"id": oid, "admin_user_id": uid, "invite_url": url}


class OrgPlanPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    plan: Optional[str] = Field(None, pattern=PLAN_RE)
    scan_quota: Optional[int] = Field(None, ge=-1)     # -1 = back to the plan default
    user_limit: Optional[int] = Field(None, ge=-1)
    active: Optional[bool] = None


@router.patch("/orgs/{oid}")
def org_update(oid: int, body: OrgPlanPatch, request: Request, user=Depends(auth.platform_admin)):
    if not db.get_org(oid):
        raise HTTPException(404, "Company not found.")
    if oid == user["org_id"] and body.active is False:
        raise HTTPException(400, "You cannot disable your own company.")
    vals = {}
    for k in ("name", "plan", "active"):
        v = getattr(body, k)
        if v is not None:
            vals[k] = v
    for k in ("scan_quota", "user_limit"):
        v = getattr(body, k)
        if v is not None:
            vals[k] = None if v < 0 else v
    if vals:
        db.update_org(oid, **vals)
        audit(request, user, "platform_org_updated", "org", oid, vals)
        db.log_event(oid, user["id"], "org_updated_by_platform", "org", oid, vals)
    return next(o for o in db.list_orgs() if o["id"] == oid)
