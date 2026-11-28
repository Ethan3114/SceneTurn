import copy
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun, AgentStep
from app.core.config import Settings, sqlite_database_path
from app.core.errors import AppError
from app.core.security import hash_password
from app.creative_artifacts import image_prompt_items, render_artifact, validate_artifact
from app.image_models import ImageAccount, ImageJob
from app.material_models import MaterialConversation, MaterialMessage
from app.material_schemas import MaterialAnswer
from app.models import Base, Project, User, new_id
from app.services.agent import require_step_delivery, resume_run
from app.services.agent_provider import MATERIAL_NATIVE_AGENT_PROTOCOL, agent_request_body, agent_tools
from app.services.image_queue import _finish_failure, claim_job, recover_jobs
from app.services.material_review import answer_review, pause_for_review, public_review
from app.services.materials import view
from app.services.projects import create_project_record, payload_digest
from app.tools.project_tools import execute_tool, require_canvas


ROOT = Path(__file__).resolve().parents[2]
RUN_ID = "eafe14be-35fd-4bf6-97c3-613133121dcd"


@pytest.fixture
def actual_database(writable_snapshot, request):
    database = writable_snapshot(sqlite_database_path(ROOT / "data"))
    engine = create_engine(f"sqlite:///{database}", connect_args={"timeout": 10})
    request.addfinalizer(engine.dispose)
    return engine


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


@pytest.fixture
def serial_image_queue(tmp_path, request):
    engine = create_engine(f"sqlite:///{tmp_path / 'image-queue.sqlite3'}", connect_args={"timeout": 10})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for account_count in getattr(request, "param", (2, 1)):
            user = User(email=f"{new_id()}@queue.invalid", password_hash=hash_password(new_id()))
            session.add(user)
            session.flush()
            project = create_project_record(session, user.id, "独立图片队列验证")
            scene_id = next(node["id"] for node in project.data_json["graphData"]["nodes"] if node["type"] == "scene")
            run = AgentRun(owner_id=user.id, project_id=project.id, scene_id=scene_id, operation_id=new_id(),
                role="storyboarder", executor="platform", goal="验证单张图片领取", limits_json={}, plan_json=[],
                initial_revision=project.revision, project_revision=project.revision, status="waiting_confirmation")
            session.add(run)
            session.flush()
            for slot in range(1, account_count + 1):
                session.add(ImageAccount(owner_id=user.id, slot=slot, credential_hash=payload_digest({"slot": slot, "owner": user.id}),
                    api_key_encrypted="", status="available"))
            for label in ("静物首帧", "静物尾帧"):
                session.add(ImageJob(owner_id=user.id, project_id=project.id, scene_id=scene_id, run_id=run.id,
                    node_id=new_id(), artifact_hash=payload_digest({"title": label}), operation_id=new_id(), label=label,
                    status="queued", confirmed_at=time.time(), request_json={"width": 2048, "height": 1152, "prompt": label}))
        session.commit()
    yield engine
    engine.dispose()


def test_parallel_claims_allow_one_image_across_accounts_and_users(serial_image_queue):
    start = Barrier(4)

    def claim():
        with Session(serial_image_queue) as session:
            start.wait(timeout=10)
            return claim_job(session)

    with ThreadPoolExecutor(max_workers=4) as executor:
        attempts = [executor.submit(claim) for _ in range(4)]
        claimed = [attempt.result() for attempt in attempts]
    identifiers = [identifier for identifier in claimed if identifier]
    assert len(identifiers) == 1
    with Session(serial_image_queue) as session:
        accounts = session.scalars(select(ImageAccount)).all()
        assert len(accounts) == 3 and len({account.owner_id for account in accounts}) == 2
        assert [account.active_job_id for account in accounts if account.active_job_id] == identifiers
        assert len(session.scalars(select(ImageJob).where(ImageJob.status == "queued")).all()) == 3
        recover_jobs(session)
    with Session(serial_image_queue) as session:
        assert claim_job(session) == identifiers[0]


@pytest.mark.parametrize("outcome", ["ready", "failed", "connection_requeued"])
def test_five_second_gap_uses_persisted_provider_completion(serial_image_queue, outcome):
    with Session(serial_image_queue) as session:
        identifier = claim_job(session)
        job = session.get(ImageJob, identifier)
        account = session.get(ImageAccount, job.account_id)
        job.status, job.attempt_count, job.submitted_at = "submitting", 1, time.time() - 30
        if outcome == "failed":
            _finish_failure(session, job, "IMAGE_PROVIDER_REJECTED")
            released_at = job.finished_at
        else:
            released_at = time.time()
            job.updated_at = released_at
            account.active_job_id = None
            if outcome == "ready":
                job.status, job.finished_at = "ready", released_at
            else:
                job.status, job.account_id = "queued", None
            session.commit()
    serial_image_queue.dispose()
    with Session(serial_image_queue) as session:
        assert claim_job(session) is None
    time.sleep(max(0, released_at + 5.02 - time.time()))
    with Session(serial_image_queue) as session:
        next_id = claim_job(session)
        assert next_id
        assert session.get(ImageJob, next_id).updated_at >= released_at + 5


def test_unknown_submission_keeps_even_other_users_blocked_after_reopening(serial_image_queue):
    with Session(serial_image_queue) as session:
        identifier = claim_job(session)
        job = session.get(ImageJob, identifier)
        account_id = job.account_id
        job.status, job.attempt_count, job.submitted_at = "submitting", 1, time.time() - 60
        session.commit()
    serial_image_queue.dispose()
    with Session(serial_image_queue) as session:
        recover_jobs(session)
        assert claim_job(session) is None
        job = session.get(ImageJob, identifier)
        account = session.get(ImageAccount, account_id)
        assert job.status == "submission_unknown" and job.finished_at is None
        assert account.status == "quarantined" and account.active_job_id == identifier
        assert len(session.scalars(select(ImageAccount).where(ImageAccount.active_job_id.is_(None))).all()) == 2
    serial_image_queue.dispose()
    with Session(serial_image_queue) as session:
        recover_jobs(session)
        assert claim_job(session) is None
        assert session.get(ImageAccount, account_id).active_job_id == identifier


def test_receipt_deadline_survives_restart_and_rotates_original_job_with_dependency(serial_image_queue):
    with Session(serial_image_queue) as session:
        identifier = claim_job(session)
        job = session.get(ImageJob, identifier)
        follower = session.scalar(select(ImageJob).where(ImageJob.run_id == job.run_id, ImageJob.id != identifier))
        follower.dependency_ids = [identifier]
        follower_id, project_id, account_id = follower.id, job.project_id, job.account_id
        submitted_at = time.time() - 179
        job.status, job.attempt_count, job.submitted_at = "submitting", 1, submitted_at
        session.commit()
    serial_image_queue.dispose()
    with Session(serial_image_queue) as session:
        recover_jobs(session)
        assert claim_job(session) is None
        assert session.get(ImageAccount, account_id).active_job_id == identifier
        assert session.get(ImageJob, identifier).status == "submission_unknown"
    time.sleep(max(0, submitted_at + 180.02 - time.time()))
    serial_image_queue.dispose()
    with Session(serial_image_queue) as session:
        recover_jobs(session)
        assert claim_job(session) is None
        job = session.get(ImageJob, identifier)
        account = session.get(ImageAccount, account_id)
        assert job.status == "queued" and job.submitted_at == submitted_at and job.attempt_count == 1
        assert account.status == "available" and account.active_job_id is None
        released_at = job.updated_at
        assert released_at >= submitted_at + 180
        assert job.request_json["expiredAttempts"] == [{"accountId": account_id, "attempt": 1,
            "submittedAt": submitted_at, "releasedAt": released_at, "errorCode": "IMAGE_SUBMISSION_UNKNOWN"}]
        assert session.get(ImageJob, follower_id).dependency_ids == [identifier]
    time.sleep(max(0, released_at + 5.02 - time.time()))
    with Session(serial_image_queue) as session:
        assert claim_job(session) == identifier
        job = session.get(ImageJob, identifier)
        assert job.project_id == project_id and job.account_id != account_id and job.attempt_count == 1
        assert session.get(ImageAccount, job.account_id).owner_id == job.owner_id
        assert job.updated_at >= released_at + 5
        follower = session.get(ImageJob, follower_id)
        assert follower.status == "queued" and follower.dependency_ids == [identifier]


@pytest.mark.parametrize("serial_image_queue", [(3,)], indirect=True)
def test_three_expired_accounts_finish_original_job_as_timeout(serial_image_queue):
    with Session(serial_image_queue) as session:
        identifier = claim_job(session)
        job = session.get(ImageJob, identifier)
        follower = session.scalar(select(ImageJob).where(ImageJob.run_id == job.run_id, ImageJob.id != identifier))
        follower.dependency_ids = [identifier]
        session.commit()
    accounts = []
    for attempt in range(1, 4):
        with Session(serial_image_queue) as session:
            job = session.get(ImageJob, identifier)
            assert job.status == "claimed" and job.account_id not in accounts
            accounts.append(job.account_id)
            job.status, job.attempt_count, job.submitted_at = "submitting", attempt, time.time() - 181
            _finish_failure(session, job, "IMAGE_RESPONSE_TIMEOUT", unknown=True)
            assert claim_job(session) is None
            released_at = session.get(ImageJob, identifier).updated_at
        time.sleep(max(0, released_at + 5.02 - time.time()))
        with Session(serial_image_queue) as session:
            assert claim_job(session) == (identifier if attempt < 3 else None)
    with Session(serial_image_queue) as session:
        job = session.get(ImageJob, identifier)
        assert job.status == "failed" and job.error_code == "IMAGE_RESPONSE_TIMEOUT"
        assert job.finished_at >= released_at and job.attempt_count == 3
        attempts = job.request_json["expiredAttempts"]
        assert [item["accountId"] for item in attempts] == accounts and len(set(accounts)) == 3
        assert [item["attempt"] for item in attempts] == [1, 2, 3]
        assert all(account.active_job_id is None for account in session.scalars(select(ImageAccount)))


@pytest.mark.parametrize("run_status,stop_requested,expected", [
    ("stopping", 1, "cancelled"), ("stopped", 0, "cancelled"),
    ("completed", 0, "failed"), ("failed", 0, "failed"),
])
def test_expired_closed_run_only_releases_its_account(serial_image_queue, run_status, stop_requested, expected):
    with Session(serial_image_queue) as session:
        identifier = claim_job(session)
        job = session.get(ImageJob, identifier)
        account_id = job.account_id
        run = session.get(AgentRun, job.run_id)
        run.status, run.stop_requested = run_status, stop_requested
        job.status, job.attempt_count, job.submitted_at = "submitting", 1, time.time() - 181
        _finish_failure(session, job, "IMAGE_RESPONSE_TIMEOUT", unknown=True)
    with Session(serial_image_queue) as session:
        assert claim_job(session) is None
        job = session.get(ImageJob, identifier)
        assert job.status == expected and job.finished_at is not None and job.attempt_count == 1
        assert job.request_json["expiredAttempts"][0]["accountId"] == account_id
        account = session.get(ImageAccount, account_id)
        assert account.status == "available" and account.active_job_id is None


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
    database = sqlite_database_path(ROOT / "data")
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        provider_id = ImageJob.request_json["modelProfile"]["providerId"].as_string()
        jobs = session.scalars(select(ImageJob).where(
            ImageJob.status == "ready",
            func.coalesce(provider_id, "sensetime") == "sensetime",
        )).all()
        assert len(jobs) >= 4
        for job in jobs:
            response = json.loads((ROOT / "data" / job.response_relpath).read_bytes())
            raw = decode_image(response, job.request_json["width"], job.request_json["height"])
            asset = session.get(Asset, job.asset_id)
            assert raw == (ROOT / "data" / asset.relpath).read_bytes()
            assert (asset.width, asset.height) == (job.request_json["width"], job.request_json["height"])
    engine.dispose()


@pytest.mark.parametrize("statuses,queue_blocked,expected", [
    (["ready"] * 5 + ["submission_unknown"], True, "图片已完成5/6张，1张请求结果待核实。回执最多等待3分钟，超时后尝试换账号。"),
    (["ready", "ready", "submission_unknown", "failed", "cancelled", "queued"], True, "图片已完成2/6张，1张请求结果待核实，1张生成失败，1张已取消。另有1张仍在排队或处理中。回执最多等待3分钟，超时后尝试换账号。"),
    (["ready"] * 4 + ["failed", "cancelled"], False, "图片已完成4/6张，1张生成失败，1张已取消。请先核对未完成的图片。"),
    (["ready"] * 5 + ["timed_out"], False, "图片已完成5/6张，1张等待回执超时。请先核对未完成的图片。"),
    (["ready"] * 5 + ["submitting"], False, "图片生成中，完成后请确认图片成果。"),
    (["ready"] * 5 + ["queued"], False, "图片正在排队，上一单结束后至少间隔5秒再提交。"),
    (["ready"] * 5 + ["queued"], True, "上一单回执待返回，超时后队列会继续。"),
    (["ready"] * 6, False, "图片已生成，请查看画布并确认后继续。"),
])
def test_image_review_message_uses_isolated_confirmed_workflow(actual_database, statuses, queue_blocked, expected):
    with Session(actual_database) as session:
        run, step, node, conversation = actual_role(session, "character_designer")
        run.status, run.stop_requested, step.status = "running", 0, "running"
        artifact = copy.deepcopy(node["metadata"]["creativeArtifact"])
        character = artifact["characters"][0]
        prompt = character["imagePrompts"][0]
        prompt["outputSpec"]["imageCount"] = 6
        character["imagePrompts"] = [prompt]
        artifact["characters"] = [character]
        settings = Settings(_env_file=None, data_dir=ROOT / "data", worker_enabled=False)
        session.commit()
        execute_tool(session, run, step.role, "write_artifact", {"artifact": artifact}, str(uuid.uuid4()), run.project_revision, settings, step)
        while run.result_json["materialReview"]["status"] == "pending":
            pending = public_review(session, run)
            answer_review(session, conversation, MaterialAnswer(operation_id=str(uuid.uuid4()), expected_revision=run.project_revision,
                review_id=pending["id"], answers={item["id"]: item["suggestedAnswer"] for item in pending["questions"]}), settings)
            session.commit()
        review = copy.deepcopy(run.result_json["materialReview"])
        assert review["status"] == "generating"
        jobs = [session.get(ImageJob, identifier) for identifier in review["imageJobIds"]]
        assert len(jobs) == 6 and all(job.run_id == run.id and job.status == "queued" for job in jobs)
        for job, status in zip(jobs, statuses, strict=True):
            job.status = "failed" if status == "timed_out" else status
            job.error_code = "IMAGE_RESPONSE_TIMEOUT" if status == "timed_out" else None
        accounts = session.scalars(select(ImageAccount).where(ImageAccount.owner_id == run.owner_id)).all()
        for account in accounts:
            account.status, account.active_job_id = "available", None
        if queue_blocked:
            account = accounts[0]
            waiting = ImageJob(owner_id=run.owner_id, project_id=run.project_id, scene_id=run.scene_id,
                run_id=run.id, node_id=node["id"], artifact_hash=review["artifactHash"], operation_id=new_id(),
                label="另一笔待核实的商汤请求", status="submission_unknown", request_json=jobs[0].request_json,
                account_id=account.id, submitted_at=time.time() - 60, attempt_count=1)
            session.add(waiting)
            session.flush()
            account.status, account.active_job_id = "quarantined", waiting.id
        review["status"] = "generating"
        run.result_json = {**run.result_json, "materialReview": review}
        session.flush()
        before_review = copy.deepcopy(run.result_json)
        before_limits = copy.deepcopy(run.limits_json)
        before_jobs = [(job.id, job.status, job.error_code, job.account_id, job.submitted_at, job.finished_at, job.attempt_count) for job in jobs]
        before_accounts = [(account.id, account.status, account.active_job_id) for account in accounts]
        result = public_review(session, run)
        assert result["message"] == expected
        assert result["canConfirm"] is all(status == "ready" for status in statuses)
        assert result["status"] == ("review_images" if result["canConfirm"] else "generating")
        session.flush()
        session.expire_all()
        assert run.result_json == before_review and run.limits_json == before_limits
        assert [(job.id, job.status, job.error_code, job.account_id, job.submitted_at, job.finished_at, job.attempt_count) for job in jobs] == before_jobs
        assert [(account.id, account.status, account.active_job_id) for account in session.scalars(select(ImageAccount).where(ImageAccount.owner_id == run.owner_id))] == before_accounts


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
