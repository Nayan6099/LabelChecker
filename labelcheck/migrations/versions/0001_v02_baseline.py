"""Baseline: the schema of LabelCheck 0.2.

Revision ID: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade():
    op.create_table("orgs",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("name", sa.String(200), nullable=False),
                    sa.Column("created_at", TZ))
    op.create_table("users",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False, index=True),
                    sa.Column("email", sa.String(254), nullable=False, unique=True),
                    sa.Column("name", sa.String(200), nullable=False),
                    sa.Column("password_hash", sa.String(255), nullable=False),
                    sa.Column("role", sa.String(20), nullable=False),
                    sa.Column("active", sa.Boolean, nullable=False),
                    sa.Column("terms_accepted_at", TZ),
                    sa.Column("created_at", TZ))
    op.create_table("sessions",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
                    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False, index=True),
                    sa.Column("created_at", TZ),
                    sa.Column("expires_at", TZ, nullable=False))
    op.create_table("products",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False, index=True),
                    sa.Column("key", sa.String(200), nullable=False),
                    sa.Column("name", sa.String(200)),
                    sa.Column("created_at", TZ),
                    sa.UniqueConstraint("org_id", "key"))
    op.create_table("batches",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False, index=True),
                    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
                    sa.Column("name", sa.String(200)),
                    sa.Column("total", sa.Integer),
                    sa.Column("created_at", TZ))
    op.create_table("scans",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False, index=True),
                    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
                    sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), index=True),
                    sa.Column("version", sa.Integer),
                    sa.Column("batch_id", sa.Integer, sa.ForeignKey("batches.id"), index=True),
                    sa.Column("created_at", TZ),
                    sa.Column("product", sa.String(200)),
                    sa.Column("sku", sa.String(100)),
                    sa.Column("category", sa.String(40)),
                    sa.Column("filename", sa.String(255)),
                    sa.Column("source_type", sa.String(10)),
                    sa.Column("upload_path", sa.Text),
                    sa.Column("image_path", sa.Text),
                    sa.Column("status", sa.String(12), nullable=False),
                    sa.Column("error", sa.Text),
                    sa.Column("verdict", sa.String(20)),
                    sa.Column("fails", sa.Integer),
                    sa.Column("warns", sa.Integer),
                    sa.Column("manuals", sa.Integer),
                    sa.Column("passes", sa.Integer),
                    sa.Column("options", sa.Text),
                    sa.Column("ocr", sa.Text),
                    sa.Column("extracted", sa.Text),
                    sa.Column("result", sa.Text),
                    sa.Column("listing", sa.Text),
                    sa.Column("prep", sa.Text),
                    sa.Column("review_status", sa.String(20), nullable=False),
                    sa.Column("reviewed_by", sa.Integer, sa.ForeignKey("users.id")),
                    sa.Column("reviewed_at", TZ))
    op.create_table("finding_reviews",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("scan_id", sa.Integer, sa.ForeignKey("scans.id"), nullable=False, index=True),
                    sa.Column("finding_key", sa.String(40), nullable=False),
                    sa.Column("decision", sa.String(12), nullable=False),
                    sa.Column("comment", sa.Text),
                    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
                    sa.Column("created_at", TZ))
    op.create_table("comments",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("scan_id", sa.Integer, sa.ForeignKey("scans.id"), nullable=False, index=True),
                    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
                    sa.Column("text", sa.Text, nullable=False),
                    sa.Column("created_at", TZ))
    op.create_table("audit_log",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), index=True),
                    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id")),
                    sa.Column("action", sa.String(60), nullable=False),
                    sa.Column("target_type", sa.String(30)),
                    sa.Column("target_id", sa.Integer),
                    sa.Column("detail", sa.Text),
                    sa.Column("ip", sa.String(64)),
                    sa.Column("created_at", TZ))


def downgrade():
    for t in ("audit_log", "comments", "finding_reviews", "scans", "batches", "products", "sessions",
              "users", "orgs"):
        op.drop_table(t)
