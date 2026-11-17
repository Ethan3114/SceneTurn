from alembic import op
import sqlalchemy as sa


revision = "0006_codex_connection"
down_revision = "0005_context_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("codex_pairings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("tools_json", sa.JSON, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("connection_expires_at", sa.Float, nullable=False),
        sa.Column("claimed_at", sa.Float, nullable=True),
        sa.Column("created_at", sa.Float, nullable=False))
    op.create_table("codex_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False, index=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("tools_json", sa.JSON, nullable=False),
        sa.Column("client_version", sa.String(100), nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("last_seen_at", sa.Float, nullable=False),
        sa.Column("revoked_at", sa.Float, nullable=True),
        sa.Column("created_at", sa.Float, nullable=False))
    op.create_table("codex_connection_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("connection_id", sa.String(36), sa.ForeignKey("codex_connections.id"), nullable=False, index=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=True),
        sa.Column("turn_id", sa.String(200), nullable=True),
        sa.Column("details_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False))


def downgrade() -> None:
    raise RuntimeError("连接授权及事件需要保留，请使用新的前向迁移")
