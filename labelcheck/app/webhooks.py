"""Webhooks: tell a customer's own system (ERP, PLM, e-commerce back office) when something happens,
instead of it polling the API.

How it works (the "outbox" pattern used by payment providers):
1. An event (scan finished, label approved, ...) is written to `webhook_deliveries` in the database,
   one row per subscribed webhook. Writing never calls the network, so a slow customer server can
   never slow down a scan or a review.
2. Worker processes send due rows (`deliver_due`). A delivery is retried with growing delays
   (1 min, 5 min, 30 min, 2 h, 6 h, 12 h) if the customer's server is down or answers non-2xx.
   Rows survive restarts; several workers never send the same row twice at the same time.
3. Each request is signed, so the customer can check it really came from LabelCheck:
     X-LabelCheck-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>
   Receivers must recompute the HMAC over the raw body, compare in constant time, and reject
   timestamps older than 5 minutes (replay protection). Deliveries can arrive more than once:
   deduplicate on X-LabelCheck-Delivery.
4. Only public https URLs (http allowed outside production) on ports 80/443 are accepted, checked
   again on every send (SSRF protection). A webhook is switched off after 20 failed attempts in a row.
"""
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from datetime import timedelta

import httpx

from . import auth, db
from .config import settings
from .listing import FetchError, check_url
from .observability import WEBHOOKS

log = logging.getLogger("labelcheck.webhooks")

EVENTS = {
    "scan.completed": "A label scan finished (any result)",
    "scan.failed": "A file could not be processed",
    "review.submitted": "A label was submitted for review",
    "review.approved": "A reviewer approved a label",
    "review.changes_requested": "A reviewer sent a label back with requested changes",
    "batch.completed": "Every file of a bulk upload is finished",
}
RETRY_MINUTES = [1, 5, 30, 120, 360, 720]       # after attempt 1, 2, ... ; then 'failed'
DISABLE_AFTER = 20
TIMEOUT = 10
MAX_RESPONSE_LOG = 300
USER_AGENT = "LabelCheck-Webhooks/1.0"


class WebhookError(ValueError):
    pass


def new_secret():
    return "whsec_" + secrets.token_urlsafe(32)


def validate_url(url):
    url = (url or "").strip()
    if settings.production and not url.startswith("https://"):
        raise WebhookError("Webhook URLs must start with https:// in production.")
    try:
        check_url(url)
    except FetchError as e:
        raise WebhookError(str(e)) from None
    return url


def validate_events(events):
    events = sorted(set(events or []))
    bad = [e for e in events if e not in EVENTS]
    if bad:
        raise WebhookError(f"Unknown event(s): {', '.join(bad)}. Allowed: {', '.join(EVENTS)}.")
    if not events:
        raise WebhookError("Choose at least one event.")
    return events


def sign(secret, ts, body):
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret, header, body, tolerance=300, now=None):
    """Reference implementation for receivers (also used in tests)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((now or time.time()) - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, ts, body), header)


# ---------------- producing events ----------------

def scan_data(s):
    """Payload for scan events: what a customer system needs to act, plus links back."""
    failed = []
    if s.get("result"):
        failed = sorted({f["rule"] for f in s["result"]["findings"] if f["status"] == "fail"})
    base = settings.public_url
    return {"scan_id": s["id"], "file": s.get("filename"), "product": s.get("product"), "sku": s.get("sku"),
            "version": s.get("version"), "category": s.get("category"), "batch_id": s.get("batch_id"),
            "status": s.get("status"), "error": s.get("error"), "verdict": s.get("verdict"),
            "counts": {"fail": s.get("fails"), "check": s.get("warns"), "manual": s.get("manuals"),
                       "pass": s.get("passes")},
            "failed_rules": failed, "review_status": s.get("review_status"),
            "url": f"{base}/#/scan/{s['id']}", "api_url": f"{base}/api/scans/{s['id']}",
            "report_pdf": f"{base}/api/scans/{s['id']}/report.pdf"}


def emit(org_id, event, data):
    """Queue an event for every active webhook of the company subscribed to it. Never raises."""
    try:
        for h in db.hooks_for_event(org_id, event):
            uid = "dlv_" + uuid.uuid4().hex
            body = json.dumps({"id": uid, "event": event, "created_at": db.now().isoformat(timespec="seconds"),
                               "org_id": org_id, "data": data}, separators=(",", ":"), default=str)
            db.queue_delivery(h["id"], event, uid, body)
    except Exception:
        log.exception("could not queue webhook event %s for org %s", event, org_id)


def emit_scan(s, event):
    emit(s["org_id"], event, scan_data(s))


def send_test(hook):
    uid = "dlv_" + uuid.uuid4().hex
    data = {"message": "LabelCheck test event", "webhook_id": hook["id"]}
    body = json.dumps({"id": uid, "event": "ping", "created_at": db.now().isoformat(timespec="seconds"),
                       "org_id": hook["org_id"], "data": data}, separators=(",", ":"))
    return db.queue_delivery(hook["id"], "ping", uid, body)


# ---------------- delivering ----------------

def _send(hook, d, client):
    secret = auth.decrypt(hook["secret_enc"])
    body = d["payload"].encode()
    ts = int(time.time())
    validate_url(hook["url"])          # checked again on every send: DNS may have changed
    r = client.post(hook["url"], content=body, headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT,
        "X-LabelCheck-Event": d["event"], "X-LabelCheck-Delivery": d["uid"],
        "X-LabelCheck-Signature": sign(secret, ts, body)})
    return r.status_code, r.text[:MAX_RESPONSE_LOG]


def deliver(did, client=None):
    d = db.get_delivery(did)
    if not d:
        return None
    hook = db.get_webhook(d["webhook_id"])
    if not hook or not hook["active"]:
        db.finish_delivery(did, False, None, "Webhook removed or turned off.")
        return False
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
    code, err = None, None
    try:
        code, text = _send(hook, d, client)
        ok = 200 <= code < 300
        if not ok:
            err = f"HTTP {code}: {text}"
    except WebhookError as e:
        ok, err = False, f"URL refused: {e}"
    except httpx.TimeoutException:
        ok, err = False, f"Timed out after {TIMEOUT} s"
    except httpx.HTTPError as e:
        ok, err = False, f"{e.__class__.__name__}: {e}"[:300]
    finally:
        if own:
            client.close()
    attempt = d["attempts"] + 1
    retry_at = None
    if not ok and attempt <= len(RETRY_MINUTES):
        retry_at = db.now() + timedelta(minutes=RETRY_MINUTES[attempt - 1])
    db.finish_delivery(did, ok, code, err, retry_at)
    WEBHOOKS.labels("delivered" if ok else ("retry" if retry_at else "failed")).inc()
    if db.note_webhook_result(hook["id"], ok, DISABLE_AFTER):
        db.log_event(hook["org_id"], None, "webhook_disabled", "webhook", hook["id"],
                     {"reason": f"{DISABLE_AFTER} failed attempts in a row"})
        log.warning("webhook %s turned off after %s failures", hook["id"], DISABLE_AFTER)
    log.info("webhook delivery %s (%s) attempt %s: %s", did, d["event"], attempt,
             "delivered" if ok else f"failed ({err})")
    return ok


def deliver_due(limit=20):
    ids = db.claim_deliveries(limit)
    if not ids:
        return 0
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
        for did in ids:
            try:
                deliver(did, client)
            except Exception:
                log.exception("webhook delivery %s crashed", did)
                db.finish_delivery(did, False, None, "Internal error",
                                   db.now() + timedelta(minutes=RETRY_MINUTES[0]))
    return len(ids)
