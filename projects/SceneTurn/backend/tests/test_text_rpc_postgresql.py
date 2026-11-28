import json
import secrets
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.security import encode_secret
from app.services.cloudbase_text import _compose
from app.services.deployment_queue import epoch_milliseconds
from app.services.projects import payload_digest
from app.services.text_provider import task_instructions
from conftest import PRODUCT_ROOT
from test_api import create_project, scene_id
from test_generation_canvas import node, save_canvas
from test_postgresql_persistence import postgres_runtime
from test_transfer_rpc_postgresql import transfer_rpc_database


@pytest.fixture
def text_rpc_database(transfer_rpc_database, request):
    admin, _, _ = transfer_rpc_database
    sql_path = PRODUCT_ROOT / "backend" / "sql" / "cloudbase"
    with admin.begin() as connection:
        for filename in ("projects.sql", "text_generation.sql"):
            connection.exec_driver_sql((sql_path / filename).read_text(), execution_options={"no_parameters": True})

    def cleanup():
        with admin.begin() as connection:
            own_rpc = connection.exec_driver_sql("""
                SELECT pg_get_function_identity_arguments(p.oid), p.proname
                FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'public' AND (starts_with(p.proname, 'sceneturn_text_')
                    OR starts_with(p.proname, 'sceneturn_project_') OR starts_with(p.proname, 'sceneturn_projects_'))
            """).all()
            for arguments, name in own_rpc:
                connection.exec_driver_sql(f"DROP FUNCTION public.{name}({arguments})")

    request.addfinalizer(cleanup)
    return transfer_rpc_database


def test_real_flash_rpc_reservation_claim_settlement_and_canvas_adoption(text_rpc_database):
    admin, start, rpc_transaction = text_rpc_database
    email = f"flash-{uuid.uuid4().hex}@example.com"
    with start("text-rpc") as service, service.client() as client, service.client() as neighbor:
        owner = client.post("/auth/register", json={"email": email, "password": secrets.token_urlsafe(24)}).json()["user"]["id"]
        other_owner = neighbor.post("/auth/register", json={"email": f"neighbor-{uuid.uuid4().hex}@example.com",
            "password": secrets.token_urlsafe(24)}).json()["user"]["id"]
        project = create_project(client)
        scene = scene_id(project)
        source = node(str(uuid.uuid4()), "text", "输入", content="晨雾中的旅人")
        output = node(str(uuid.uuid4()), "text", "结果")
        revision = save_canvas(client, project, [source, output], [])["revision"]
    project_id = project["project"]["id"]
    encoded = encode_secret(secrets.token_urlsafe(32), Settings(_env_file=None, master_key=Fernet.generate_key().decode()))
    settings_sql = text("SELECT public.sceneturn_text_settings(:owner, :email, :key)")
    with pytest.raises(DBAPIError) as forbidden:
        with rpc_transaction() as connection:
            connection.execute(settings_sql, {"owner": other_owner, "email": email, "key": encoded})
    assert forbidden.value.orig.sqlstate == "PT403"
    with rpc_transaction() as connection:
        config = connection.execute(settings_sql, {"owner": owner, "email": email, "key": encoded}).scalar_one()
        assert config["canManage"] and config["configured"]
        context = connection.execute(text("SELECT public.sceneturn_text_prepare(:owner,:project,:scene)"),
            {"owner": owner, "project": project_id, "scene": scene}).scalar_one()
        assert context["providerRevision"] == config["revision"] and context["revision"] == revision
        _, source_digest, composition = _compose(context, {"sourceNodeId": source["id"], "outputNodeId": output["id"],
            "prompt": "晨雾中的旅人", "task": "text"})
        assert composition["prompt"] == "晨雾中的旅人"

    accept = text("""SELECT public.sceneturn_text_accept(
        :owner, :project, :job, :scene, :source, :output, :operation, 'text', :revision, :payload, :source_digest,
        :provider_revision, CAST(:frozen AS jsonb), 30000, 1000, :marker, :due)""")
    command = "晨雾中的旅人"
    messages = [{"role": "system", "content": task_instructions("text")}, {"role": "user", "content": command}]
    args = {"owner": owner, "project": project_id, "job": str(uuid.uuid4()), "scene": scene, "source": source["id"], "output": output["id"],
        "operation": str(uuid.uuid4()), "revision": revision, "payload": payload_digest(messages), "source_digest": source_digest,
        "provider_revision": config["revision"], "frozen": json.dumps({**composition, "messages": messages, "maxTokens": 64}),
        "marker": str(uuid.uuid4()), "due": epoch_milliseconds() + 45_000}
    replay = text("SELECT public.sceneturn_text_replay(:owner,:project,:operation,:payload)")
    with pytest.raises(DBAPIError) as expired:
        with rpc_transaction() as connection:
            connection.execute(accept, {**args, "due": epoch_milliseconds() - 1000})
    assert expired.value.orig.sqlstate == "PT409"
    with admin.connect() as connection:
        assert connection.execute(text("SELECT text_reserved_micros FROM sceneturn.budgets WHERE id = 'global'")).scalar_one() == 0
    with rpc_transaction() as connection:
        assert connection.execute(replay, args).scalar_one() is None
        accepted = connection.execute(accept, args).scalar_one()
        assert not accepted["replayed"] and accepted["job"]["reserved_cost_micros"] > 0
        assert connection.execute(accept, {**args, "job": str(uuid.uuid4())}).scalar_one()["replayed"]
        assert connection.execute(replay, args).scalar_one()["id"] == args["job"]
    with pytest.raises(DBAPIError) as foreign:
        with rpc_transaction() as connection:
            connection.execute(text("SELECT public.sceneturn_text_read(:owner, :job)"), {"owner": other_owner, "job": args["job"]})
    assert foreign.value.orig.sqlstate == "PT404"

    claim = text("SELECT public.sceneturn_text_claim(:job, :worker, :marker, :due, :immediate, :recovery, :recovery_due, 65, 'peak', :email)")
    worker = str(uuid.uuid4())
    claim_args = {"job": args["job"], "worker": worker, "marker": args["marker"], "due": args["due"],
        "immediate": False, "recovery": str(uuid.uuid4()), "recovery_due": epoch_milliseconds() + 95_000, "email": email}
    with rpc_transaction() as connection:
        assert connection.execute(claim, claim_args).scalar_one()["state"] == "early"
        claimed = connection.execute(claim, {**claim_args, "immediate": True}).scalar_one()
        assert claimed["state"] == "claimed" and claimed["job"]["model"] == "deepseek-flash"
        assert connection.execute(claim, {**claim_args, "immediate": True}).scalar_one()["state"] == "stale"
    second = {**args, "job": str(uuid.uuid4()), "operation": str(uuid.uuid4()), "marker": str(uuid.uuid4())}
    with rpc_transaction() as connection:
        connection.execute(accept, second)
        assert connection.execute(claim, {**claim_args, "job": second["job"], "marker": second["marker"],
            "immediate": True}).scalar_one()["state"] == "busy"
        stopped = connection.execute(text("SELECT public.sceneturn_text_stop(:owner,:job)"), second).scalar_one()
        assert stopped["job"]["status"] == "interrupted" and stopped["job"]["reserved_cost_micros"] == 0

    finish = text("""SELECT public.sceneturn_text_finish(
        :job, :worker, :outcome, :receipt, :model, CAST(:usage AS jsonb), :partial, CAST(:result AS jsonb), :output, :error, :message)""")
    receipt = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
               "prompt_cache_hit_tokens": 1, "prompt_cache_miss_tokens": 2}
    content = "旅人沿着石阶走进晨雾。"
    completion = {"job": args["job"], "worker": worker, "outcome": "received", "receipt": str(uuid.uuid4()),
        "model": "deepseek-flash", "usage": json.dumps(receipt), "partial": json.dumps({"text": content}),
        "result": json.dumps({"text": content}), "output": content, "error": None, "message": None}
    with pytest.raises(DBAPIError) as rejected_model:
        with rpc_transaction() as connection:
            connection.execute(finish, {**completion, "model": "deepseek-v4-pro"})
    assert rejected_model.value.orig.sqlstate == "PT422"
    with rpc_transaction() as connection:
        settled = connection.execute(finish, completion).scalar_one()["job"]
        assert settled["status"] == "ready" and settled["applied_revision"] == revision + 1
        assert settled["reserved_cost_micros"] == 0 and settled["cost_micros"] == 21
        assert connection.execute(finish, completion).scalar_one()["state"] == "lease_lost"
        usage = connection.execute(text("SELECT public.sceneturn_text_usage(:owner, :project)"), args).scalar_one()
        assert usage["totalTokens"] == 5 and usage["costMicros"] == 21 and len(usage["entries"]) == 1
        assert connection.execute(replay, args).scalar_one()["applied_revision"] == revision + 1
    with admin.connect() as connection:
        row = connection.execute(text("""SELECT value #>> '{metadata,content}' FROM sceneturn.canvases
            CROSS JOIN LATERAL json_array_elements(data_json -> 'nodes') WHERE project_id = :project AND value ->> 'id' = :output"""), args).scalar_one()
        assert row == content
        assert connection.execute(text("SELECT count(*) FROM sceneturn.project_versions WHERE project_id = :project AND revision = :new_revision"),
            {**args, "new_revision": revision + 1}).scalar_one() == 1

    next_args = {**args, "job": str(uuid.uuid4()), "operation": str(uuid.uuid4()), "marker": str(uuid.uuid4()),
        "revision": revision + 1, "due": epoch_milliseconds() + 45_000}
    with rpc_transaction() as connection:
        next_job = connection.execute(accept, next_args).scalar_one()["job"]
        assert connection.execute(claim, {**claim_args, "job": next_args["job"], "marker": next_args["marker"],
            "due": next_args["due"], "immediate": True}).scalar_one()["state"] == "claimed"
        unknown = connection.execute(finish, {"job": next_args["job"], "worker": worker, "outcome": "unknown", "receipt": None,
            "model": None, "usage": None, "partial": None, "result": None, "output": None,
            "error": "TEXT_SUBMISSION_UNKNOWN", "message": "原请求仍待核实"}).scalar_one()
        assert unknown["job"]["status"] == "submission_unknown"
        assert unknown["job"]["reserved_cost_micros"] == next_job["reserved_cost_micros"] > 0
        assert unknown["obsolete"]["jobId"] == next_args["job"]
        usage = connection.execute(text("SELECT public.sceneturn_text_usage(:owner, :project)"), args).scalar_one()
        assert usage["costMicros"] == 21 and len(usage["entries"]) == 1
