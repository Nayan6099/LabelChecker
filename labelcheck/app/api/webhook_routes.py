"""Webhook management for company admins. See app/webhooks.py for delivery, signing and retries."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import auth, db, webhooks
from .common import audit, paginate_response

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
MAX_HOOKS = 10


class HookIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    events: list[str] = Field(min_length=1)
    description: str = Field("", max_length=200)


class HookPatch(BaseModel):
    url: Optional[str] = Field(None, min_length=8, max_length=2000)
    events: Optional[list[str]] = None
    description: Optional[str] = Field(None, max_length=200)
    active: Optional[bool] = None


def _public(h):
    return {k: h[k] for k in ("id", "url", "events", "description", "active", "created_at",
                              "consecutive_failures", "disabled_reason")} | {"last_delivery": h.get("last_delivery")}


def _hook_or_404(hid, user):
    h = db.get_webhook(hid, user["org_id"])
    if not h:
        raise HTTPException(404, "Webhook not found.")
    return h


def _check(fn, *a):
    try:
        return fn(*a)
    except webhooks.WebhookError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/events")
def events(user=Depends(auth.require("admin"))):
    return webhooks.EVENTS


@router.get("")
def list_hooks(user=Depends(auth.require("admin"))):
    return [_public(h) for h in db.list_webhooks(user["org_id"])]


@router.post("")
def create_hook(body: HookIn, request: Request, user=Depends(auth.require("admin"))):
    if len(db.list_webhooks(user["org_id"])) >= MAX_HOOKS:
        raise HTTPException(409, f"A company can have at most {MAX_HOOKS} webhooks.")
    url = _check(webhooks.validate_url, body.url)
    ev = _check(webhooks.validate_events, body.events)
    secret = webhooks.new_secret()
    hid = db.create_webhook(user["org_id"], user["id"], url, ev, auth.encrypt(secret), body.description.strip() or None)
    audit(request, user, "webhook_created", "webhook", hid, {"url": url, "events": ev})
    return {**_public(db.get_webhook(hid)), "secret": secret,
            "note": "Copy the signing secret now. It is shown only once (use 'rotate' to get a new one)."}


@router.patch("/{hid}")
def update_hook(hid: int, body: HookPatch, request: Request, user=Depends(auth.require("admin"))):
    _hook_or_404(hid, user)
    vals = {}
    if body.url is not None:
        vals["url"] = _check(webhooks.validate_url, body.url)
    if body.events is not None:
        vals["events"] = _check(webhooks.validate_events, body.events)
    if body.description is not None:
        vals["description"] = body.description.strip() or None
    if body.active is not None:
        vals["active"] = body.active
        if body.active:
            vals.update(consecutive_failures=0, disabled_reason=None)
    if vals:
        db.update_webhook(hid, **vals)
        audit(request, user, "webhook_updated", "webhook", hid, {k: v for k, v in vals.items() if k != "events"}
              | ({"events": vals["events"]} if "events" in vals else {}))
    return _public(db.get_webhook(hid))


@router.post("/{hid}/rotate-secret")
def rotate(hid: int, request: Request, user=Depends(auth.require("admin"))):
    _hook_or_404(hid, user)
    secret = webhooks.new_secret()
    db.update_webhook(hid, secret_enc=auth.encrypt(secret))
    audit(request, user, "webhook_secret_rotated", "webhook", hid)
    return {"secret": secret, "note": "The old secret stops working now. Update your receiver."}


@router.delete("/{hid}")
def delete_hook(hid: int, request: Request, user=Depends(auth.require("admin"))):
    h = _hook_or_404(hid, user)
    db.delete_webhook(hid)
    audit(request, user, "webhook_deleted", "webhook", hid, {"url": h["url"]})
    return {"ok": True}


@router.post("/{hid}/test")
def test_hook(hid: int, request: Request, user=Depends(auth.require("admin"))):
    h = _hook_or_404(hid, user)
    if not h["active"]:
        raise HTTPException(409, "Turn the webhook on first.")
    did = webhooks.send_test(h)
    audit(request, user, "webhook_test", "webhook", hid)
    return {"delivery_id": did, "note": "A 'ping' event is queued; it is sent within a few seconds."}


@router.get("/{hid}/deliveries")
def deliveries(hid: int, page: Optional[int] = None, limit: int = 10, paginated: bool = False,
               user=Depends(auth.require("admin"))):
    _hook_or_404(hid, user)
    if paginated or page is not None:
        p = max(1, page or 1)
        lim = max(1, min(limit, 100))
        offset = (p - 1) * lim
        items = db.list_deliveries(hid, limit=lim, offset=offset)
        total = db.count_deliveries(hid)
        return paginate_response(items, total, p, lim)
    return db.list_deliveries(hid)


@router.post("/{hid}/deliveries/{did}/retry")
def retry(hid: int, did: int, request: Request, user=Depends(auth.require("admin"))):
    _hook_or_404(hid, user)
    d = db.get_delivery(did)
    if not d or d["webhook_id"] != hid:
        raise HTTPException(404, "Delivery not found.")
    if not db.retry_delivery(did):
        raise HTTPException(409, "Only finished (delivered or failed) deliveries can be sent again.")
    audit(request, user, "webhook_redeliver", "webhook", hid, {"delivery": d["uid"]})
    return {"ok": True}
