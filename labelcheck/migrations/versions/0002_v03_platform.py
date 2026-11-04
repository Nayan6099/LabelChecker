"""0.3: plans and quotas, 2FA, notifications, tokens (reset / invite), API keys, shared rate limits,
usage metering, safe worker claiming.

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade():
    with op.batch_alter_table("orgs") as b:
        b.add_column(sa.Column("plan", sa.String(20), nullable=False, server_default="trial"))
        b.add_column(sa.Column("scan_quota", sa.Integer))
        b.add_column(sa.Column("user_limit", sa.Integer))
        b.add_column(sa.Column("retention_days", sa.Integer))
        b.add_column(sa.Column("require_2fa", sa.Boolean, nullable=False, server_default="0"))
        b.add_column(sa.Column("active", sa.Boolean, nullable=False, server_default="1"))
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("is_platform_admin", sa.Boolean, nullable=False, server_default="0"))
        b.add_column(sa.Column("totp_secret", sa.Text))
        b.add_column(sa.Column("totp_enabled", sa.Boolean, nullable=False, server_default="0"))
        b.add_column(sa.Column("notify", sa.Boolean, nullable=False, server_default="1"))
        b.add_column(sa.Column("last_login_at", TZ))
    op.create_table("api_keys",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False, index=True),
                    sa.Column("name", sa.String(100), nullable=False),
                    sa.Column("prefix", sa.String(16), nullable=False, unique=True),
                    sa.Column("key_hash", sa.String(64), nullable=False),
                    sa.Column("scopes", sa.String(200), nullable=False),
                    sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
                    sa.Column("created_at", TZ),
                    sa.Column("last_used_at", TZ),
                    sa.Column("revoked_at", TZ))
    with op.batch_alter_table("scans") as b:
        b.add_column(sa.Column("claimed_at", TZ))
        b.add_column(sa.Column("worker", sa.String(64)))
        b.add_column(sa.Column("api_key_id", sa.Integer, sa.ForeignKey("api_keys.id", name="fk_scans_api_key")))
        b.add_column(sa.Column("duration_ms", sa.Integer))
    op.create_index("ix_scans_status", "scans", ["status", "id"])
    op.create_index("ix_scans_org_created", "scans", ["org_id", "created_at"])
    op.create_table("tokens",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
                    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False, index=True),
                    sa.Column("kind", sa.String(20), nullable=False),
                    sa.Column("created_at", TZ),
                    sa.Column("expires_at", TZ, nullable=False),
                    sa.Column("used_at", TZ))
    op.create_table("rate_limits",
                    sa.Column("key", sa.String(200), primary_key=True),
                    sa.Column("count", sa.Integer, nullable=False),
                    sa.Column("expires_at", TZ, nullable=False, index=True))
    op.create_table("usage",
                    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), primary_key=True),
                    sa.Column("month", sa.String(7), primary_key=True),
                    sa.Column("scans", sa.Integer, nullable=False, server_default="0"))
    # existing companies keep working without limits until a plan is chosen
    op.execute("UPDATE orgs SET plan = 'enterprise'")
    # count existing scans as usage of their month (dialect-neutral: done in Python)
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT org_id, created_at FROM scans")).fetchall()
    counts = {}
    for org_id, created in rows:
        month = str(created)[:7]
        counts[(org_id, month)] = counts.get((org_id, month), 0) + 1
    for (org_id, month), n in counts.items():
        conn.execute(sa.text("INSERT INTO usage (org_id, month, scans) VALUES (:o, :m, :n)"),
                     {"o": org_id, "m": month, "n": n})


def downgrade():
    op.drop_table("usage")
    op.drop_table("rate_limits")
    op.drop_table("tokens")
    op.drop_index("ix_scans_org_created", "scans")
    op.drop_index("ix_scans_status", "scans")
    with op.batch_alter_table("scans") as b:
        b.drop_constraint("fk_scans_api_key", type_="foreignkey")
        for c in ("duration_ms", "api_key_id", "worker", "claimed_at"):
            b.drop_column(c)
    op.drop_table("api_keys")
    with op.batch_alter_table("users") as b:
        for c in ("last_login_at", "notify", "totp_enabled", "totp_secret", "is_platform_admin"):
            b.drop_column(c)
    with op.batch_alter_table("orgs") as b:
        for c in ("active", "require_2fa", "retention_days", "user_limit", "scan_quota", "plan"):
            b.drop_column(c)
