import hashlib
import sqlite3
import time
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun
from app.codex_image_models import CodexImageJob
from app.connection_models import CodexConnection
from app.core.errors import AppError
from app.core.security import new_token, token_hash
from app.image_models import ImageJob
from app.material_schemas import MaterialAnswer
from app.services.agent import stop_run
from app.services.codex_images import claim_job, connection_job
from app.services.connections import revoke_connection
from app.services.image_queue import claim_job as claim_cloud_image
from app.services.material_review import answer_review, public_review
from app.services.materials import can_start_production
from app.services.projects import create_project_record
from app.tools.project_tools import execute_tool
from test_codex_connections import account_project, pair
from test_material_reference_assets import reference_image, reference_workspace, storyboard_input
from test_media_tools import RestartableMediaService




def create(client, project_id, reference_ids=None):
    revision = client.get(f"/projects/{project_id}").json()["revision"]
    body = {"operationId": str(uuid.uuid4()), "expectedRevision": revision, "prompt": "本人图片任务本地接口检查，不提交模型请求。",
        "referenceAssetIds": reference_ids or [], "transparentBackground": False}
    first = client.post(f"/projects/{project_id}/codex-images", json=body)
    assert first.status_code == 201, first.text
    assert client.post(f"/projects/{project_id}/codex-images", json=body).json() == first.json()
    return first.json()["job"]


def capable(client, claimed):
    headers = {"Authorization": f"Bearer {claimed['token']}"}
    value = client.post("/connections/heartbeat", json={"capabilities": ["native_image_v1"]}, headers=headers)
    assert value.status_code == 200, value.text
    assert value.json()["connection"]["canGenerateImages"] is True
    return headers


def confirm(client, project_id, job, claimed):
    body = {"operationId": str(uuid.uuid4()), "connectionId": claimed["connection"]["id"], "accountUsageConfirmed": True}
    response = client.post(f"/projects/{project_id}/codex-images/{job['id']}/confirm", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["job"]["status"] == "queued"
    assert client.post(f"/projects/{project_id}/codex-images/{job['id']}/confirm", json=body).json() == response.json()


def test_image_job_requires_explicit_account_confirmation_and_capable_connection(live_service):
    with live_service.client() as client:
        project_id = account_project(client)
        _, claimed = pair(client, project_id)
        job = create(client, project_id)
        assert job["status"] == "awaiting_confirmation" and job["asset"] is None
        assert job["requestedModel"] == "gpt-image-2" and job["costStatus"] == "personal_account_unknown"
        body = {"operationId": str(uuid.uuid4()), "connectionId": claimed["connection"]["id"], "accountUsageConfirmed": True}
        path = f"/projects/{project_id}/codex-images/{job['id']}/confirm"
        assert client.post(path, json={**body, "accountUsageConfirmed": False}).status_code == 422
        assert client.post(path, json=body).status_code == 409
        headers = capable(client, claimed)
        confirm(client, project_id, job, claimed)
        response = client.post("/connections/images/claim", json={}, headers=headers)
        assert response.status_code == 200, response.text
        manifest = response.json()["result"]
        assert manifest["job"]["id"] == job["id"] and manifest["submissionAttempted"] is False
        assert manifest["job"]["threadId"] is None and manifest["job"]["turnId"] is None
        with sqlite3.connect(live_service.data_dir / "sceneturn.sqlite3") as database:
            assert database.execute("SELECT COUNT(*) FROM agent_calls WHERE run_id IN (SELECT id FROM agent_runs WHERE project_id=?)", (project_id,)).fetchone()[0] == 0


def test_image_reference_download_checks_selected_asset_owner_and_connection(live_service, sample_input_media):
    with live_service.client() as owner, live_service.client() as other:
        project_id = account_project(owner)
        _, claimed = pair(owner, project_id)
        headers = capable(owner, claimed)
        image_path, mime_type = sample_input_media["image"]
        original = image_path.read_bytes()
        uploaded = owner.post(f"/projects/{project_id}/assets", files={"file": (image_path.name, original, mime_type)})
        assert uploaded.status_code == 201, uploaded.text
        asset_id = uploaded.json()["asset"]["id"]
        job = create(owner, project_id, [asset_id])
        confirm(owner, project_id, job, claimed)
        owner.post("/connections/images/claim", json={}, headers=headers).raise_for_status()
        path = f"/connections/images/{job['id']}/references/{asset_id}/content"
        response = owner.get(path, headers=headers)
        assert response.status_code == 200 and hashlib.sha256(response.content).hexdigest() == hashlib.sha256(original).hexdigest()
        assert owner.get(path).status_code == 401
        assert owner.get(f"/connections/images/{job['id']}/references/{uuid.uuid4()}/content", headers=headers).status_code == 403
        other_id = account_project(other)
        _, other_claimed = pair(other, other_id)
        other_headers = capable(other, other_claimed)
        assert other.get(f"/projects/{project_id}/codex-images/{job['id']}").status_code == 404
        assert other.get(path, headers=other_headers).status_code == 404


def test_result_rejects_missing_native_completion_and_unresolved_file_id(live_service, sample_input_media):
    with live_service.client() as client:
        project_id = account_project(client)
        _, claimed = pair(client, project_id)
        headers = capable(client, claimed)
        job = create(client, project_id)
        confirm(client, project_id, job, claimed)
        client.post("/connections/images/claim", json={}, headers=headers).raise_for_status()
        path = f"/connections/images/{job['id']}"
        assert client.post(path + "/events", json={"eventType": "image_completed", "itemId": "unverified-event"}, headers=headers).status_code == 409
        image_path, mime_type = sample_input_media["image"]
        response = client.post(path + "/result", data={"metadata": '{"fileId":"unresolved"}'}, files={"file": (image_path.name, image_path.read_bytes(), mime_type)}, headers=headers)
        assert response.status_code == 422 and response.json()["error"]["code"] == "CODEX_IMAGE_METADATA_INVALID"
        assert client.get(f"/projects/{project_id}/codex-images/{job['id']}").json()["job"]["asset"] is None


def test_stopped_or_revoked_job_cannot_read_reference_or_accept_events(live_service):
    with live_service.client() as client:
        project_id = account_project(client)
        _, claimed = pair(client, project_id)
        headers = capable(client, claimed)
        job = create(client, project_id)
        confirm(client, project_id, job, claimed)
        client.post("/connections/images/claim", json={}, headers=headers).raise_for_status()
        path = f"/projects/{project_id}/codex-images/{job['id']}"
        body = {"operationId": str(uuid.uuid4())}
        stopped = client.post(path + "/stop", json=body)
        assert stopped.status_code == 200 and stopped.json()["job"]["status"] == "stopped"
        assert client.post(path + "/stop", json=body).json() == stopped.json()
        event = f"/connections/images/{job['id']}/events"
        assert client.post(event, json={"eventType": "heartbeat"}, headers=headers).status_code == 409
        client.post(f"/projects/{project_id}/connections/{claimed['connection']['id']}/revoke", json={"operationId": str(uuid.uuid4())}).raise_for_status()
        assert client.post(event, json={"eventType": "heartbeat"}, headers=headers).status_code == 403


def test_confirmed_native_image_task_and_connection_survive_real_restart(tmp_path, request):
    service = RestartableMediaService(tmp_path)
    request.addfinalizer(service.stop)
    service.start()
    with service.client() as client:
        project_id = account_project(client)
        _, claimed = pair(client, project_id)
        headers = capable(client, claimed)
        job = create(client, project_id)
        confirm(client, project_id, job, claimed)
        previous_pid = service.process.pid
        service.stop()
        service.start()
        assert service.process.pid != previous_pid
        current = client.get(f"/projects/{project_id}/codex-images/{job['id']}")
        assert current.status_code == 200 and current.json()["job"]["status"] == "queued"
        manifest = client.post("/connections/images/claim", json={}, headers=headers)
        assert manifest.status_code == 200 and manifest.json()["result"]["job"]["id"] == job["id"]
        assert manifest.json()["result"]["submissionAttempted"] is False


@pytest.mark.parametrize("revoke_first", [False, True])
def test_material_codex_approval_claim_scope_and_stop_survive_database_reopen(reference_workspace, revoke_first):
    session, settings, project, user, add_image, create_run = reference_workspace
    reference = add_image()
    run, step, conversation = create_run("storyboarder", [reference])
    run.limits_json = {**run.limits_json, "modelSelection": {"text": "deepseek-flash", "image": "codex-image", "video": "wan-3-0"}}
    session.commit()
    execute_tool(session, run, step.role, "write_artifact", {"artifact": storyboard_input()}, str(uuid.uuid4()), project.revision, settings, step)
    review = public_review(session, run)
    assert review["requiresPersonalAccountConfirmation"] is True and not review["usesProjectQuota"]
    assert {item["model"] for item in review["images"]} == {"gpt-image-2"}
    body = MaterialAnswer(operation_id=str(uuid.uuid4()), expected_revision=project.revision, review_id=review["id"])
    with pytest.raises(AppError) as missing_confirmation:
        answer_review(session, conversation, body, settings)
    assert missing_confirmation.value.code == "CODEX_IMAGE_ACCOUNT_CONFIRMATION_REQUIRED"
    body.account_usage_confirmed = True
    with pytest.raises(AppError) as missing_connection:
        answer_review(session, conversation, body, settings)
    assert missing_connection.value.code == "CODEX_IMAGE_CONNECTION_UNAVAILABLE"

    other = create_project_record(session, user.id, "另一部作品")

    def connect(project_id):
        return CodexConnection(owner_id=user.id, project_id=project_id, token_hash=token_hash(new_token()), tools_json=["read_project"],
            client_version="0.168.0", capabilities_json=["native_image_v1"], expires_at=time.time() + 600, last_seen_at=time.time())

    connection, unrelated = connect(project.id), connect(other.id)
    session.add_all([connection, unrelated])
    session.commit()
    answer_review(session, conversation, body, settings)
    session.commit()
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id).order_by(ImageJob.created_at)).all()
    assert len(jobs) == 2 and all(job.request_json["codexConnectionId"] == connection.id for job in jobs)
    assert claim_cloud_image(session) is None
    assert claim_job(session, unrelated, settings) is None
    with Session(session.get_bind()) as reopened:
        paired = reopened.get(CodexConnection, connection.id)
        claimed = claim_job(reopened, paired, settings)["job"]
        assert claimed["id"] == jobs[0].id and claimed["referenceAssets"][0]["sha256"] == reference.sha256
        assert claim_job(reopened, paired, settings)["job"]["id"] == jobs[0].id
        assert reopened.get(CodexImageJob, jobs[1].id) is None
        if revoke_first:
            revoke_connection(reopened, user.id, project.id, connection.id, str(uuid.uuid4()))
            current = reopened.get(AgentRun, run.id)
            assert "授权已失效" in public_review(reopened, current)["message"]
            assert not can_start_production(current, [])
        stopped = stop_run(reopened, project.id, run.id, user.id, str(uuid.uuid4()))
        assert stopped.status == "stopped" and can_start_production(stopped, [])
        assert reopened.get(CodexImageJob, jobs[0].id).status == "stopped"
        assert all(reopened.get(ImageJob, job.id).status == "cancelled" for job in jobs)
        if not revoke_first:
            assert claim_job(reopened, paired, settings) is None
        with pytest.raises(AppError) as stopped_connection:
            connection_job(reopened, paired, jobs[0].id)
        assert stopped_connection.value.code == ("CONNECTION_REVOKED" if revoke_first else "CODEX_IMAGE_LEASE_LOST")
