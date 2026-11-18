import copy
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent_models import AgentCall, AgentRun, AgentStep
from app.core.config import Settings
from app.core.errors import AppError
from app.image_models import ImageJob
from app.material_models import MaterialConversation, MaterialMessage
from app.material_schemas import MaterialAnswer
from app.media_models import MediaSequence
from app.models import Asset, BudgetReservation, Generation, Project
from app.services.agent import _apply_native_call, _material_revision_request, _native_correction_info, _native_messages, _recoverable_material_revision, _safe_native_rejection, require_step_delivery, resume_run, run_view
from app.services.agent_provider import MATERIAL_NATIVE_AGENT_PROTOCOL, agent_tools, decode_platform_message
from app.services.material_review import answer_review, public_review
from app.services.generation import quote_generation
from app.services.projects import payload_digest
from app.tools.project_tools import execute_tool, require_canvas


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "0847961a-7349-4df2-9420-6fb9abac76a8"
COMPLETE_ID = "2cded3b6-a7a4-45cd-8690-c952397f3d72"


@pytest.fixture
def revision_database(tmp_path):
    snapshot_path = os.environ.get("OPENFMV_REVISION_SNAPSHOT")
    if not snapshot_path:
        pytest.fail("请设置OPENFMV_REVISION_SNAPSHOT，指向本次已授权的真实失败任务SQLite一致性副本；该验证不会读取生产数据库。")
    snapshot = Path(snapshot_path).resolve()
    if not snapshot.is_file():
        pytest.fail("OPENFMV_REVISION_SNAPSHOT指定的SQLite一致性副本不存在。")
    database = tmp_path / "openfmv.sqlite3"
    with sqlite3.connect(f"{snapshot.as_uri()}?immutable=1", uri=True) as source, sqlite3.connect(database) as destination:
        source.backup(destination)
    engine = create_engine(f"sqlite:///{database}")
    settings = Settings(_env_file=None, data_dir=tmp_path, worker_enabled=False)
    with Session(engine) as session:
        run = session.get(AgentRun, RUN_ID)
        references = run.limits_json["materialContext"]["referenceAssets"]
        assert len(references) == 1
        asset = session.get(Asset, references[0]["assetId"])
        source = ROOT / "docs/设计参考/斗破苍穹-20261117/origin-poster.png"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == asset.sha256
        destination = tmp_path / asset.relpath
        destination.parent.mkdir(parents=True)
        shutil.copyfile(source, destination)
    yield engine, settings
    engine.dispose()


def actual_revision(session):
    run = session.get(AgentRun, RUN_ID)
    call = session.get(AgentCall, COMPLETE_ID)
    step = session.get(AgentStep, call.step_id)
    return run, step, call, session.get(Project, run.project_id)


def test_actual_failed_turn_resumes_same_review_without_media_or_budget_changes(revision_database):
    engine, settings = revision_database
    with Session(engine) as session:
        run, step, call, project = actual_revision(session)
        original_limits = copy.deepcopy(run.limits_json)
        original_input = payload_digest(call.input_json)
        original_raw = hashlib.sha256(call.raw_output.encode()).hexdigest()
        identifier = call.usage_json["toolCallId"]
        reservation_query = select(BudgetReservation).where(BudgetReservation.id.in_(select(AgentCall.id).where(AgentCall.run_id == run.id)))
        reservations = [(r.id, r.status, r.reserved_micros, r.cost_micros) for r in session.scalars(reservation_query)]
        jobs = [(job.id, job.status, job.submitted_at) for job in session.scalars(select(ImageJob).where(ImageJob.run_id == run.id))]
        assert len(jobs) == 6 and all(status == "cancelled" and submitted is None for _, status, submitted in jobs)
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
        assert len(calls) == 8 and sum(item.cost_micros for item in calls) == 425155
        assert original_limits["maxSteps"] == 24 and original_limits["maxCostMicros"] == 10000000
        assert _recoverable_material_revision(session, run, project.revision, settings).id == call.id
        assert run_view(session, run, settings)["canResume"]
        operation = str(uuid.uuid4())
        resume_run(session, run.project_id, run.id, run.owner_id, operation, project.revision, settings)
        resume_run(session, run.project_id, run.id, run.owner_id, operation, project.revision, settings)
        session.expire_all()
        run, step, call, project = actual_revision(session)
        assert run.status == step.status == "queued" and run.finished_at is None
        assert call.status == "tool_rejected" and call.usage_json["toolError"]["code"] == "MATERIAL_REVISION_REQUIRED"
        assert call.usage_json["toolError"]["businessExecuted"] is False and not call.usage_json.get("toolExecutionDone")
        assert payload_digest(call.input_json) == original_input
        assert hashlib.sha256(call.raw_output.encode()).hexdigest() == original_raw and call.usage_json["toolCallId"] == identifier
        assert [(r.id, r.status, r.reserved_micros, r.cost_micros) for r in session.scalars(reservation_query)] == reservations
        assert [(job.id, job.status, job.submitted_at) for job in session.scalars(select(ImageJob).where(ImageJob.run_id == run.id))] == jobs
        assert all(run.limits_json[key] == original_limits[key] for key in ("maxSteps", "maxTokens", "maxCostMicros"))
        assert project.revision == 21 and len(calls) == 8
        assert step.result_json is None


def test_actual_native_complete_rejection_is_correctable_and_context_excludes_old_evidence(revision_database):
    engine, settings = revision_database
    factory = sessionmaker(engine)
    with Session(engine) as session:
        run, step, call, project = actual_revision(session)
        decision, identifier = decode_platform_message(json.loads(call.raw_output), False, step.role,
            MATERIAL_NATIVE_AGENT_PROTOCOL, run.limits_json["toolCatalogVersion"])
        assert payload_digest(decision) == payload_digest(call.response_json) and identifier == call.usage_json["toolCallId"]
        call.status = "ready"
        session.commit()
    _apply_native_call(settings, factory, RUN_ID, COMPLETE_ID)
    with Session(engine) as session:
        run, step, call, project = actual_revision(session)
        assert call.status == "tool_rejected" and run.status == "queued"
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
        assert sum(bool(item.input_json.get("correctionOf")) for item in calls) == 2
        assert _native_correction_info(session, run, step.id, calls) == {"correctionOf": call.id, "correctionAttempt": 1}
        context = json.loads(call.input_json["messages"][1]["content"])["context"]
        messages = _native_messages(run, step, step.role, context, calls, [])
        instruction = json.loads(messages[1]["content"])
        revision = instruction["materialRevision"]
        assert revision["requiresArtifactWrite"] and revision["nodeId"] == run.result_json["materialReview"]["nodeId"]
        assert payload_digest(revision["requestedChanges"]) == payload_digest(run.result_json["materialReview"]["answerPayload"]["requestedChanges"])
        assert instruction["verificationResults"] == []
        assistants = [message for message in messages if message["role"] == "assistant"]
        assert len(assistants) == 2
        assert all(message["tool_calls"][0]["function"]["name"] in {"validate_artifacts", "complete_step"} for message in assistants)
        assert json.loads(messages[-1]["content"])["error"]["code"] == "MATERIAL_REVISION_REQUIRED"
        assert "当前materialRevision明确要求修订已有成果" in messages[0]["content"]
        assert "新建产物时targetId填写null" in messages[0]["content"]


def test_actual_resumed_read_patch_creates_new_review_and_unsubmitted_images(revision_database):
    engine, settings = revision_database
    with Session(engine) as session:
        run, step, call, project = actual_revision(session)
        resume_run(session, run.project_id, run.id, run.owner_id, str(uuid.uuid4()), project.revision, settings)
        old_review = copy.deepcopy(run.result_json["materialReview"])
        target = old_review["nodeId"]
        read = execute_tool(session, run, step.role, "read_artifact", {"targetId": target}, str(uuid.uuid4()), project.revision, settings, step)
        baseline = read["result"]
        assert baseline["artifactSha256"] == old_review["artifactHash"]
        old_cost = sum(session.scalars(select(AgentCall.cost_micros).where(AgentCall.run_id == run.id)))
        path = "/characters/0/costumeMaterials"
        value = "深墨黑收脚长裤；黑色哑光短靴；深褐宽布腰带配素面金属方扣，无挂件；红黑外衣下摆至大腿中段，两侧开衩。"
        patched = execute_tool(session, run, step.role, "patch_artifact", {"targetId": target,
            "expectedRevision": baseline["revision"], "baseArtifactSha256": baseline["baseArtifactSha256"],
            "patches": [{"op": "replace", "path": path, "value": value}]}, str(uuid.uuid4()), project.revision, settings, step)
        review = run.result_json["materialReview"]
        assert patched["result"]["persisted"] and patched["result"]["artifactSha256"] != old_review["artifactHash"]
        assert review["id"] != old_review["id"] and review["status"] == "pending" and review["artifactHash"] == patched["result"]["artifactSha256"]
        assert run.status == step.status == "waiting_confirmation"
        old_jobs = [session.get(ImageJob, identifier) for identifier in old_review["imageJobIds"]]
        new_jobs = [session.get(ImageJob, identifier) for identifier in review["imageJobIds"]]
        assert len(new_jobs) == 6 and not set(review["imageJobIds"]) & set(old_review["imageJobIds"])
        assert all(job.status == "cancelled" and job.submitted_at is None for job in old_jobs)
        assert all(job.status == "awaiting_confirmation" and job.confirmed_at is None and job.submitted_at is None for job in new_jobs)
        assert all(job.reference_asset_ids == [run.limits_json["materialContext"]["referenceAssets"][0]["assetId"]] for job in new_jobs)
        assert sum(session.scalars(select(AgentCall.cost_micros).where(AgentCall.run_id == run.id))) == old_cost
        assert _material_revision_request(run, step.id)["requiresArtifactWrite"] is False
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
        instruction = json.loads(_native_messages(run, step, step.role, {}, calls, [])[1]["content"])
        assert instruction["materialRevision"]["currentArtifactHash"] == review["artifactHash"]
        assert instruction["verificationResults"] == []
        assert not run_view(session, run, settings)["canResume"]
        with pytest.raises(AppError) as waiting:
            require_step_delivery(session, run, step, project, settings)
        assert waiting.value.code == "MATERIAL_CONFIRMATION_REQUIRED"
        session.commit()
    with Session(engine) as session:
        run, step, _, _ = actual_revision(session)
        assert run.result_json["materialReview"]["status"] == "pending"
        assert len(public_review(session, run)["images"]) == 6


def test_actual_review_answer_persists_explicit_revision_and_clears_old_step_result(revision_database):
    engine, settings = revision_database
    with Session(engine) as session:
        run, step, _, project = actual_revision(session)
        original = run.result_json["materialReview"]
        review = {key: value for key, value in original.items() if key not in {"operationId", "answerPayload", "answeredAt"}}
        review.update(status="pending", answers={})
        run.result_json = {**run.result_json, "materialReview": review}
        run.status = step.status = "waiting_confirmation"
        step.result_json = {"summary": "修订前完成记录"}
        message = session.scalar(select(MaterialMessage).where(MaterialMessage.run_id == run.id))
        conversation = session.get(MaterialConversation, message.conversation_id)
        public = public_review(session, run)
        body = MaterialAnswer(operation_id=str(uuid.uuid4()), expected_revision=project.revision, review_id=review["id"],
            answers={item["id"]: original["answers"][item["id"]] for item in public["questions"]},
            requested_changes=original["answerPayload"]["requestedChanges"])
        answer_review(session, conversation, body, settings)
        session.commit()
        session.expire_all()
        run, step, _, project = actual_revision(session)
        request = run.limits_json["materialContext"]["revisionRequest"]
        assert request["operationId"] == body.operation_id and request["reviewId"] == review["id"]
        assert request["requestedAt"] == run.result_json["materialReview"]["answeredAt"]
        assert payload_digest(request["answers"]) == payload_digest(body.answers)
        assert run.status == step.status == "queued" and step.result_json is None
        assert project.revision == 21
        assert run.limits_json["maxSteps"] == 24 and run.limits_json["maxCostMicros"] == 10000000


@pytest.mark.parametrize("boundary", ["pending", "generating", "review_images", "approved", "wrong_step", "wrong_hash", "stale_revision", "stopped", "unknown_call", "unsettled", "executed", "wrong_protocol", "request_limit", "budget_limit", "missing_reference", "changed_reference", "image_unknown"])
def test_actual_failed_recovery_rejects_invalid_or_unconfirmed_boundaries(revision_database, boundary):
    engine, settings = revision_database
    with Session(engine) as session:
        run, step, call, project = actual_revision(session)
        review = copy.deepcopy(run.result_json["materialReview"])
        if boundary in {"pending", "generating", "review_images", "approved"}:
            review["status"] = boundary
        elif boundary == "wrong_step":
            review["stepId"] = str(uuid.uuid4())
        elif boundary == "wrong_hash":
            review["artifactHash"] = "0" * 64
        elif boundary == "stale_revision":
            project.revision += 1
        elif boundary == "stopped":
            run.stop_requested = 1
        elif boundary == "unknown_call":
            call.status = "submission_unknown"
        elif boundary == "unsettled":
            session.get(BudgetReservation, call.id).reserved_micros = 1
        elif boundary == "executed":
            call.usage_json = {**call.usage_json, "toolExecutionDone": True}
        elif boundary == "wrong_protocol":
            run.limits_json = {**run.limits_json, "providerProtocol": "native-tools-v1"}
        elif boundary == "request_limit":
            run.limits_json = {**run.limits_json, "maxSteps": 8}
        elif boundary == "budget_limit":
            run.limits_json = {**run.limits_json, "maxCostMicros": 425155}
        elif boundary in {"missing_reference", "changed_reference"}:
            asset = session.get(Asset, run.limits_json["materialContext"]["referenceAssets"][0]["assetId"])
            path = settings.data_dir / asset.relpath
            if boundary == "missing_reference":
                path.unlink()
            else:
                path.write_bytes(path.read_bytes() + b"changed")
        elif boundary == "image_unknown":
            session.get(ImageJob, review["imageJobIds"][0]).status = "submission_unknown"
        run.result_json = {**run.result_json, "materialReview": review}
        session.flush()
        before = payload_digest(run.limits_json)
        assert _recoverable_material_revision(session, run, project.revision, settings) is None
        assert not run_view(session, run, settings)["canResume"]
        session.commit()
        with pytest.raises(AppError):
            resume_run(session, run.project_id, run.id, run.owner_id, str(uuid.uuid4()), project.revision, settings)
        assert payload_digest(run.limits_json) == before
        assert call.status == ("submission_unknown" if boundary == "unknown_call" else "failed")


def test_revision_error_is_specific_to_complete_and_strict_schema_uses_null():
    for code, status in [("MATERIAL_CONFIRMATION_REQUIRED", 409), ("MATERIAL_IMAGES_PENDING", 409), ("MATERIAL_REVISION_REQUIRED", 409)]:
        assert not _safe_native_rejection(AppError(status, code, "待确认"), "complete_step")
    assert _safe_native_rejection(AppError(422, "MATERIAL_REVISION_REQUIRED", "需要修订"), "complete_step")
    assert not _safe_native_rejection(AppError(422, "MATERIAL_REVISION_REQUIRED", "需要修订"), "submit_generation")
    schema = next(item["function"]["parameters"] for item in agent_tools("character_designer", False,
        protocol=MATERIAL_NATIVE_AGENT_PROTOCOL, catalog_version="R6") if item["function"]["name"] == "write_artifact")
    assert set(schema["required"]) == {"targetId", "title", "artifact"}
    assert "null" in schema["properties"]["targetId"]["description"] and "null" in schema["properties"]["title"]["description"]


@pytest.mark.parametrize("boundary", ["unknown", "unsettled"])
def test_actual_earlier_call_uncertainty_keeps_original_reservation(revision_database, boundary):
    engine, settings = revision_database
    with Session(engine) as session:
        run, _, latest, project = actual_revision(session)
        earlier = session.scalar(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at))
        assert earlier.id != latest.id
        reservation = session.get(BudgetReservation, earlier.id)
        if boundary == "unknown":
            earlier.status, earlier.total_tokens = "submission_unknown", None
        reservation.status, reservation.reserved_micros = "reserved", 100
        session.commit()
        before = (reservation.status, reservation.reserved_micros, reservation.cost_micros)
        assert not run_view(session, run, settings)["canResume"]
        with pytest.raises(AppError):
            resume_run(session, run.project_id, run.id, run.owner_id, str(uuid.uuid4()), project.revision, settings)
        assert (reservation.status, reservation.reserved_micros, reservation.cost_micros) == before
        assert latest.status == "failed"


def test_actual_two_corrections_are_bounded_inside_one_revision(revision_database):
    engine, settings = revision_database
    with Session(engine) as session:
        run, step, call, project = actual_revision(session)
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
        corrections = [item for item in calls if item.step_id == step.id and item.input_json.get("correctionOf")]
        assert len(corrections) == 2 and all(item.remote_request_id and item.usage_json["settled"] for item in corrections)
        review = copy.deepcopy(run.result_json["materialReview"])
        review["answeredAt"] = min(item.created_at for item in corrections) - 1
        rejected = next(item for item in corrections if item.status == "tool_rejected")
        run.result_json = {**run.result_json, "materialReview": review,
            "nativeCorrection": {"callId": rejected.id, "tool": "write_artifact"}}
        assert _recoverable_material_revision(session, run, project.revision, settings) is None
        with pytest.raises(AppError) as bounded:
            _native_correction_info(session, run, step.id, calls)
        assert bounded.value.code == "AGENT_NATIVE_CORRECTION_LIMIT"
        assert run.limits_json["maxSteps"] == 24 and run.limits_json["maxCostMicros"] == 10000000


@pytest.mark.parametrize("status", ["processing", "query_interrupted", "submission_unknown"])
@pytest.mark.parametrize("binding", ["receipt", "sequence"])
def test_unfinished_real_generation_quote_blocks_both_material_bindings(revision_database, status, binding):
    engine, settings = revision_database
    with Session(engine) as session:
        run, step, _, project = actual_revision(session)
        canvas = require_canvas(session, run, project)
        source = next(node for node in canvas.data_json["nodes"] if node["id"] == run.result_json["materialReview"]["nodeId"])
        generation = quote_generation(session, project.id, run.owner_id, {"operationId": str(uuid.uuid4()),
            "sceneId": run.scene_id, "sourceNodeId": source["id"], "prompt": source["metadata"]["content"],
            "duration": 2, "resolution": "720P"}, settings)
        assert generation.status == "awaiting_confirmation" and generation.remote_task_id is None
        # 隔离数据库保存待核实状态，只验证恢复拒绝，不提交或模拟供应商结果。
        generation.status, generation.reserved_cents = status, generation.quoted_cents
        if binding == "receipt":
            step.tool_results_json = [*step.tool_results_json, {"tool": "submit_generation", "operationId": str(uuid.uuid4()),
                "revision": project.revision, "arguments": {}, "result": {"generation": {"id": generation.id}}}]
        else:
            sequence = MediaSequence(owner_id=run.owner_id, project_id=project.id, scene_id=run.scene_id,
                input_json={"runId": run.id}, input_sha256=payload_digest({"runId": run.id}))
            session.add(sequence)
            session.flush()
            generation.input_json = {**generation.input_json, "boundaryFrames": {"sequenceId": sequence.id}}
        session.commit()
        original = (generation.status, generation.reserved_cents, generation.remote_task_id)
        assert not run_view(session, run, settings)["canResume"]
        with pytest.raises(AppError):
            resume_run(session, run.project_id, run.id, run.owner_id, str(uuid.uuid4()), project.revision, settings)
        assert (generation.status, generation.reserved_cents, generation.remote_task_id) == original
