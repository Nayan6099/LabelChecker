"""Security & logic tests for payment gateway integrity, subscriptions, and quota pools."""
import hashlib
import hmac
import json
import secrets
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.config import Settings, settings
from app.main import app


def test_signup_payload_cannot_escalate_privilege(client):
    """1. Attempting to sign up with {'plan': 'business'} results in a company on the free/trial plan."""
    email = f"attacker_{secrets.token_hex(4)}@example.com"
    res = client.post("/api/auth/register", json={
        "company_name": "Exploit Corp",
        "name": "Attacker",
        "email": email,
        "password": "Tr0ub4dor&3X!Secure",
        "plan": "business",             # Attacker attempts to get business tier for free
        "tier": "enterprise",
        "quota": 999999,
    })
    assert res.status_code == 200, res.text
    u = db.user_by_email(email)
    assert u is not None
    org = db.get_org(u["org_id"])
    assert org["plan"] == "trial", f"Expected trial but got {org['plan']}"
    assert org["scan_quota"] == 100
    assert org["user_limit"] == 3


def test_missing_razorpay_keys_in_production(monkeypatch):
    """2. Missing Razorpay keys in production blocks application bootstrap."""
    monkeypatch.setenv("LABELCHECK_ENV", "production")
    monkeypatch.setenv("LABELCHECK_COOKIE_SECURE", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db/lc")
    monkeypatch.setenv("LABELCHECK_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("LABELCHECK_PUBLIC_URL", "https://labelcheck.example.com")
    monkeypatch.delenv("LABELCHECK_RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("LABELCHECK_RAZORPAY_KEY_SECRET", raising=False)
    monkeypatch.delenv("LABELCHECK_RAZORPAY_WEBHOOK_SECRET", raising=False)

    s = Settings()
    probs = " ".join(s.problems())
    assert "Missing Razorpay production credentials" in probs


def test_forged_webhook_rejected_with_400(client, monkeypatch):
    """3. Forged webhook payloads lacking valid HMAC headers are rejected with HTTP 400."""
    webhook_secret = "test_webhook_secret_key_123"
    monkeypatch.setattr(settings, "razorpay_webhook_secret", webhook_secret)

    payload = json.dumps({"event": "payment.captured", "payload": {}}).encode()

    # Missing signature header -> 400
    r1 = client.post("/api/billing/webhook", content=payload)
    assert r1.status_code == 400
    assert "Missing X-Razorpay-Signature" in r1.json()["detail"]

    # Forged invalid signature -> 400
    r2 = client.post("/api/billing/webhook", content=payload,
                     headers={"X-Razorpay-Signature": "forged_invalid_signature_hex"})
    assert r2.status_code == 400
    assert "Invalid webhook signature" in r2.json()["detail"]

    # Valid HMAC SHA256 signature -> 200
    valid_sig = hmac.new(webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
    r3 = client.post("/api/billing/webhook", content=payload,
                     headers={"X-Razorpay-Signature": valid_sig})
    assert r3.status_code == 200


def test_addon_packs_decrement_independently_and_do_not_mutate_monthly_base(client):
    """4. Purchased scan packs decrement independently and do not increase base monthly recurring limits."""
    org_id = db.create_org("Pack Test Corp", plan="trial")  # Base quota: 100
    org = db.get_org(org_id)
    assert org["scan_quota"] == 100
    assert org["purchased_addon_credits"] == 0

    # Simulate purchasing a 50 scans pack
    order_id = f"order_{secrets.token_hex(8)}"
    db.create_payment(org_id, None, order_id, 399900, "pack_50")
    already_paid, p, applied = db.settle_payment(order_id, f"pay_{secrets.token_hex(8)}")
    assert not already_paid and applied

    # Verify base quota is still 100, and addon credits are 50
    org = db.get_org(org_id)
    assert org["scan_quota"] == 100, "Base monthly quota must remain 100"
    assert org["purchased_addon_credits"] == 50

    # Consume 90 scans -> should come entirely from base monthly allowance
    ok, used, quota = db.reserve_scans(org_id, 90)
    assert ok
    org = db.get_org(org_id)
    assert org["purchased_addon_credits"] == 50, "Base allowance was sufficient; addon credits must be untouched"

    # Consume 20 more scans (total 110: 10 from base, 10 from addon)
    ok2, used2, quota2 = db.reserve_scans(org_id, 20)
    assert ok2
    org = db.get_org(org_id)
    assert org["purchased_addon_credits"] == 40, "Excess of 10 must be deducted from purchased_addon_credits"


def test_enterprise_accounts_reject_scan_pack_purchases(client):
    """5. Enterprise accounts reject scan pack purchases cleanly without corrupting unlimited status."""
    org_id = db.create_org("Enterprise Corp", plan="enterprise")
    uid = db.create_user(org_id, "ent_admin@example.com", "Ent Admin", auth.hash_password("Password#2026!"), "admin")
    token = auth.new_session(uid)

    # Attempt to create order for pack_50 on unlimited account -> 400 Bad Request
    res = client.post("/api/billing/create-order", json={"plan": "pack_50"},
                      headers={"Authorization": f"Bearer {token}", "X-LabelCheck": "1"})
    assert res.status_code == 400
    assert "Account has unlimited scans" in res.json()["detail"]

    # Verify enterprise scan_quota remains None (unlimited)
    org = db.get_org(org_id)
    assert org["scan_quota"] is None
    assert org["plan"] == "enterprise"


def test_concurrent_browser_and_webhook_settlement_idempotency(client, monkeypatch):
    """6. Firing concurrent browser verification and webhook calls with identical payment_id credits exactly once."""
    secret = "rzp_secret_test_key_999"
    webhook_secret = "rzp_webhook_test_key_888"
    monkeypatch.setattr(settings, "razorpay_key_secret", secret)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", webhook_secret)

    org_id = db.create_org("Race Corp", plan="trial")
    uid = db.create_user(org_id, "race_admin@example.com", "Race Admin", auth.hash_password("Password#2026!"), "admin")
    user_token = auth.new_session(uid)

    order_id = f"order_{secrets.token_hex(8)}"
    payment_id = f"pay_{secrets.token_hex(8)}"
    db.create_payment(org_id, uid, order_id, 399900, "pack_50")

    # Generate valid signature for browser verification
    payload_str = f"{order_id}|{payment_id}"
    sig = hmac.new(secret.encode(), payload_str.encode(), hashlib.sha256).hexdigest()

    # Generate valid webhook payload and signature
    wh_payload = json.dumps({
        "event": "payment.captured",
        "payload": {
            "payment": {"entity": {"id": payment_id, "order_id": order_id}}
        }
    }).encode()
    wh_sig = hmac.new(webhook_secret.encode(), wh_payload, hashlib.sha256).hexdigest()

    results = []

    def call_browser():
        r = client.post("/api/billing/verify-payment", json={
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": sig
        }, headers={"Authorization": f"Bearer {user_token}", "X-LabelCheck": "1"})
        results.append(("browser", r.status_code))

    def call_webhook():
        r = client.post("/api/billing/webhook", content=wh_payload,
                        headers={"X-Razorpay-Signature": wh_sig})
        results.append(("webhook", r.status_code))

    # Fire concurrently in threads
    t1 = threading.Thread(target=call_browser)
    t2 = threading.Thread(target=call_webhook)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert all(status == 200 for _, status in results)

    # Assert pack_50 was credited EXACTLY ONCE (addon credits == 50, NOT 100)
    org = db.get_org(org_id)
    assert org["purchased_addon_credits"] == 50, f"Expected exactly 50 credits but got {org['purchased_addon_credits']}"


def test_expired_subscription_automatically_revokes_paid_features(client):
    """7. An expired subscription automatically revokes paid features upon reaching current_period_end."""
    org_id = db.create_org("Sub Corp", plan="business")
    # Set subscription that expired 10 minutes ago
    past_start = db.now() - timedelta(days=31)
    past_end = db.now() - timedelta(minutes=10)
    db.update_org(org_id, plan="business", scan_quota=10000, user_limit=50,
                  current_period_start=past_start, current_period_end=past_end,
                  subscription_status="active")

    # Fetching org automatically evaluates expiry
    org = db.get_org(org_id)
    assert org["plan"] == "trial", f"Expected downgrade to trial but got {org['plan']}"
    assert org["scan_quota"] == 100
    assert org["user_limit"] == 3
    assert org["subscription_status"] == "expired"
