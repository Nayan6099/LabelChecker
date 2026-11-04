"""Razorpay payment gateway integration for paid plans (Standard, Business) and scan packs.

Keeps Free / Trial plan intact with zero payment.
"""
import hashlib
import hmac
import logging
import secrets
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import auth, db, mailer
from ..config import settings
from .common import audit, paginate_response

router = APIRouter(prefix="/api/billing", tags=["billing"])
log = logging.getLogger("labelcheck.billing")

PLANS_DATA = {
    "standard": {
        "label": "Standard Plan",
        "price_inr": 4999,
        "amount_paise": 499900,
        "type": "subscription",
        "scans": 2000,
        "users": 10,
        "desc": "LabelCheck Standard Plan (2,000 scans/mo, 10 seats)",
    },
    "business": {
        "label": "Business Plan",
        "price_inr": 14999,
        "amount_paise": 1499900,
        "type": "subscription",
        "scans": 10000,
        "users": 50,
        "desc": "LabelCheck Business Plan (10,000 scans/mo, 50 seats)",
    },
    "pack_1": {
        "label": "Single Scan Credit",
        "price_inr": 99,
        "amount_paise": 9900,
        "type": "pack",
        "scans": 1,
        "desc": "LabelCheck On-Demand Top-Up: 1 Scan Credit",
    },
    "pack_50": {
        "label": "Batch Pack (50 Scans)",
        "price_inr": 3999,
        "amount_paise": 399900,
        "type": "pack",
        "scans": 50,
        "desc": "LabelCheck Scan Credit Pack: 50 Scans",
    },
    "pack_250": {
        "label": "Agency Pack (250 Scans)",
        "price_inr": 14999,
        "amount_paise": 1499900,
        "type": "pack",
        "scans": 250,
        "desc": "LabelCheck Scan Credit Pack: 250 Scans",
    },
}


class OrderIn(BaseModel):
    plan: str = Field(pattern="^(standard|business|pack_1|pack_50|pack_250)$")


class VerifyIn(BaseModel):
    razorpay_order_id: str = Field(min_length=6, max_length=100)
    razorpay_payment_id: str = Field(min_length=6, max_length=100)
    razorpay_signature: str = Field(min_length=6, max_length=200)
    plan: Optional[str] = Field(None, pattern="^(standard|business|pack_1|pack_50|pack_250)$")


@router.get("/config")
def billing_config():
    """Return public Razorpay configuration for checkout."""
    is_live = bool(settings.razorpay_key_id and settings.razorpay_key_secret)
    return {
        "key_id": settings.razorpay_key_id or "rzp_test_placeholder",
        "currency": "INR",
        "is_mock": not is_live,
        "plans": {k: {"label": v["label"], "price_inr": v["price_inr"], "scans": v["scans"]}
                  for k, v in PLANS_DATA.items()},
    }


async def _create_razorpay_order(amount_paise: int, receipt: str, notes: dict) -> tuple[str, bool]:
    """Create order via Razorpay REST API or fallback to mock in dev."""
    if settings.razorpay_key_id and settings.razorpay_key_secret:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    "https://api.razorpay.com/v1/orders",
                    auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
                    json={
                        "amount": amount_paise,
                        "currency": "INR",
                        "receipt": receipt,
                        "notes": notes,
                    },
                )
            if resp.status_code != 200:
                detail = resp.json().get("error", {}).get("description") or resp.text
                raise HTTPException(502, f"Razorpay order creation failed: {detail}")
            return resp.json()["id"], False
        except httpx.RequestError as e:
            log.error("Network error communicating with Razorpay: %s", e)
            raise HTTPException(502, "Could not communicate with Razorpay payment gateway.")
    # Sandbox / mock fallback
    mock_id = f"order_mock_{secrets.token_hex(8)}"
    return mock_id, True


def _verify_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Cryptographic HMAC SHA256 verification of Razorpay signature."""
    payload = f"{order_id}|{payment_id}".encode()
    if settings.razorpay_key_secret:
        expected = hmac.new(settings.razorpay_key_secret.encode(), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    # In test/mock mode
    mock_expected = hmac.new(b"mock_secret", payload, hashlib.sha256).hexdigest()
    return signature == "mock_signature" or hmac.compare_digest(mock_expected, signature) or signature == "test_ok"


@router.post("/create-order")
async def create_order(body: OrderIn, request: Request, user=Depends(auth.require("admin"))):
    """Create a Razorpay order for an upgraded plan or scan credit pack."""
    plan_info = PLANS_DATA[body.plan]
    receipt = f"rcpt_{user['org_id']}_{int(time.time())}"
    notes = {
        "org_id": str(user["org_id"]),
        "user_id": str(user["id"]),
        "plan": body.plan,
    }
    order_id, is_mock = await _create_razorpay_order(plan_info["amount_paise"], receipt, notes)
    db.create_payment(user["org_id"], user["id"], order_id, plan_info["amount_paise"], body.plan, currency="INR")
    audit(request, user, "payment_order_created", "payment", None,
          {"order_id": order_id, "plan": body.plan, "amount_inr": plan_info["price_inr"]})
    return {
        "order_id": order_id,
        "amount": plan_info["amount_paise"],
        "currency": "INR",
        "key_id": settings.razorpay_key_id or "rzp_test_placeholder",
        "plan": body.plan,
        "plan_label": plan_info["label"],
        "description": plan_info["desc"],
        "is_mock": is_mock,
    }


@router.post("/verify-payment")
def verify_payment(body: VerifyIn, request: Request, user=Depends(auth.require("admin"))):
    """Verify Razorpay payment signature and activate the purchased plan/credits."""
    payment = db.get_payment_by_order(body.razorpay_order_id)
    if not payment:
        raise HTTPException(404, "Order not found.")
    if payment["org_id"] != user["org_id"]:
        raise HTTPException(403, "Payment belongs to another company.")
    if payment["status"] == "paid":
        return {"ok": True, "message": "Payment already processed.", "plan": payment["plan"]}

    if not _verify_signature(body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature):
        raise HTTPException(400, "Payment verification failed: invalid signature.")

    plan_key = payment["plan"]
    plan_info = PLANS_DATA.get(plan_key)
    if not plan_info:
        raise HTTPException(400, "Invalid plan.")

    org = db.get_org(user["org_id"])
    if plan_info["type"] == "subscription":
        # Monthly plan upgrade (Standard or Business)
        db.update_org(user["org_id"], plan=plan_key, scan_quota=plan_info["scans"], user_limit=plan_info["users"])
    else:
        # Scan top-up pack (add to current scan quota)
        current_quota = org.get("scan_quota")
        if current_quota is None:
            current_quota = db.PLANS.get(org["plan"], {}).get("scans") or 100
        new_quota = current_quota + plan_info["scans"]
        db.update_org(user["org_id"], scan_quota=new_quota)

    db.update_payment(body.razorpay_order_id, body.razorpay_payment_id, status="paid")
    audit(request, user, "payment_verified", "payment", payment["id"],
          {"plan": plan_key, "amount_inr": plan_info["price_inr"], "payment_id": body.razorpay_payment_id})

    # Send email receipt
    try:
        mailer.payment_receipt(
            to=user["email"],
            name=user["name"],
            org_name=org["name"],
            plan_label=plan_info["label"],
            amount_inr=plan_info["price_inr"],
            payment_id=body.razorpay_payment_id,
            order_id=body.razorpay_order_id,
        )
    except Exception as e:
        log.warning("Could not send payment receipt email: %s", e)

    return {
        "ok": True,
        "message": f"Successfully activated {plan_info['label']}!",
        "plan": plan_key,
        "plan_label": plan_info["label"],
        "scans": plan_info["scans"],
    }


@router.get("/history")
def payment_history(page: Optional[int] = None, limit: int = 10, paginated: bool = False,
                    user=Depends(auth.require("admin"))):
    """List payment transactions and receipts for the organization."""
    if paginated or page is not None:
        p = max(1, page or 1)
        lim = max(1, min(limit, 100))
        offset = (p - 1) * lim
        items = db.list_payments(user["org_id"], limit=lim, offset=offset)
        total = db.count_payments(user["org_id"])
        return paginate_response(items, total, p, lim)
    return db.list_payments(user["org_id"])


@router.post("/webhook")
async def razorpay_webhook(request: Request):
    """Handle asynchronous Razorpay webhook events (e.g. payment.captured)."""
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    if settings.razorpay_webhook_secret:
        expected = hmac.new(settings.razorpay_webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(400, "Invalid webhook signature.")
    try:
        data = await request.json()
    except Exception:
        return {"ok": True}

    event = data.get("event")
    if event in ("order.paid", "payment.captured"):
        payload = data.get("payload", {})
        entity = payload.get("payment", {}).get("entity", {})
        order_id = entity.get("order_id")
        payment_id = entity.get("id")
        if order_id and payment_id:
            payment = db.get_payment_by_order(order_id)
            if payment and payment["status"] != "paid":
                db.update_payment(order_id, payment_id, status="paid")
                plan_info = PLANS_DATA.get(payment["plan"])
                if plan_info:
                    org = db.get_org(payment["org_id"])
                    if plan_info["type"] == "subscription":
                        db.update_org(payment["org_id"], plan=payment["plan"],
                                      scan_quota=plan_info["scans"], user_limit=plan_info["users"])
                    else:
                        current = org.get("scan_quota") or 100
                        db.update_org(payment["org_id"], scan_quota=current + plan_info["scans"])
                log.info("Webhook updated payment for order %s to paid", order_id)
    return {"ok": True}

