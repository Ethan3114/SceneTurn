from alembic import op
import sqlalchemy as sa


revision = "0003_budget_reservation"
down_revision = "0002_text_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("budget_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("purpose", sa.String(100), nullable=False),
        sa.Column("reserved_micros", sa.Integer, nullable=False),
        sa.Column("cost_micros", sa.Integer, nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("details_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False))


def downgrade() -> None:
    raise RuntimeError("费用账目需要保留；结构变更使用新的前向迁移")
