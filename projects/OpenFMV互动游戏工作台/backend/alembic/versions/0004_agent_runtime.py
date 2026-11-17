from alembic import op

from app.agent_models import AgentCall, AgentRun, AgentStep


revision = "0004_agent_runtime"
down_revision = "0003_budget_reservation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in (AgentRun.__table__, AgentStep.__table__, AgentCall.__table__):
        table.create(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Agent任务、工具结果与费用需要保留；结构变更使用新的前向迁移")
