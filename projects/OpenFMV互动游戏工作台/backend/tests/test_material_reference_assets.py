import copy
import hashlib
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun, AgentStep
from app.agent_schemas import RunCreate
from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import hash_password
from app.creative_artifacts import validate_artifact
from app.image_models import ImageAccount, ImageJob
from app.material_models import MaterialConversation, MaterialMessage
from app.material_schemas import MaterialAnswer, MaterialSend, material_request_payload
from app.models import Asset, Base, Canvas, Project, User, new_id
from app.services.agent import _new_run_limits
from app.services.material_references import frozen_reference_assets, validate_reference_assets
from app.services.material_review import answer_review, pause_for_review, public_review
from app.services.materials import view
from app.services.projects import create_project_record, payload_digest
from app.tools.project_tools import _validate_artifact_sources, execute_tool
from test_api import create_project, error_code, registered_client, scene_id


@pytest.fixture(scope="session")
def reference_image(sample_video):
    image = sample_video.parent / "flower-frame.png"
    if not image.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(sample_video), "-frames:v", "1", str(image)], check=True, capture_output=True)
    return image


@pytest.fixture
def reference_workspace(tmp_path, reference_image):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", worker_enabled=False)
    settings.data_dir.mkdir()
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        user = User(id=new_id(), email=f"{uuid.uuid4()}@reference.invalid", password_hash=hash_password(str(uuid.uuid4())))
        session.add(user)
        session.flush()
        project = create_project_record(session, user.id, "单段素材参考测试")
        scene = next(node["id"] for node in project.data_json["graphData"]["nodes"] if node["type"] == "scene")
        session.commit()

        def add_image(name="选中的花卉.png", target_project=None):
            target = target_project or project
            identifier = new_id()
            destination = settings.data_dir / "media" / f"{identifier}.png"
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(reference_image, destination)
            raw = destination.read_bytes()
            asset = Asset(id=identifier, owner_id=target.owner_id, project_id=target.id, name=name,
                relpath=destination.relative_to(settings.data_dir).as_posix(), mime_type="image/png", bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(), width=960, height=540, metadata_json={})
            session.add(asset)
            session.commit()
            return asset

        def create_run(role="writer", selected=()):
            selection = validate_reference_assets(session, project, [asset.id for asset in selected], settings)
            body = RunCreate(operation_id=new_id(), expected_revision=project.revision, scene_id=scene, workspace="materials",
                role=role, executor="platform", delivery_mode="professional", goal="依据明确选择的图片制作一段素材")
            limits = _new_run_limits(body)
            limits["materialContext"] = {**limits["materialContext"], "referenceAssets": selection, "referenceAssetsRevision": project.revision}
            plan = [{"role": role, "title": "制作当前段素材", "instruction": "只使用当前段输入", "kind": "produce", "dependsOn": []}]
            run = AgentRun(id=new_id(), owner_id=user.id, project_id=project.id, scene_id=scene, operation_id=body.operation_id,
                role=role, executor="platform", goal=body.goal, target_ids_json=[], created_ids_json=[], limits_json=limits,
                initial_revision=project.revision, project_revision=project.revision, plan_json=plan, status="running", stop_requested=0)
            session.add(run)
            session.flush()
            step = AgentStep(id=new_id(), run_id=run.id, index=0, role=role, title=plan[0]["title"], instruction=plan[0]["instruction"],
                status="running", tool_results_json=[])
            conversation = MaterialConversation(id=new_id(), owner_id=user.id, project_id=project.id, scene_id=scene,
                title="当前段素材", active_run_id=run.id)
            session.add_all([step, conversation])
            session.flush()
            session.add(MaterialMessage(owner_id=user.id, conversation_id=conversation.id, operation_id=new_id(), sequence=1,
                content=body.goal, references_json=[], run_id=run.id))
            session.commit()
            return run, step, conversation

        yield session, settings, project, user, add_image, create_run
    engine.dispose()


def writer_input():
    return validate_artifact("writer", {"schemaVersion": 1, "role": "writer", "title": "查看花卉",
        "sources": [{"kind": "user_requirement", "referenceId": None, "description": "用户要求制作独立的一段观察花卉的素材"}],
        "assumptions": [{"topic": "镜头时长", "proposedValue": "两秒", "confirmationNeeded": True}],
        "premise": "观察者在庭院中发现一朵盛开的花。", "plot": "观察者走近花朵，停下观察，然后离开庭院。",
        "characters": [{"key": "observer", "name": "观察者", "identity": "庭院来访者", "motivation": "观察花朵", "arc": "保持平静", "visualAnchors": ["蓝色外套"]}],
        "scenes": [{"key": "opening", "heading": "外景，庭院，白天", "dramaticGoal": "发现花朵", "characters": ["observer"],
            "action": "观察者停在花朵旁边，俯身观察花瓣。", "dialogueDirection": "没有对白，保留自然环境声音。", "dialogues": [], "endingState": "观察者起身离开"}],
        "branchingDesign": "这段素材没有分支。", "branches": [],
        "endings": [{"key": "finish", "condition": "观察结束", "resolution": "观察者离开庭院。", "finalState": "花朵保留原位"}],
        "continuityRules": ["外套与光线保持一致"], "productionHandoff": ["人物参考使用用户选中的图片"]})


def image_prompt():
    return {"subject": "庭院中的花朵保持真实植物结构，花瓣数量和叶片关系明确，主体没有其他人物遮挡。",
        "action": "花朵静止生长在枝条顶端，叶片自然舒展，花瓣表面的细小纹理完整可见。",
        "scene": "白天的家庭庭院，背景保留绿色植被与土壤，前景没有遮挡主体的杂物和人物。",
        "composition": "横向中景画面，花朵位于画面中央偏右位置，镜头平视，背景轻微虚化而主体清晰。",
        "style": "真实自然摄影，保留花瓣和叶片的纹理，不添加绘画笔触或者塑料材质。",
        "lighting": "柔和的侧面自然光从画面左侧照入，花瓣阴影自然，绿色背景不过度饱和也不出现逆光光斑。",
        "details": "主体花瓣边缘清晰，枝条和花朵连接自然，前后画面中主体位置和植物形状保持一致。",
        "negativePrompt": ["水印文字", "重复花瓣", "塑料材质"],
        "outputSpec": {"purpose": "独立镜头的连续边界画面", "aspectRatio": "16:9", "width": 2048, "height": 1152,
            "imageCount": 1, "fileFormat": "png", "background": "自然庭院", "textPolicy": "没有文字和水印", "deliveryNotes": "单独保存画面"}}


def storyboard_input():
    prompt = image_prompt()
    return validate_artifact("storyboarder", {"schemaVersion": 2, "role": "storyboarder", "title": "单段花卉镜头",
        "sources": [{"kind": "user_requirement", "referenceId": None, "description": "使用用户选择的参考制作两秒独立镜头"}], "assumptions": [],
        "sequenceIntent": "保持连续的固定构图", "shots": [{"key": "one", "sceneKey": "opening", "durationSeconds": 2.0,
            "dramaticPurpose": "展示花朵", "framing": "中景", "cameraPosition": "平视", "cameraMovement": "固定机位",
            "action": "花朵轻微摆动", "dialogueSound": "自然风声", "transitionIn": "固定首帧", "transitionOut": "固定尾帧",
            "imagePrompt": prompt, "motionRequirements": ["运动缓慢"], "continuityChecks": ["花朵位置不变"],
            "firstFrameKey": "first", "lastFrameKey": "last"}],
        "keyframes": [{"key": "first", "title": "开始画面", "imagePrompt": prompt}, {"key": "last", "title": "结束画面", "imagePrompt": copy.deepcopy(prompt)}],
        "continuityRules": ["相同花朵与机位"], "productionHandoff": ["视频绑定固定首尾帧"]})


def save_writer(session, settings, project, run, step):
    result = execute_tool(session, run, step.role, "write_artifact", {"artifact": writer_input()}, new_id(), project.revision, settings, step)
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.scene_id == run.scene_id))
    node = next(node for node in canvas.data_json["nodes"] if node["id"] == result["result"]["sourceNodeId"])
    return node


def disabled_accounts(session, user):
    for slot in range(1, 4):
        session.add(ImageAccount(owner_id=user.id, slot=slot, credential_hash=hashlib.sha256(new_id().encode()).hexdigest(),
            api_key_encrypted="", status="disabled", active_job_id=None))
    session.commit()


def test_selected_real_files_are_frozen_and_survive_reopening(reference_workspace):
    session, settings, project, _, add_image, create_run = reference_workspace
    image = add_image()
    run, _, conversation = create_run(selected=[image])
    expected = [{"assetId": image.id, "name": image.name, "sha256": image.sha256}]
    assert frozen_reference_assets(session, run, settings) == expected
    assert view(session, conversation)["selectedReferenceAssets"] == expected
    session.expire_all()
    assert session.get(AgentRun, run.id).limits_json["materialContext"]["referenceAssets"] == expected


def test_ownership_type_and_frozen_hash_are_checked(reference_workspace):
    session, settings, project, user, add_image, create_run = reference_workspace
    image = add_image()
    run, _, _ = create_run(selected=[image])
    second = create_project_record(session, user.id, "其他项目")
    session.commit()
    foreign = add_image(target_project=second)
    with pytest.raises(AppError) as invalid:
        validate_reference_assets(session, project, [foreign.id], settings)
    assert invalid.value.code == "MATERIAL_REFERENCE_ASSET_INVALID"
    image.mime_type = "video/mp4"
    with pytest.raises(AppError) as invalid:
        validate_reference_assets(session, project, [image.id], settings)
    assert invalid.value.code == "MATERIAL_REFERENCE_IMAGE_REQUIRED"
    image.mime_type = "image/png"
    path = settings.data_dir / image.relpath
    path.write_bytes(path.read_bytes() + b"changed")
    image.bytes = path.stat().st_size
    image.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(AppError) as invalid:
        frozen_reference_assets(session, run, settings)
    assert invalid.value.code == "MATERIAL_REFERENCE_ASSET_CHANGED"


def test_registered_image_with_nonimage_file_is_rejected(reference_workspace):
    session, settings, project, _, add_image, _ = reference_workspace
    image = add_image()
    raw = "这是一段真实的文本文件内容。".encode()
    (settings.data_dir / image.relpath).write_bytes(raw)
    image.sha256, image.bytes = hashlib.sha256(raw).hexdigest(), len(raw)
    with pytest.raises(AppError) as invalid:
        validate_reference_assets(session, project, [image.id], settings)
    assert invalid.value.code == "MATERIAL_REFERENCE_IMAGE_INVALID"


def test_read_project_only_exposes_selected_images_for_current_round(reference_workspace):
    session, settings, project, _, add_image, create_run = reference_workspace
    selected, historical = add_image(), add_image("未选择的历史图片.png")
    run, step, _ = create_run(selected=[selected])
    result = execute_tool(session, run, step.role, "read_project", {}, new_id(), project.revision, settings, step)["result"]
    assert [asset["id"] for asset in result["assets"]] == [selected.id]
    assert result["referenceAssets"] == run.limits_json["materialContext"]["referenceAssets"]
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.scene_id == run.scene_id))
    artifact = writer_input()
    artifact["sources"] = [{"kind": "asset", "referenceId": historical.id, "description": "尝试引用未选择的图片"}]
    with pytest.raises(AppError) as invalid:
        _validate_artifact_sources(session, run, project, canvas, artifact, settings)
    assert invalid.value.code == "AGENT_ARTIFACT_SOURCE_INVALID"


def test_new_confirmation_image_requires_changes_and_preserves_old_artifact(reference_workspace):
    session, settings, project, _, add_image, create_run = reference_workspace
    first, added = add_image(), add_image("补充的真实参考.png")
    run, step, conversation = create_run(selected=[first])
    node = save_writer(session, settings, project, run, step)
    original_hash = payload_digest(node["metadata"]["creativeArtifact"])
    review = public_review(session, run)
    answers = {item["id"]: item["suggestedAnswer"] for item in review["questions"]}
    body = MaterialAnswer(operation_id=new_id(), expected_revision=project.revision, review_id=review["id"], answers=answers,
        reference_asset_ids=[added.id], requested_changes="   ")
    with pytest.raises(AppError) as invalid:
        answer_review(session, conversation, body, settings)
    assert invalid.value.code == "MATERIAL_REFERENCE_CHANGES_REQUIRED"
    assert run.status == "waiting_confirmation"
    body = body.model_copy(update={"requested_changes": "重新按补充的图片修订当前剧本中的人物锚点。"})
    answer_review(session, conversation, body, settings)
    session.commit()
    assert run.status == step.status == "queued"
    assert run.result_json["materialReview"]["status"] == "revision_requested"
    assert [item["assetId"] for item in view(session, conversation)["selectedReferenceAssets"]] == [first.id, added.id]
    assert payload_digest(node["metadata"]["creativeArtifact"]) == original_hash
    assert not session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()
    answer_review(session, conversation, body, settings)
    assert len(run.limits_json["materialContext"]["humanAnswers"]) == 1


def test_empty_answer_selection_keeps_frozen_images(reference_workspace):
    session, settings, project, _, add_image, create_run = reference_workspace
    image = add_image()
    run, step, conversation = create_run(selected=[image])
    save_writer(session, settings, project, run, step)
    review = public_review(session, run)
    body = MaterialAnswer(operation_id=new_id(), expected_revision=project.revision, review_id=review["id"],
        answers={item["id"]: item["suggestedAnswer"] for item in review["questions"]})
    answer_review(session, conversation, body, settings)
    session.commit()
    assert run.result_json["materialReview"]["status"] == "approved"
    assert [item["assetId"] for item in run.limits_json["materialContext"]["referenceAssets"]] == [image.id]


def test_storyboard_jobs_keep_selected_references_and_wait_for_confirmation(reference_workspace):
    session, settings, project, user, add_image, create_run = reference_workspace
    image = add_image()
    run, step, _ = create_run("storyboarder", [image])
    disabled_accounts(session, user)
    artifact = storyboard_input()
    node = {"id": new_id()}
    pause_for_review(session, run, step, node, artifact, settings)
    session.commit()
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id).order_by(ImageJob.created_at)).all()
    assert len(jobs) == 2
    assert all(job.status == "awaiting_confirmation" and job.reference_asset_ids == [image.id] for job in jobs)
    assert all(job.request_json["referenceAssets"] == run.limits_json["materialContext"]["referenceAssets"] for job in jobs)
    assert jobs[0].dependency_ids == [] and jobs[1].dependency_ids == [jobs[0].id]
    assert all(account.active_job_id is None for account in session.scalars(select(ImageAccount)))


def test_five_images_and_previous_frame_fail_before_any_job_is_added(reference_workspace):
    session, settings, _, user, add_image, create_run = reference_workspace
    images = [add_image(f"参考{index}.png") for index in range(5)]
    run, step, conversation = create_run("storyboarder", images)
    disabled_accounts(session, user)
    with pytest.raises(AppError) as invalid:
        pause_for_review(session, run, step, {"id": new_id()}, storyboard_input(), settings)
    assert invalid.value.code == "MATERIAL_REFERENCE_CAPACITY_EXCEEDED"
    assert "5张参考图片" in invalid.value.message and "1张上一画面" in invalid.value.message
    assert not [item for item in session.new if isinstance(item, ImageJob)]
    run.status, run.error_code, run.error_message = "failed", invalid.value.code, invalid.value.message
    session.commit()
    assert not session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()
    message = view(session, conversation)["messages"][-1]["run"]["error"]["message"]
    assert "共6张" in message and "至多4张" in message
    assert run.id not in message and all(image.id not in message for image in images)


def test_new_visual_reference_recreates_review_and_keeps_new_jobs_unconfirmed(reference_workspace):
    session, settings, project, user, add_image, create_run = reference_workspace
    first, added = add_image(), add_image("新增构图参考.png")
    run, step, conversation = create_run("storyboarder", [first])
    disabled_accounts(session, user)
    execute_tool(session, run, step.role, "write_artifact", {"artifact": storyboard_input()}, new_id(), project.revision, settings, step)
    previous = public_review(session, run)
    answer_review(session, conversation, MaterialAnswer(operation_id=new_id(), expected_revision=project.revision,
        review_id=previous["id"], reference_asset_ids=[added.id], requested_changes="根据补充参考重新制作这个镜头的两张边界画面。"), settings)
    session.commit()
    old_jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()
    assert len(old_jobs) == 2 and all(job.status == "cancelled" for job in old_jobs)
    revised = storyboard_input()
    revised["title"] = "依据新增参考修订的单段花卉镜头"
    revised["sources"].append({"kind": "asset", "referenceId": added.id, "description": "用户在确认卡中补充的构图参考图片"})
    execute_tool(session, run, step.role, "write_artifact", {"artifact": revised}, new_id(), project.revision, settings, step)
    current = public_review(session, run)
    assert current["id"] != previous["id"]
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id, ImageJob.status == "awaiting_confirmation")).all()
    assert len(jobs) == 2 and all(job.reference_asset_ids == [first.id, added.id] for job in jobs)
    assert all(job.confirmed_at is None and job.submitted_at is None for job in jobs)
    assert run.status == step.status == "waiting_confirmation"
    assert all(account.active_job_id is None for account in session.scalars(select(ImageAccount)))


def test_answer_combined_reference_count_is_limited_before_changes(reference_workspace):
    session, settings, project, _, add_image, create_run = reference_workspace
    images = [add_image(f"参考{index}.png") for index in range(6)]
    run, step, conversation = create_run(selected=images[:5])
    save_writer(session, settings, project, run, step)
    review = public_review(session, run)
    body = MaterialAnswer(operation_id=new_id(), expected_revision=project.revision, review_id=review["id"],
        answers={item["id"]: item["suggestedAnswer"] for item in review["questions"]},
        reference_asset_ids=[images[5].id], requested_changes="使用新增图片重新修订。")
    with pytest.raises(AppError) as invalid:
        answer_review(session, conversation, body, settings)
    assert invalid.value.code == "MATERIAL_REFERENCE_ASSET_LIMIT"
    assert len(run.limits_json["materialContext"]["referenceAssets"]) == 5
    assert run.status == "waiting_confirmation" and run.result_json["materialReview"]["status"] == "pending"


def test_reference_lists_reject_duplicates_and_more_than_five():
    base = {"operationId": new_id(), "expectedRevision": 1, "content": "制作当前段素材"}
    for identifiers in (["same", "same"], [""], [str(index) for index in range(6)]):
        with pytest.raises(ValidationError):
            MaterialSend.model_validate({**base, "referenceAssetIds": identifiers})
    assert MaterialSend.model_validate(base).reference_asset_ids == []
    assert "referenceAssetIds" not in material_request_payload(MaterialSend.model_validate(base))
    answer = MaterialAnswer(operation_id=new_id(), expected_revision=1, review_id=new_id())
    assert "referenceAssetIds" not in material_request_payload(answer)
    with pytest.raises(ValidationError):
        MaterialAnswer.model_validate({**answer.model_dump(by_alias=True), "referenceAssetIds": ["same", "same"]})


def test_real_http_upload_and_reference_errors_keep_uniform_envelope(live_service, reference_image):
    client, _ = registered_client(live_service)
    outsider, _ = registered_client(live_service)
    try:
        project = create_project(client)
        identifier = project["project"]["id"]
        with reference_image.open("rb") as stream:
            uploaded = client.post(f"/projects/{identifier}/assets", files={"file": ("花卉参考.png", stream, "image/png")})
        assert uploaded.status_code == 201, uploaded.text
        envelope = uploaded.json()
        path = f"/projects/{identifier}/scenes/{scene_id(project)}/material-conversations"
        conversation = client.post(path, json={"operationId": new_id()}).json()["conversation"]
        assert conversation["selectedReferenceAssets"] == []
        endpoint = f"{path}/{conversation['id']}/messages"
        body = {"operationId": new_id(), "expectedRevision": project["revision"], "content": "根据图片制作当前段", "referenceAssetIds": [envelope["asset"]["id"]]}
        assert error_code(client.post(endpoint, json=body), 409) == "REVISION_CONFLICT"
        body.update(operationId=new_id(), expectedRevision=envelope["revision"], referenceAssetIds=["missing"])
        assert error_code(client.post(endpoint, json=body), 422) == "MATERIAL_REFERENCE_ASSET_INVALID"
        assert error_code(outsider.post(endpoint, json=body), 404) == "PROJECT_NOT_FOUND"
        assert client.get(f"{path}/{conversation['id']}").json()["conversation"]["messages"] == []
    finally:
        client.close()
        outsider.close()
