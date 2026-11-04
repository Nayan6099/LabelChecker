"""0.4: email verification on user accounts.

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("email_verified", sa.Boolean(), server_default="1", nullable=False))


def downgrade():
    op.drop_column("users", "email_verified")

