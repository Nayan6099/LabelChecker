"""Database layer (SQLAlchemy Core). Works with SQLite (development) and PostgreSQL (production).

DATABASE_URL, e.g. postgresql+psycopg://user:pass@host:5432/labelcheck.
Default: a SQLite file in LABELCHECK_DATA.

The schema is managed by Alembic migrations (migrations/). `migrate()` runs them on
start-up; the tables below must always match the latest migration (a test checks this).

Every query that returns customer data is filtered by org_id, so one company can never
see another company's data.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Index, Integer, MetaData, String, Table,
                        Text, UniqueConstraint, and_, create_engine, delete, event, func, insert,
                        inspect, or_, select, update)

from .config import settings

logger = logging.getLogger("labelcheck.db")
md = MetaData()
ROOT = Path(__file__).resolve().parent.parent


def now():
    return datetime.now(timezone.utc)


def month_key(d=None):
    return (d or now()).strftime("%Y-%m")


orgs = Table("orgs", md,
             Column("id", Integer, primary_key=True),
             Column("name", String(200), nullable=False),
             Column("created_at", DateTime(timezone=True), default=now),
             Column("plan", String(20), nullable=False, server_default="trial"),
             Column("scan_quota", Integer),            # per calendar month; NULL = plan default
             Column("user_limit", Integer),            # NULL = plan default
             Column("retention_days", Integer),        # NULL = keep forever
             Column("require_2fa", Boolean, nullable=False, server_default="0"),
             Column("active", Boolean, nullable=False, server_default="1"))

users = Table("users", md,
              Column("id", Integer, primary_key=True),
              Column("org_id", ForeignKey("orgs.id"), nullable=False, index=True),
              Column("email", String(254), nullable=False, unique=True),
              Column("name", String(200), nullable=False),
              Column("password_hash", String(255), nullable=False),
              Column("role", String(20), nullable=False),  # admin | reviewer | uploader
              Column("active", Boolean, default=True, nullable=False),
              Column("terms_accepted_at", DateTime(timezone=True)),
              Column("created_at", DateTime(timezone=True), default=now),
              Column("is_platform_admin", Boolean, nullable=False, server_default="0"),
              Column("totp_secret", Text),                 # encrypted (Fernet, LABELCHECK_SECRET_KEY)
              Column("totp_enabled", Boolean, nullable=False, server_default="0"),
              Column("notify", Boolean, nullable=False, server_default="1"),
              Column("last_login_at", DateTime(timezone=True)))

sessions = Table("sessions", md,
                 Column("id", Integer, primary_key=True),
                 Column("token_hash", String(64), nullable=False, unique=True),
                 Column("user_id", ForeignKey("users.id"), nullable=False, index=True),
                 Column("created_at", DateTime(timezone=True), default=now),
                 Column("expires_at", DateTime(timezone=True), nullable=False))

products = Table("products", md,
                 Column("id", Integer, primary_key=True),
                 Column("org_id", ForeignKey("orgs.id"), nullable=False, index=True),
                 Column("key", String(200), nullable=False),
                 Column("name", String(200)),
                 Column("created_at", DateTime(timezone=True), default=now),
                 UniqueConstraint("org_id", "key"))

batches = Table("batches", md,
                Column("id", Integer, primary_key=True),
                Column("org_id", ForeignKey("orgs.id"), nullable=False, index=True),
                Column("user_id", ForeignKey("users.id"), nullable=False),
                Column("name", String(200)),
                Column("total", Integer, default=0),
                Column("created_at", DateTime(timezone=True), default=now))

api_keys = Table("api_keys", md,
                 Column("id", Integer, primary_key=True),
                 Column("org_id", ForeignKey("orgs.id"), nullable=False, index=True),
                 Column("name", String(100), nullable=False),
                 Column("prefix", String(16), nullable=False, unique=True),
                 Column("key_hash", String(64), nullable=False),
                 Column("scopes", String(200), nullable=False),     # space separated
                 Column("created_by", ForeignKey("users.id"), nullable=False),
                 Column("created_at", DateTime(timezone=True), default=now),
                 Column("last_used_at", DateTime(timezone=True)),
                 Column("revoked_at", DateTime(timezone=True)))

scans = Table("scans", md,
              Column("id", Integer, primary_key=True),
              Column("org_id", ForeignKey("orgs.id"), nullable=False, index=True),
              Column("user_id", ForeignKey("users.id"), nullable=False),
              Column("product_id", ForeignKey("products.id"), index=True),
              Column("version", Integer, default=1),
              Column("batch_id", ForeignKey("batches.id"), index=True),
              Column("created_at", DateTime(timezone=True), default=now),
              Column("product", String(200)),
              Column("sku", String(100)),
              Column("category", String(40)),
              Column("filename", String(255)),
              Column("source_type", String(10)),
              Column("upload_path", Text),          # storage key of the original upload
              Column("image_path", Text),           # storage key of the checked image
              Column("status", String(12), nullable=False, default="queued"),
              Column("error", Text),
              Column("verdict", String(20)),
              Column("fails", Integer, default=0),
              Column("warns", Integer, default=0),
              Column("manuals", Integer, default=0),
              Column("passes", Integer, default=0),
              Column("options", Text),
              Column("ocr", Text),
              Column("extracted", Text),
              Column("result", Text),
              Column("listing", Text),
              Column("prep", Text),
              Column("review_status", String(20), nullable=False, default="not_submitted"),
              Column("reviewed_by", ForeignKey("users.id")),
              Column("reviewed_at", DateTime(timezone=True)),
              Column("claimed_at", DateTime(timezone=True)),
              Column("worker", String(64)),
              Column("api_key_id", ForeignKey("api_keys.id", name="fk_scans_api_key")),
              Column("duration_ms", Integer))
Index("ix_scans_status", scans.c.status, scans.c.id)
Index("ix_scans_org_created", scans.c.org_id, scans.c.created_at)

finding_reviews = Table("finding_reviews", md,
                        Column("id", Integer, primary_key=True),
                        Column("scan_id", ForeignKey("scans.id"), nullable=False, index=True),
                        Column("finding_key", String(40), nullable=False),
                        Column("decision", String(12), nullable=False),
                        Column("comment", Text),
                        Column("user_id", ForeignKey("users.id"), nullable=False),
                        Column("created_at", DateTime(timezone=True), default=now))

comments = Table("comments", md,
                 Column("id", Integer, primary_key=True),
                 Column("scan_id", ForeignKey("scans.id"), nullable=False, index=True),
                 Column("user_id", ForeignKey("users.id"), nullable=False),
                 Column("text", Text, nullable=False),
                 Column("created_at", DateTime(timezone=True), default=now))

audit = Table("audit_log", md,
              Column("id", Integer, primary_key=True),
              Column("org_id", ForeignKey("orgs.id"), index=True),
              Column("user_id", ForeignKey("users.id")),
              Column("action", String(60), nullable=False),
              Column("target_type", String(30)),
              Column("target_id", Integer),
              Column("detail", Text),
              Column("ip", String(64)),
              Column("created_at", DateTime(timezone=True), default=now))

tokens = Table("tokens", md,
               Column("id", Integer, primary_key=True),
               Column("token_hash", String(64), nullable=False, unique=True),
               Column("user_id", ForeignKey("users.id"), nullable=False, index=True),
               Column("kind", String(20), nullable=False),     # reset | invite
               Column("created_at", DateTime(timezone=True), default=now),
               Column("expires_at", DateTime(timezone=True), nullable=False),
               Column("used_at", DateTime(timezone=True)))

rate_limits = Table("rate_limits", md,
                    Column("key", String(200), primary_key=True),   # name:subject:window-number
                    Column("count", Integer, nullable=False),
                    Column("expires_at", DateTime(timezone=True), nullable=False, index=True))

usage = Table("usage", md,
              Column("org_id", ForeignKey("orgs.id"), primary_key=True),
              Column("month", String(7), primary_key=True),
              Column("scans", Integer, nullable=False, server_default="0"))

webhooks = Table("webhooks", md,
                 Column("id", Integer, primary_key=True),
                 Column("org_id", ForeignKey("orgs.id"), nullable=False, index=True),
                 Column("url", String(2000), nullable=False),
                 Column("events", String(300), nullable=False),          # comma list, see webhooks.EVENTS
                 Column("secret_enc", Text, nullable=False),              # signing secret, encrypted
                 Column("active", Boolean, nullable=False, server_default="1"),
                 Column("description", String(200)),
                 Column("created_by", ForeignKey("users.id"), nullable=False),
                 Column("created_at", DateTime(timezone=True), default=now),
                 Column("consecutive_failures", Integer, nullable=False, server_default="0"),
                 Column("disabled_reason", String(300)))

webhook_deliveries = Table("webhook_deliveries", md,
                           Column("id", Integer, primary_key=True),
                           Column("webhook_id", ForeignKey("webhooks.id", ondelete="CASCADE"), nullable=False,
                                  index=True),
                           Column("event", String(40), nullable=False),
                           Column("uid", String(40), nullable=False, unique=True),   # sent as X-LabelCheck-Delivery
                           Column("payload", Text, nullable=False),
                           Column("status", String(12), nullable=False, server_default="pending"),
                           # pending | sending | delivered | failed
                           Column("attempts", Integer, nullable=False, server_default="0"),
                           Column("next_attempt_at", DateTime(timezone=True), nullable=False),
                           Column("claimed_at", DateTime(timezone=True)),
                           Column("last_code", Integer),
                           Column("last_error", String(500)),
                           Column("created_at", DateTime(timezone=True), default=now),
                           Column("delivered_at", DateTime(timezone=True)),
                           Index("ix_whd_due", "status", "next_attempt_at"))

PLANS = {
    "trial": {"label": "Trial", "scans": 100, "users": 3},
    "standard": {"label": "Standard", "scans": 2000, "users": 10},
    "business": {"label": "Business", "scans": 10000, "users": 50},
    "enterprise": {"label": "Enterprise", "scans": None, "users": None},
}

_engine = None


def database_url():
    if settings.database_url:
        return settings.database_url
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{settings.data_dir / 'labelcheck.db'}"


def engine():
    global _engine
    if _engine is None:
        url = database_url()
        kw = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kw.update(pool_size=10, max_overflow=10, pool_recycle=1800)
        _engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _pragmas(con, _):
                cur = con.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
    return _engine


def is_sqlite():
    return engine().dialect.name == "sqlite"


MIGRATION_LOCK_ID = 0x4C41424C  # any constant shared by all LabelCheck processes ("LABL")


def migrate():
    """Bring the schema up to date with Alembic. A database made by version 0.2 (tables but no
    alembic_version) is first marked as revision 0001, then upgraded."""
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    with engine().connect() as conn:
        if conn.dialect.name == "sqlite":
            # SQLite rebuilds tables to alter them; foreign-key checks must be off meanwhile
            # (PRAGMA only works outside a transaction).
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            conn.commit()                       # end the autobegun transaction
        with conn.begin():
            if conn.dialect.name == "postgresql":
                # web + every worker run this at start-up; without a lock they race to create
                # the same tables on a fresh database and all but one crash. The lock is held
                # until this transaction ends (Postgres DDL is transactional), so the others
                # wait, then find the schema already at head.
                conn.exec_driver_sql(f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_ID})")
            cfg.attributes["connection"] = conn
            names = set(inspect(conn).get_table_names())
            if "scans" in names and "alembic_version" not in names:
                logger.info("existing 0.2 database found: marking it as revision 0001")
                command.stamp(cfg, "0001")
            command.upgrade(cfg, "head")
        if conn.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.commit()


def reset_engine():
    """Tests only: forget the engine so a new DATABASE_URL / data dir is used."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def ping():
    with engine().connect() as c:
        c.execute(select(1))


def _row(r):
    return dict(r._mapping) if r is not None else None


def _aware(v):
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)   # SQLite returns naive datetimes; stored in UTC
    return v


def _iso(v):
    v = _aware(v)
    return v.isoformat(timespec="seconds") if isinstance(v, datetime) else v


def _clean(d):
    return {k: _iso(v) for k, v in d.items()} if d else d


# ---------------- orgs ----------------

def create_org(name, plan="trial"):
    with engine().begin() as c:
        return c.execute(insert(orgs).values(name=name, plan=plan, created_at=now())).inserted_primary_key[0]


def get_org(org_id):
    with engine().connect() as c:
        return _clean(_row(c.execute(select(orgs).where(orgs.c.id == org_id)).first()))


def update_org(org_id, **values):
    with engine().begin() as c:
        c.execute(update(orgs).where(orgs.c.id == org_id).values(**values))


def org_limits(org):
    plan = PLANS.get(org["plan"], PLANS["trial"])
    return {"plan": org["plan"], "plan_label": plan["label"],
            "scan_quota": org["scan_quota"] if org["scan_quota"] is not None else plan["scans"],
            "user_limit": org["user_limit"] if org["user_limit"] is not None else plan["users"]}


def list_orgs(limit=None, offset=0):
    m = month_key()
    with engine().connect() as c:
        q = select(orgs).order_by(orgs.c.id)
        if limit is not None:
            q = q.offset(offset).limit(limit)
        rows = [_clean(_row(r)) for r in c.execute(q)]
        ucount = dict(c.execute(select(users.c.org_id, func.count()).where(users.c.active.is_(True))
                                .group_by(users.c.org_id)).all())
        used = dict(c.execute(select(usage.c.org_id, usage.c.scans).where(usage.c.month == m)).all())
    for o in rows:
        o.update(org_limits(o), users=ucount.get(o["id"], 0), scans_this_month=used.get(o["id"], 0))
    return rows


def count_orgs():
    with engine().connect() as c:
        return int(c.execute(select(func.count(orgs.c.id))).scalar() or 0)


def org_name(org_id):
    with engine().connect() as c:
        return c.execute(select(orgs.c.name).where(orgs.c.id == org_id)).scalar()


# ---------------- usage / quota ----------------

def usage_this_month(org_id):
    with engine().connect() as c:
        return c.execute(select(usage.c.scans).where(usage.c.org_id == org_id,
                                                     usage.c.month == month_key())).scalar() or 0


def usage_history(org_id, months=12):
    with engine().connect() as c:
        return [{"month": r[0], "scans": r[1]} for r in c.execute(
            select(usage.c.month, usage.c.scans).where(usage.c.org_id == org_id)
            .order_by(usage.c.month.desc()).limit(months))]


def reserve_scans(org_id, n):
    """Count n scans against this month's quota, atomically. Returns (ok, used_after, quota)."""
    org = get_org(org_id)
    quota = org_limits(org)["scan_quota"]
    m = month_key()
    with engine().begin() as c:
        cur = c.execute(select(usage.c.scans).where(usage.c.org_id == org_id, usage.c.month == m)
                        .with_for_update()).scalar()
        if cur is None:
            cur = 0
            c.execute(insert(usage).values(org_id=org_id, month=m, scans=0))
        if quota is not None and cur + n > quota:
            return False, cur, quota
        c.execute(update(usage).where(usage.c.org_id == org_id, usage.c.month == m)
                  .values(scans=usage.c.scans + n))
    return True, cur + n, quota


def release_scans(org_id, n):
    """Give back reserved scans (e.g. a batch file rejected after reservation)."""
    with engine().begin() as c:
        c.execute(update(usage).where(usage.c.org_id == org_id, usage.c.month == month_key())
                  .values(scans=func.max(usage.c.scans - n, 0) if is_sqlite()
                          else func.greatest(usage.c.scans - n, 0)))


# ---------------- users ----------------

def create_user(org_id, email, name, password_hash, role, platform_admin=False):
    with engine().begin() as c:
        return c.execute(insert(users).values(
            org_id=org_id, email=email.lower().strip(), name=name, password_hash=password_hash,
            role=role, active=True, created_at=now(), is_platform_admin=platform_admin)).inserted_primary_key[0]


def user_by_email(email):
    with engine().connect() as c:
        return _row(c.execute(select(users).where(users.c.email == email.lower().strip())).first())


def user_by_id(uid):
    with engine().connect() as c:
        return _row(c.execute(select(users).where(users.c.id == uid)).first())


USER_COLS = [users.c.id, users.c.email, users.c.name, users.c.role, users.c.active, users.c.created_at,
             users.c.totp_enabled, users.c.last_login_at, users.c.is_platform_admin]


def list_users(org_id, limit=None, offset=0):
    q = select(*USER_COLS).where(users.c.org_id == org_id).order_by(users.c.id)
    if limit is not None:
        q = q.offset(offset).limit(limit)
    with engine().connect() as c:
        return [_clean(_row(r)) for r in c.execute(q)]


def count_org_users(org_id):
    with engine().connect() as c:
        return int(c.execute(select(func.count(users.c.id)).where(users.c.org_id == org_id)).scalar() or 0)


def count_active_users(org_id):
    with engine().connect() as c:
        return c.execute(select(func.count()).select_from(users).where(
            users.c.org_id == org_id, users.c.active.is_(True))).scalar()


def org_reviewers(org_id, exclude_user=None):
    q = select(users.c.id, users.c.email, users.c.name).where(
        users.c.org_id == org_id, users.c.active.is_(True), users.c.notify.is_(True),
        users.c.role.in_(("reviewer", "admin")))
    if exclude_user:
        q = q.where(users.c.id != exclude_user)
    with engine().connect() as c:
        return [_row(r) for r in c.execute(q)]


def update_user(uid, **values):
    with engine().begin() as c:
        c.execute(update(users).where(users.c.id == uid).values(**values))


def count_users():
    with engine().connect() as c:
        return c.execute(select(func.count()).select_from(users)).scalar()


# ---------------- sessions / tokens ----------------

def create_session(token_hash, user_id, hours):
    with engine().begin() as c:
        c.execute(insert(sessions).values(token_hash=token_hash, user_id=user_id, created_at=now(),
                                          expires_at=now() + timedelta(hours=hours)))


def session_user(token_hash):
    with engine().connect() as c:
        r = c.execute(select(users, sessions.c.expires_at, orgs.c.active.label("org_active"))
                      .join(sessions, sessions.c.user_id == users.c.id)
                      .join(orgs, orgs.c.id == users.c.org_id)
                      .where(sessions.c.token_hash == token_hash)).first()
    if not r:
        return None
    d = _row(r)
    if _aware(d["expires_at"]) < now() or not d["active"] or not d["org_active"]:
        return None
    return d


def delete_session(token_hash):
    with engine().begin() as c:
        c.execute(sessions.delete().where(sessions.c.token_hash == token_hash))


def delete_user_sessions(user_id):
    with engine().begin() as c:
        c.execute(sessions.delete().where(sessions.c.user_id == user_id))


def create_token(token_hash, user_id, kind, hours):
    with engine().begin() as c:
        # a new token of the same kind replaces older unused ones
        c.execute(update(tokens).where(tokens.c.user_id == user_id, tokens.c.kind == kind,
                                       tokens.c.used_at.is_(None)).values(used_at=now()))
        c.execute(insert(tokens).values(token_hash=token_hash, user_id=user_id, kind=kind,
                                        created_at=now(), expires_at=now() + timedelta(hours=hours)))


def use_token(token_hash, kind):
    """Mark a token used and return its user id, or None if unknown / used / expired."""
    with engine().begin() as c:
        r = _row(c.execute(select(tokens).where(tokens.c.token_hash == token_hash, tokens.c.kind == kind)
                           .with_for_update()).first())
        if not r or r["used_at"] or _aware(r["expires_at"]) < now():
            return None
        c.execute(update(tokens).where(tokens.c.id == r["id"]).values(used_at=now()))
        return r["user_id"]


def peek_token(token_hash, kind):
    with engine().connect() as c:
        r = _row(c.execute(select(tokens).where(tokens.c.token_hash == token_hash, tokens.c.kind == kind)).first())
    if not r or r["used_at"] or _aware(r["expires_at"]) < now():
        return None
    return r["user_id"]


# ---------------- rate limits (shared by every server through the database) ----------------

def hit(name, subject, limit, window_seconds):
    """Fixed-window counter. Returns (allowed, count). Works across processes and servers."""
    bucket = int(now().timestamp() // window_seconds)
    key = f"{name}:{subject}:{bucket}"[:200]
    expires = datetime.fromtimestamp((bucket + 1) * window_seconds, timezone.utc)
    if is_sqlite():
        from sqlalchemy.dialects.sqlite import insert as dinsert
    else:
        from sqlalchemy.dialects.postgresql import insert as dinsert
    stmt = dinsert(rate_limits).values(key=key, count=1, expires_at=expires)
    stmt = stmt.on_conflict_do_update(index_elements=["key"],
                                      set_={"count": rate_limits.c.count + 1})
    with engine().begin() as c:
        c.execute(stmt)
        n = c.execute(select(rate_limits.c.count).where(rate_limits.c.key == key)).scalar()
    return n <= limit, n


def count_hits(name, subject, window_seconds):
    bucket = int(now().timestamp() // window_seconds)
    with engine().connect() as c:
        return c.execute(select(rate_limits.c.count).where(
            rate_limits.c.key == f"{name}:{subject}:{bucket}"[:200])).scalar() or 0


def clear_hits(name, subject, window_seconds):
    bucket = int(now().timestamp() // window_seconds)
    with engine().begin() as c:
        c.execute(delete(rate_limits).where(rate_limits.c.key == f"{name}:{subject}:{bucket}"[:200]))


# ---------------- API keys ----------------

def create_api_key(org_id, name, prefix, key_hash, scopes, user_id):
    with engine().begin() as c:
        return c.execute(insert(api_keys).values(org_id=org_id, name=name, prefix=prefix, key_hash=key_hash,
                                                 scopes=scopes, created_by=user_id,
                                                 created_at=now())).inserted_primary_key[0]


def api_key_by_prefix(prefix):
    with engine().connect() as c:
        return _row(c.execute(select(api_keys).where(api_keys.c.prefix == prefix)).first())


def touch_api_key(kid):
    with engine().begin() as c:
        c.execute(update(api_keys).where(api_keys.c.id == kid).values(last_used_at=now()))


def list_api_keys(org_id, limit=None, offset=0):
    cols = [api_keys.c.id, api_keys.c.name, api_keys.c.prefix, api_keys.c.scopes, api_keys.c.created_at,
            api_keys.c.last_used_at, api_keys.c.revoked_at, users.c.name.label("created_by_name")]
    q = (select(*cols).join(users, users.c.id == api_keys.c.created_by)
         .where(api_keys.c.org_id == org_id).order_by(api_keys.c.id.desc()))
    if limit is not None:
        q = q.offset(offset).limit(limit)
    with engine().connect() as c:
        return [_clean(_row(r)) for r in c.execute(q)]


def count_api_keys(org_id):
    with engine().connect() as c:
        return int(c.execute(select(func.count(api_keys.c.id)).where(api_keys.c.org_id == org_id)).scalar() or 0)


def revoke_api_key(org_id, kid):
    with engine().begin() as c:
        return c.execute(update(api_keys).where(api_keys.c.id == kid, api_keys.c.org_id == org_id,
                                                api_keys.c.revoked_at.is_(None))
                         .values(revoked_at=now())).rowcount == 1


# ---------------- audit ----------------

def log_event(org_id, user_id, action, target_type=None, target_id=None, detail=None, ip=None):
    with engine().begin() as c:
        c.execute(insert(audit).values(org_id=org_id, user_id=user_id, action=action,
                                       target_type=target_type, target_id=target_id,
                                       detail=json.dumps(detail) if detail is not None else None,
                                       ip=ip, created_at=now()))


log = log_event  # backwards-compatible name used across the code base


def audit_list(org_id, target_type=None, target_id=None, limit=200, before_id=None, offset=0):
    q = (select(audit, users.c.name.label("user_name"))
         .outerjoin(users, users.c.id == audit.c.user_id)
         .where(audit.c.org_id == org_id))
    if target_type:
        q = q.where(audit.c.target_type == target_type, audit.c.target_id == target_id)
    if before_id:
        q = q.where(audit.c.id < before_id)
    q = q.order_by(audit.c.id.desc()).offset(offset).limit(limit)
    with engine().connect() as c:
        out = []
        for r in c.execute(q):
            d = _clean(_row(r))
            d["detail"] = json.loads(d["detail"]) if d["detail"] else None
            out.append(d)
        return out


def count_audit(org_id, target_type=None, target_id=None):
    q = select(func.count(audit.c.id)).where(audit.c.org_id == org_id)
    if target_type:
        q = q.where(audit.c.target_type == target_type, audit.c.target_id == target_id)
    with engine().connect() as c:
        return int(c.execute(q).scalar() or 0)


# ---------------- products / versions ----------------

def product_for(org_id, sku, name):
    key = (sku or name or "").strip().lower()
    if not key:
        return None, 1
    with engine().begin() as c:
        pid = c.execute(select(products.c.id).where(products.c.org_id == org_id, products.c.key == key)).scalar()
        if pid is None:
            pid = c.execute(insert(products).values(org_id=org_id, key=key, name=name,
                                                    created_at=now())).inserted_primary_key[0]
        last = c.execute(select(func.max(scans.c.version)).where(scans.c.product_id == pid)).scalar()
    return pid, (last or 0) + 1


# ---------------- scans ----------------

def create_scan(org_id, user_id, opts, filename, source_type, data, suffix, batch_id=None,
                listing=None, status="queued", api_key_id=None):
    from .storage import store
    pid, version = product_for(org_id, opts.get("sku"), opts.get("product_name"))
    with engine().begin() as c:
        sid = c.execute(insert(scans).values(
            org_id=org_id, user_id=user_id, product_id=pid, version=version, batch_id=batch_id,
            created_at=now(), product=opts.get("product_name"), sku=opts.get("sku"),
            category=opts.get("category"), filename=filename, source_type=source_type,
            status=status, options=json.dumps(opts), api_key_id=api_key_id,
            claimed_at=now() if status == "processing" else None,
            listing=json.dumps(listing) if listing else None,
            review_status="not_submitted")).inserted_primary_key[0]
    key = f"uploads/{org_id}/{sid}{suffix}"
    store().put(key, data)
    with engine().begin() as c:
        c.execute(update(scans).where(scans.c.id == sid).values(upload_path=key))
    return sid


def claim_next(worker="worker"):
    """Take the oldest queued scan. The status check in the UPDATE makes this safe with several
    workers on several servers: only one can move a row from queued to processing."""
    with engine().begin() as c:
        sid = c.execute(select(scans.c.id).where(scans.c.status == "queued", scans.c.upload_path.is_not(None))
                        .order_by(scans.c.id).limit(1)).scalar()
        if sid is None:
            return None
        n = c.execute(update(scans).where(scans.c.id == sid, scans.c.status == "queued")
                      .values(status="processing", claimed_at=now(), worker=worker[:64])).rowcount
    return sid if n == 1 else claim_next(worker)


def requeue_stale(minutes):
    """Scans stuck in 'processing' longer than `minutes` (their worker crashed) go back to the queue.
    Time-based, so a healthy worker's scan on another server is never taken away."""
    with engine().begin() as c:
        return c.execute(update(scans).where(scans.c.status == "processing",
                                             or_(scans.c.claimed_at.is_(None),
                                                 scans.c.claimed_at < now() - timedelta(minutes=minutes)))
                         .values(status="queued", worker=None)).rowcount


def queue_depth():
    with engine().connect() as c:
        return dict(c.execute(select(scans.c.status, func.count()).where(
            scans.c.status.in_(("queued", "processing"))).group_by(scans.c.status)).all())


def finish_scan(sid, image_key, ocr, extracted, result, listing, prep, duration_ms=None):
    cnt = result["summary"]["counts"]
    with engine().begin() as c:
        c.execute(update(scans).where(scans.c.id == sid).values(
            status="done", error=None, image_path=image_key, verdict=result["summary"]["verdict"],
            fails=cnt["fail"], warns=cnt["warn"], manuals=cnt["manual"], passes=cnt["pass"],
            ocr=json.dumps(ocr), extracted=json.dumps(extracted), result=json.dumps(result),
            listing=json.dumps(listing) if listing else None, prep=json.dumps(prep), duration_ms=duration_ms))


def fail_scan(sid, message, duration_ms=None):
    with engine().begin() as c:
        c.execute(update(scans).where(scans.c.id == sid).values(status="error", error=message[:500],
                                                                duration_ms=duration_ms))


def get_scan(sid, org_id=None):
    q = select(scans).where(scans.c.id == sid)
    if org_id is not None:
        q = q.where(scans.c.org_id == org_id)
    with engine().connect() as c:
        d = _row(c.execute(q).first())
    if not d:
        return None
    for k in ("options", "ocr", "extracted", "result", "listing", "prep"):
        d[k] = json.loads(d[k]) if d[k] else None
    return _clean(d)


def delete_scan(sid):
    """Delete a scan, its reviews and comments, and its files. Returns the storage keys removed."""
    from .storage import store
    with engine().begin() as c:
        row = _row(c.execute(select(scans.c.upload_path, scans.c.image_path).where(scans.c.id == sid)).first())
        if not row:
            return []
        c.execute(delete(finding_reviews).where(finding_reviews.c.scan_id == sid))
        c.execute(delete(comments).where(comments.c.scan_id == sid))
        c.execute(delete(scans).where(scans.c.id == sid))
    keys = [k for k in (row["upload_path"], row["image_path"]) if k]
    for k in keys:
        try:
            store().delete(k)
        except Exception:   # a missing file must not block deleting the record
            logger.warning("could not delete file %s", k)
    return keys


LIST_COLS = [scans.c.id, scans.c.created_at, scans.c.product, scans.c.sku, scans.c.category,
             scans.c.filename, scans.c.source_type, scans.c.status, scans.c.error, scans.c.verdict,
             scans.c.fails, scans.c.warns, scans.c.manuals, scans.c.passes, scans.c.version,
             scans.c.product_id, scans.c.batch_id, scans.c.review_status, scans.c.user_id]


def list_scans(org_id, q=None, review_status=None, batch_id=None, limit=200, before_id=None,
               status=None, verdict=None, offset=0):
    s = select(*LIST_COLS).where(scans.c.org_id == org_id)
    if q:
        like = f"%{q.lower()}%"
        s = s.where(or_(func.lower(scans.c.product).like(like), func.lower(scans.c.filename).like(like),
                        func.lower(scans.c.sku).like(like)))
    if review_status:
        s = s.where(scans.c.review_status == review_status)
    if batch_id:
        s = s.where(scans.c.batch_id == batch_id)
    if status:
        s = s.where(scans.c.status == status)
    if verdict:
        s = s.where(scans.c.verdict == verdict)
    if before_id:
        s = s.where(scans.c.id < before_id)
    s = s.order_by(scans.c.id.desc()).offset(offset).limit(limit)
    with engine().connect() as c:
        return [_clean(_row(r)) for r in c.execute(s)]


def count_scans(org_id, q=None, review_status=None, batch_id=None, status=None, verdict=None):
    s = select(func.count(scans.c.id)).where(scans.c.org_id == org_id)
    if q:
        like = f"%{q.lower()}%"
        s = s.where(or_(func.lower(scans.c.product).like(like), func.lower(scans.c.filename).like(like),
                        func.lower(scans.c.sku).like(like)))
    if review_status:
        s = s.where(scans.c.review_status == review_status)
    if batch_id:
        s = s.where(scans.c.batch_id == batch_id)
    if status:
        s = s.where(scans.c.status == status)
    if verdict:
        s = s.where(scans.c.verdict == verdict)
    with engine().connect() as c:
        return int(c.execute(s).scalar() or 0)


def versions(org_id, product_id):
    with engine().connect() as c:
        return [_clean(_row(r)) for r in c.execute(
            select(*LIST_COLS).where(scans.c.org_id == org_id, scans.c.product_id == product_id)
            .order_by(scans.c.version))]


def previous_version(scan):
    if not scan.get("product_id"):
        return None
    with engine().connect() as c:
        return c.execute(select(scans.c.id).where(
            scans.c.org_id == scan["org_id"], scans.c.product_id == scan["product_id"],
            scans.c.version < scan["version"], scans.c.status == "done")
            .order_by(scans.c.version.desc()).limit(1)).scalar()


def set_review_status(sid, status, user_id=None):
    vals = {"review_status": status}
    if status in ("approved", "changes_requested"):
        vals.update(reviewed_by=user_id, reviewed_at=now())
    with engine().begin() as c:
        c.execute(update(scans).where(scans.c.id == sid).values(**vals))


# ---------------- reviews / comments ----------------

def add_finding_review(sid, key, decision, comment, user_id):
    with engine().begin() as c:
        c.execute(insert(finding_reviews).values(scan_id=sid, finding_key=key, decision=decision,
                                                 comment=comment, user_id=user_id, created_at=now()))


def finding_decisions(sid):
    q = (select(finding_reviews, users.c.name.label("user_name"))
         .join(users, users.c.id == finding_reviews.c.user_id)
         .where(finding_reviews.c.scan_id == sid).order_by(finding_reviews.c.id))
    out = {}
    with engine().connect() as c:
        for r in c.execute(q):
            d = _clean(_row(r))
            out[d["finding_key"]] = d
    return out


def add_comment(sid, user_id, text):
    with engine().begin() as c:
        return c.execute(insert(comments).values(scan_id=sid, user_id=user_id, text=text,
                                                 created_at=now())).inserted_primary_key[0]


def list_comments(sid):
    q = (select(comments, users.c.name.label("user_name")).join(users, users.c.id == comments.c.user_id)
         .where(comments.c.scan_id == sid).order_by(comments.c.id))
    with engine().connect() as c:
        return [_clean(_row(r)) for r in c.execute(q)]


# ---------------- batches ----------------

def create_batch(org_id, user_id, name, total):
    with engine().begin() as c:
        return c.execute(insert(batches).values(org_id=org_id, user_id=user_id, name=name, total=total,
                                                created_at=now())).inserted_primary_key[0]


def _batch_counts(c, bid):
    return dict(c.execute(select(scans.c.status, func.count()).where(scans.c.batch_id == bid)
                          .group_by(scans.c.status)).all())


def get_batch(bid, org_id):
    with engine().connect() as c:
        b = _row(c.execute(select(batches).where(batches.c.id == bid, batches.c.org_id == org_id)).first())
        if not b:
            return None
        b = _clean(b)
        b["status_counts"] = _batch_counts(c, bid)
    return b


def list_batches(org_id, limit=50, offset=0):
    with engine().connect() as c:
        rows = [_clean(_row(r)) for r in c.execute(
            select(batches).where(batches.c.org_id == org_id).order_by(batches.c.id.desc()).offset(offset).limit(limit))]
        for b in rows:
            b["status_counts"] = _batch_counts(c, b["id"])
    return rows


def count_batches(org_id):
    with engine().connect() as c:
        return int(c.execute(select(func.count(batches.c.id)).where(batches.c.org_id == org_id)).scalar() or 0)


# ---------------- dashboard ----------------

def dashboard(org_id):
    with engine().connect() as c:
        done = and_(scans.c.org_id == org_id, scans.c.status == "done")
        total = c.execute(select(func.count()).select_from(scans).where(done)).scalar()
        by_verdict = dict(c.execute(select(scans.c.verdict, func.count()).where(done)
                                    .group_by(scans.c.verdict)).all())
        by_review = dict(c.execute(select(scans.c.review_status, func.count()).where(done)
                                   .group_by(scans.c.review_status)).all())
        queued = c.execute(select(func.count()).select_from(scans).where(
            scans.c.org_id == org_id, scans.c.status.in_(("queued", "processing")))).scalar()
        rule_fails = {}
        for (res,) in c.execute(select(scans.c.result).where(done).order_by(scans.c.id.desc()).limit(5000)):
            for f in json.loads(res)["findings"]:
                if f["status"] == "fail":
                    key = f"{f['rule']} {f['title']}"
                    rule_fails[key] = rule_fails.get(key, 0) + 1
    top = sorted(rule_fails.items(), key=lambda kv: -kv[1])[:8]
    return {"total": total, "by_verdict": by_verdict, "by_review": by_review, "in_queue": queued,
            "top_failing_rules": [{"rule": k, "count": v} for k, v in top]}


# ---------------- maintenance ----------------

def purge_expired():
    """Remove expired sessions, used / expired tokens and old rate-limit counters."""
    t = now()
    with engine().begin() as c:
        a = c.execute(delete(sessions).where(sessions.c.expires_at < t)).rowcount
        b = c.execute(delete(tokens).where(or_(tokens.c.expires_at < t - timedelta(days=7),
                                               tokens.c.used_at < t - timedelta(days=7)))).rowcount
        r = c.execute(delete(rate_limits).where(rate_limits.c.expires_at < t)).rowcount
    return {"sessions": a, "tokens": b, "rate_limits": r}


def scans_past_retention(limit=500):
    """(scan id, org id) of scans older than their company's retention period."""
    out = []
    with engine().connect() as c:
        for oid, days in c.execute(select(orgs.c.id, orgs.c.retention_days).where(orgs.c.retention_days.is_not(None))):
            cutoff = now() - timedelta(days=days)
            out += [(r[0], oid) for r in c.execute(select(scans.c.id).where(
                scans.c.org_id == oid, scans.c.created_at < cutoff,
                scans.c.status.in_(("done", "error"))).limit(limit))]
    return out[:limit]


# ---------------- webhooks ----------------

def create_webhook(org_id, user_id, url, events, secret_enc, description=None):
    with engine().begin() as c:
        return c.execute(insert(webhooks).values(
            org_id=org_id, url=url, events=",".join(events), secret_enc=secret_enc, active=True,
            description=description, created_by=user_id, created_at=now())).inserted_primary_key[0]


def _hook(d):
    if not d:
        return d
    d = _clean(d)
    d["events"] = [e for e in (d["events"] or "").split(",") if e]
    return d


def list_webhooks(org_id):
    with engine().connect() as c:
        rows = [_hook(_row(r)) for r in c.execute(
            select(webhooks).where(webhooks.c.org_id == org_id).order_by(webhooks.c.id))]
        for h in rows:
            last = c.execute(select(webhook_deliveries.c.status, webhook_deliveries.c.last_code,
                                    webhook_deliveries.c.created_at)
                             .where(webhook_deliveries.c.webhook_id == h["id"])
                             .order_by(webhook_deliveries.c.id.desc()).limit(1)).first()
            h["last_delivery"] = _clean(_row(last)) if last else None
    return rows


def get_webhook(hid, org_id=None):
    q = select(webhooks).where(webhooks.c.id == hid)
    if org_id is not None:
        q = q.where(webhooks.c.org_id == org_id)
    with engine().connect() as c:
        return _hook(_row(c.execute(q).first()))


def update_webhook(hid, **values):
    if "events" in values:
        values["events"] = ",".join(values["events"])
    with engine().begin() as c:
        c.execute(update(webhooks).where(webhooks.c.id == hid).values(**values))


def delete_webhook(hid):
    with engine().begin() as c:
        c.execute(delete(webhook_deliveries).where(webhook_deliveries.c.webhook_id == hid))
        c.execute(delete(webhooks).where(webhooks.c.id == hid))


def hooks_for_event(org_id, event):
    with engine().connect() as c:
        rows = [_hook(_row(r)) for r in c.execute(
            select(webhooks).where(webhooks.c.org_id == org_id, webhooks.c.active.is_(True)))]
    return [h for h in rows if event in h["events"]]


def queue_delivery(webhook_id, event, uid, payload):
    with engine().begin() as c:
        return c.execute(insert(webhook_deliveries).values(
            webhook_id=webhook_id, event=event, uid=uid, payload=payload, status="pending", attempts=0,
            next_attempt_at=now(), created_at=now())).inserted_primary_key[0]


def claim_deliveries(limit=20, stale_minutes=5):
    """Take due deliveries. 'sending' rows older than stale_minutes (a worker died mid-send) are due again.
    The conditional UPDATE makes this safe with many workers."""
    out = []
    with engine().begin() as c:
        ids = [r[0] for r in c.execute(
            select(webhook_deliveries.c.id).where(or_(
                and_(webhook_deliveries.c.status == "pending", webhook_deliveries.c.next_attempt_at <= now()),
                and_(webhook_deliveries.c.status == "sending",
                     webhook_deliveries.c.claimed_at < now() - timedelta(minutes=stale_minutes))))
            .order_by(webhook_deliveries.c.id).limit(limit))]
    for did in ids:
        with engine().begin() as c:
            n = c.execute(update(webhook_deliveries).where(
                webhook_deliveries.c.id == did,
                or_(webhook_deliveries.c.status == "pending",
                    and_(webhook_deliveries.c.status == "sending",
                         webhook_deliveries.c.claimed_at < now() - timedelta(minutes=stale_minutes))))
                .values(status="sending", claimed_at=now())).rowcount
        if n == 1:
            out.append(did)
    return out


def get_delivery(did):
    with engine().connect() as c:
        return _clean(_row(c.execute(select(webhook_deliveries).where(webhook_deliveries.c.id == did)).first()))


def finish_delivery(did, ok, code, error, retry_at=None):
    """ok -> delivered; retry_at set -> pending again later; else failed for good."""
    vals = {"last_code": code, "last_error": (error or "")[:500] or None, "claimed_at": None,
            "attempts": webhook_deliveries.c.attempts + 1}
    if ok:
        vals.update(status="delivered", delivered_at=now())
    elif retry_at is not None:
        vals.update(status="pending", next_attempt_at=retry_at)
    else:
        vals.update(status="failed")
    with engine().begin() as c:
        c.execute(update(webhook_deliveries).where(webhook_deliveries.c.id == did).values(**vals))


def note_webhook_result(hid, ok, disable_after):
    """Count consecutive failed attempts; switch the webhook off after `disable_after`. Returns True if
    it was just disabled."""
    with engine().begin() as c:
        if ok:
            c.execute(update(webhooks).where(webhooks.c.id == hid).values(consecutive_failures=0))
            return False
        c.execute(update(webhooks).where(webhooks.c.id == hid)
                  .values(consecutive_failures=webhooks.c.consecutive_failures + 1))
        n = c.execute(select(webhooks.c.consecutive_failures, webhooks.c.active)
                      .where(webhooks.c.id == hid)).first()
        if n and n[1] and n[0] >= disable_after:
            c.execute(update(webhooks).where(webhooks.c.id == hid).values(
                active=False, disabled_reason=f"Turned off after {n[0]} failed attempts in a row."))
            return True
    return False


def list_deliveries(hid, limit=50, offset=0):
    cols = [webhook_deliveries.c[k] for k in ("id", "event", "uid", "status", "attempts", "next_attempt_at",
                                               "last_code", "last_error", "created_at", "delivered_at")]
    with engine().connect() as c:
        return [_clean(_row(r)) for r in c.execute(
            select(*cols).where(webhook_deliveries.c.webhook_id == hid)
            .order_by(webhook_deliveries.c.id.desc()).offset(offset).limit(limit))]


def count_deliveries(hid):
    with engine().connect() as c:
        return int(c.execute(select(func.count(webhook_deliveries.c.id)).where(webhook_deliveries.c.webhook_id == hid)).scalar() or 0)


def retry_delivery(did):
    with engine().begin() as c:
        return c.execute(update(webhook_deliveries).where(
            webhook_deliveries.c.id == did, webhook_deliveries.c.status.in_(("failed", "delivered")))
            .values(status="pending", next_attempt_at=now())).rowcount == 1


def purge_old_deliveries(days=30):
    with engine().begin() as c:
        return c.execute(delete(webhook_deliveries).where(
            webhook_deliveries.c.status.in_(("delivered", "failed")),
            webhook_deliveries.c.created_at < now() - timedelta(days=days))).rowcount
