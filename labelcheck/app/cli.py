"""Admin command line.

  python -m app.cli migrate                               apply database migrations
  python -m app.cli create-admin --org "Sunrise Foods" --email admin@sunrise.in --name "Asha" [--platform]
      (asks for the password; or set LABELCHECK_NEW_PASSWORD for scripts)
  python -m app.cli check-config                          print problems with the current settings
  python -m app.cli maintenance                           run housekeeping once (cron-friendly)
  python -m app.cli backup --out /backups                 SQLite + local files only (PostgreSQL: scripts/backup.sh)
"""
import argparse
import getpass
import os
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from . import auth, db, pipeline
from .config import settings


def create_admin(a):
    db.migrate()
    pw = os.environ.get("LABELCHECK_NEW_PASSWORD") or getpass.getpass("Password: ")
    problem = auth.password_problem(pw)
    if problem:
        sys.exit(problem)
    if db.user_by_email(a.email):
        sys.exit("A user with this email already exists.")
    org = db.create_org(a.org, a.plan)
    uid = db.create_user(org, a.email, a.name, auth.hash_password(pw), "admin", platform_admin=a.platform)
    db.log_event(org, uid, "admin_created_cli", "user", uid)
    print(f"Created company #{org} '{a.org}' ({a.plan}) and admin {a.email}"
          + (" with platform-admin rights." if a.platform else "."))


def check_config(_a):
    bad, warn = settings.problems(), settings.warnings()
    for p in bad:
        print("ERROR  ", p)
    for w in warn:
        print("WARNING", w)
    if not bad and not warn:
        print("OK")
    sys.exit(1 if bad else 0)


def backup(a):
    """Consistent copy of the SQLite database (online backup API) + uploaded files."""
    if not db.database_url().startswith("sqlite") or settings.storage != "local":
        sys.exit("Only for SQLite + local storage. PostgreSQL: scripts/backup.sh; S3: use bucket versioning.")
    db.migrate()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = out / f"labelcheck-{stamp}.db"
    src = sqlite3.connect(settings.data_dir / "labelcheck.db")
    dst = sqlite3.connect(snap)
    src.backup(dst)
    dst.close()
    src.close()
    arch = out / f"labelcheck-{stamp}.tar.gz"
    with tarfile.open(arch, "w:gz") as t:
        t.add(snap, arcname=snap.name)
        for sub in ("uploads", "checked", "images"):
            if (settings.data_dir / sub).exists():
                t.add(settings.data_dir / sub, arcname=sub)
    snap.unlink()
    print(f"Backup written: {arch}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    c = sub.add_parser("create-admin")
    c.add_argument("--org", required=True)
    c.add_argument("--email", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--plan", default="enterprise", choices=list(db.PLANS))
    c.add_argument("--platform", action="store_true", help="also give platform-admin rights")
    sub.add_parser("check-config")
    sub.add_parser("maintenance")
    b = sub.add_parser("backup")
    b.add_argument("--out", required=True)
    a = p.parse_args(argv)
    if a.cmd == "migrate":
        db.migrate()
        print("Database is up to date.")
    elif a.cmd == "maintenance":
        db.migrate()
        print(pipeline.maintenance())
    else:
        {"create-admin": create_admin, "backup": backup, "check-config": check_config}[a.cmd](a)


if __name__ == "__main__":
    main()
