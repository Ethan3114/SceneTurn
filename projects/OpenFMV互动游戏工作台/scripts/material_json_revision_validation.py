import copy
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent_models import AgentCall
from app.core.errors import AppError
from app.image_models import ImageJob
from app.material_schemas import MaterialAnswer
from app.media_models import MediaSequence
from app.models import BudgetReservation, Canvas, new_id
from app.services.agent import _apply_native_call, _recoverable_material_revision, resume_run
from app.services.agent_provider import MATERIAL_JSON_AGENT_PROTOCOL, decode_platform_message
from app.services.material_review import answer_review, public_review
from app.tools.project_tools import MATERIAL_SOP_TOOL_CATALOG_VERSION
from conftest import sample_video
from test_material_reference_assets import reference_image, reference_workspace, save_writer


def test_json_revision_replays_real_completion_messages_and_recovers_once_without_business_writes(reference_workspace):
    recordings_root = os.environ.get("OPENFMV_JSON_REVISION_RECORDINGS")
    if not recordings_root:
        pytest.fail("请设置 OPENFMV_JSON_REVISION_RECORDINGS，指向本机保存真实回执的 agent-smoke 目录。")
    session, settings, project, user, _, create_run = reference_workspace
    run, step, conversation = create_run("writer")
    protocol = MATERIAL_JSON_AGENT_PROTOCOL
    version = MATERIAL_SOP_TOOL_CATALOG_VERSION
    run.limits_json = {**run.limits_json, "providerProtocol": protocol, "toolCatalogVersion": version, "model": "deepseek-flash"}
    session.commit()
    node = save_writer(session, settings, project, run, step)
    saved_body = copy.deepcopy(node["metadata"])
    review = public_review(session, run)
    answers = {item["id"]: item["suggestedAnswer"] for item in review["questions"]}
    revision_request = MaterialAnswer(review_id=review["id"], operation_id=new_id(), expected_revision=project.revision,
        answers=answers, requested_changes="补充尾镜动作后再保存。")
    answer_review(session, conversation, revision_request, settings)
    session.commit()
    revision = project.revision
    limits = copy.deepcopy(run.limits_json)
    factory = sessionmaker(session.bind)
    recordings = {
        "9a6ba3d1-34f6-4261-9bb1-a2acfd19263a": "1e0a589d-ecad-487d-86b1-cbfe5b3a785d",
        "21c641f3-68a8-4ca3-8462-0e5ebc3f7508": "a0ae64aa-20b3-4b3d-a7e6-9934579ac010",
        "0f0e6c6b-4ad2-47ad-ba0d-2ab864f1b96a": "d24adcd1-cfe4-42f6-a803-00d6b116619f",
    }
    original_reservations = []
    for attempt, (recording, call_id) in enumerate(recordings.items(), 1):
        database = (Path(recordings_root).expanduser() / recording / "data" / "openfmv.sqlite3").resolve(strict=True)
        engine = create_engine(f"sqlite:///{database.as_uri()}?mode=ro&uri=true")
        with Session(engine) as recorded:
            source = recorded.get(AgentCall, call_id)
            ledger = recorded.get(BudgetReservation, call_id)
            assert source and source.role == "writer" and source.remote_request_id and source.usage_json["settled"]
            assert ledger and ledger.status == "settled" and ledger.reserved_micros == 0 and ledger.cost_micros == source.cost_micros
            action, identifier = decode_platform_message(json.loads(source.raw_output), False, source.role, protocol, version)
            assert identifier is None and action["action"] == "complete"
            # 在隔离数据库使用真实历史回复验证当前业务规则，原始请求和账目记录保持原样。
            call = AgentCall(id=call_id, run_id=run.id, step_id=step.id, role=source.role, provider=source.provider, model=source.model,
                status="ready", input_json={"protocol": protocol, "toolCatalogVersion": version, "planning": False,
                    "projectRevision": revision, "materialRevisionOperationId": revision_request.operation_id},
                response_json=action, raw_output=source.raw_output, remote_request_id=source.remote_request_id,
                total_tokens=source.total_tokens, cost_micros=source.cost_micros, usage_json={"settled": True})
            session.add(call)
            session.add(BudgetReservation(id=call_id, purpose="agent-model", reserved_micros=0,
                cost_micros=ledger.cost_micros, status=ledger.status, details_json={"runId": run.id}))
            original_reservations.append((call_id, ledger.cost_micros))
        engine.dispose()
        run.status = step.status = "running"
        session.commit()
        _apply_native_call(settings, factory, run.id, call_id)
        session.expire_all()
        assert call.status == "tool_rejected" and call.usage_json["toolError"]["businessExecuted"] is False
        assert call.usage_json["toolError"]["code"] == "MATERIAL_REVISION_REQUIRED" and not call.usage_json.get("toolExecutionDone")
        assert run.result_json["materialWorkflowCorrection"]["rejections"] == attempt
        assert project.revision == revision and len(step.tool_results_json) == 1
        canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.scene_id == run.scene_id))
        assert next(item for item in canvas.data_json["nodes"] if item["id"] == node["id"])["metadata"] == saved_body
        assert not session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()
        if attempt == 1:
            assert run.status == "queued" and _recoverable_material_revision(session, run, revision, settings) is None
        elif attempt == 2:
            assert run.status == "failed" and run.error_code == "MATERIAL_REVISION_CORRECTION_LIMIT"
            assert _recoverable_material_revision(session, run, revision, settings).id == call_id
            operation = new_id()
            resume_run(session, project.id, run.id, user.id, operation, revision, settings)
            resume_run(session, project.id, run.id, user.id, operation, revision, settings)
            assert run.status == "queued" and run.result_json["materialRevisionResume"]["operationId"] == operation
        else:
            assert run.status == "failed" and run.error_code == "MATERIAL_REVISION_CORRECTION_LIMIT"
            assert _recoverable_material_revision(session, run, revision, settings) is None
            with pytest.raises(AppError) as rejection:
                resume_run(session, project.id, run.id, user.id, new_id(), revision, settings)
            assert rejection.value.code == "AGENT_RUN_TERMINAL"
            session.rollback()
    assert run.limits_json == limits
    assert not session.scalars(select(MediaSequence).where(MediaSequence.project_id == project.id)).all()
    for call_id, cost in original_reservations:
        reservation = session.get(BudgetReservation, call_id)
        assert reservation.status == "settled" and reservation.reserved_micros == 0 and reservation.cost_micros == cost
