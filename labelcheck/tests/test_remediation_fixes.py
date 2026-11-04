import io
import secrets
import pytest
from app import auth, db, mailer
from app.config import settings


@pytest.fixture
def test_org_and_users(client):
    mailer.OUTBOX.clear()
    oid = db.create_org(f"Remediation Org {secrets.token_hex(3)}", plan="trial")
    admin_pw = "Admin&Secret#Pass2026!"
    user_pw = "User&Secret#Pass2026!"
    admin_id = db.create_user(oid, f"admin_{secrets.token_hex(3)}@test.org", "Org Admin",
                              auth.hash_password(admin_pw), "admin", email_verified=True)
    user_id = db.create_user(oid, f"user_{secrets.token_hex(3)}@test.org", "Target User",
                             auth.hash_password(user_pw), "uploader", email_verified=True)

    # Login as admin
    r_adm = client.post("/api/auth/login", json={
        "email": db.user_by_id(admin_id)["email"],
        "password": admin_pw
    })
    assert r_adm.status_code == 200

    return {
        "org_id": oid,
        "admin_id": admin_id,
        "admin_email": db.user_by_id(admin_id)["email"],
        "admin_pw": admin_pw,
        "user_id": user_id,
        "user_email": db.user_by_id(user_id)["email"],
        "user_pw": user_pw,
    }


# =========================================================================
# 1. Admin Password Reset Fix Tests
# =========================================================================
def test_admin_password_reset_succeeds_with_200(client, test_org_and_users, monkeypatch):
    """Admin resetting a user's password completes with HTTP 200 without throwing 500."""
    admin_headers = {"X-LabelCheck": "1"}
    # Log in as admin
    client.post("/api/auth/login", json={
        "email": test_org_and_users["admin_email"],
        "password": test_org_and_users["admin_pw"]
    })

    new_user_pw = "XyZ!9876#AlphaBetaGamma"
    uid = test_org_and_users["user_id"]

    # Even if mailer fails, endpoint must not crash with 500
    def mock_fail_email(*args, **kwargs):
        raise RuntimeError("SMTP connection timeout simulated")
    monkeypatch.setattr(mailer, "password_reset_by_admin", mock_fail_email)

    r = client.patch(f"/api/users/{uid}", json={"password": new_user_pw}, headers=admin_headers)
    assert r.status_code == 200, f"Expected 200 but got {r.status_code}: {r.text}"
    assert r.json().get("ok") is True

    # User can log in with new password
    r_login = client.post("/api/auth/login", json={
        "email": test_org_and_users["user_email"],
        "password": new_user_pw
    })
    assert r_login.status_code == 200

    # Old password no longer works
    r_old = client.post("/api/auth/login", json={
        "email": test_org_and_users["user_email"],
        "password": test_org_and_users["user_pw"]
    })
    assert r_old.status_code == 401


# =========================================================================
# 2. Rate Limiting: Successful Logins Do Not Lock Out & Test Exemption
# =========================================================================
def test_rapid_successful_logins_do_not_trigger_429(client, test_org_and_users):
    """35+ consecutive successful logins from the same IP must not trigger 429."""
    for i in range(36):
        r = client.post("/api/auth/login", json={
            "email": test_org_and_users["admin_email"],
            "password": test_org_and_users["admin_pw"]
        })
        assert r.status_code == 200, f"Login {i+1} failed with status {r.status_code}: {r.text}"


def test_consecutive_failed_logins_trigger_account_lockout(client, test_org_and_users, monkeypatch):
    """In production mode with non-exempt IP, 5 consecutive failed attempts lock the account."""
    monkeypatch.setattr(settings, "production", True)
    # Simulate a corporate external IP (not loopback)
    client_headers = {"X-Forwarded-For": "203.0.113.42"}
    monkeypatch.setattr(settings, "trust_proxy", True)

    target_email = test_org_and_users["user_email"]

    # 5 failed attempts
    for _ in range(5):
        r = client.post("/api/auth/login", json={
            "email": target_email,
            "password": "WrongPassword!1234"
        }, headers=client_headers)
        assert r.status_code == 401

    # 6th attempt should be locked out with HTTP 429
    r_locked = client.post("/api/auth/login", json={
        "email": target_email,
        "password": test_org_and_users["user_pw"]  # even with correct password
    }, headers=client_headers)
    assert r_locked.status_code == 429
    assert "Too many failed attempts on this account" in r_locked.json()["detail"]


# =========================================================================
# 3. Listing URL Persistence & Validation Tests
# =========================================================================
def test_listing_url_validation_and_persistence(client, test_org_and_users):
    """Submitting listing_url persists the URL and rejects invalid URLs."""
    headers = {"X-LabelCheck": "1"}
    # Log in as uploader
    client.post("/api/auth/login", json={
        "email": test_org_and_users["user_email"],
        "password": test_org_and_users["user_pw"]
    })

    # 1. Invalid URL must be rejected with 400
    dummy_img = io.BytesIO(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
    r_invalid = client.post("/api/scans", data={
        "product_name": "Test Honey",
        "category": "food",
        "listing_url": "javascript:alert(1)"
    }, files={"image": ("label.png", dummy_img, "image/png")}, headers=headers)
    assert r_invalid.status_code == 400
    assert "Invalid listing_url" in r_invalid.json()["detail"]

    # 2. Valid URL must be stored and returned in payload
    dummy_img.seek(0)
    target_url = "https://www.amazon.in/dp/B08XYZ1234"
    r_valid = client.post("/api/scans", data={
        "product_name": "Organic Honey 500g",
        "category": "food",
        "listing_text": "Net quantity: 500 g. MRP Rs 350. Packed by Acme.",
        "listing_url": target_url,
        "wait": "false"  # Queue scan
    }, files={"image": ("honey.png", dummy_img, "image/png")}, headers=headers)
    assert r_valid.status_code == 200, r_valid.text
    scan_data = r_valid.json()
    assert scan_data.get("listing") is not None
    assert scan_data["listing"].get("url") == target_url
    assert "500 g" in scan_data["listing"].get("text", "")

    # 3. GET /api/scans/{id} retains listing_url
    sid = scan_data["id"]
    r_get = client.get(f"/api/scans/{sid}")
    assert r_get.status_code == 200
    get_data = r_get.json()
    assert get_data["listing"]["url"] == target_url
