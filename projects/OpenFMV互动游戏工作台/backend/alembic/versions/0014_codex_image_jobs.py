from alembic import op
import sqlalchemy as sa


revision = "0014_codex_image_jobs"
down_revision = "0013_public_release"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("codex_connections", sa.Column("capabilities_json", sa.JSON(), nullable=False, server_default="[]"))
    op.create_table("codex_image_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("codex_connections.id"), index=True),
        sa.Column("status", sa.String(30), nullable=False, index=True),
        sa.Column("input_json", sa.JSON(), nullable=False), sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("thread_id", sa.String(200)), sa.Column("turn_id", sa.String(200)), sa.Column("item_id", sa.String(200)),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("assets.id")),
        sa.Column("stop_requested", sa.Boolean(), nullable=False), sa.Column("lease_expires_at", sa.Float()),
        sa.Column("error_code", sa.String(100)), sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.Float(), nullable=False), sa.Column("updated_at", sa.Float(), nullable=False))


def downgrade():
    op.drop_table("codex_image_jobs")
    op.drop_column("codex_connections", "capabilities_json")
