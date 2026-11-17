from alembic import op
import sqlalchemy as sa


revision = "0001_project_media"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False))
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table("sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False))
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_table("projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("data_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False))
    op.create_index("ix_projects_owner_id", "projects", ["owner_id"])
    op.create_table("project_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("data_json", sa.JSON, nullable=False),
        sa.Column("canvases_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.UniqueConstraint("project_id", "revision"))
    op.create_index("ix_project_versions_project_id", "project_versions", ["project_id"])
    op.create_table("canvases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("scene_id", sa.String(100), nullable=False),
        sa.Column("data_json", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint("project_id", "scene_id"))
    op.create_index("ix_canvases_project_id", "canvases", ["project_id"])
    op.create_table("provider_settings",
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("api_key_encrypted", sa.Text, nullable=False),
        sa.Column("workspace_id", sa.String(100), nullable=False),
        sa.Column("region", sa.String(40), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False))
    op.create_table("assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("generation_id", sa.String(36), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("relpath", sa.Text, nullable=False),
        sa.Column("poster_relpath", sa.Text, nullable=True),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("bytes", sa.Integer, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("width", sa.Integer, nullable=True),
        sa.Column("height", sa.Integer, nullable=True),
        sa.Column("metadata_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False))
    op.create_index("ix_assets_owner_id", "assets", ["owner_id"])
    op.create_index("ix_assets_project_id", "assets", ["project_id"])
    op.create_table("generations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("scene_id", sa.String(100), nullable=False),
        sa.Column("source_node_id", sa.String(100), nullable=False),
        sa.Column("clip_id", sa.String(100), nullable=False),
        sa.Column("operation_id", sa.String(100), nullable=False),
        sa.Column("project_revision", sa.Integer, nullable=False),
        sa.Column("input_json", sa.JSON, nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("quoted_cents", sa.Integer, nullable=False),
        sa.Column("reserved_cents", sa.Integer, nullable=False),
        sa.Column("actual_cents", sa.Integer, nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("remote_task_id", sa.String(200), nullable=True),
        sa.Column("remote_request_id", sa.String(200), nullable=True),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("assets.id"), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("lease_expires_at", sa.Float, nullable=True),
        sa.Column("next_poll_at", sa.Float, nullable=True),
        sa.Column("usage_json", sa.JSON, nullable=False),
        sa.Column("retry_count", sa.Integer, nullable=False),
        sa.Column("started_at", sa.Float, nullable=True),
        sa.Column("finished_at", sa.Float, nullable=True),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint("project_id", "operation_id"))
    op.create_index("ix_generations_owner_id", "generations", ["owner_id"])
    op.create_index("ix_generations_project_id", "generations", ["project_id"])
    op.create_index("ix_generations_status", "generations", ["status"])
    op.create_table("budgets",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("limit_cents", sa.Integer, nullable=False),
        sa.Column("spent_cents", sa.Integer, nullable=False),
        sa.Column("reserved_cents", sa.Integer, nullable=False),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("lease_expires_at", sa.Float, nullable=True))
    op.create_table("operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("operation_id", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(100), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("response_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.UniqueConstraint("owner_id", "project_id", "operation_id"))
    op.create_index("ix_operations_project_id", "operations", ["project_id"])


def downgrade() -> None:
    raise RuntimeError("业务数据需要保留；结构变更使用新的前向迁移")
