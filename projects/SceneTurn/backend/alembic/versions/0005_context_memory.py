from alembic import op
import sqlalchemy as sa


revision = "0005_context_memory"
down_revision = "0004_agent_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("operation_id", sa.String(100), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("subject", sa.String(200), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("scene_id", sa.String(100), nullable=True),
        sa.Column("branch_id", sa.String(100), nullable=True),
        sa.Column("story_time", sa.String(200), nullable=True),
        sa.Column("sources_json", sa.JSON(), nullable=False),
        sa.Column("project_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("supersedes_id", sa.String(36), sa.ForeignKey("project_memories.id"), nullable=True),
        sa.Column("superseded_by", sa.String(36), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("project_id", "operation_id"),
    )
    for column in ("owner_id", "project_id", "kind", "scene_id"):
        op.create_index(f"ix_project_memories_{column}", "project_memories", [column])


def downgrade() -> None:
    raise RuntimeError("创作记忆及来源需要保留；结构变更使用新的前向迁移")
