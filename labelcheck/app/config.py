"""All settings in one place, read from environment variables (12-factor style).

`LABELCHECK_ENV=production` turns on safety checks: the app refuses to start with
settings that are unsafe on the internet (see `problems()`), so a wrong deployment
fails loudly at start-up instead of running insecurely.
"""
import os
from pathlib import Path


def _env(name, default=None):
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _bool(name, default):
    return str(_env(name, "1" if default else "0")).strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    v = _env(name)
    return int(v) if v is not None else default


class Settings:
    def __init__(self):
        self.reload()

    def reload(self):
        """Re-read the environment (used by tests)."""
        self.env = _env("LABELCHECK_ENV", "development")
        self.production = self.env == "production"
        root = Path(__file__).resolve().parent.parent
        self.data_dir = Path(_env("LABELCHECK_DATA", root / "data"))
        self.database_url = _env("DATABASE_URL")
        self.public_url = (_env("LABELCHECK_PUBLIC_URL", "http://localhost:8000")).rstrip("/")
        self.secret_key = _env("LABELCHECK_SECRET_KEY")
        # web / sessions
        self.cookie_secure = _bool("LABELCHECK_COOKIE_SECURE", True)
        self.trust_proxy = _bool("LABELCHECK_TRUST_PROXY", False)
        self.session_hours = _int("LABELCHECK_SESSION_HOURS", 12)
        self.four_eyes = _bool("LABELCHECK_FOUR_EYES", True)
        self.enable_docs = _bool("LABELCHECK_ENABLE_DOCS", True)
        self.metrics_token = _env("LABELCHECK_METRICS_TOKEN")
        self.log_format = _env("LABELCHECK_LOG_FORMAT", "json" if self.production else "text")
        self.log_level = _env("LABELCHECK_LOG_LEVEL", "INFO")
        # workers
        self.workers = _int("LABELCHECK_WORKERS", 2)
        self.stale_minutes = _int("LABELCHECK_STALE_MINUTES", 15)
        self.maintenance_minutes = _int("LABELCHECK_MAINTENANCE_MINUTES", 60)
        self.worker_metrics_port = _int("LABELCHECK_WORKER_METRICS_PORT", 0)
        # storage
        self.storage = _env("LABELCHECK_STORAGE", "local")          # local | s3
        self.s3_bucket = _env("LABELCHECK_S3_BUCKET")
        self.s3_prefix = _env("LABELCHECK_S3_PREFIX", "labelcheck/")
        self.s3_endpoint = _env("LABELCHECK_S3_ENDPOINT")            # MinIO / other S3-compatible
        self.s3_region = _env("AWS_REGION", _env("AWS_DEFAULT_REGION"))
        # email
        self.smtp_host = _env("LABELCHECK_SMTP_HOST")
        self.smtp_port = _int("LABELCHECK_SMTP_PORT", 587)
        self.smtp_user = _env("LABELCHECK_SMTP_USER")
        self.smtp_password = _env("LABELCHECK_SMTP_PASSWORD")
        self.smtp_tls = _env("LABELCHECK_SMTP_TLS", "starttls")      # starttls | ssl | none
        self.mail_from = _env("LABELCHECK_MAIL_FROM", "LabelCheck <no-reply@localhost>")
        # limits
        self.rate_upload_per_min = _int("LABELCHECK_RATE_UPLOADS_PER_MIN", 120)   # per company
        self.rate_api_per_min = _int("LABELCHECK_RATE_API_PER_MIN", 600)          # per API key
        # payments (Razorpay)
        self.razorpay_key_id = _env("LABELCHECK_RAZORPAY_KEY_ID")
        self.razorpay_key_secret = _env("LABELCHECK_RAZORPAY_KEY_SECRET")
        self.razorpay_webhook_secret = _env("LABELCHECK_RAZORPAY_WEBHOOK_SECRET")
        # bootstrap
        self.admin_email = _env("LABELCHECK_ADMIN_EMAIL")
        self.admin_password = _env("LABELCHECK_ADMIN_PASSWORD")
        self.org_name = _env("LABELCHECK_ORG_NAME", "My Company")
        self.platform_admin = _bool("LABELCHECK_ADMIN_IS_PLATFORM", True)
        return self

    def problems(self):
        """Settings that are unsafe in production. Empty list = OK."""
        out = []
        if not self.production:
            return out
        if not self.cookie_secure:
            out.append("LABELCHECK_COOKIE_SECURE must be 1 (cookies only over HTTPS).")
        if not self.database_url or self.database_url.startswith("sqlite"):
            out.append("DATABASE_URL must point to PostgreSQL in production.")
        if not self.secret_key or len(self.secret_key) < 32:
            out.append("LABELCHECK_SECRET_KEY must be set (32+ random characters).")
        if not self.public_url.startswith("https://"):
            out.append("LABELCHECK_PUBLIC_URL must be the https:// address users open.")
        if _bool("LABELCHECK_ALLOW_PRIVATE_FETCH", False):
            out.append("LABELCHECK_ALLOW_PRIVATE_FETCH must not be set in production (allows internal requests).")
        if self.storage == "s3" and not self.s3_bucket:
            out.append("LABELCHECK_S3_BUCKET is required when LABELCHECK_STORAGE=s3.")
        return out

    def warnings(self):
        out = []
        if self.production and not self.smtp_host:
            out.append("No SMTP server set: password-reset and invite e-mails are only written to the log.")
        if self.production and not self.metrics_token:
            out.append("/metrics has no token: keep it unreachable from the internet (proxy blocks it).")
        if not self.secret_key:
            out.append("No LABELCHECK_SECRET_KEY: a development key is used for 2FA secrets.")
        return out


settings = Settings()
