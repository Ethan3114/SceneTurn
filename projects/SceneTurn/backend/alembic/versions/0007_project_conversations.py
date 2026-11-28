from alembic import op
import sqlalchemy as sa


revision = "0007_project_conversations"
down_revision = "0006_codex_connection"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("project_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=True),
        sa.Column("active_run_id", sa.String(36), sa.ForeignKey("agent_runs.id"), nullable=True),
        sa.Column("created_at", sa.Float, nullable=False), sa.Column("updated_at", sa.Float, nullable=False))
    op.create_table("conversation_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("project_conversations.id"), nullable=False, index=True),
        sa.Column("operation_id", sa.String(100), nullable=False), sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("role", sa.String(20), nullable=False), sa.Column("content", sa.Text, nullable=False),
        sa.Column("context_json", sa.JSON, nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.id"), nullable=True),
        sa.Column("created_at", sa.Float, nullable=False), sa.UniqueConstraint("conversation_id", "operation_id"))


def downgrade():
    op.drop_table("conversation_messages")
    op.drop_table("project_conversations")
