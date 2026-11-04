"""Shared test set-up: an isolated data folder and database for the whole test run.
Set TEST_DATABASE_URL=postgresql+psycopg://... to run every test on PostgreSQL."""
import os
import tempfile

os.environ["LABELCHECK_DATA"] = tempfile.mkdtemp()
os.environ["LABELCHECK_COOKIE_SECURE"] = "0"
os.environ["LABELCHECK_WORKERS"] = "2"
os.environ["LABELCHECK_ENV"] = "test"
os.environ["LABELCHECK_PUBLIC_URL"] = "http://testserver"
for k in ("DATABASE_URL", "LABELCHECK_SMTP_HOST", "LABELCHECK_STORAGE", "LABELCHECK_ADMIN_EMAIL"):
    os.environ.pop(k, None)
if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:   # runs start-up: migrations, workers
        yield c
