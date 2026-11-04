"""LabelCheck web application (API + web UI).

Run (development):  uvicorn app.main:app --reload
Run (production):   see README / docker-compose.yml (web and worker as separate containers).
Routes live in app/api/*. Settings in app/config.py.
"""
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, db, pipeline
from .api import auth_routes, batches, billing, ops, platform, scans, users, webhook_routes
from .api.ops import VERSION
from .config import settings
from .observability import HTTP_REQUESTS, HTTP_SECONDS, request_id, setup_logging

log = logging.getLogger("labelcheck.web")
STATIC = Path(__file__).resolve().parent / "static"
MAX_REQUEST = 27 * 1024 * 1024        # one file (25 MB) + form fields
MAX_BATCH_REQUEST = 400 * 1024 * 1024


def bootstrap_admin():
    """First company + admin from env vars, only when the database has no users yet."""
    if settings.admin_email and settings.admin_password and db.count_users() == 0:
        problem = auth.password_problem(settings.admin_password)
        if problem:
            raise SystemExit(f"LABELCHECK_ADMIN_PASSWORD: {problem}")
        org = db.create_org(settings.org_name, "enterprise")
        uid = db.create_user(org, settings.admin_email, "Administrator", auth.hash_password(settings.admin_password),
                             "admin", platform_admin=settings.platform_admin)
        db.log_event(org, uid, "bootstrap_admin", "user", uid)
        log.info("first admin %s created", settings.admin_email)


@asynccontextmanager
async def lifespan(_app):
    setup_logging()
    bad = settings.problems()
    if bad:
        raise SystemExit("Refusing to start with unsafe production settings:\n- " + "\n- ".join(bad))
    for w in settings.warnings():
        log.warning(w)
    db.migrate()
    bootstrap_admin()
    pipeline.start()          # LABELCHECK_WORKERS=0 when workers run as their own process
    log.info("LabelCheck %s started (%s)", VERSION, settings.env)
    yield
    pipeline.stop()


app = FastAPI(title="LabelCheck API", version=VERSION, lifespan=lifespan,
              docs_url="/api/docs" if settings.enable_docs else None, redoc_url=None,
              openapi_url="/api/openapi.json" if settings.enable_docs else None,
              description="Label and listing compliance checker (Legal Metrology + FSSAI).\n\n"
                          "Authenticate with an API key: header `X-API-Key: lc_...` "
                          "(create one under Company > API keys).")

CSP = ("default-src 'self'; img-src 'self' data: blob: https://cdn.razorpay.com; style-src 'self' 'unsafe-inline'; "
       "script-src 'self' 'unsafe-inline' https://checkout.razorpay.com; "
       "connect-src 'self' https://api.razorpay.com https://lumberjack.razorpay.com; "
       "frame-src 'self' https://api.razorpay.com https://checkout.razorpay.com; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
# Swagger UI loads its script and style from jsdelivr; allow that on the docs page only.
CSP_DOCS = ("default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; frame-ancestors 'none'")


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = (request.headers.get("x-request-id") or uuid.uuid4().hex)[:64]
    token = request_id.set(rid)
    t0 = time.perf_counter()
    path = request.url.path
    size = request.headers.get("content-length")
    try:
        limit = MAX_BATCH_REQUEST if path == "/api/batches" else MAX_REQUEST
        if size and size.isdigit() and int(size) > limit:
            resp = JSONResponse({"detail": f"Request too large (limit {limit // 1024 // 1024} MB)."}, 413)
        else:
            resp = await call_next(request)
    except Exception:
        log.exception("unhandled error on %s %s", request.method, path)
        resp = JSONResponse({"detail": "Internal error. Quote this id to support.", "request_id": rid}, 500)
    ms = (time.perf_counter() - t0) * 1000
    route = getattr(request.scope.get("route"), "path", None) or ("static" if not path.startswith("/api") else "other")
    HTTP_REQUESTS.labels(request.method, route, str(resp.status_code)).inc()
    HTTP_SECONDS.labels(request.method, route).observe(ms / 1000)
    p = getattr(request.state, "principal", None) or {}
    if path.startswith("/api") and path not in ("/api/health", "/api/health/live", "/api/health/ready"):
        log.info("%s %s %s %.0fms", request.method, path, resp.status_code, ms,
                 extra={"method": request.method, "path": path, "status": resp.status_code, "ms": round(ms),
                        "user_id": p.get("id"), "org_id": p.get("org_id"), "ip": auth.client_ip(request)})
    resp.headers["X-Request-ID"] = rid
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Content-Security-Policy"] = CSP_DOCS if path == "/api/docs" else CSP
    if path.startswith("/api/") and "cache-control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-store"
    request_id.reset(token)
    return resp


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    msgs = [f"{'.'.join(str(x) for x in e['loc'][1:]) or 'body'}: {e['msg']}" for e in exc.errors()]
    return JSONResponse({"detail": "; ".join(msgs)}, status_code=422)


for r in (ops.router, auth_routes.router, users.router, platform.router, scans.router, batches.router,
          webhook_routes.router, billing.router):
    app.include_router(r)

app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
