"""0.4: subscription period tracking and purchased add-on scan credits.

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade():
    op.add_column("orgs", sa.Column("purchased_addon_credits", sa.Integer(), server_default="0", nullable=False))
    op.add_column("orgs", sa.Column("current_period_start", TZ, nullable=True))
    op.add_column("orgs", sa.Column("current_period_end", TZ, nullable=True))
    op.add_column("orgs", sa.Column("subscription_status", sa.String(length=20), server_default="active", nullable=False))


def downgrade():
    op.drop_column("orgs", "subscription_status")
    op.drop_column("orgs", "current_period_end")
    op.drop_column("orgs", "current_period_start")
    op.drop_column("orgs", "purchased_addon_credits")

