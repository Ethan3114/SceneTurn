from alembic import context, op
from sqlalchemy import inspect

from app.postgresql import SCENETURN_APP_ROLE, SCENETURN_SCHEMA


revision = "0018_postgresql_access"
down_revision = "0017_login_failures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        return
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return

    schema, role = SCENETURN_SCHEMA, SCENETURN_APP_ROLE
    op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    for name in inspect(connection).get_table_names(schema=schema):
        table = f"{schema}.{connection.dialect.identifier_preparer.quote_identifier(name)}"
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        if name == "alembic_version":
            op.execute(f"GRANT SELECT ON TABLE {table} TO {role}")
            continue
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY sceneturn_service_access ON {table} TO {role} USING (true) WITH CHECK (true)")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {role}")
    op.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {schema} TO {role}")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        raise RuntimeError("PostgreSQL 的运行角色权限只支持前向迁移")
