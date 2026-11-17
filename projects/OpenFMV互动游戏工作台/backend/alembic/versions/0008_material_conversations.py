from alembic import op
import sqlalchemy as sa


revision = "0008_material_conversations"
down_revision = "0007_project_conversations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("material_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("scene_id", sa.String(100), nullable=False), sa.Column("title", sa.String(200), nullable=False),
        sa.Column("active_run_id", sa.String(36), sa.ForeignKey("agent_runs.id")),
        sa.Column("created_at", sa.Float, nullable=False), sa.Column("updated_at", sa.Float, nullable=False))
    op.create_table("material_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("conversation_id", sa.String(36), sa.ForeignKey("material_conversations.id"), nullable=False, index=True),
        sa.Column("operation_id", sa.String(100), nullable=False), sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False), sa.Column("references_json", sa.JSON, nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.UniqueConstraint("conversation_id", "operation_id"), sa.UniqueConstraint("conversation_id", "sequence"))


def downgrade():
    op.drop_table("material_messages")
    op.drop_table("material_conversations")
