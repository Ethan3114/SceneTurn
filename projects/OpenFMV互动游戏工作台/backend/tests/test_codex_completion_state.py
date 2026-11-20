import copy
import hashlib
import json
import uuid

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.agent_models import AgentCall, AgentRun, AgentStep
from app.api.projects import save_canvas
from app.connection_models import CodexConnection
from app.creative_artifacts import ARTIFACT_CONTRACT_VERSION
from app.models import Canvas, Project, User
from app.schemas import CanvasSave
from app.services.agent import claim_codex_run, finish_codex_run
from app.services.connections import SUPPORTED_CODEX_VERSION, claim_pairing, create_pairing, record_event
from conftest import PRODUCT_ROOT


SOURCE = PRODUCT_ROOT / ".intermediate" / "codex-smoke" / "e02f90df-3044-441e-a800-1cddb1ffe545"
RUN_ID = "1e52a75c-7840-4c95-81fd-83c7be57cdfa"


@pytest.fixture
def genuine_pending_completion(writable_snapshot, request):
    original = SOURCE / "data" / "openfmv.sqlite3"
    if not original.exists():
        pytest.skip("需要本产品已有本人Codex真实完成记录")
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    proof_hash = hashlib.sha256((SOURCE / "proof.json").read_bytes()).hexdigest()
    destination = writable_snapshot(original)
    destination.chmod(0o600)
    engine = create_engine(f"sqlite:///{destination}", connect_args={"timeout": 10})
    request.addfinalizer(engine.dispose)
    with Session(engine, expire_on_commit=False) as session:
        run = session.get(AgentRun, RUN_ID)
        recorded = session.scalar(select(AgentCall).where(AgentCall.run_id == run.id, AgentCall.provider == "personal-codex"))
        assert recorded.status == "completed" and recorded.remote_request_id == run.turn_id
        completion = {"result": {**copy.deepcopy(recorded.response_json), "status": recorded.status},
                      "thread_id": run.thread_id, "turn_id": run.turn_id, "usage": copy.deepcopy(recorded.usage_json)}
        # 从实际结果重建回报前边界，原始数据库及证明保持只读。
        session.execute(delete(AgentCall).where(AgentCall.id == recorded.id))
        run.status, run.finished_at, run.result_json = "waiting_connection", None, None
        step = session.scalar(select(AgentStep).where(AgentStep.run_id == run.id))
        step.status, step.result_json = "running", None
        session.commit()
        pairing = create_pairing(session, run.owner_id, run.project_id, {"operationId": str(uuid.uuid4()),
            "tools": ["read_project", "retrieve_context", "write_script"], "expiresInSeconds": 3600})
        claimed = claim_pairing(session, pairing["pairingId"], pairing["pairingCode"], SUPPORTED_CODEX_VERSION)
        connection_id = claimed["connection"]["id"]
        assert claim_codex_run(session, run.owner_id, run.project_id, connection_id)["run"]["id"] == run.id
        completion.update(owner_id=run.owner_id, run_id=run.id, connection_id=connection_id)
        session.commit()
    yield engine, completion
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    assert hashlib.sha256((SOURCE / "proof.json").read_bytes()).hexdigest() == proof_hash


def manual_save(session, completion):
    run = session.get(AgentRun, completion["run_id"])
    project = session.get(Project, run.project_id)
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.scene_id == run.scene_id))
    snapshot = copy.deepcopy(canvas.data_json)
    target = next(node for node in snapshot["nodes"] if node["id"] in run.target_ids_json)
    target["metadata"]["content"] = "这是最后一次工具之后由本人保存的正文。"
    user = session.get(User, run.owner_id)
    return save_canvas(project.id, run.scene_id,
        CanvasSave(expected_revision=project.revision, operation_id=str(uuid.uuid4()), nodes=snapshot["nodes"], connections=snapshot["connections"], viewport=snapshot["viewport"]),
        user, session)


def test_genuine_completion_after_manual_save_remains_conflict(genuine_pending_completion):
    engine, completion = genuine_pending_completion
    with Session(engine, expire_on_commit=False) as session:
        saved = manual_save(session, completion)
        before_revision = saved["revision"]
        result = finish_codex_run(session, **completion)
        assert result["status"] == "conflict" and result["error"]["code"] == "REVISION_CONFLICT"
        assert result["result"]["candidate"]["finalText"] == completion["result"]["finalText"]
        assert result["result"]["candidate"]["currentRevision"] == before_revision
        assert result["steps"][0]["status"] == "conflict" and result["finishedAt"] is None
        assert result["usage"]["costMicros"] is None
        canvas = session.scalar(select(Canvas).where(Canvas.project_id == result["projectId"], Canvas.scene_id == result["sceneId"]))
        target = next(node for node in canvas.data_json["nodes"] if node["id"] in result["targetIds"])
        assert target["metadata"]["content"] == "这是最后一次工具之后由本人保存的正文。"
        assert session.get(Project, result["projectId"]).revision == before_revision
        session.commit()
        replay = finish_codex_run(session, **completion)
        assert replay["status"] == "conflict"
        assert len(replay["calls"]) == 1 and replay["updatedAt"] == result["updatedAt"]


def test_genuine_completion_preserves_reported_conflict(genuine_pending_completion):
    engine, completion = genuine_pending_completion
    with Session(engine, expire_on_commit=False) as session:
        connection = session.get(CodexConnection, completion["connection_id"])
        record_event(session, connection, {"runId": completion["run_id"], "eventType": "failed",
            "threadId": completion["thread_id"], "turnId": completion["turn_id"], "errorCode": "REVISION_CONFLICT"})
        finished = finish_codex_run(session, **completion)
        assert finished["status"] == "conflict"
        assert finished["error"]["code"] == "REVISION_CONFLICT"
        assert finished["result"]["candidate"]["finalText"] == completion["result"]["finalText"]
        assert len(finished["calls"]) == 1


def test_genuine_duplicate_completed_turn_keeps_historical_result(genuine_pending_completion):
    engine, completion = genuine_pending_completion
    with Session(engine, expire_on_commit=False) as session:
        first = finish_codex_run(session, **completion)
        assert first["status"] == "completed"
        finished_at = first["finishedAt"]
        saved = manual_save(session, completion)
        duplicate = finish_codex_run(session, **completion)
        assert duplicate["status"] == "completed" and duplicate["finishedAt"] == finished_at
        assert len(duplicate["calls"]) == 1 and duplicate["currentRevision"] == saved["revision"]


def test_genuine_late_completion_keeps_unknown_state_and_candidate(genuine_pending_completion):
    engine, completion = genuine_pending_completion
    with Session(engine, expire_on_commit=False) as session:
        run = session.get(AgentRun, completion["run_id"])
        run.status, run.error_code = "submission_unknown", "CODEX_SUBMISSION_UNKNOWN"
        session.commit()
        result = finish_codex_run(session, **completion)
        assert result["status"] == "submission_unknown" and result["finishedAt"] is None
        assert result["error"]["code"] == "CODEX_SUBMISSION_UNKNOWN"
        assert result["steps"][0]["status"] == "submission_unknown"
        assert result["result"]["candidate"]["finalText"] == completion["result"]["finalText"]
        assert result["calls"][0]["response"] == {"finalText": completion["result"]["finalText"]}


def test_genuine_completion_without_professional_artifact_preserves_actual_usage(genuine_pending_completion):
    engine, completion = genuine_pending_completion
    with Session(engine, expire_on_commit=False) as session:
        run = session.get(AgentRun, completion["run_id"])
        run.limits_json = {**run.limits_json, "deliveryMode": "professional", "artifactContractVersion": ARTIFACT_CONTRACT_VERSION}
        session.commit()
        result = finish_codex_run(session, **completion)
        assert result["status"] == "failed" and result["error"]["code"] == "AGENT_PROFESSIONAL_ARTIFACT_MISSING"
        assert result["result"]["candidate"]["finalText"] == completion["result"]["finalText"]
        assert len(result["calls"]) == 1 and result["calls"][0]["usage"] == completion["usage"]
        assert result["calls"][0]["response"] == {"finalText": completion["result"]["finalText"]}
