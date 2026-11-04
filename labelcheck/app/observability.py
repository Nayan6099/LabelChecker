"""Logs and metrics.

- Logs: one JSON object per line in production (LABELCHECK_LOG_FORMAT=json), so CloudWatch, Loki or ELK
  can filter by field. Every request gets an id (X-Request-ID, taken from the proxy or generated),
  which appears in every log line written while handling it and in the response header.
- Metrics: Prometheus format at /metrics (web) and on LABELCHECK_WORKER_METRICS_PORT (worker).
"""
import contextvars
import json
import logging
import sys
import time

from prometheus_client import Counter, Gauge, Histogram

from .config import settings

request_id = contextvars.ContextVar("request_id", default="-")

HTTP_REQUESTS = Counter("labelcheck_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_SECONDS = Histogram("labelcheck_http_request_seconds", "HTTP request time", ["method", "route"],
                         buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
SCANS = Counter("labelcheck_scans_total", "Scans processed", ["status"])
SCAN_SECONDS = Histogram("labelcheck_scan_seconds", "Time to check one label",
                         buckets=(1, 2, 3, 4, 6, 8, 12, 20, 40, 80))
QUEUE = Gauge("labelcheck_queue", "Scans waiting or running", ["status"])
LOGINS = Counter("labelcheck_logins_total", "Login attempts", ["result"])
MAILS = Counter("labelcheck_mails_total", "E-mails queued", ["kind"])
WEBHOOKS = Counter("labelcheck_webhook_deliveries_total", "Webhook delivery attempts", ["result"])


class JsonFormatter(logging.Formatter):
    def format(self, r):
        d = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(r.created)) + f".{int(r.msecs):03d}Z",
             "level": r.levelname, "logger": r.name, "msg": r.getMessage(), "request_id": request_id.get()}
        for k in ("method", "path", "status", "ms", "user_id", "org_id", "ip", "scan_id"):
            if hasattr(r, k):
                d[k] = getattr(r, k)
        if r.exc_info:
            d["exc"] = self.formatException(r.exc_info)
        return json.dumps(d, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, r):
        base = super().format(r)
        rid = request_id.get()
        return f"{base} [{rid}]" if rid != "-" else base


def setup_logging():
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter() if settings.log_format == "json"
                   else TextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(settings.log_level)
    for noisy in ("uvicorn.access", "httpx", "botocore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
