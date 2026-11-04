"""0.4: payments and billing for paid plans and scan packs (Razorpay).

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False, index=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("order_id", sa.String(64), nullable=False, unique=True),
        sa.Column("payment_id", sa.String(64)),
        sa.Column("amount_paise", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(10), nullable=False, server_default="INR"),
        sa.Column("plan", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="created"),
        sa.Column("created_at", TZ),
        sa.Column("paid_at", TZ),
    )


def downgrade():
    op.drop_table("payments")

