import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.security import hash_password, new_token, token_hash, verify_password
from app.services.auth_repository import login_lookup, login_result, logout_result, optional_user
from conftest import PRODUCT_ROOT
from test_postgresql_persistence import postgres_runtime


def test_real_cloudbase_auth_transactions(postgres_runtime, request):
    admin, _, _ = postgres_runtime
    sql_dir = PRODUCT_ROOT / "backend" / "sql" / "cloudbase"
    scope = new_token()
    roles = ("anon", "authenticated", "service_role")
    with admin.begin() as connection:
        for role in roles:
            connection.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        for script in ("access.sql", "auth.sql"):
            connection.exec_driver_sql((sql_dir / script).read_text(encoding="utf-8"))
        connection.execute(text("""
            INSERT INTO sceneturn.gateway_credentials (token_sha256)
            VALUES (sha256(convert_to(:scope, 'UTF8')))
        """), {"scope": scope})

    def cleanup():
        with admin.begin() as connection:
            connection.exec_driver_sql("""
                DROP FUNCTION public.sceneturn_auth_register(uuid, text, text, text, integer);
                DROP FUNCTION public.sceneturn_auth_login_lookup(text, text);
                DROP FUNCTION public.sceneturn_auth_login_finish(text, text, text, text, text, integer);
                DROP FUNCTION public.sceneturn_auth_session_user(text);
                DROP FUNCTION public.sceneturn_auth_logout(text);
                DROP FUNCTION public.sceneturn_schema_revision();
                DROP FUNCTION sceneturn.require_gateway_access();
                DROP TABLE sceneturn.gateway_credentials;
            """)
            for role in roles:
                connection.exec_driver_sql(f"DROP ROLE {role}")

    request.addfinalizer(cleanup)

    def invoke(statement, parameters, *, role="service_role", scope_token=scope):
        with admin.begin() as connection:
            connection.execute(text("SELECT set_config('request.jwt.claims', :claims, true)"),
                {"claims": json.dumps({"role": role} if role is not None else {})})
            connection.execute(text("SELECT set_config('request.headers', :headers, true)"),
                {"headers": json.dumps({"x-sceneturn-database-scope": scope_token} if scope_token is not None else {})})
            connection.exec_driver_sql("SET LOCAL ROLE service_role")
            return connection.execute(text(statement), parameters).scalar_one()

    register = """
        SELECT public.sceneturn_auth_register(CAST(:id AS uuid), :email, :password_hash, :token, 604800)
    """
    lookup = "SELECT public.sceneturn_auth_login_lookup(:source, :email)"
    finish = """
        SELECT public.sceneturn_auth_login_finish(:source, :email, :verified_id, :verified_hash, :token, 604800)
    """
    session_user = "SELECT public.sceneturn_auth_session_user(:token)"
    logout = "SELECT public.sceneturn_auth_logout(:token)"

    password = new_token()
    identity = {"id": str(uuid4()), "email": f"{uuid4().hex}@example.com",
                "password_hash": hash_password(password), "token": token_hash(new_token())}
    for role, supplied_scope in ((None, scope), ("authenticated", scope), ("service_role", None),
                                 ("service_role", new_token())):
        with pytest.raises(DBAPIError) as forbidden:
            invoke(register, identity, role=role, scope_token=supplied_scope)
        assert forbidden.value.orig.sqlstate == "42501"

    user = optional_user.validate_python(invoke(register, identity), strict=True)
    assert user is not None and user.model_dump() == {"id": identity["id"], "email": identity["email"]}
    assert invoke(register, {**identity, "id": str(uuid4()), "token": token_hash(new_token())}) is None
    assert optional_user.validate_python(invoke(session_user, identity), strict=True) == user
    owner_source = token_hash("192.0.2.1")
    found = login_lookup.validate_python(invoke(lookup, {**identity, "source": owner_source}), strict=True)
    assert found.status == "lookup" and found.account.id == user.id
    assert verify_password(password, found.account.password_hash.get_secret_value())
    new_identity = {**identity, "id": str(uuid4()), "email": f"{uuid4().hex}@example.com"}
    with pytest.raises(DBAPIError) as duplicate_session:
        invoke(register, new_identity)
    assert duplicate_session.value.orig.sqlstate == "23505"
    new_identity["token"] = token_hash(new_token())
    assert invoke(register, new_identity)["id"] == new_identity["id"]

    attempt = {"source": owner_source, "email": identity["email"], "verified_id": None,
               "verified_hash": None, "token": None}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: login_result.validate_python(invoke(finish, attempt), strict=True), range(8)))
    assert [result.status for result in results].count("invalid") == 4
    assert [result.status for result in results].count("locked") == 4
    locked = login_lookup.validate_python(invoke(lookup, attempt), strict=True)
    assert locked.status == "locked" and 0 < locked.retry_after <= 900

    verified = {**attempt, "verified_id": user.id, "verified_hash": found.account.password_hash.get_secret_value(),
                "token": token_hash(new_token())}
    assert invoke(finish, verified)["status"] == "locked"
    assert invoke(session_user, verified) is None
    verified["source"] = token_hash("192.0.2.2")
    assert invoke(finish, {**attempt, "source": verified["source"]}) == {"status": "invalid"}
    accepted = login_result.validate_python(invoke(finish, verified), strict=True)
    assert accepted.status == "accepted" and accepted.user == user
    assert invoke(session_user, verified)["id"] == user.id

    with admin.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM sceneturn.login_failures WHERE source_hash = :source"),
            {"source": verified["source"]}).scalar_one() == 0
        connection.execute(text("UPDATE sceneturn.sessions SET expires_at = 0 WHERE token_hash = :token"), identity)
    assert invoke(session_user, identity) is None
    assert not invoke(logout, identity)
    assert logout_result.validate_python(invoke(logout, verified), strict=True)
    assert invoke(session_user, verified) is None
    assert not invoke(logout, verified)
