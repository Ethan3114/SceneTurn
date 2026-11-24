import sqlalchemy as sa
from alembic import context, op

from app.postgresql import SCENETURN_APP_ROLE, SCENETURN_SCHEMA


revision = "0019_deployment_dispatch"
down_revision = "0018_postgresql_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deployment_dispatch",
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.String(36), nullable=False),
        sa.Column("due_ms", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("due_ms >= 0 AND due_ms <= 9999999999999", name="ck_deployment_dispatch_due"),
    )
    if not context.is_offline_mode() and op.get_bind().dialect.name == "postgresql":
        table = f"{SCENETURN_SCHEMA}.deployment_dispatch"
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY sceneturn_service_access ON {table} TO {SCENETURN_APP_ROLE} USING (true) WITH CHECK (true)")
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO {SCENETURN_APP_ROLE}")


def downgrade() -> None:
    op.drop_table("deployment_dispatch")
