from alembic import op
import sqlalchemy as sa


revision = "0002_text_generation"
down_revision = "0001_project_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("text_generations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("scene_id", sa.String(100), nullable=False),
        sa.Column("source_node_id", sa.String(100), nullable=False),
        sa.Column("output_node_id", sa.String(100), nullable=False),
        sa.Column("operation_id", sa.String(100), nullable=False),
        sa.Column("task", sa.String(30), nullable=False),
        sa.Column("project_revision", sa.Integer, nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("input_json", sa.JSON, nullable=False),
        sa.Column("result_json", sa.JSON, nullable=True),
        sa.Column("output_text", sa.Text, nullable=True),
        sa.Column("partial_text", sa.Text, nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("remote_request_id", sa.String(200), nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("total_tokens", sa.Integer, nullable=False),
        sa.Column("reserved_cost_micros", sa.Integer, nullable=False),
        sa.Column("cost_micros", sa.Integer, nullable=False),
        sa.Column("usage_json", sa.JSON, nullable=False),
        sa.Column("events_json", sa.JSON, nullable=False),
        sa.Column("applied_revision", sa.Integer, nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("stop_requested", sa.Integer, nullable=False),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("lease_expires_at", sa.Float, nullable=True),
        sa.Column("started_at", sa.Float, nullable=True),
        sa.Column("finished_at", sa.Float, nullable=True),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint("project_id", "operation_id"))
    for name in ("owner_id", "project_id", "status"):
        op.create_index(f"ix_text_generations_{name}", "text_generations", [name])
    op.create_table("text_provider_settings",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("admin_owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("api_key_encrypted", sa.Text, nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False))
    op.create_table("text_usage_ledger",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("generation_id", sa.String(36), sa.ForeignKey("text_generations.id"), nullable=False, unique=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("remote_request_id", sa.String(200), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("total_tokens", sa.Integer, nullable=False),
        sa.Column("cache_hit_tokens", sa.Integer, nullable=False),
        sa.Column("cache_miss_tokens", sa.Integer, nullable=False),
        sa.Column("cost_micros", sa.Integer, nullable=False),
        sa.Column("price_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False))
    op.create_index("ix_text_usage_ledger_owner_id", "text_usage_ledger", ["owner_id"])
    op.create_index("ix_text_usage_ledger_project_id", "text_usage_ledger", ["project_id"])
    op.add_column("budgets", sa.Column("text_spent_micros", sa.Integer, nullable=False, server_default="0"))
    op.add_column("budgets", sa.Column("text_reserved_micros", sa.Integer, nullable=False, server_default="0"))


def downgrade() -> None:
    raise RuntimeError("业务数据需要保留；结构变更使用新的前向迁移")
