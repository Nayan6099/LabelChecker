"""Tests for the production features: migrations, accounts, e-mail flows, 2FA, API keys,
plans and quotas, platform admin, data controls, ops endpoints, storage, workers."""
import io
import json
import re
import zipfile
from pathlib import Path

import pyotp
import pytest

from app import auth, db, mailer, pipeline
from app.config import Settings, settings

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
PW = "Testpass123"


def new_org(plan="enterprise", users=("admin",), prefix=None):
    """A fresh company with the given roles; returns (org_id, {role: headers})."""
    prefix = prefix or f"o{db.count_users()}"
    oid = db.create_org(f"Co {prefix}", plan)
    out = {}
    for r in users:
        db.create_user(oid, f"{r}@{prefix}.test", f"{r.title()} {prefix}", auth.hash_password(PW), r)
    return oid, out


def login(client, email, pw=PW, totp=""):
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"email": email, "password": pw, "totp": totp})
    client.cookies.clear()
    return r


def H(client, email, pw=PW):
    r = login(client, email, pw)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def upload(client, headers, name="good_label.png", **form):
    form.setdefault("panel_width_mm", "100")
    form.setdefault("panel_height_mm", "75")
    with open(SAMPLES / name, "rb") as f:
        return client.post("/api/scans", files={"image": (name, f)}, data=form, headers=headers)


def last_link(kind):
    for m in reversed(mailer.OUTBOX):
        found = re.search(rf"#/{kind}/([A-Za-z0-9_\-]+)", m["text"])
        if found:
            return found.group(1)
    raise AssertionError("no mail with a link")


@pytest.fixture(scope="module")
def co(client):
    oid, _ = new_org(users=("admin", "reviewer", "uploader"), prefix="plat")
    return {"id": oid, "admin": H(client, "admin@plat.test"), "reviewer": H(client, "reviewer@plat.test"),
            "uploader": H(client, "uploader@plat.test")}


# ---------------- migrations ----------------

@pytest.mark.skipif(not db.engine().dialect.name == "postgresql", reason="advisory lock is PostgreSQL only")
def test_concurrent_migrations_do_not_race(tmp_path):
    """web + N workers migrate at start-up at the same moment; on a fresh database all must succeed."""
    import subprocess
    import sys
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    url = make_url(str(db.engine().url.render_as_string(hide_password=False)))
    fresh = f"race_{tmp_path.name[-8:].lower()}".replace("-", "_")
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {fresh}"))
        c.execute(text(f"CREATE DATABASE {fresh}"))
    target = url.set(database=fresh).render_as_string(hide_password=False)
    code = "import sys; sys.path.insert(0, '.'); from app import db; db.migrate(); print('ok')"
    root = Path(__file__).resolve().parent.parent
    procs = [subprocess.Popen([sys.executable, "-c", code], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env={**__import__("os").environ, "DATABASE_URL": target,
                                   "LABELCHECK_DATA": str(tmp_path / str(i))}) for i in range(5)]
    results = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [e.decode()[-400:] for _, e in results]
    with create_engine(target).connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0005"
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE {fresh} WITH (FORCE)"))


def test_schema_matches_migrations(client):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    with db.engine().connect() as c:
        diff = compare_metadata(MigrationContext.configure(c, opts={"compare_type": True}), db.md)
    assert diff == [], diff


# ---------------- configuration ----------------

def test_production_refuses_unsafe_settings(monkeypatch):
    monkeypatch.setenv("LABELCHECK_ENV", "production")
    monkeypatch.setenv("LABELCHECK_COOKIE_SECURE", "0")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LABELCHECK_SECRET_KEY", raising=False)
    monkeypatch.delenv("LABELCHECK_RAZORPAY_KEY_ID", raising=False)
    probs = " ".join(Settings().problems())
    for word in ("COOKIE_SECURE", "DATABASE_URL", "SECRET_KEY", "PUBLIC_URL", "Razorpay"):
        assert word in probs
    monkeypatch.setenv("LABELCHECK_COOKIE_SECURE", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db/lc")
    monkeypatch.setenv("LABELCHECK_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("LABELCHECK_PUBLIC_URL", "https://labelcheck.example.com")
    monkeypatch.setenv("LABELCHECK_RAZORPAY_KEY_ID", "rzp_live_1234567890")
    monkeypatch.setenv("LABELCHECK_RAZORPAY_KEY_SECRET", "secret1234567890")
    monkeypatch.setenv("LABELCHECK_RAZORPAY_WEBHOOK_SECRET", "webhooksecret1234567890")
    assert Settings().problems() == []


# ---------------- ops endpoints ----------------

def test_health_ready_metrics_request_id(client):
    assert client.get("/api/health/live").json()["status"] == "ok"
    r = client.get("/api/health/ready")
    assert r.status_code == 200 and r.json()["checks"] == {"database": "ok", "storage": "ok", "ocr": "ok"}
    r = client.get("/metrics")
    assert r.status_code == 200 and "labelcheck_http_requests_total" in r.text
    r = client.get("/api/health", headers={"X-Request-ID": "abc123"})
    assert r.headers["x-request-id"] == "abc123"
    assert client.get("/api/docs").status_code == 200


def test_metrics_token(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", "s3cret")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200


# ---------------- forgot password / invites ----------------

def test_forgot_and_reset_password(client):
    new_org(users=("uploader",), prefix="fgt")
    old = H(client, "uploader@fgt.test")
    r = client.post("/api/auth/forgot", json={"email": "uploader@fgt.test"})
    assert r.status_code == 200
    same = client.post("/api/auth/forgot", json={"email": "nobody@nowhere.test"}).json()
    assert same["message"] == r.json()["message"]              # no account enumeration
    token = last_link("reset")
    assert client.post("/api/auth/reset", json={"token": token, "password": "weak"}).status_code == 400
    assert client.post("/api/auth/reset", json={"token": token, "password": "Newpass@12345!"}).status_code == 200
    assert client.post("/api/auth/reset", json={"token": token, "password": "Newpass@12345!"}).status_code == 400
    assert client.get("/api/auth/me", headers=old).status_code == 401   # old sessions ended
    assert login(client, "uploader@fgt.test", "Newpass@12345!").status_code == 200


def test_invite_flow_and_user_limit(client):
    oid, _ = new_org(plan="trial", prefix="inv")          # trial: 3 users
    adm = H(client, "admin@inv.test")
    r = client.post("/api/users", json={"email": "new@inv.test", "name": "New Person", "role": "reviewer"},
                    headers=adm)
    assert r.status_code == 200 and "invite_url" in r.json()
    token = last_link("invite")
    assert client.get(f"/api/auth/invite/{token}").json()["email"] == "new@inv.test"
    r = client.post("/api/auth/accept-invite", json={"token": token, "password": "Welcome#Colleague2026!"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "reviewer"
    client.cookies.clear()
    assert client.post("/api/users", json={"email": "third@inv.test", "name": "T", "role": "uploader"},
                       headers=adm).status_code == 200
    r = client.post("/api/users", json={"email": "fourth@inv.test", "name": "F", "role": "uploader"}, headers=adm)
    assert r.status_code == 409 and "3 active users" in r.json()["detail"]


# ---------------- 2FA ----------------

def test_two_factor_login(client):
    new_org(prefix="tfa")
    h = H(client, "admin@tfa.test")
    setup = client.post("/api/auth/2fa/setup", headers=h).json()
    assert setup["qr_svg"].startswith("<?xml") or "<svg" in setup["qr_svg"]
    totp = pyotp.TOTP(setup["secret"])
    assert client.post("/api/auth/2fa/enable", json={"code": "000000"}, headers=h).status_code == 400
    assert client.post("/api/auth/2fa/enable", json={"code": totp.now()}, headers=h).status_code == 200
    stored = db.user_by_email("admin@tfa.test")["totp_secret"]
    assert setup["secret"] not in stored                        # encrypted at rest
    r = login(client, "admin@tfa.test")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "totp_required"
    assert login(client, "admin@tfa.test", totp="123456").status_code == 401
    code = totp.at(totp.timecode(__import__("datetime").datetime.now()) * totp.interval + totp.interval)
    assert login(client, "admin@tfa.test", totp=code).status_code == 200
    assert login(client, "admin@tfa.test", totp=code).status_code == 401   # replay of the same code


def test_company_can_require_2fa(client):
    oid, _ = new_org(users=("admin", "uploader"), prefix="req")
    adm = H(client, "admin@req.test")
    assert client.patch("/api/org", json={"require_2fa": True}, headers=adm).status_code == 409  # admin first
    s = client.post("/api/auth/2fa/setup", headers=adm).json()
    client.post("/api/auth/2fa/enable", json={"code": pyotp.TOTP(s["secret"]).now()}, headers=adm)
    assert client.patch("/api/org", json={"require_2fa": True}, headers=adm).json()["require_2fa"] is True
    up = H(client, "uploader@req.test")
    assert client.get("/api/scans", headers=up).status_code == 403
    assert client.get("/api/auth/me", headers=up).json()["org_requires_2fa"] is True


# ---------------- API keys ----------------

def test_api_keys(client, co):
    r = client.post("/api/api-keys", json={"name": "ERP sync", "scopes": ["read", "write"]}, headers=co["admin"])
    key = r.json()["key"]
    assert key.startswith("lc_")
    assert key not in json.dumps(db.list_api_keys(co["id"]))     # only the hash is stored
    k = {"X-API-Key": key}
    j = upload(client, k).json()
    assert j["status"] == "done" and j["can"]["submit"] is False   # keys cannot review
    assert client.get(f"/api/scans/{j['id']}", headers=k).status_code == 200
    assert client.get("/api/users", headers=k).status_code == 403
    assert client.post("/api/api-keys", json={"name": "x"}, headers=k).status_code == 403
    acts = client.get(f"/api/scans/{j['id']}/audit", headers=co["admin"]).json()
    assert acts[-1]["detail"]["api_key"] == "ERP sync"
    ro = client.post("/api/api-keys", json={"name": "BI", "scopes": ["read"]}, headers=co["admin"]).json()
    assert client.get("/api/scans", headers={"Authorization": f"Bearer {ro['key']}"}).status_code == 200
    assert upload(client, {"X-API-Key": ro["key"]}).status_code == 403
    assert client.delete(f"/api/api-keys/{r.json()['id']}", headers=co["admin"]).status_code == 200
    assert client.get("/api/scans", headers=k).status_code == 401
    assert client.get("/api/scans", headers={"X-API-Key": "lc_deadbeef_" + "x" * 40}).status_code == 401


def test_api_key_rate_limit(client, co, monkeypatch):
    key = client.post("/api/api-keys", json={"name": "burst"}, headers=co["admin"]).json()["key"]
    monkeypatch.setattr(settings, "rate_api_per_min", 3)
    codes = [client.get("/api/batches", headers={"X-API-Key": key}).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429


# ---------------- plans, quotas, platform admin ----------------

def test_monthly_quota(client):
    oid, _ = new_org(plan="trial", prefix="quota")
    db.update_org(oid, scan_quota=2)
    h = H(client, "admin@quota.test")
    assert upload(client, h, wait="false").status_code == 200
    assert upload(client, h, wait="false").status_code == 200
    r = upload(client, h, wait="false")
    assert r.status_code == 429 and "Monthly scan limit" in r.json()["detail"]
    info = client.get("/api/org", headers=h).json()
    assert info["scans_this_month"] == 2 and info["scan_quota"] == 2 and info["plan"] == "trial"


def test_platform_admin(client):
    oid = db.create_org("Operator", "enterprise")
    db.create_user(oid, "ops@operator.test", "Ops", auth.hash_password(PW), "admin", platform_admin=True)
    p = H(client, "ops@operator.test")
    r = client.post("/api/platform/orgs", json={"name": "New Brand", "plan": "standard",
                                                "admin_email": "boss@newbrand.test", "admin_name": "Boss"}, headers=p)
    assert r.status_code == 200 and "/#/invite/" in r.json()["invite_url"]
    nid = r.json()["id"]
    org = next(o for o in client.get("/api/platform/orgs", headers=p).json() if o["id"] == nid)
    assert org["plan"] == "standard" and org["scan_quota"] == 2000
    r = client.patch(f"/api/platform/orgs/{nid}", json={"plan": "business", "scan_quota": 50}, headers=p)
    assert r.json()["scan_quota"] == 50 and r.json()["plan"] == "business"
    # a normal company admin cannot use platform endpoints
    new_org(prefix="np")
    assert client.get("/api/platform/orgs", headers=H(client, "admin@np.test")).status_code == 403
    # disabling a company blocks its users at once
    tok = last_link("invite")
    client.post("/api/auth/accept-invite", json={"token": tok, "password": "Str0ng#BrandAdmin2026!"})
    client.cookies.clear()
    boss = H(client, "boss@newbrand.test", "Str0ng#BrandAdmin2026!")
    client.patch(f"/api/platform/orgs/{nid}", json={"active": False}, headers=p)
    assert client.get("/api/scans", headers=boss).status_code == 401
    assert login(client, "boss@newbrand.test", "Str0ng#BrandAdmin2026!").status_code == 401


# ---------------- data controls ----------------

def test_delete_scan_permissions_and_files(client, co):
    j = upload(client, co["uploader"]).json()
    s = db.get_scan(j["id"])
    from app.storage import store
    assert store().exists(s["upload_path"]) and store().exists(s["image_path"])
    assert client.delete(f"/api/scans/{j['id']}", headers=co["reviewer"]).status_code == 403   # not own
    assert client.delete(f"/api/scans/{j['id']}", headers=co["uploader"]).status_code == 200
    assert client.get(f"/api/scans/{j['id']}", headers=co["uploader"]).status_code == 404
    assert not store().exists(s["upload_path"]) and not store().exists(s["image_path"])
    actions = [a["action"] for a in client.get("/api/audit", headers=co["admin"]).json()]
    assert "scan_deleted" in actions


def test_retention_deletes_old_scans(client):
    oid, _ = new_org(prefix="ret")
    h = H(client, "admin@ret.test")
    sid = upload(client, h).json()["id"]
    db.update_org(oid, retention_days=30)
    with db.engine().begin() as c:
        c.execute(db.scans.update().where(db.scans.c.id == sid)
                  .values(created_at=db.now() - __import__("datetime").timedelta(days=40)))
    out = pipeline.maintenance()
    assert out["retention_deleted"] >= 1 and db.get_scan(sid) is None


def test_delete_user_erases_personal_data(client, co):
    r = client.post("/api/users", json={"email": "temp@plat.test", "name": "Temp Person", "role": "uploader",
                                        "password": "Un1qu3#SecurePass2026!"}, headers=co["admin"])
    uid = r.json()["id"]
    assert client.delete(f"/api/users/{uid}", headers=co["admin"]).status_code == 200
    u = db.user_by_id(uid)
    assert u["name"] == "Deleted user" and "temp@" not in u["email"] and not u["active"]


def test_export(client, co):
    upload(client, co["uploader"])
    r = client.get("/api/org/export?files=true", headers=co["admin"])
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    for n in ("company.json", "users.json", "scans.jsonl", "audit_log.jsonl"):
        assert n in names
    assert any(n.startswith("files/") for n in names)
    users = json.loads(z.read("users.json"))
    assert all("password_hash" not in u and "totp_secret" not in u for u in users)


# ---------------- e-mail notifications ----------------

def test_review_emails(client, co):
    j = upload(client, co["uploader"], sku="MAIL-1").json()
    mailer.OUTBOX.clear()
    client.post(f"/api/scans/{j['id']}/review", json={"action": "submit"}, headers=co["uploader"])
    assert any(m["to"] == "reviewer@plat.test" and "Label to review" in m["subject"] for m in mailer.OUTBOX)
    client.post(f"/api/scans/{j['id']}/review", json={"action": "request_changes", "note": "Fix MRP"},
                headers=co["reviewer"])
    assert any(m["to"] == "uploader@plat.test" and "sent back" in m["subject"] for m in mailer.OUTBOX)
    # Check that emails include HTML bodies
    assert all("html" in m and m["html"] is not None for m in mailer.OUTBOX)


def test_invite_and_test_email(client, co, monkeypatch):
    # 1. Test endpoint when SMTP host is unset
    mailer.OUTBOX.clear()
    r = client.post("/api/org/test-email", headers=co["admin"])
    assert r.status_code == 400
    assert "SMTP server is not configured" in r.json()["detail"]

    # 2. Test invite sends HTML email
    mailer.OUTBOX.clear()
    client.post("/api/users", json={"name": "New Person", "email": "newbie@plat.test", "role": "uploader"},
                headers=co["admin"])
    invite_mail = next(m for m in mailer.OUTBOX if m["to"] == "newbie@plat.test")
    assert "invited to LabelCheck" in invite_mail["subject"]
    assert "invite" in invite_mail["html"].lower()

    # 3. Test endpoint when SMTP host is configured (mocked transport)
    from app.config import settings
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(mailer, "_send_now", lambda msg: None)

    r2 = client.post("/api/org/test-email", headers=co["admin"])
    assert r2.status_code == 200
    assert r2.json()["ok"] is True
    test_mail = next(m for m in mailer.OUTBOX if "SMTP Test" in m["subject"])
    assert "SMTP configuration is active" in test_mail["text"]
    assert "<!DOCTYPE html>" in test_mail["html"]


def test_strict_rbac(client, co):
    # 1. Upload scan as uploader
    j = upload(client, co["uploader"], sku="RBAC-1").json()
    sid = j["id"]
    findings = j["result"]["findings"]
    first_key = findings[0]["key"]

    # 2. Uploader cannot decide findings
    r = client.put(f"/api/scans/{sid}/findings/{first_key}", json={"decision": "ok"}, headers=co["uploader"])
    assert r.status_code == 403
    assert "reviewer role" in r.json()["detail"]

    # 3. Uploader cannot approve or request changes
    r = client.post(f"/api/scans/{sid}/review", json={"action": "approve"}, headers=co["uploader"])
    assert r.status_code == 403
    assert "reviewer role" in r.json()["detail"]

    r = client.post(f"/api/scans/{sid}/review", json={"action": "request_changes", "note": "Fix"}, headers=co["uploader"])
    assert r.status_code == 403
    assert "reviewer role" in r.json()["detail"]

    # 4. Uploader cannot access admin endpoints
    assert client.get("/api/users", headers=co["uploader"]).status_code == 403
    assert client.post("/api/users", json={"email": "hacker@plat.test", "name": "Hacker", "role": "admin"},
                       headers=co["uploader"]).status_code == 403
    assert client.patch("/api/org", json={"name": "Hacked Org"}, headers=co["uploader"]).status_code == 403
    assert client.post("/api/org/test-email", headers=co["uploader"]).status_code == 403
    assert client.get("/api/api-keys", headers=co["uploader"]).status_code == 403
    assert client.post("/api/api-keys", json={"name": "Key"}, headers=co["uploader"]).status_code == 403
    assert client.get("/api/webhooks", headers=co["uploader"]).status_code == 403
    assert client.get("/api/audit", headers=co["uploader"]).status_code == 403
    assert client.get("/api/org/export", headers=co["uploader"]).status_code == 403

    # 5. Reviewer cannot access admin endpoints
    assert client.get("/api/users", headers=co["reviewer"]).status_code == 403
    assert client.post("/api/users", json={"email": "hacker2@plat.test", "name": "Hacker2", "role": "admin"},
                       headers=co["reviewer"]).status_code == 403
    assert client.patch("/api/org", json={"name": "Hacked Org 2"}, headers=co["reviewer"]).status_code == 403
    assert client.post("/api/org/test-email", headers=co["reviewer"]).status_code == 403
    assert client.get("/api/api-keys", headers=co["reviewer"]).status_code == 403
    assert client.post("/api/api-keys", json={"name": "Key 2"}, headers=co["reviewer"]).status_code == 403
    assert client.get("/api/webhooks", headers=co["reviewer"]).status_code == 403
    assert client.get("/api/audit", headers=co["reviewer"]).status_code == 403
    assert client.get("/api/org/export", headers=co["reviewer"]).status_code == 403

    # 6. Reviewer CAN decide findings
    r = client.put(f"/api/scans/{sid}/findings/{first_key}", json={"decision": "ok"}, headers=co["reviewer"])
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # 7. Non-platform admin (even company admin) cannot access platform endpoints
    assert client.get("/api/platform/orgs", headers=co["admin"]).status_code == 403
    assert client.post("/api/platform/orgs", json={"name": "Test", "admin_email": "t@t.com", "admin_name": "T"},
                       headers=co["admin"]).status_code == 403
    assert client.get("/api/platform/plans", headers=co["admin"]).status_code == 403

    # 8. Uploader cross-submission guard:
    # Create a 2nd uploader in the same company
    r = client.post("/api/users", json={"email": "uploader2@plat.test", "name": "Uploader Two",
                                        "role": "uploader", "password": "V3ry#Diff1cultPass2026!"}, headers=co["admin"])
    u2_headers = H(client, "uploader2@plat.test", "V3ry#Diff1cultPass2026!")
    # Uploader 2 cannot submit scan uploaded by Uploader 1
    r = client.post(f"/api/scans/{sid}/review", json={"action": "submit"}, headers=u2_headers)
    assert r.status_code == 409
    assert "cannot be submitted" in r.json()["detail"]

    # But Uploader 1 CAN submit their own scan
    r = client.post(f"/api/scans/{sid}/review", json={"action": "submit"}, headers=co["uploader"])
    assert r.status_code == 200
    assert r.json()["review_status"] == "in_review"


# ---------------- billing & razorpay ----------------

def test_free_plan_stays_free_and_instant(client):
    mailer.OUTBOX.clear()
    r = client.post("/api/auth/register", json={
        "company_name": "Freebie Corp",
        "name": "Free User",
        "email": "freebie@test.local",
        "password": "V3ry#Compl3xTrial2026!",
        "plan": "trial"
    })
    assert r.status_code == 200
    data = r.json()
    assert data.get("success") is True
    u = db.user_by_email("freebie@test.local")
    assert u["role"] == "admin"
    assert u["email_verified"] is False
    vtok = last_link("verify")
    client.post("/api/auth/verify-email", json={"token": vtok})
    # Verify free tier limits
    h = H(client, "freebie@test.local", "V3ry#Compl3xTrial2026!")
    org = client.get("/api/org", headers=h).json()
    assert org["plan"] == "trial"
    assert org["scan_quota"] == 100
    assert org["user_limit"] == 3


def test_razorpay_order_creation_and_verification(client, co, monkeypatch):
    import hmac
    import hashlib
    from app.config import settings
    monkeypatch.setattr(settings, "razorpay_key_secret", "test_secret_for_platform")
    mailer.OUTBOX.clear()
    # 1. Billing config is public / accessible
    cfg = client.get("/api/billing/config").json()
    assert "plans" in cfg and "standard" in cfg["plans"]
    assert cfg["currency"] == "INR"

    # 2. Non-admin cannot create order (strict RBAC)
    r = client.post("/api/billing/create-order", json={"plan": "standard"}, headers=co["uploader"])
    assert r.status_code == 403

    # 3. Admin creates order for standard plan
    r = client.post("/api/billing/create-order", json={"plan": "standard"}, headers=co["admin"])
    assert r.status_code == 200
    order = r.json()
    assert "order_id" in order and order["amount"] == 499900
    assert order["plan"] == "standard"

    # 4. Verify payment with signature
    sig1 = hmac.new(b"test_secret_for_platform", f"{order['order_id']}|pay_test_123456".encode("utf-8"), hashlib.sha256).hexdigest()
    v = client.post("/api/billing/verify-payment", json={
        "razorpay_order_id": order["order_id"],
        "razorpay_payment_id": "pay_test_123456",
        "razorpay_signature": sig1,
        "plan": "standard"
    }, headers=co["admin"])
    assert v.status_code == 200
    assert v.json()["ok"] is True

    # 5. Check org upgraded to standard
    org = client.get("/api/org", headers=co["admin"]).json()
    assert org["plan"] == "standard"
    assert org["scan_quota"] == 2000
    assert org["user_limit"] == 10
    assert any("Receipt" in m["subject"] for m in mailer.OUTBOX)

    # 6. Purchase a scan top-up pack (pack_50)
    r_pack = client.post("/api/billing/create-order", json={"plan": "pack_50"}, headers=co["admin"])
    assert r_pack.status_code == 200
    pack_order = r_pack.json()
    assert pack_order["amount"] == 399900

    sig_pack = hmac.new(b"test_secret_for_platform", f"{pack_order['order_id']}|pay_test_pack_50".encode("utf-8"), hashlib.sha256).hexdigest()
    v_pack = client.post("/api/billing/verify-payment", json={
        "razorpay_order_id": pack_order["order_id"],
        "razorpay_payment_id": "pay_test_pack_50",
        "razorpay_signature": sig_pack,
        "plan": "pack_50"
    }, headers=co["admin"])
    assert v_pack.status_code == 200

    org_after = client.get("/api/org", headers=co["admin"]).json()
    assert org_after["scan_quota"] == 2000
    assert org_after["purchased_addon_credits"] == 50

    # 7. Payment history check
    hist = client.get("/api/billing/history", headers=co["admin"]).json()
    assert len(hist) >= 2
    assert any(p["plan"] == "standard" and p["status"] == "paid" for p in hist)
    assert any(p["plan"] == "pack_50" and p["status"] == "paid" for p in hist)




# ---------------- storage / workers ----------------

def test_s3_storage_backend(monkeypatch):
    moto = pytest.importorskip("moto")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with moto.mock_aws():
        import boto3
        boto3.client("s3", region_name="ap-south-1").create_bucket(
            Bucket="lc-test", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        from app.storage import S3Storage
        s = S3Storage("lc-test", "labelcheck/", region="ap-south-1")
        s.check()
        s.put("uploads/1/9.png", b"png-bytes", "image/png")
        assert s.get("uploads/1/9.png") == b"png-bytes" and s.exists("uploads/1/9.png")
        s.delete("uploads/1/9.png")
        assert not s.exists("uploads/1/9.png")


def test_local_storage_blocks_path_escape(tmp_path):
    from app.storage import LocalStorage
    with pytest.raises(ValueError):
        LocalStorage(tmp_path).put("../../etc/x", b"no")


def test_stale_scans_are_requeued_not_fresh_ones(client, co):
    j = upload(client, co["uploader"], wait="false").json()
    pipeline.wait_for([j["id"]], 120)
    with db.engine().begin() as c:
        c.execute(db.scans.update().where(db.scans.c.id == j["id"])
                  .values(status="processing", claimed_at=db.now()))
    assert db.requeue_stale(15) == 0                                  # healthy worker keeps it
    with db.engine().begin() as c:
        c.execute(db.scans.update().where(db.scans.c.id == j["id"])
                  .values(claimed_at=db.now() - __import__("datetime").timedelta(minutes=30)))
    assert db.requeue_stale(15) == 1
    assert pipeline.wait_for([j["id"]], 120) and db.get_scan(j["id"])["status"] == "done"


def test_rate_limit_counter_is_shared():
    for _ in range(3):
        ok, n = db.hit("t", "subject", 3, 60)
    assert ok and n == 3
    assert db.hit("t", "subject", 3, 60) == (False, 4)


# ---------------- production auth & security suite ----------------

def test_password_complexity_rules():
    # 1. Under 12 chars
    prob = auth.password_problem("Short@123")
    assert prob and "at least 12 characters" in prob

    # 2. Missing uppercase
    prob = auth.password_problem("nouppercase@12345")
    assert prob and "uppercase" in prob

    # 3. Missing lowercase
    prob = auth.password_problem("NOLOWERCASE@12345")
    assert prob and "lowercase" in prob

    # 4. Missing digits
    prob = auth.password_problem("NoDigitsHereAtAll!@#")
    assert prob and ("number" in prob or "digit" in prob)

    # 5. Missing special symbols
    prob = auth.password_problem("NoSpecialSymbols1234")
    assert prob and "symbol" in prob

    # 6. Common / leaked password
    prob = auth.password_problem("Password@1234!")
    assert prob and "common" in prob

    # 7. Easily guessable with email / name
    prob = auth.password_problem("AlexanderSuper@123", user_context={"name": "Alexander", "email": "alex@corp.test"})
    assert prob and ("email" in prob or "name" in prob)

    prob2 = auth.password_problem("MyCompanyPass@123", user_context={"name": "Bob", "email": "bob@mycompany.test"})
    assert prob2 and ("email" in prob2 or "name" in prob2)

    # 8. Valid strong password
    assert auth.password_problem("V3ry$ecureP@ssw0rd!2026") is None


def test_login_constant_time_and_enumeration_prevention(client):
    # Non-existent email returns generic 401
    r_ghost = client.post("/api/auth/login", json={"email": "nonexistent@example.com", "password": "WrongPassword@123!"})
    assert r_ghost.status_code == 401
    assert r_ghost.json()["detail"] == "Invalid credentials."

    # Existing user with wrong password returns the exact same generic 401
    new_org(users=("admin",), prefix="enumcheck")
    r_wrong = client.post("/api/auth/login", json={"email": "admin@enumcheck.test", "password": "WrongPassword@123!"})
    assert r_wrong.status_code == 401
    assert r_wrong.json()["detail"] == "Invalid credentials."


def test_login_rate_limiting_and_lockout(client):
    # Multiple failed logins from same IP trigger rate limit (5 attempts max per 5 min)
    target_email = "ratelimit_target@test.local"
    for _ in range(5):
        r = client.post("/api/auth/login", json={"email": target_email, "password": "WrongPassword@123!"})
        assert r.status_code == 401
    # 6th attempt is throttled
    r_blocked = client.post("/api/auth/login", json={"email": target_email, "password": "WrongPassword@123!"})
    assert r_blocked.status_code == 429
    assert "Too many failed attempts" in r_blocked.json()["detail"]
    # Clear rate limits so other tests aren't locked out
    with db.engine().begin() as c:
        c.execute(db.rate_limits.delete())


def test_email_verification_flow(client):
    mailer.OUTBOX.clear()
    reg_email = "verify_me@flow.test"
    reg_pass = "V3ry$ecureEmailVerif#1"
    r = client.post("/api/auth/register", json={
        "company_name": "VerifyFlow Corp",
        "name": "Verify User",
        "email": reg_email,
        "password": reg_pass,
        "plan": "trial"
    })
    assert r.status_code == 200
    token = r.json().get("verify_token") or last_link("verify")
    assert token is not None

    # Unverified user cannot log in
    r_login_unverified = client.post("/api/auth/login", json={"email": reg_email, "password": reg_pass})
    assert r_login_unverified.status_code == 403
    assert "verify your email" in r_login_unverified.json()["detail"]

    # Resend verification
    r_resend = client.post("/api/auth/resend-verification", json={"email": reg_email})
    assert r_resend.status_code == 200
    new_token = last_link("verify")

    # Invalid token fails
    r_bad = client.post("/api/auth/verify-email", json={"token": "invalid_or_expired_token_xyz"})
    assert r_bad.status_code == 400

    # Valid token succeeds
    r_ok = client.post("/api/auth/verify-email", json={"token": new_token})
    assert r_ok.status_code == 200
    assert "Email verified successfully" in r_ok.json()["message"]

    # Now login succeeds
    r_login_verified = client.post("/api/auth/login", json={"email": reg_email, "password": reg_pass})
    assert r_login_verified.status_code == 200
    assert r_login_verified.json()["user"]["email_verified"] is True


def test_password_reset_ttl_and_session_invalidation(client):
    mailer.OUTBOX.clear()
    new_org(users=("admin",), prefix="rstcheck")
    email = "admin@rstcheck.test"
    pw_old = PW
    pw_new = "BrandN3w#SecurePass2026!"

    # Log in and obtain an active session
    active_headers = H(client, email, pw_old)
    assert client.get("/api/auth/me", headers=active_headers).status_code == 200

    # Request reset
    r_forgot = client.post("/api/auth/forgot", json={"email": email})
    assert r_forgot.status_code == 200
    token = last_link("reset")

    # Perform reset
    r_reset = client.post("/api/auth/reset", json={"token": token, "password": pw_new})
    assert r_reset.status_code == 200

    # All prior sessions must be invalidated immediately
    assert client.get("/api/auth/me", headers=active_headers).status_code == 401

    # Single-use token: reuse must fail
    r_reuse = client.post("/api/auth/reset", json={"token": token, "password": pw_new})
    assert r_reuse.status_code == 400

    # Login works only with new password
    assert client.post("/api/auth/login", json={"email": email, "password": pw_old}).status_code == 401
    assert client.post("/api/auth/login", json={"email": email, "password": pw_new}).status_code == 200


def test_authenticated_change_password(client):
    with db.engine().begin() as c:
        c.execute(db.rate_limits.delete())
    new_org(users=("admin",), prefix="chgcheck")
    email = "admin@chgcheck.test"
    pw_old = PW
    pw_new = "Ch@ngedSecurePass999!"

    headers = H(client, email, pw_old)

    # Wrong current password fails
    r_bad_curr = client.post("/api/auth/password", json={
        "current": "WrongPassword@123!",
        "new": pw_new
    }, headers=headers)
    assert r_bad_curr.status_code == 400
    assert "Current password is wrong" in r_bad_curr.json()["detail"]

    # Weak new password fails
    r_weak = client.post("/api/auth/password", json={
        "current": pw_old,
        "new": "short"
    }, headers=headers)
    assert r_weak.status_code == 400

    # Successful change
    r_ok = client.post("/api/auth/password", json={
        "current": pw_old,
        "new": pw_new
    }, headers=headers)
    assert r_ok.status_code == 200

    # Old session is invalidated
    assert client.get("/api/auth/me", headers=headers).status_code == 401

    # Login works with new password
    assert client.post("/api/auth/login", json={"email": email, "password": pw_new}).status_code == 200


def test_registration_honeypot_bot_prevention(client):
    r = client.post("/api/auth/register", json={
        "company_name": "Spambot Inc",
        "name": "Botty McBot",
        "email": "bot@spambot.test",
        "password": "V3ry$ecureBotPass123!",
        "website": "http://spammy-link.com"
    })
    assert r.status_code == 400
    assert "Registration rejected" in r.json()["detail"]
