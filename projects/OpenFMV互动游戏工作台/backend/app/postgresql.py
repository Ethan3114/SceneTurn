from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool


SCENETURN_SCHEMA = "sceneturn"
SCENETURN_OWNER_ROLE = "sceneturn_owner"
SCENETURN_APP_ROLE = "sceneturn_app"
SCENETURN_WRITE_LOCK = int.from_bytes(b"scnturnd", "big")
POSTGRES_WRITE_LOCK = text("SELECT pg_advisory_xact_lock(:key)").bindparams(key=SCENETURN_WRITE_LOCK)


def create_postgresql_engine(secret_url: str, *, allow_local_plaintext: bool = False) -> Engine:
    try:
        url = make_url(secret_url)
    except (ArgumentError, ValueError):
        raise ValueError("PostgreSQL 连接配置格式无效") from None
    if (url.drivername not in {"postgresql", "postgresql+psycopg"} or not url.database or not url.username
            or not url.host or "," in url.host):
        raise ValueError("PostgreSQL 连接配置需包含唯一主机、数据库和专用账号")
    if url.query.keys() - {"sslmode", "sslrootcert"}:
        raise ValueError("PostgreSQL 连接配置只能附加 TLS 参数")
    sslmode = url.query.get("sslmode", "require")
    if sslmode not in {"require", "verify-ca", "verify-full"} and not (
        allow_local_plaintext and url.host == "127.0.0.1" and sslmode == "disable"
    ):
        raise ValueError("PostgreSQL 连接必须使用 TLS")

    return create_engine(
        url.set(drivername="postgresql+psycopg"),
        poolclass=NullPool,
        isolation_level="READ COMMITTED",
        hide_parameters=True,
        connect_args={
            "application_name": "SceneTurn",
            "connect_timeout": 5,
            "options": f"-c search_path={SCENETURN_SCHEMA} -c lock_timeout=10000",
            "sslmode": sslmode,
        },
    )


def validate_postgresql_access(connection: Connection, *, migration: bool = False) -> None:
    expected_role = SCENETURN_OWNER_ROLE if migration else SCENETURN_APP_ROLE
    role = connection.execute(text("""
        SELECT current_user AS active_user, session_user AS login_user,
            current_schema() AS active_schema, current_schemas(false) AS schema_path,
            n.nspowner = r.oid AS owns_schema,
            has_schema_privilege(r.oid, n.oid, 'CREATE') AS may_create,
            r.rolsuper OR r.rolbypassrls OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication AS elevated,
            EXISTS (SELECT FROM pg_auth_members WHERE member = r.oid) AS has_memberships,
            EXISTS (
                SELECT FROM aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner)))
                WHERE grantee = 0
            ) AS public_access
        FROM pg_roles AS r
        LEFT JOIN pg_namespace AS n ON n.nspname = :schema
        WHERE r.rolname = current_user
    """), {"schema": SCENETURN_SCHEMA}).mappings().one()
    if role["active_user"] != expected_role or role["login_user"] != expected_role or role["elevated"] or role["has_memberships"]:
        raise RuntimeError(f"PostgreSQL 必须使用无额外权限的专用角色 {expected_role}")
    if role["active_schema"] != SCENETURN_SCHEMA or role["schema_path"] != [SCENETURN_SCHEMA] or role["public_access"]:
        raise RuntimeError(f"PostgreSQL 必须配置私有 schema {SCENETURN_SCHEMA}")
    if role["owns_schema"] is not migration or role["may_create"] is not migration:
        raise RuntimeError("PostgreSQL 迁移权限和运行权限必须由不同的角色持有")
