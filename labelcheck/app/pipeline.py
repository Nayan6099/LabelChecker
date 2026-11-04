"""Processing pipeline + background workers.

A scan row is created first (status 'queued'); a worker takes it, runs
prepare -> OCR -> extract -> listing -> rules, and stores the result.
The queue is the database itself, so queued work survives a restart and several workers
on several servers can share it safely (see db.claim_next).

Workers run either inside the web process (LABELCHECK_WORKERS > 0, simple installs) or as
a separate process: `python -m app.worker` (production: scale web and workers separately).
"""
import logging
import os
import socket
import threading
import time
import traceback

import cv2

from . import db, engine, extract, inputs, listing, mailer, ocr, webhooks
from .config import settings
from .observability import QUEUE, SCAN_SECONDS, SCANS
from .storage import store

log = logging.getLogger("labelcheck.worker")
WEBHOOK_POLL_SECONDS = float(os.environ.get("LABELCHECK_WEBHOOK_POLL_SECONDS", "3"))
_stop = threading.Event()
_threads = []
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def process(sid):
    """Run the full check for one scan. Never raises: errors are stored on the scan."""
    t0 = time.monotonic()
    s = db.get_scan(sid)
    if not s:
        return
    try:
        opts = dict(s["options"])
        data = store().get(s["upload_path"])
        img, updates, notes, _ = inputs.prepare(data, opts)
        opts.update(updates)
        o = ocr.read_label(img)
        ext = extract.extract_from_ocr(o)

        lst = s["listing"] or {}
        listing_ext = None
        if lst.get("text"):
            listing_ext = extract.extract_from_text(lst["text"])
            lst["extracted"] = listing_ext

        result = engine.run(o, ext, img, opts, listing_ext)
        result["options_used"] = opts

        ok, png = cv2.imencode(".png", img)
        key = f"checked/{s['org_id']}/{sid}.png"
        store().put(key, png.tobytes(), "image/png")
        ms = int((time.monotonic() - t0) * 1000)
        db.finish_scan(sid, key, o, ext, result, lst or None, notes, ms)
        SCANS.labels("done").inc()
        SCAN_SECONDS.observe(ms / 1000)
        log.info("scan %s done in %d ms (%s)", sid, ms, result["summary"]["verdict"])
    except inputs.InputError as e:
        db.fail_scan(sid, str(e), int((time.monotonic() - t0) * 1000))
        SCANS.labels("rejected").inc()
    except Exception as e:  # keep the worker alive; store the reason for the user
        log.error("scan %s failed: %s", sid, traceback.format_exc())
        db.fail_scan(sid, f"Processing error: {e.__class__.__name__}", int((time.monotonic() - t0) * 1000))
        SCANS.labels("error").inc()
    done = db.get_scan(sid)
    if done:
        webhooks.emit_scan(done, "scan.completed" if done["status"] == "done" else "scan.failed")
    _maybe_batch_done(s)


def _maybe_batch_done(s):
    """E-mail the uploader once when the last scan of a batch finishes."""
    bid = s.get("batch_id")
    if not bid:
        return
    try:
        b = db.get_batch(bid, s["org_id"])
        counts = b["status_counts"]
        if counts.get("queued", 0) or counts.get("processing", 0):
            return
        ok, _ = db.hit("batch-mail", bid, 1, 60 * 60 * 24 * 30)
        if not ok:
            return
        webhooks.emit(s["org_id"], "batch.completed", {
            "batch_id": bid, "name": b.get("name"), "total": b.get("total"), "status_counts": counts,
            "url": f"{settings.public_url}/#/batch/{bid}",
            "summary_csv": f"{settings.public_url}/api/batches/{bid}/summary.csv"})
        u = db.user_by_id(b["user_id"])
        if u and u["active"] and u["notify"]:
            mailer.batch_done(u["email"], u["name"], b, counts, f"{settings.public_url}/#/batch/{bid}")
    except Exception:
        log.exception("batch notification failed")


def maintenance():
    """Housekeeping, run by workers every LABELCHECK_MAINTENANCE_MINUTES (safe to run on many workers)."""
    out = {"requeued": db.requeue_stale(settings.stale_minutes)}
    out.update(db.purge_expired())
    deleted = 0
    for sid, org_id in db.scans_past_retention():
        db.delete_scan(sid)
        db.log_event(org_id, None, "scan_deleted_retention", "scan", sid)
        deleted += 1
    out["retention_deleted"] = deleted
    out["webhook_deliveries_purged"] = db.purge_old_deliveries()
    out["subscriptions_expired"] = db.expire_subscriptions()
    QUEUE.labels("queued").set(0)
    QUEUE.labels("processing").set(0)
    for k, v in db.queue_depth().items():
        QUEUE.labels(k).set(v)
    if any(out.values()):
        log.info("maintenance: %s", out)
    return out


def _loop(n):
    wid = f"{WORKER_ID}#{n}"
    while not _stop.is_set():
        try:
            sid = db.claim_next(wid)
        except Exception:
            log.exception("queue read failed; retrying")
            _stop.wait(5)
            continue
        if sid is None:
            _stop.wait(1.0)
            continue
        process(sid)


def _webhook_loop():
    """Send due webhook deliveries every few seconds (safe on many workers)."""
    while not _stop.wait(WEBHOOK_POLL_SECONDS):
        try:
            webhooks.deliver_due()
        except Exception:
            log.exception("webhook delivery loop failed")


def _maintenance_loop():
    while not _stop.wait(settings.maintenance_minutes * 60):
        try:
            maintenance()
        except Exception:
            log.exception("maintenance failed")


def start(workers=None):
    workers = settings.workers if workers is None else workers
    if _threads or workers <= 0:
        return
    _stop.clear()
    try:
        maintenance()        # requeues scans left by a crashed worker, cleans up
    except Exception:
        log.exception("start-up maintenance failed")
    for i in range(workers):
        t = threading.Thread(target=_loop, args=(i,), name=f"labelcheck-worker-{i}", daemon=True)
        t.start()
        _threads.append(t)
    for target, name in ((_maintenance_loop, "labelcheck-maintenance"), (_webhook_loop, "labelcheck-webhooks")):
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        _threads.append(t)
    log.info("%d scan worker(s) started (%s)", workers, WORKER_ID)


def stop(timeout=60):
    """Graceful stop: workers finish the scan in hand (up to `timeout` s) and take no new ones."""
    _stop.set()
    for t in _threads:
        t.join(timeout=timeout)
    _threads.clear()


def wait_for(sids, timeout=600):
    """Tests/CLI: wait until the scans are no longer queued/processing."""
    end = time.time() + timeout
    while time.time() < end:
        if all(db.get_scan(s)["status"] in ("done", "error") for s in sids):
            return True
        time.sleep(0.5)
    return False
