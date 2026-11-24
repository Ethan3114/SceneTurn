from pathlib import Path

from alembic import context

from app.models import Base
from app.postgresql import POSTGRES_WRITE_LOCK, SCENETURN_SCHEMA, validate_postgresql_access


def run_migrations() -> None:
    from app.db import engine

    with engine.connect() as connection:
        postgres = connection.dialect.name == "postgresql"
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            render_as_batch=not postgres,
            version_table_schema=SCENETURN_SCHEMA if postgres else None,
        )
        with context.begin_transaction():
            if postgres:
                validate_postgresql_access(connection, migration=True)
                connection.execute(POSTGRES_WRITE_LOCK)
            context.run_migrations()


def export_gateway_migrations() -> None:
    if context.get_x_argument(as_dictionary=True).get("deployment") != "cloudbase_gateway":
        raise RuntimeError("离线迁移需要指定 CloudBase 网关部署目标")
    context.configure(
        dialect_name="postgresql",
        target_metadata=Base.metadata,
        version_table_schema=SCENETURN_SCHEMA,
        render_as_batch=False,
        literal_binds=True,
    )
    with context.begin_transaction():
        sql_root = Path(__file__).resolve().parents[1] / "sql" / "cloudbase"
        if context.get_starting_revision_argument() is None:
            context.execute(f"CREATE SCHEMA {SCENETURN_SCHEMA}")
            context.execute(f"REVOKE ALL ON SCHEMA {SCENETURN_SCHEMA} FROM PUBLIC")
        context.execute(f"SET LOCAL search_path TO {SCENETURN_SCHEMA}")
        context.run_migrations()
        context.execute((sql_root / "privileges.sql").read_text(encoding="utf-8"))
        if context.get_starting_revision_argument() is None:
            context.execute((sql_root / "access.sql").read_text(encoding="utf-8"))
        context.execute("NOTIFY pgrst, 'reload schema'")


if context.is_offline_mode():
    export_gateway_migrations()
else:
    run_migrations()
