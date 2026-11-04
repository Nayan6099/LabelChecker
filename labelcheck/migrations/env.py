"""Alembic environment. Uses the app's engine (DATABASE_URL or the SQLite default) and its tables."""
from alembic import context

from app import db

target_metadata = db.md


def run():
    conn = context.config.attributes.get("connection")
    if conn is not None:                     # called from app.db.migrate()
        _configure(conn)
        return
    with db.engine().begin() as conn:       # called from the alembic command line
        _configure(conn)


def _configure(conn):
    context.configure(connection=conn, target_metadata=target_metadata,
                      render_as_batch=conn.dialect.name == "sqlite", compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline (SQL script) mode is not supported; run against a database.")
run()
