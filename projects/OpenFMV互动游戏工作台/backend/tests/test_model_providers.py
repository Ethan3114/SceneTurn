import asyncio
import base64
import copy
import json
import time
import uuid
from contextlib import closing

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent_models import AgentRun
from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import hash_password
from app.image_models import ImageJob
from app.models import Base, Budget, BudgetReservation, Project, ProjectVersion, User, new_id
from app.provider_models import ModelProviderConnection
from app.provider_schemas import ModelProviderUpdate, ProjectModelDefaultsUpdate
from app.services.ark import image_request, video_request
from app.services.image_queue import _execute_ark_job, claim_job, recover_jobs
from app.services.projects import create_project_record, payload_digest
from app.services.providers import LEGACY_DEFAULTS, MODEL_PROFILES, freeze_selection, profile, project_defaults, registry_view, set_project_defaults, update_provider
from app.services.qwen import endpoint, request_body
from test_api import create_project, error_code, registered_client, save_generation_node, quote_request, scene_id
from test_codex_connections import pair


@pytest.fixture
def provider_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'providers.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = User(email=f"{new_id()}@provider.invalid", password_hash=hash_password(new_id()))
        session.add(user)
        session.flush()
        project = create_project_record(session, user.id, "供应商设置验证")
        session.add(Budget(id="global", limit_cents=30000))
        session.commit()
        identifiers = user.id, project.id
    yield engine, identifiers
    engine.dispose()


def test_real_http_registry_defaults_ownership_and_unconfigured_generation(live_service):
    first, _ = registered_client(live_service)
    second, _ = registered_client(live_service)
    with closing(first), closing(second):
        envelope = create_project(first)
        project_id = envelope["project"]["id"]
        result = first.get("/settings/model-providers", params={"projectId": project_id})
        assert result.status_code == 200, result.text
        catalog = result.json()
        assert catalog["defaults"] == LEGACY_DEFAULTS
        assert {item["id"] for item in catalog["models"]} == set(MODEL_PROFILES)
        assert not any("doubao" in item["model"] for item in catalog["models"] if item["kind"] == "text")
        assert not any("qwen" in item["model"] for item in catalog["models"] if item["kind"] == "image")
        assert not any(item["available"] for item in catalog["models"])
        assert "apiKey" not in json.dumps(catalog)
        error_code(second.get("/settings/model-providers", params={"projectId": project_id}), 404)
        error_code(first.put("/settings/model-providers/aliyun-qwen", json={"expectedRevision": 0, "workspaceId": "workspace"}), 403)
        forbidden = {**LEGACY_DEFAULTS, "image": "codex-image", "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4())}
        assert error_code(first.put(f"/projects/{project_id}/model-defaults", json=forbidden), 409) == "CODEX_IMAGE_CONNECTION_UNAVAILABLE"
        request = {**LEGACY_DEFAULTS, "text": "qwen-flash", "image": "seedream-5-5", "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4())}
        saved = first.put(f"/projects/{project_id}/model-defaults", json=request)
        assert saved.status_code == 200, saved.text
        assert first.put(f"/projects/{project_id}/model-defaults", json=request).json() == saved.json()
        assert first.get(f"/projects/{project_id}/model-defaults").json() == saved.json()
        stale = {**request, "operationId": str(uuid.uuid4())}
        assert error_code(first.put(f"/projects/{project_id}/model-defaults", json=stale), 409) == "REVISION_CONFLICT"
        envelope = first.get(f"/projects/{project_id}").json()
        scene_id, node_id, _ = save_generation_node(first, envelope)
        video = {**quote_request(scene_id, node_id), "modelProfileId": "seedance-2-5"}
        assert error_code(first.post(f"/projects/{project_id}/generations/quote", json=video), 503) == "MODEL_NOT_CONFIGURED"


def test_personal_codex_default_requires_an_image_capable_project_connection(live_service):
    owner, _ = registered_client(live_service)
    with closing(owner):
        project = create_project(owner)
        project_id = project["project"]["id"]
        other_id = create_project(owner)["project"]["id"]
        registry = "/settings/model-providers"
        defaults = f"/projects/{project_id}/model-defaults"

        def image_model(identifier):
            response = owner.get(registry, params={"projectId": identifier})
            response.raise_for_status()
            return next(model for model in response.json()["models"] if model["id"] == "codex-image")

        _, claimed = pair(owner, project_id)
        assert image_model(project_id)["available"] is False
        headers = {"Authorization": f"Bearer {claimed['token']}"}
        owner.post("/connections/heartbeat", json={"capabilities": ["native_image_v1"]}, headers=headers).raise_for_status()
        assert image_model(project_id)["available"] is True
        assert image_model(other_id)["available"] is False

        body = {**LEGACY_DEFAULTS, "image": "codex-image", "expectedRevision": project["revision"], "operationId": new_id()}
        saved = owner.put(defaults, json=body)
        saved.raise_for_status()
        connection_id = claimed["connection"]["id"]
        owner.post(f"/projects/{project_id}/connections/{connection_id}/revoke", json={"operationId": new_id()}).raise_for_status()
        assert image_model(project_id)["available"] is False
        assert owner.get(defaults).json() == saved.json()
        assert owner.put(defaults, json=body).json() == saved.json()
        assert saved.json()["defaults"]["image"] == "codex-image"


def test_prices_and_entitlements_are_versioned_without_claiming_a_key(provider_database, tmp_path):
    engine, (owner_id, project_id) = provider_database
    settings = Settings(data_dir=tmp_path, text_admin_email="")
    with Session(engine) as session:
        user = session.get(User, owner_id)
        body = ModelProviderUpdate(expected_revision=0, entitled_model_ids=["seedance-1-0-pro"], prices={
            "seedance-1-0-pro": {"version": "作者核实价表", "verified": True, "centsPerSecond": {"720P": 50}, "outputMicrosPerMillion": 12000000}})
        result = update_provider(session, user, "volcengine-ark", body, settings)
        stored = session.scalar(select(ModelProviderConnection))
        assert stored.api_key_encrypted is None and stored.revision == 1
        item = next(value for value in result["models"] if value["id"] == "seedance-1-0-pro")
        assert item["configured"] is False and item["available"] is False
        selected = ProjectModelDefaultsUpdate(**{**LEGACY_DEFAULTS, "video": "seedance-1-0-pro"}, expected_revision=1, operation_id=new_id())
        saved = set_project_defaults(session, project_id, user, selected)
        assert saved["revision"] == 2
        project = session.get(Project, project_id)
        assert project_defaults(project)["video"] == "seedance-1-0-pro"
        assert len(session.scalars(select(ProjectVersion).where(ProjectVersion.project_id == project_id)).all()) == 2
        with pytest.raises(AppError) as failure:
            freeze_selection(session, owner_id, project, "video")
        assert failure.value.code == "MODEL_NOT_CONFIGURED"
        session.rollback()
        with pytest.raises(AppError) as failure:
            update_provider(session, user, "volcengine-ark", body, settings)
        assert failure.value.code == "MODEL_CONFIGURATION_CONFLICT"


def test_capabilities_and_official_request_bodies_are_frozen_without_supplier_calls(sample_input_media):
    assert profile("seedance-2-5-flash")["capabilities"]["resolutions"] == ["480P", "720P"]
    assert profile("seedance-1-0-pro-fast")["capabilities"]["lastFrame"] is False
    image_path, mime = sample_input_media["image"]
    image_url = f"data:{mime};base64,{base64.b64encode(image_path.read_bytes()).decode()}"
    video = {"modelProfile": {"profileId": "seedance-2-5"}, "model": profile("seedance-2-5")["model"],
        "prompt": "作者填写的连续镜头需求", "duration": 5, "resolution": "720P", "audio": True,
        "media": [{"type": "first_frame", "url": image_url}, {"type": "last_frame", "url": image_url}]}
    original = copy.deepcopy(video)
    body = video_request(video)
    assert video == original and body["ratio"] == "adaptive" and body["generate_audio"] is True
    assert [item.get("role") for item in body["content"][1:]] == ["first_frame", "last_frame"]
    invalid = {**video, "modelProfile": {"profileId": "seedance-1-0-pro-fast"}, "model": profile("seedance-1-0-pro-fast")["model"]}
    with pytest.raises(AppError) as failure:
        video_request(invalid)
    assert failure.value.code == "MODEL_LAST_FRAME_UNSUPPORTED"
    request = {"modelProfile": {"profileId": "seedream-5-5"}, "prompt": "作者填写的图片需求", "width": 2048, "height": 1152}
    assert image_request(request, [])["response_format"] == "b64_json"
    with pytest.raises(AppError):
        image_request({**request, "width": 512}, [])
    qwen = request_body([{"role": "user", "content": "请按json格式交付脚本"}], 2048)
    assert qwen["model"] == "qwen3.8-flash" and qwen["enable_thinking"] is False and qwen["preserve_thinking"] is False
    assert "tools" not in qwen and endpoint("workspace") == "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
    with pytest.raises(AppError):
        endpoint("workspace", "international")


def test_paid_image_claim_recovery_and_missing_configuration_never_reserves_or_sends(provider_database, tmp_path):
    engine, (owner_id, project_id) = provider_database
    with Session(engine) as session:
        project = session.get(Project, project_id)
        scene_id = next(item["id"] for item in project.data_json["graphData"]["nodes"] if item["type"] == "scene")
        run = AgentRun(owner_id=owner_id, project_id=project_id, scene_id=scene_id, operation_id=new_id(), role="character_designer",
            executor="platform", goal="作者要求生成已确认图片", initial_revision=1, project_revision=1, limits_json={}, status="waiting_confirmation")
        session.add(run)
        session.flush()
        job = ImageJob(owner_id=owner_id, project_id=project_id, scene_id=scene_id, run_id=run.id, node_id=new_id(), artifact_hash=payload_digest({"authorRequirement": "作者填写的图片需求"}),
            operation_id=new_id(), label="作者确认的图片", status="queued", confirmed_at=time.time(), request_json={"modelProfile": {
                "providerId": "volcengine-ark", "profileId": "seedream-5-5", "model": profile("seedream-5-5")["model"],
                "connectionId": new_id(), "connectionRevision": 1, "price": {"microsPerImage": 100000}},
                "width": 2048, "height": 1152, "prompt": "作者填写的图片需求"})
        session.add(job)
        session.commit()
        identifier = claim_job(session)
        assert identifier == job.id and job.account_id is None and job.status == "claimed"
        assert claim_job(session) is None
        recover_jobs(session)
        assert session.get(ImageJob, identifier).status == "queued"
        assert claim_job(session) == identifier
    with pytest.raises(AppError) as failure:
        asyncio.run(_execute_ark_job(identifier, Settings(data_dir=tmp_path), sessionmaker(engine)))
    assert failure.value.code == "MODEL_CONFIGURATION_CHANGED"
    with Session(engine) as session:
        budget = session.get(Budget, "global")
        assert (budget.text_reserved_micros, budget.text_spent_micros) == (0, 0)
        assert session.scalar(select(BudgetReservation)) is None
        job = session.get(ImageJob, identifier)
        assert job.status == "claimed" and job.submitted_at is None and job.attempt_count == 0


def test_real_sequence_keeps_original_model_when_project_default_changes(live_service, sample_input_media):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        project_id, target_scene = envelope["project"]["id"], scene_id(envelope)
        image_path, mime = sample_input_media["image"]
        with image_path.open("rb") as stream:
            upload = client.post(f"/projects/{project_id}/assets", files={"file": (image_path.name, stream, mime)})
        assert upload.status_code == 201, upload.text
        asset_id = upload.json()["asset"]["id"]
        sequence = client.post(f"/projects/{project_id}/scenes/{target_scene}/media-sequences", json={
            "operationId": str(uuid.uuid4()), "expectedRevision": upload.json()["revision"], "title": "作者连续镜头",
            "frameAssetIds": [asset_id, asset_id], "segments": [{"title": "静物镜头", "prompt": "镜头保持稳定", "duration": 5, "resolution": "720P"}]})
        assert sequence.status_code == 201, sequence.text
        value = sequence.json()
        config = client.put("/settings/model-providers/volcengine-ark", json={"expectedRevision": 0, "entitledModelIds": ["seedance-2-5"]})
        assert config.status_code == 200, config.text
        defaults = client.put(f"/projects/{project_id}/model-defaults", json={**LEGACY_DEFAULTS, "video": "seedance-2-5",
            "expectedRevision": value["project"]["revision"], "operationId": str(uuid.uuid4())})
        assert defaults.status_code == 200, defaults.text
        quoted = client.post(f"/projects/{project_id}/media-sequences/{value['sequence']['id']}/segments/segment-1/quote", json={
            "operationId": str(uuid.uuid4()), "expectedRevision": defaults.json()["revision"]})
        assert quoted.status_code == 201, quoted.text
        generation = quoted.json()["generation"]
        assert generation["input"]["model"] == "wan3.0-video" and generation["input"]["request"]["modelProfileId"] == "wan-3-0"
        assert generation["status"] == "awaiting_confirmation"
