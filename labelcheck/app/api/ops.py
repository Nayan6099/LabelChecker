"""Health probes, metrics and public metadata."""
import hmac
import logging
import shutil

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .. import db
from .. import rules_data as R
from ..config import settings
from ..observability import QUEUE
from ..storage import store

router = APIRouter(tags=["ops"])
VERSION = "0.4.0"


@router.get("/api/health")
@router.get("/api/health/live")
def live():
    """Liveness: the process answers. (Kubernetes livenessProbe / ALB health check.)"""
    return {"status": "ok", "version": VERSION, "ruleset": R.RULESET_VERSION}


@router.get("/api/health/ready")
def ready():
    """Readiness: database, file storage and OCR engine all usable. 503 if not."""
    checks = {}
    for name, fn in (("database", db.ping), ("storage", lambda: store().check()), ("ocr", _ocr_check)):
        try:
            fn()
            checks[name] = "ok"
        except Exception as e:
            checks[name] = f"error: {e.__class__.__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ok" if ok else "unavailable", "checks": checks}, status_code=200 if ok else 503)


def _ocr_check():
    if not shutil.which("tesseract"):
        raise RuntimeError("tesseract not installed")


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    if settings.metrics_token:
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given, settings.metrics_token):
            raise HTTPException(401, "Metrics token required.")
    try:
        QUEUE.labels("queued").set(0)
        QUEUE.labels("processing").set(0)
        for k, v in db.queue_depth().items():
            QUEUE.labels(k).set(v)
    except Exception:
        logging.getLogger("labelcheck.web").warning("queue depth not available for /metrics")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/api/meta")
def meta():
    return {"version": VERSION,
            "ruleset": {"version": R.RULESET_VERSION, "note": R.RULESET_NOTE},
            "categories": {k: v["label"] for k, v in R.CATEGORIES.items()},
            "rules": R.RULES, "four_eyes": settings.four_eyes, "plans": db.PLANS,
            "email_enabled": bool(settings.smtp_host)}
