from collections.abc import Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import JSON, column, create_engine, event, func, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.postgresql import POSTGRES_WRITE_LOCK, SCENETURN_SCHEMA, create_postgresql_engine, validate_postgresql_access


settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
settings.data_dir.chmod(0o700)
if settings.postgres_url is None:
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False, "timeout": 10})

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()
else:
    engine = create_postgresql_engine(
        settings.postgres_url.get_secret_value(), allow_local_plaintext=settings.deployment == "local"
    )


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def lock_write(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(POSTGRES_WRITE_LOCK)
    else:
        session.execute(text("BEGIN IMMEDIATE"))


def json_array_items(session: Session, expression, *, name=None, joins_implicitly=False):
    expand = func.json_array_elements if session.get_bind().dialect.name == "postgresql" else func.json_each
    return expand(expression).table_valued(column("value", JSON), name=name, joins_implicitly=joins_implicitly)


def initialize_database() -> None:
    from app.models import Budget

    configuration = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    if engine.dialect.name == "sqlite":
        command.upgrade(configuration, "head")
        (settings.data_dir.resolve() / "openfmv.sqlite3").chmod(0o600)
    with SessionLocal.begin() as session:
        if engine.dialect.name == "postgresql":
            validate_postgresql_access(session.connection())
            version = session.scalar(text(f"SELECT version_num FROM {SCENETURN_SCHEMA}.alembic_version"))
            if version != ScriptDirectory.from_config(configuration).get_current_head():
                raise RuntimeError("PostgreSQL 迁移版本未匹配当前应用，请先运行独立的数据迁移")
            lock_write(session)
        if session.get(Budget, "global") is None:
            session.add(Budget(id="global", limit_cents=settings.budget_cents, spent_cents=0, reserved_cents=0))


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
