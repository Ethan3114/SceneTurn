import json
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.services.cloudbase_video import video_delivery
from app.services.deployment_queue import epoch_milliseconds
from app.services.projects import payload_digest
from conftest import PRODUCT_ROOT
from test_api import create_project, scene_id
from test_generation_canvas import node, save_canvas
from test_postgresql_persistence import postgres_runtime
from test_transfer_rpc_postgresql import transfer_rpc_database


@pytest.fixture
def video_rpc_database(transfer_rpc_database, request):
    admin, _, _ = transfer_rpc_database
    source = PRODUCT_ROOT / "backend" / "sql" / "cloudbase" / "video_generations.sql"
    with admin.begin() as connection:
        connection.exec_driver_sql(source.read_text(), execution_options={"no_parameters": True})

    def cleanup():
        with admin.begin() as connection:
            functions = connection.exec_driver_sql("""
                SELECT pg_get_function_identity_arguments(function.oid), function.proname
                FROM pg_proc AS function JOIN pg_namespace AS namespace ON namespace.oid = function.pronamespace
                WHERE namespace.nspname = 'public' AND starts_with(function.proname, 'sceneturn_video_')
            """).all()
            for arguments, name in functions:
                connection.exec_driver_sql(f"DROP FUNCTION public.{name}({arguments})")

    request.addfinalizer(cleanup)
    return transfer_rpc_database


@pytest.mark.parametrize("profile,status", [("wan-3-0", "submission_unknown"), ("seedance-2-0", "processing")])
def test_paid_video_confirmation_atomically_reserves_and_never_duplicates(video_rpc_database, profile, status):
    admin, start, rpc_transaction = video_rpc_database
    prompt = "春雾散开，画面停在一座石桥上。"
    with start(profile) as service, service.client() as client, service.client() as stranger:
        def register(api):
            response = api.post("/auth/register", json={"email": f"video-{uuid.uuid4().hex}@example.com",
                "password": secrets.token_urlsafe(24)})
            assert response.status_code == 201
            return response.json()["user"]["id"]

        owner, neighbor = register(client), register(stranger)
        if profile == "wan-3-0":
            configured = client.put("/settings/wan", json={"workspaceId": "ws-test-project", "apiKey": secrets.token_urlsafe(24)})
            per_second = 60
        else:
            configured = client.put("/settings/model-providers/volcengine-ark", json={"expectedRevision": 0,
                "apiKey": secrets.token_urlsafe(24), "entitledModelIds": [profile], "prices": {profile: {
                    "version": "integration-test-record", "verified": True, "centsPerSecond": {"720P": 100},
                    "outputMicrosPerMillion": 70000000}}})
            per_second = 100
        assert configured.status_code == 200
        envelope = save_canvas(client, create_project(client), [node("source", "video", "镜头", prompt=prompt)])
        project_id, scene = envelope["project"]["id"], scene_id(envelope)
        quote_response = client.post(f"/projects/{project_id}/generations/quote", json={"sceneId": scene,
            "sourceNodeId": "source", "prompt": prompt, "duration": 4, "operationId": str(uuid.uuid4()), "modelProfileId": profile})
        assert quote_response.status_code == 201
        local_job_id = quote_response.json()["generation"]["id"]

    with admin.connect() as connection:
        source, frozen = connection.execute(text("SELECT source_digest,input_json FROM sceneturn.generations WHERE id = :job"),
            {"job": local_job_id}).one()

    operation = str(uuid.uuid4())
    frozen["request"]["operationId"] = operation
    params = {"owner": owner, "project": project_id, "job": str(uuid.uuid4()), "scene": scene, "source": source,
        "clip": str(uuid.uuid4()), "revision": envelope["revision"], "operation": operation,
        "payload": payload_digest(frozen["request"]), "frozen": json.dumps(frozen),
        "dispatch": str(uuid.uuid4()), "due": epoch_milliseconds() + 45_000}
    quote = text("""SELECT public.sceneturn_video_quote(:owner, :project, :job, :scene, 'source', :clip,
        :revision, :operation, :payload, :source, CAST(:frozen AS jsonb))""")
    confirm = text("""SELECT public.sceneturn_video_confirm(:owner, :job, :hash, :revision, :source,
        :budget_limit, 1000, :dispatch, :due)""")

    with rpc_transaction() as connection:
        value = connection.execute(quote, params).scalar_one()["job"]
        view = video_delivery(value)
        assert view["status"] == "awaiting_confirmation" and view["quotedCents"] == 4 * per_second and view["reservedCents"] == 0
        assert connection.execute(quote, {**params, "job": str(uuid.uuid4())}).scalar_one()["replayed"]
        assert len(connection.execute(text("SELECT public.sceneturn_video_list(:owner,:project)"), params).scalar_one()) == 2
    params.update(hash=view["confirmationHash"], budget_limit=30000)
    for changes, error_code in [({"owner": neighbor}, "PT404"), ({"hash": "a" * 64}, "PT409"), ({"budget_limit": 1}, "PT402"),
                                ({"due": epoch_milliseconds() - 1000}, "PT409")]:
        with pytest.raises(DBAPIError) as rejected:
            with rpc_transaction() as connection:
                connection.execute(confirm, {**params, **changes})
        assert rejected.value.orig.sqlstate == error_code
    with admin.connect() as connection:
        assert connection.execute(text("SELECT reserved_cents FROM sceneturn.budgets WHERE id='global'")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM sceneturn.deployment_dispatch WHERE job_id=:job"), params).scalar_one() == 0

    def confirm_once(_):
        with rpc_transaction() as connection:
            return connection.execute(confirm, params).scalar_one()

    with ThreadPoolExecutor(max_workers=2) as workers:
        confirmed = list(workers.map(confirm_once, range(2)))
    assert sorted(item["replayed"] for item in confirmed) == [False, True]
    assert all(video_delivery(item["job"])["reservedCents"] == 4 * per_second for item in confirmed)

    claim = text("SELECT public.sceneturn_video_claim(:job,:worker,:dispatch,:due,:immediate,:recovery,:recover_at,60)")
    receive = text("""SELECT public.sceneturn_video_submission(:job,:worker,:outcome,:remote_task,:remote_request,
        :error,:message,:next_dispatch,:next_due)""")
    claim_params = {**params, "worker": str(uuid.uuid4()), "recovery": str(uuid.uuid4()), "recover_at": epoch_milliseconds() + 90_000}
    with rpc_transaction() as connection:
        assert connection.execute(claim, {**claim_params, "immediate": False}).scalar_one()["state"] == "early"
        claimed = connection.execute(claim, {**claim_params, "immediate": True}).scalar_one()
        assert claimed["phase"] == "submit" and claimed["job"]["status"] == "submitting"
        assert connection.execute(claim, {**claim_params, "immediate": True}).scalar_one()["state"] == "stale"
        receipt = {**claim_params, "outcome": "unknown", "remote_task": None, "remote_request": None,
            "error": "VIDEO_SUBMISSION_UNKNOWN", "message": "原请求待核实", "next_dispatch": None, "next_due": None}
        if status == "processing":
            receipt.update(outcome="submitted", remote_task=uuid.uuid4().hex, remote_request=uuid.uuid4().hex, error=None,
                message=None, next_dispatch=str(uuid.uuid4()), next_due=epoch_milliseconds() + 30_000)
        recorded = connection.execute(receive, receipt).scalar_one()
        assert video_delivery(recorded["job"])["status"] == status
        assert connection.execute(receive, receipt).scalar_one()["state"] == "lease_lost"
        replayed = connection.execute(confirm, params).scalar_one()
        assert replayed["replayed"] and video_delivery(replayed["job"])["status"] == status
        read = connection.execute(text("SELECT public.sceneturn_video_read(:owner,:job)"), params).scalar_one()
        assert video_delivery(read)["reservedCents"] == 4 * per_second
    with admin.connect() as connection:
        assert connection.execute(text("SELECT reserved_cents FROM sceneturn.budgets WHERE id='global'")).scalar_one() == 4 * per_second
