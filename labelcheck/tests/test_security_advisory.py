import secrets
import pyotp
import pytest
from app import auth, db, mailer
from app.config import settings


@pytest.fixture
def auth_user(client):
    mailer.OUTBOX.clear()
    email = f"sec_user_{secrets.token_hex(4)}@example.com"
    pw = "Tr0ub4dor&3X!Passkey"
    # Register user
    r = client.post("/api/auth/register", json={
        "company_name": "Security Test Corp",
        "name": "Alice Tester",
        "email": email,
        "password": pw,
    })
    assert r.status_code == 200, r.text
    # Verify email
    u = db.user_by_email(email)
    assert u is not None
    db.update_user(u["id"], email_verified=True)
    return {"email": email, "password": pw, "id": u["id"], "org_id": u["org_id"]}


# 1. Overly Permissive CSP ('unsafe-inline' removed from script-src)
def test_csp_header_no_unsafe_inline_in_script_src(client):
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers.get("Content-Security-Policy", "")
    assert "script-src" in csp
    # Find script-src directive
    directives = [d.strip() for d in csp.split(";")]
    script_directive = next((d for d in directives if d.startswith("script-src")), None)
    assert script_directive is not None
    assert "'unsafe-inline'" not in script_directive, f"script-src must not contain 'unsafe-inline': {script_directive}"


def test_theme_init_static_asset(client):
    r = client.get("/theme-init.js")
    assert r.status_code == 200
    assert "data-theme" in r.text


# 2. Session Token Storage & Cookie Mode
def test_login_issues_httponly_cookie_and_authenticates_via_cookie(client, auth_user):
    r = client.post("/api/auth/login", json={
        "email": auth_user["email"],
        "password": auth_user["password"]
    })
    assert r.status_code == 200
    # Verify Set-Cookie header contains HttpOnly and SameSite=lax
    cookies_header = r.headers.get("set-cookie", "")
    assert "lc_session=" in cookies_header
    assert "httponly" in cookies_header.lower()
    assert "samesite=lax" in cookies_header.lower()

    # Call /api/auth/me using the cookie set on client (without Authorization header)
    r_me = client.get("/api/auth/me")
    assert r_me.status_code == 200
    assert r_me.json()["email"] == auth_user["email"]


# 3. Token Leakage via Query Parameters (?session_token=)
def test_query_param_session_token_rejected_with_400(client, auth_user):
    # Log in first to get a valid session
    client.post("/api/auth/login", json={
        "email": auth_user["email"],
        "password": auth_user["password"]
    })
    # Attempt to pass session_token via URL query parameter
    r = client.get("/api/auth/me?session_token=leak_token_attempt_123")
    assert r.status_code == 400
    assert "query parameters are forbidden" in r.json()["detail"]

    r_org = client.get("/api/org?session_token=leak_token_attempt_123")
    assert r_org.status_code == 400
    assert "query parameters are forbidden" in r_org.json()["detail"]


# 4. Broken Authentication: 2FA Disabled Without TOTP Verification
def test_disable_2fa_requires_both_password_and_totp(client, auth_user):
    headers = {"X-LabelCheck": "1"}
    # Log in
    client.post("/api/auth/login", json={
        "email": auth_user["email"],
        "password": auth_user["password"]
    })

    # Set up 2FA
    r_setup = client.post("/api/auth/2fa/setup", headers=headers)
    assert r_setup.status_code == 200
    secret = r_setup.json()["secret"]

    # Enable 2FA with valid TOTP code
    totp = pyotp.TOTP(secret)
    valid_code = totp.now()
    r_enable = client.post("/api/auth/2fa/enable", json={"code": valid_code}, headers=headers)
    assert r_enable.status_code == 200

    u = db.user_by_id(auth_user["id"])
    assert u["totp_enabled"] is True

    # 4a. Attempt to disable 2FA with ONLY password (missing code) -> 422 Unprocessable Entity
    r_missing_code = client.post("/api/auth/2fa/disable", json={"password": auth_user["password"]}, headers=headers)
    assert r_missing_code.status_code == 422

    # 4b. Attempt to disable 2FA with wrong password -> 401 Unauthorized
    r_wrong_pw = client.post("/api/auth/2fa/disable", json={
        "password": "WrongPassword999!",
        "code": totp.now()
    }, headers=headers)
    assert r_wrong_pw.status_code == 401

    # 4c. Attempt to disable 2FA with valid password but invalid TOTP code -> 403 Forbidden
    r_wrong_code = client.post("/api/auth/2fa/disable", json={
        "password": auth_user["password"],
        "code": "000000"
    }, headers=headers)
    assert r_wrong_code.status_code == 403
    assert "Invalid or expired" in r_wrong_code.json()["detail"]

    # 2FA must remain enabled after failed attempts
    u_after_fails = db.user_by_id(auth_user["id"])
    assert u_after_fails["totp_enabled"] is True

    # 4d. Disabling with both valid password AND fresh valid TOTP code succeeds
    import time
    fresh_code = totp.at(int(time.time()) + 30)
    r_success = client.post("/api/auth/2fa/disable", json={
        "password": auth_user["password"],
        "code": fresh_code
    }, headers=headers)
    assert r_success.status_code == 200
    assert r_success.json()["ok"] is True

    u_disabled = db.user_by_id(auth_user["id"])
    assert u_disabled["totp_enabled"] is False
    assert u_disabled["totp_secret"] is None


# 5. Verification Token Leaked in API Response on SMTP Fallback
def test_signup_response_never_leaks_verification_token(client):
    email = "new_signup_sec@example.com"
    r = client.post("/api/auth/register", json={
        "company_name": "No Leak Corp",
        "name": "No Leak User",
        "email": email,
        "password": "Tr0ub4dor&3X!Secure",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert "Account created" in data["message"]

    # Assert strictly NO sensitive verification data is leaked in response
    assert "verify_token" not in data
    assert "verify_url" not in data
    assert "token" not in data
    assert "password" not in data


def test_signup_in_production_fails_if_smtp_missing(client, monkeypatch):
    monkeypatch.setattr(settings, "production", True)
    monkeypatch.setattr(settings, "smtp_host", "")
    r = client.post("/api/auth/register", json={
        "company_name": "Prod Leak Check Corp",
        "name": "Prod User",
        "email": "prod_leak@example.com",
        "password": "Tr0ub4dor&3X!Secure",
    })
    assert r.status_code == 500
    assert "Email delivery service unavailable" in r.json()["detail"]
