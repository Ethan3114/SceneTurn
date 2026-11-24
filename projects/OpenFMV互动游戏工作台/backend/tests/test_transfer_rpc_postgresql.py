import json
import secrets
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.services.deployment_queue import epoch_milliseconds
from app.services.project_transfers import build_archive
from app.services.projects import payload_digest
from app.services.transfer_rpc import ClaimedTransfer
from conftest import PRODUCT_ROOT
from test_api import create_project
from test_postgresql_persistence import postgres_runtime


@pytest.fixture
def transfer_rpc_database(request):
    admin, _, start = request.getfixturevalue("postgres_runtime")
    token = secrets.token_urlsafe(48)
    source = PRODUCT_ROOT / "backend" / "sql" / "cloudbase"
    with admin.begin() as connection:
        for role in ("anon", "authenticated", "service_role"):
            connection.exec_driver_sql(f"CREATE ROLE {role}")
        for filename in ("access.sql", "transfers.sql"):
            connection.exec_driver_sql((source / filename).read_text(), execution_options={"no_parameters": True})
        connection.execute(text("INSERT INTO sceneturn.gateway_credentials (token_sha256) VALUES (sha256(convert_to(:token, 'UTF8')))"),
                           {"token": token})

    def remove_rpc_functions():
        with admin.begin() as connection:
            functions = connection.exec_driver_sql("""
                SELECT pg_get_function_identity_arguments(p.oid), p.proname
                FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public' AND (starts_with(p.proname, 'sceneturn_transfer_') OR p.proname = 'sceneturn_schema_revision')
            """).all()
            for arguments, name in functions:
                connection.exec_driver_sql(f"DROP FUNCTION public.{name}({arguments})")
            connection.exec_driver_sql("DROP FUNCTION sceneturn.transfer_schedule(uuid, uuid, bigint)")
            connection.exec_driver_sql("DROP FUNCTION sceneturn.require_gateway_access()")
            connection.exec_driver_sql("DROP TABLE sceneturn.gateway_credentials")
            connection.exec_driver_sql("DROP ROLE anon, authenticated, service_role")

    request.addfinalizer(remove_rpc_functions)

    @contextmanager
    def rpc_transaction(role="service_role", scope=True):
        with admin.begin() as connection:
            assert role in {"anon", "service_role"}
            connection.exec_driver_sql(f"SET LOCAL ROLE {role}")
            connection.execute(text("SELECT set_config('request.jwt.claims', :claims, true), set_config('request.headers', :headers, true)"),
                {"claims": json.dumps({"role": role}), "headers": json.dumps({"x-sceneturn-database-scope": token} if scope else {})})
            yield connection

    return admin, start, rpc_transaction


def test_real_transfer_rpc_protects_owner_rolls_back_acceptance_and_prevents_duplicate_claims(transfer_rpc_database):
    admin, start, rpc_transaction = transfer_rpc_database
    with start("transfers") as service, service.client() as client, service.client() as neighbor:
        account = {"email": f"export-{uuid.uuid4().hex}@example.com", "password": uuid.uuid4().hex}
        other_account = {"email": f"export-{uuid.uuid4().hex}@example.com", "password": uuid.uuid4().hex}
        owner = client.post("/auth/register", json=account).json()["user"]["id"]
        other_owner = neighbor.post("/auth/register", json=other_account).json()["user"]["id"]
        envelope = create_project(client)
        data_dir = service.data_dir
    project_id = envelope["project"]["id"]
    revision = envelope["revision"]
    job_id = str(uuid.uuid4())
    marker_revision = str(uuid.uuid4())
    snapshot = {**envelope, "files": [], "history": {}}
    arguments = {
        "owner": owner, "project": project_id, "job": job_id, "operation": str(uuid.uuid4()), "version": revision,
        "identity": payload_digest({"projectId": project_id, "revision": revision, "kind": "backup"}),
        "confirm": payload_digest({"id": job_id, "private": snapshot}), "snapshot": json.dumps(snapshot),
        "preview": json.dumps({"issues": []}), "marker": marker_revision, "due": epoch_milliseconds() + 45_000,
    }
    accept = text("""SELECT public.sceneturn_transfer_accept_export(:owner, :project, :job, :operation, 'backup',
        :version, :identity, :confirm, CAST(:snapshot AS jsonb), CAST(:preview AS jsonb), :marker, :due)""")
    read = text("SELECT public.sceneturn_transfer_read(:owner, :job)")
    with pytest.raises(DBAPIError) as denied:
        with rpc_transaction(role="anon") as connection:
            connection.execute(accept, arguments)
    assert denied.value.orig.sqlstate == "42501"
    with pytest.raises(DBAPIError) as denied:
        with rpc_transaction(scope=False) as connection:
            connection.execute(accept, arguments)
    assert denied.value.orig.sqlstate == "42501"
    with pytest.raises(DBAPIError) as deadline:
        with rpc_transaction() as connection:
            connection.execute(accept, {**arguments, "due": epoch_milliseconds() - 1000})
    assert deadline.value.orig.sqlstate == "PT409"
    with admin.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM sceneturn.transfer_jobs WHERE id = :job"), {"job": job_id}).scalar_one() == 0
    with rpc_transaction() as connection:
        accepted = connection.execute(accept, arguments).scalar_one()
        assert not accepted["replayed"] and accepted["job"]["status"] == "queued"
    with pytest.raises(DBAPIError) as unauthorized:
        with rpc_transaction() as connection:
            connection.execute(read, {"owner": other_owner, "job": job_id})
    assert unauthorized.value.orig.sqlstate == "PT404"
    with rpc_transaction() as connection:
        replay = connection.execute(accept, {**arguments, "job": str(uuid.uuid4())}).scalar_one()
        assert replay["replayed"] and replay["job"]["id"] == job_id

    claim = text("SELECT public.sceneturn_transfer_claim(:job, :worker, :marker, :due, :immediate, :recovery, :recovery_due)")

    def claim_job(immediate):
        with rpc_transaction() as connection:
            result = connection.execute(claim, {"job": job_id, "worker": str(uuid.uuid4()), "marker": marker_revision,
                "due": arguments["due"], "immediate": immediate, "recovery": str(uuid.uuid4()),
                "recovery_due": epoch_milliseconds() + 65_000}).scalar_one()
        return result

    assert claim_job(False)["state"] == "early"
    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = list(executor.map(claim_job, (True, True)))
    assert sorted(item["state"] for item in attempts) == ["claimed", "stale"]
    claimed = ClaimedTransfer.model_validate(next(item["job"] for item in attempts if item["state"] == "claimed"))
    renew = text("SELECT public.sceneturn_transfer_renew(:job, :worker)")
    with rpc_transaction() as connection:
        assert connection.execute(renew, {"job": job_id, "worker": str(uuid.uuid4())}).scalar_one() is False
        assert connection.execute(renew, {"job": job_id, "worker": claimed.lease_owner}).scalar_one() is True

    proof = build_archive(claimed, Settings(_env_file=None, data_dir=data_dir), claimed.lease_owner)
    with zipfile.ZipFile(data_dir / proof["archive"]) as archive:
        assert archive.testzip() is None
    settlement = text("SELECT public.sceneturn_transfer_settle_export(:job, :worker, CAST(:archive AS jsonb), NULL, NULL)")
    settle_parameters = {"job": job_id, "worker": claimed.lease_owner, "archive": json.dumps(proof)}
    with rpc_transaction() as connection:
        assert connection.execute(settlement, settle_parameters).scalar_one()["job"]["status"] == "ready"
    with rpc_transaction() as connection:
        assert connection.execute(settlement, settle_parameters).scalar_one()["state"] == "lease_lost"
        assert connection.execute(read, {"owner": owner, "job": job_id}).scalar_one()["private_json"]["archive"] == proof["archive"]
    with admin.connect() as connection:
        assert connection.execute(text("SELECT due_ms IS NULL FROM sceneturn.deployment_dispatch WHERE job_id = :job"),
                                  {"job": job_id}).scalar_one()

    retry = text("SELECT public.sceneturn_transfer_resume_export(:owner, :job, NULL, :sha, :marker, :due)")
    retry_parameters = {"owner": owner, "job": job_id, "sha": proof["archiveSha256"],
        "marker": str(uuid.uuid4()), "due": epoch_milliseconds() + 45_000}
    with pytest.raises(DBAPIError) as wrong_archive:
        with rpc_transaction() as connection:
            connection.execute(retry, {**retry_parameters, "sha": "0" * 64})
    assert wrong_archive.value.orig.sqlstate == "PT409"
    (data_dir / proof["archive"]).unlink()
    with rpc_transaction() as connection:
        assert connection.execute(retry, retry_parameters).scalar_one()["job"]["status"] == "queued"
        assert connection.execute(retry, retry_parameters).scalar_one()["replayed"]
