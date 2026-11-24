from alembic import op
import sqlalchemy as sa


revision = "0017_login_failures"
down_revision = "0016_canvas_image_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "login_failures",
        sa.Column("source_hash", sa.String(64), primary_key=True),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_login_failures_expires_at", "login_failures", ["expires_at"])


def downgrade():
    op.drop_index("ix_login_failures_expires_at", table_name="login_failures")
    op.drop_table("login_failures")
