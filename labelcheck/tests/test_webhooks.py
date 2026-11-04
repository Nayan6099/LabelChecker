"""Webhooks: signed delivery to a real local HTTP receiver, retries, SSRF refusal, auto-disable,
permissions and events from scans, reviews and batches."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import auth, db, pipeline, webhooks

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
PW = "Testpass123"


class Receiver:
    """A tiny HTTP server that records requests; `fail_next` makes it answer 500."""

    def __init__(self):
        self.calls, self.fail_next = [], 0
        rec = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                rec.calls.append({"headers": dict(self.headers), "body": body})
                code = 500 if rec.fail_next > 0 else 200
                rec.fail_next -= 1 if rec.fail_next > 0 else 0
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"ok" if code == 200 else b"boom")

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/hook"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def events(self):
        return [json.loads(c["body"])["event"] for c in self.calls]

    def wait(self, n, timeout=60):
        end = time.time() + timeout
        while len(self.calls) < n and time.time() < end:
            webhooks.deliver_due()
            time.sleep(0.2)
        return len(self.calls) >= n


@pytest.fixture()
def allow_local(monkeypatch):
    monkeypatch.setenv("LABELCHECK_ALLOW_PRIVATE_FETCH", "1")


@pytest.fixture(scope="module")
def recv():
    r = Receiver()
    yield r
    r.srv.shutdown()


def _org(prefix):
    oid = db.create_org(f"Hook {prefix}", "enterprise")
    for role in ("admin", "reviewer", "uploader"):
        db.create_user(oid, f"{role}@{prefix}.test", f"{role.title()} {prefix}", auth.hash_password(PW), role)
    return oid


def _h(client, email):
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"email": email, "password": PW})
    client.cookies.clear()
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture(scope="module")
def co(client):
    _org("wh")
    return {r: _h(client, f"{r}@wh.test") for r in ("admin", "reviewer", "uploader")}


def _create(client, co, url, events=("scan.completed",)):
    r = client.post("/api/webhooks", json={"url": url, "events": list(events)}, headers=co["admin"])
    assert r.status_code == 200, r.text
    return r.json()


def test_private_urls_refused_without_test_switch(client, co, monkeypatch):
    monkeypatch.delenv("LABELCHECK_ALLOW_PRIVATE_FETCH", raising=False)
    for url in ("http://127.0.0.1/hook", "http://169.254.169.254/latest", "http://10.0.0.5/x",
                "ftp://example.com/x", "http://localhost:8080/x"):
        r = client.post("/api/webhooks", json={"url": url, "events": ["scan.completed"]}, headers=co["admin"])
        assert r.status_code == 400, (url, r.text)


def test_only_admins_manage_webhooks(client, co):
    assert client.get("/api/webhooks", headers=co["uploader"]).status_code == 403
    assert client.get("/api/webhooks", headers=co["reviewer"]).status_code == 403
    assert client.get("/api/webhooks/events", headers=co["admin"]).json()["scan.completed"]


def test_unknown_event_rejected(client, co, allow_local, recv):
    r = client.post("/api/webhooks", json={"url": recv.url, "events": ["scan.exploded"]}, headers=co["admin"])
    assert r.status_code == 400 and "Unknown event" in r.text


def test_signed_delivery_scan_and_review_events(client, co, allow_local, recv):
    hook = _create(client, co, recv.url, ["scan.completed", "review.submitted", "review.approved"])
    secret = hook["secret"]
    assert secret.startswith("whsec_")
    assert "secret" not in client.get("/api/webhooks", headers=co["admin"]).json()[0]   # shown only once
    n0 = len(recv.calls)
    with open(SAMPLES / "good_label.png", "rb") as f:
        s = client.post("/api/scans", files={"image": ("good_label.png", f)}, headers=co["uploader"],
                        data={"product_name": "Roasted Peanuts", "panel_width_mm": "100", "panel_height_mm": "75",
                              "sku": "WH-1"}).json()
    assert recv.wait(n0 + 1)
    call = recv.calls[-1]
    body = json.loads(call["body"])
    assert body["event"] == "scan.completed" and body["data"]["scan_id"] == s["id"]
    assert body["data"]["sku"] == "WH-1" and body["data"]["verdict"] in ("needs_review", "no_issues_found")
    assert call["headers"]["X-LabelCheck-Event"] == "scan.completed"
    assert call["headers"]["X-LabelCheck-Delivery"] == body["id"]
    assert webhooks.verify(secret, call["headers"]["X-LabelCheck-Signature"], call["body"])
    assert not webhooks.verify("whsec_wrong", call["headers"]["X-LabelCheck-Signature"], call["body"])
    assert not webhooks.verify(secret, call["headers"]["X-LabelCheck-Signature"], call["body"] + b" ")
    # review flow
    client.post(f"/api/scans/{s['id']}/review", json={"action": "submit"}, headers=co["uploader"])
    full = client.get(f"/api/scans/{s['id']}", headers=co["reviewer"]).json()
    for f in full["result"]["findings"]:
        if f["status"] not in ("pass", "na"):
            client.put(f"/api/scans/{s['id']}/findings/{f['key']}", json={"decision": "ok"}, headers=co["reviewer"])
    r = client.post(f"/api/scans/{s['id']}/review", json={"action": "approve"}, headers=co["reviewer"])
    assert r.status_code == 200, r.text
    assert recv.wait(n0 + 3)
    assert recv.events()[-2:] == ["review.submitted", "review.approved"]
    approved = json.loads(recv.calls[-1]["body"])["data"]
    assert approved["review_status"] == "approved" and approved["by"] == "Reviewer wh"
    client.delete(f"/api/webhooks/{hook['id']}", headers=co["admin"])


def test_retry_after_failure_then_delivered(client, co, allow_local, recv):
    hook = _create(client, co, recv.url, ["scan.completed"])
    recv.fail_next = 1
    client.post(f"/api/webhooks/{hook['id']}/test", headers=co["admin"])
    n0 = len(recv.calls)
    assert recv.wait(n0 + 1)
    d = client.get(f"/api/webhooks/{hook['id']}/deliveries", headers=co["admin"]).json()[0]
    assert d["status"] == "pending" and d["attempts"] == 1 and d["last_code"] == 500   # retry scheduled
    with db.engine().begin() as c:      # pretend the retry time has come
        c.execute(db.webhook_deliveries.update().where(db.webhook_deliveries.c.id == d["id"])
                  .values(next_attempt_at=db.now()))
    assert recv.wait(n0 + 2)
    d = client.get(f"/api/webhooks/{hook['id']}/deliveries", headers=co["admin"]).json()[0]
    assert d["status"] == "delivered" and d["attempts"] == 2
    # manual redelivery of a delivered event
    assert client.post(f"/api/webhooks/{hook['id']}/deliveries/{d['id']}/retry", headers=co["admin"]).status_code == 200
    assert recv.wait(n0 + 3)
    assert recv.calls[-1]["headers"]["X-LabelCheck-Delivery"] == recv.calls[-2]["headers"]["X-LabelCheck-Delivery"]
    client.delete(f"/api/webhooks/{hook['id']}", headers=co["admin"])


def test_gives_up_after_all_retries_and_disables(client, co, allow_local, monkeypatch):
    hook = _create(client, co, "http://127.0.0.1:9/nothing-listens", ["scan.completed"])   # port 9: refused
    monkeypatch.setattr(webhooks, "DISABLE_AFTER", 3)
    client.post(f"/api/webhooks/{hook['id']}/test", headers=co["admin"])
    for _ in range(len(webhooks.RETRY_MINUTES) + 1):
        with db.engine().begin() as c:
            c.execute(db.webhook_deliveries.update().where(db.webhook_deliveries.c.webhook_id == hook["id"],
                                                           db.webhook_deliveries.c.status == "pending")
                      .values(next_attempt_at=db.now()))
        webhooks.deliver_due()
    d = client.get(f"/api/webhooks/{hook['id']}/deliveries", headers=co["admin"]).json()[0]
    assert d["status"] == "failed" and d["attempts"] >= 3
    h = client.get("/api/webhooks", headers=co["admin"]).json()
    h = next(x for x in h if x["id"] == hook["id"])
    assert h["active"] is False and "failed attempts" in h["disabled_reason"]
    # re-enabling resets the failure counter
    r = client.patch(f"/api/webhooks/{hook['id']}", json={"active": True}, headers=co["admin"])
    assert r.json()["active"] is True and r.json()["consecutive_failures"] == 0
    client.delete(f"/api/webhooks/{hook['id']}", headers=co["admin"])


def test_rotate_secret(client, co, allow_local, recv):
    hook = _create(client, co, recv.url)
    new = client.post(f"/api/webhooks/{hook['id']}/rotate-secret", headers=co["admin"]).json()["secret"]
    assert new != hook["secret"]
    client.post(f"/api/webhooks/{hook['id']}/test", headers=co["admin"])
    n0 = len(recv.calls)
    assert recv.wait(n0 + 1)
    sig = recv.calls[-1]["headers"]["X-LabelCheck-Signature"]
    assert webhooks.verify(new, sig, recv.calls[-1]["body"]) and not webhooks.verify(hook["secret"], sig,
                                                                                    recv.calls[-1]["body"])
    client.delete(f"/api/webhooks/{hook['id']}", headers=co["admin"])


def test_other_company_cannot_see_or_touch(client, co, allow_local, recv):
    hook = _create(client, co, recv.url)
    _org("wh2")
    other = _h(client, "admin@wh2.test")
    assert client.get("/api/webhooks", headers=other).json() == []
    assert client.delete(f"/api/webhooks/{hook['id']}", headers=other).status_code == 404
    assert client.get(f"/api/webhooks/{hook['id']}/deliveries", headers=other).status_code == 404
    client.delete(f"/api/webhooks/{hook['id']}", headers=co["admin"])


def test_batch_completed_event(client, co, allow_local, recv):
    hook = _create(client, co, recv.url, ["batch.completed"])
    files = [("files", ("good_label.png", (SAMPLES / "good_label.png").read_bytes())),
             ("files", ("bad_label.png", (SAMPLES / "bad_label.png").read_bytes()))]
    b = client.post("/api/batches", files=files, data={"panel_width_mm": "100", "panel_height_mm": "75"},
                    headers=co["uploader"]).json()
    assert pipeline.wait_for(b["scan_ids"], timeout=300)
    n0 = len([e for e in recv.events() if e == "batch.completed"])
    end = time.time() + 60
    while len([e for e in recv.events() if e == "batch.completed"]) <= n0 - 0 and time.time() < end:
        webhooks.deliver_due()
        if any(json.loads(c["body"])["event"] == "batch.completed" and
               json.loads(c["body"])["data"]["batch_id"] == b["id"] for c in recv.calls):
            break
        time.sleep(0.3)
    got = [json.loads(c["body"])["data"] for c in recv.calls if json.loads(c["body"])["event"] == "batch.completed"]
    mine = [g for g in got if g["batch_id"] == b["id"]]
    assert len(mine) == 1 and mine[0]["status_counts"] == {"done": 2}
    client.delete(f"/api/webhooks/{hook['id']}", headers=co["admin"])


def test_production_requires_https(client, co, allow_local, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "production", True)
    r = client.post("/api/webhooks", json={"url": "http://example.com/hook", "events": ["scan.completed"]},
                    headers=co["admin"])
    assert r.status_code == 400 and "https" in r.text


def test_production_refuses_private_fetch_switch(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("LABELCHECK_ENV", "production")
    monkeypatch.setenv("LABELCHECK_ALLOW_PRIVATE_FETCH", "1")
    assert any("ALLOW_PRIVATE_FETCH" in p for p in Settings().problems())
