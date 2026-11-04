"""Standalone scan worker: `python -m app.worker`.

Runs LABELCHECK_WORKERS scan threads (OCR is CPU-heavy: about one per CPU core) plus housekeeping.
Stops gracefully on SIGTERM / SIGINT (finishes the scan in hand). With
LABELCHECK_WORKER_METRICS_PORT set, Prometheus metrics are served on that port.
"""
import logging
import signal
import threading

from prometheus_client import start_http_server

from . import db, pipeline
from .config import settings
from .observability import setup_logging

log = logging.getLogger("labelcheck.worker")


def main():
    setup_logging()
    bad = settings.problems()
    if bad:
        raise SystemExit("Unsafe production settings:\n- " + "\n- ".join(bad))
    db.migrate()
    if settings.worker_metrics_port:
        start_http_server(settings.worker_metrics_port)
        log.info("worker metrics on :%d/metrics", settings.worker_metrics_port)
    done = threading.Event()

    def _stop(signum, _frame):
        log.info("signal %s: finishing current scans, then stopping", signum)
        done.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    pipeline.start(max(1, settings.workers))
    done.wait()
    pipeline.stop()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
