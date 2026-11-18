import copy
import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun, AgentStep
from app.core.errors import AppError
from app.creative_artifacts import image_prompt_items, render_artifact, validate_artifact
from app.image_models import ImageAccount, ImageJob
from app.material_models import MaterialConversation, MaterialMessage
from app.material_schemas import MaterialAnswer
from app.models import Project
from app.services.agent import require_step_delivery, resume_run
from app.services.agent_provider import MATERIAL_NATIVE_AGENT_PROTOCOL, agent_request_body, agent_tools
from app.core.config import Settings
from app.services.image_queue import claim_job, recover_jobs
from app.services.material_review import answer_review, pause_for_review, public_review
from app.services.materials import view
from app.tools.project_tools import require_canvas


ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "eafe14be-35fd-4bf6-97c3-613133121dcd"


@pytest.fixture
def actual_database(tmp_path):
    database = tmp_path / "openfmv.sqlite3"
    with sqlite3.connect(f"{(ROOT / 'data/openfmv.sqlite3').resolve().as_uri()}?mode=ro", uri=True) as source, sqlite3.connect(database) as destination:
        source.backup(destination)
    engine = create_engine(f"sqlite:///{database}", connect_args={"timeout": 10})
    yield engine
    engine.dispose()


def actual_role(session, role):
    run = session.get(AgentRun, RUN_ID)
    step = session.scalar(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.role == role).order_by(AgentStep.index))
    project = session.get(Project, run.project_id)
    canvas = require_canvas(session, run, project)
    node = next(item for item in canvas.data_json["nodes"] if item["id"] in run.created_ids_json and item.get("metadata", {}).get("creativeArtifact", {}).get("role") == role)
    run.project_revision = project.revision
    run.limits_json = {**run.limits_json, "materialContext": {**run.limits_json["materialContext"], "requireHumanReview": True}}
    message = session.scalar(select(MaterialMessage).where(MaterialMessage.run_id == run.id))
    return run, step, node, session.get(MaterialConversation, message.conversation_id)


def test_real_writer_delivery_omits_prompts_sources_and_internal_receipts(actual_database):
    with Session(actual_database) as session:
        run, step, node, conversation = actual_role(session, "writer")
        artifact = node["metadata"]["creativeArtifact"]
        pause_for_review(session, run, step, node, artifact)
        session.commit()
        result = view(session, conversation)
        writer = next(item for item in result["nodes"] if item["id"] == node["id"])
        assert artifact["title"] in writer["metadata"]["content"]
        assert artifact["plot"] not in writer["metadata"]["content"]
        assert all(item["action"] in writer["metadata"]["content"] for item in artifact["scenes"])
        encoded = json.dumps(result, ensure_ascii=False)
        assert '"creativeArtifact"' not in encoded and '"generationPrompts"' not in encoded
        assert '"savedTexts"' not in encoded and '"artifactSources"' not in encoded
        assert result["pendingReview"]["questions"]
        assert len(result["pendingReview"]["questions"]) == sum(item["confirmationNeeded"] for item in artifact["assumptions"])
        assert result["messages"][-1]["run"]["steps"] and run.status == "waiting_confirmation"


def test_actual_saved_script_and_public_titles_preserve_final_words_and_private_records(actual_database):
    with Session(actual_database) as session:
        run, _, node, conversation = actual_role(session, "writer")
        artifact = copy.deepcopy(node["metadata"]["creativeArtifact"])
        original_plan = copy.deepcopy(run.plan_json)
        original_steps = [(step.id, step.title) for step in session.scalars(select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.index))]
        result = view(session, conversation)
        writer = next(item for item in result["nodes"] if item["id"] == node["id"])
        text = writer["delivery"]["text"]
        assert text == writer["metadata"]["content"]
        assert artifact["plot"] not in text
        names = {item["key"]: item["name"] for item in artifact["characters"]}
        for scene in artifact["scenes"]:
            assert scene["heading"] in text and scene["action"] in text and scene["endingState"] in text
            for line in scene["dialogues"]:
                assert names[line["speaker"]] in text and line["line"] in text and line["performance"] in text
        for ending in artifact["endings"]:
            assert all(ending[field] in text for field in ("condition", "resolution", "finalState"))
        titles = {"director": "核对制作成果", "writer": "编写脚本", "character_designer": "设计角色形象", "scene_designer": "设计场景",
                  "storyboarder": "制作分镜图片", "media_producer": "制作镜头视频", "sound_designer": "设计声音"}
        visible = next(message["run"] for message in result["messages"] if message["run"]["id"] == run.id)
        for public, original in zip(visible["plan"], original_plan, strict=True):
            assert public["title"] == ("核对制作成果" if original.get("kind") == "verify" else titles[original["role"]])
            assert public["instruction"] == ""
        for public, original in zip(visible["steps"], original_plan, strict=True):
            assert public["title"] == ("核对制作成果" if original.get("kind") == "verify" else titles[original["role"]])
        assert run.plan_json == original_plan and node["metadata"]["creativeArtifact"] == artifact
        assert [(step.id, step.title) for step in session.scalars(select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.index))] == original_steps


def test_real_confirmation_cannot_be_bypassed_and_answers_survive_session_restart(actual_database):
    with Session(actual_database) as session:
        run, step, node, conversation = actual_role(session, "writer")
        pause_for_review(session, run, step, node, node["metadata"]["creativeArtifact"])
        session.commit()
        review = public_review(session, run)
        with pytest.raises(AppError, match="素材对话"):
            resume_run(session, run.project_id, run.id, run.owner_id, str(uuid.uuid4()), run.project_revision)
        session.rollback()
        with pytest.raises(AppError) as failure:
            require_step_delivery(session, run, step, session.get(Project, run.project_id), Settings())
        assert failure.value.code == "MATERIAL_CONFIRMATION_REQUIRED"
        body = MaterialAnswer(operation_id=str(uuid.uuid4()), expected_revision=run.project_revision,
            review_id=review["id"], answers={item["id"]: item["suggestedAnswer"] for item in review["questions"]})
        answer_review(session, conversation, body)
        session.commit()
    with Session(actual_database) as session:
        run = session.get(AgentRun, RUN_ID)
        assert run.status == "queued" and run.result_json["materialReview"]["status"] == "approved"
        assert run.limits_json["materialContext"]["humanAnswers"][-1]["answers"] == body.answers
        assert public_review(session, run) is None


def test_custom_answer_requires_real_revision_before_handoff(actual_database):
    with Session(actual_database) as session:
        run, step, node, conversation = actual_role(session, "writer")
        pause_for_review(session, run, step, node, node["metadata"]["creativeArtifact"])
        session.commit()
        review = public_review(session, run)
        body = MaterialAnswer(operation_id=str(uuid.uuid4()), expected_revision=run.project_revision,
            review_id=review["id"], answers={item["id"]: item["suggestedAnswer"] for item in review["questions"]}, requested_changes="将最后的动作改为收起火莲，保留九秒时长。")
        answer_review(session, conversation, body)
        session.commit()
        assert run.result_json["materialReview"]["status"] == "revision_requested"
        with pytest.raises(AppError) as failure:
            require_step_delivery(session, run, step, session.get(Project, run.project_id), Settings())
        assert failure.value.code == "MATERIAL_REVISION_REQUIRED"


def test_concurrent_claims_respect_all_three_real_accounts_and_keep_unknown_occupied(actual_database):
    with Session(actual_database) as session:
        accounts = session.scalars(select(ImageAccount)).all()
        preserved = {account.id: account.active_job_id for account in accounts if account.active_job_id}
        available = sum(account.status == "available" and account.active_job_id is None for account in accounts)
        assert len(accounts) == 3
        run, step, node, _ = actual_role(session, "character_designer")
        pause_for_review(session, run, step, node, node["metadata"]["creativeArtifact"])
        jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()
        assert jobs
        original = jobs[0]
        while len(jobs) < 4:
            job = ImageJob(owner_id=original.owner_id, project_id=original.project_id, scene_id=original.scene_id,
                run_id=original.run_id, node_id=original.node_id, artifact_hash=original.artifact_hash,
                operation_id=str(uuid.uuid4()), label=original.label, status="queued", request_json=original.request_json,
                reference_asset_ids=[], dependency_ids=[])
            session.add(job)
            jobs.append(job)
        for job in jobs:
            job.status = "queued"
        session.commit()
    def claim():
        with Session(actual_database) as session:
            return claim_job(session)
    with ThreadPoolExecutor(max_workers=4) as executor:
        identifiers = list(executor.map(lambda _: claim(), range(4)))
    assert len([identifier for identifier in identifiers if identifier]) == available
    assert len(set(identifier for identifier in identifiers if identifier)) == available
    with Session(actual_database) as session:
        occupied = session.scalars(select(ImageAccount).where(ImageAccount.active_job_id.is_not(None))).all()
        assert len(occupied) == 3 and len({account.slot for account in occupied}) == 3
        assert session.scalar(select(ImageJob).where(ImageJob.status == "queued")) is not None
        recover_jobs(session)
    with Session(actual_database) as session:
        assert {account.id: account.active_job_id for account in session.scalars(select(ImageAccount)) if account.active_job_id} == preserved
        assert not session.scalars(select(ImageJob).where(ImageJob.status == "claimed")).all()


def test_new_storyboard_contract_reuses_boundary_keys_and_preserves_legacy_artifacts(actual_database):
    with Session(actual_database) as session:
        _, _, node, _ = actual_role(session, "storyboarder")
        original = node["metadata"]["creativeArtifact"]
        assert validate_artifact("storyboarder", original) == original
        candidate = copy.deepcopy(original)
        candidate["schemaVersion"] = 2
        candidate["keyframes"] = [{"key": f"boundary-{index}", "title": f"边界画面 {index + 1}", "imagePrompt": shot["imagePrompt"]} for index, shot in enumerate(candidate["shots"])]
        candidate["keyframes"].append({"key": f"boundary-{len(candidate['shots'])}", "title": "结束画面", "imagePrompt": candidate["shots"][-1]["imagePrompt"]})
        for index, shot in enumerate(candidate["shots"]):
            shot.update(firstFrameKey=f"boundary-{index}", lastFrameKey=f"boundary-{index + 1}")
        validated = validate_artifact("storyboarder", candidate)
        assert len(image_prompt_items(validated)) == len(validated["shots"]) + 1
        assert render_artifact(validated)
        candidate["shots"][1]["firstFrameKey"] = "different-frame"
        with pytest.raises(ValidationError):
            validate_artifact("storyboarder", candidate)


def test_new_native_schema_includes_boundary_frames_without_changing_r5():
    for version in ("R5", "R6"):
        tools = agent_tools("storyboarder", False, protocol=MATERIAL_NATIVE_AGENT_PROTOCOL, catalog_version=version)
        body = agent_request_body([{"role": "user", "content": "检查连续首尾帧工具结构"}], 32768, tools, False, Settings(), protocol=MATERIAL_NATIVE_AGENT_PROTOCOL, catalog_version=version)
        assert body["model"] == "deepseek-flash"
        schema = next(tool["function"]["parameters"] for tool in tools if tool["function"]["name"] == "write_artifact")
        assert ('"keyframes"' in json.dumps(schema)) == (version == "R6")


def test_actual_sensetime_responses_match_saved_images_and_dimensions():
    from app.models import Asset
    from app.services.sensetime import decode_image
    database = ROOT / "data/openfmv.sqlite3"
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        jobs = session.scalars(select(ImageJob).where(ImageJob.status == "ready")).all()
        assert len(jobs) >= 4
        for job in jobs:
            response = json.loads((ROOT / "data" / job.response_relpath).read_bytes())
            raw = decode_image(response, job.request_json["width"], job.request_json["height"])
            asset = session.get(Asset, job.asset_id)
            assert raw == (ROOT / "data" / asset.relpath).read_bytes()
            assert asset.width == 2048 and asset.height == 1152
    engine.dispose()


def test_internal_video_plan_does_not_satisfy_final_video_delivery(actual_database):
    from app.services.material_review import require_video_delivery
    with Session(actual_database) as session:
        run, step, node, _ = actual_role(session, "media_producer")
        review = {"nodeId": node["id"]}
        with pytest.raises(AppError) as failure:
            require_video_delivery(session, run, step, review)
        assert failure.value.code == "MATERIAL_VIDEO_NOT_READY"


def test_answer_to_a_previous_artifact_hash_is_rejected(actual_database):
    with Session(actual_database) as session:
        run, step, node, conversation = actual_role(session, "writer")
        pause_for_review(session, run, step, node, node["metadata"]["creativeArtifact"])
        session.commit()
        review = public_review(session, run)
        run.result_json = {**run.result_json, "materialReview": {**run.result_json["materialReview"], "artifactHash": "0" * 64}}
        body = MaterialAnswer(operation_id=str(uuid.uuid4()), expected_revision=run.project_revision,
            review_id=review["id"], answers={item["id"]: item["suggestedAnswer"] for item in review["questions"]})
        with pytest.raises(AppError) as failure:
            answer_review(session, conversation, body)
        assert failure.value.code == "MATERIAL_REVIEW_STALE"
