from alembic import op
import sqlalchemy as sa


revision = "0009_image_queue"
down_revision = "0008_material_conversations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("image_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("slot", sa.Integer, nullable=False), sa.Column("credential_hash", sa.String(64), nullable=False),
        sa.Column("api_key_encrypted", sa.Text, nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("active_job_id", sa.String(36)), sa.Column("last_assigned_at", sa.Float, nullable=False),
        sa.UniqueConstraint("owner_id", "slot"), sa.UniqueConstraint("credential_hash"), sa.UniqueConstraint("active_job_id"))
    op.create_table("image_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("scene_id", sa.String(100), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.id"), nullable=False, index=True),
        sa.Column("node_id", sa.String(100), nullable=False), sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("operation_id", sa.String(160), nullable=False), sa.Column("label", sa.String(500), nullable=False),
        sa.Column("frame_key", sa.String(100)), sa.Column("status", sa.String(30), nullable=False, index=True),
        sa.Column("request_json", sa.JSON, nullable=False), sa.Column("reference_asset_ids", sa.JSON, nullable=False),
        sa.Column("dependency_ids", sa.JSON, nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("image_accounts.id")),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("assets.id")), sa.Column("response_relpath", sa.Text),
        sa.Column("error_code", sa.String(100)), sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False), sa.Column("confirmed_at", sa.Float),
        sa.Column("attempt_count", sa.Integer, nullable=False), sa.UniqueConstraint("owner_id", "operation_id"))


def downgrade():
    op.drop_table("image_jobs")
    op.drop_table("image_accounts")
