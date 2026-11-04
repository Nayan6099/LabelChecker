"""0.4: webhooks (signed event notifications to customer systems) with a delivery outbox.

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade():
    op.create_table("webhooks",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False, index=True),
                    sa.Column("url", sa.String(2000), nullable=False),
                    sa.Column("events", sa.String(300), nullable=False),
                    sa.Column("secret_enc", sa.Text, nullable=False),
                    sa.Column("active", sa.Boolean, nullable=False, server_default="1"),
                    sa.Column("description", sa.String(200)),
                    sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
                    sa.Column("created_at", TZ),
                    sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
                    sa.Column("disabled_reason", sa.String(300)))
    op.create_table("webhook_deliveries",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("webhook_id", sa.Integer, sa.ForeignKey("webhooks.id", ondelete="CASCADE"),
                              nullable=False, index=True),
                    sa.Column("event", sa.String(40), nullable=False),
                    sa.Column("uid", sa.String(40), nullable=False, unique=True),
                    sa.Column("payload", sa.Text, nullable=False),
                    sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
                    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
                    sa.Column("next_attempt_at", TZ, nullable=False),
                    sa.Column("claimed_at", TZ),
                    sa.Column("last_code", sa.Integer),
                    sa.Column("last_error", sa.String(500)),
                    sa.Column("created_at", TZ),
                    sa.Column("delivered_at", TZ))
    op.create_index("ix_whd_due", "webhook_deliveries", ["status", "next_attempt_at"])


def downgrade():
    op.drop_index("ix_whd_due", "webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_table("webhooks")
