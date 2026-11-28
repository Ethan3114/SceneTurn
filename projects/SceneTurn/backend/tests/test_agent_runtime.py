import hashlib
import json
import uuid
from contextlib import closing

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent_models import AgentCall, AgentRun, AgentStep
from app.agent_schemas import AgentDecision, ExecutionDecision, PlanningDecision, StepDecision
from app.core.config import Settings, sqlite_database_path
from app.services.agent import _checked_json_repair, _json_repair_info, _repair_messages, _role_messages
from app.core.errors import AppError
from app.services.connections import SUPPORTED_CODEX_VERSION
from app.services.prompts.agent import agent_prompt
from app.models import Canvas, Project
from app.services.projects import payload_digest
from app.tools.project_tools import tool_catalog, validate_artifacts
from conftest import PRODUCT_ROOT
from test_api import create_project, error_code, registered_client, scene_id
from test_generation_canvas import connection, node, save_canvas
from test_text_generation import TextTestService


ALL_TOOLS = ["read_project", "retrieve_context", "write_artifact", "write_script", "apply_canvas_ops", "submit_generation", "inspect_generation", "adopt_asset", "validate_scene", "write_memory", "split_image", "join_videos", "validate_artifacts"]


def create_run(client, envelope, *, role="writer", targets=None, controller=None, operation=None, executor="codex", delivery_mode="tool_action"):
    payload = {"operationId": operation or str(uuid.uuid4()), "expectedRevision": envelope["revision"],
               "sceneId": scene_id(envelope), "role": role, "executor": executor, "deliveryMode": delivery_mode,
               "goal": "修改授权文字并保留其他节点与历史。", "targetIds": targets or [],
               "limits": {"maxSteps": 12, "maxTokens": 512, "maxCostMicros": 100000}}
    if controller:
        payload["controllerNodeId"] = controller
    response = client.post(f"/projects/{envelope['project']['id']}/agent-runs", json=payload)
    return response, payload


def bridge_client(service, client, project_id):
    pairing = client.post(f"/projects/{project_id}/connections/pairings", json={"operationId": str(uuid.uuid4()), "tools": ALL_TOOLS, "expiresInSeconds": 3600})
    assert pairing.status_code == 201
    pairing = pairing.json()
    claim = client.post(f"/connections/pairings/{pairing['pairingId']}/claim", json={"pairingCode": pairing["pairingCode"], "clientVersion": SUPPORTED_CODEX_VERSION})
    assert claim.status_code == 200
    bridge = service.client()
    bridge.headers["Authorization"] = f"Bearer {claim.json()['token']}"
    return bridge, claim.json()["connection"]["id"]


def claim_run(bridge, expected_id):
    response = bridge.post("/connections/claim")
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["run"]["id"] == expected_id
    assert result["tools"] and "inputSchema" in result["tools"][0]
    assert "AgentDecision" not in result["instructions"]
    return result["run"]


def tool(bridge, run, name, arguments, operation=None):
    return bridge.post("/connections/tools", json={"runId": run["id"], "operationId": operation or str(uuid.uuid4()),
        "expectedRevision": run["projectRevision"], "toolName": name, "arguments": arguments})


def test_role_catalog_owner_isolation_and_actual_unconfigured_billing(live_service):
    with closing(registered_client(live_service)[0]) as client, closing(registered_client(live_service)[0]) as stranger:
        envelope = create_project(client)
        project_id = envelope["project"]["id"]
        roles = client.get(f"/projects/{project_id}/agent-roles")
        assert roles.status_code == 200
        roles = roles.json()["roles"]
        assert len(roles) == 9 and {role["id"] for role in roles} >= {"director", "writer", "storyboarder"}
        assert all(role["tools"] and role["description"] for role in roles)
        response, _ = create_run(client, envelope, executor="platform")
        assert error_code(response, 402) == "TEXT_BILLING_NOT_CONFIGURED"
        assert client.get(f"/projects/{project_id}/agent-runs").json() == {"runs": []}
        assert error_code(stranger.get(f"/projects/{project_id}/agent-runs"), 404) == "PROJECT_NOT_FOUND"
        response, _ = create_run(client, envelope)
        assert response.status_code == 202
        run = response.json()["run"]
        assert run["status"] == "waiting_connection" and run["usage"]["totalTokens"] is None
        assert run["usage"]["costMicros"] is None
        assert error_code(stranger.get(f"/projects/{project_id}/agent-runs/{run['id']}"), 404) == "PROJECT_NOT_FOUND"


def test_run_idempotency_controller_and_target_scope(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("script", "text", "剧本", content="原文"), node("control", "agent", "角色任务", goal="创作")])
        project_id = envelope["project"]["id"]
        response, payload = create_run(client, envelope, targets=["script"], controller="control")
        assert response.status_code == 202
        run = response.json()["run"]
        assert run["controllerNodeId"] == "control" and run["targetIds"] == ["script"]
        replay = client.post(f"/projects/{project_id}/agent-runs", json=payload)
        assert replay.status_code == 202 and replay.json()["run"]["id"] == run["id"]
        assert len(client.get(f"/projects/{project_id}/agent-runs").json()["runs"]) == 1
        conflict = client.post(f"/projects/{project_id}/agent-runs", json={**payload, "goal": "其他任务"})
        assert error_code(conflict, 409) == "OPERATION_CONFLICT"
        assert error_code(create_run(client, envelope, targets=["control"])[0], 403) == "AGENT_CONTROL_NODE_PROTECTED"
        assert error_code(create_run(client, envelope, targets=["absent"])[0], 422) == "AGENT_TARGET_NOT_FOUND"
        assert error_code(create_run(client, envelope, controller="script")[0], 422) == "AGENT_CONTROLLER_INVALID"
        with bridge_client(live_service, client, project_id)[0] as bridge:
            claimed = claim_run(bridge, run["id"])
            read = tool(bridge, claimed, "read_project", {})
            assert read.status_code == 200
            business = read.json()["result"]["result"]["canvas"]["nodes"]
            assert [item["id"] for item in business] == ["script"]
            assert len(client.get(f"/projects/{project_id}").json()["canvases"][0]["nodes"]) == 2


def test_real_tool_write_replay_and_new_node_scope(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("other", "text", "人工正文", content="保持这个内容")])
        project_id = envelope["project"]["id"]
        response, _ = create_run(client, envelope)
        assert response.status_code == 202
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            denied = tool(bridge, run, "write_script", {"targetId": "other", "text": "越权修改"})
            assert error_code(denied, 403) == "AGENT_TARGET_NOT_ALLOWED"
            operation = str(uuid.uuid4())
            written = tool(bridge, run, "write_script", {"text": "雨夜的修表师走进车站。", "title": "新剧本"}, operation)
            assert written.status_code == 200, written.text
            result = written.json()["result"]
            created = result["result"]["operations"][0]["nodeId"]
            replay = tool(bridge, run, "write_script", {"text": "雨夜的修表师走进车站。", "title": "新剧本"}, operation)
            assert replay.status_code == 200 and replay.json() == written.json()
            loaded = client.get(f"/projects/{project_id}").json()
            assert loaded["revision"] == result["revision"]
            assert len(loaded["canvases"][0]["nodes"]) == 2
            proof = result["result"]["savedTexts"][0]
            assert result["result"]["persisted"] is True
            assert proof["nodeId"] == created and proof["text"] == "雨夜的修表师走进车站。"
            assert proof["sha256"] == hashlib.sha256(proof["text"].encode()).hexdigest()
            assert next(item for item in loaded["canvases"][0]["nodes"] if item["id"] == "other")["metadata"]["content"] == "保持这个内容"
            run = client.get(f"/projects/{project_id}/agent-runs/{run['id']}").json()["run"]
            assert run["createdNodeIds"] == [created]
            changed = tool(bridge, run, "write_script", {"targetId": created, "text": "老人等待末班车。"})
            assert changed.status_code == 200
            run = {**run, "projectRevision": changed.json()["result"]["revision"]}
            validated = tool(bridge, run, "validate_artifacts", {})
            assert validated.status_code == 200, validated.text
            proof = validated.json()["result"]["result"]
            assert proof["valid"] is True and proof["readOnly"] is True
            assert proof["verifiedRevision"] == run["projectRevision"]
            text_proof = next(artifact for artifact in proof["artifacts"] if artifact["kind"] == "text")
            assert text_proof["id"] == created and text_proof["sha256"] == hashlib.sha256("老人等待末班车。".encode()).hexdigest()
            assert client.get(f"/projects/{project_id}").json()["revision"] == run["projectRevision"]
            assert error_code(tool(bridge, run, "submit_generation", {"sourceNodeId": created, "outputNodeId": created, "prompt": "树林"}), 403) == "TOOL_NOT_ALLOWED"


def test_manual_edit_conflict_resume_and_stop_are_persistent(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("script", "text", "剧本", content="原始正文")])
        project_id = envelope["project"]["id"]
        response, _ = create_run(client, envelope, targets=["script"])
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            edited = save_canvas(client, envelope, [node("script", "text", "剧本", content="人工最新正文")])
            assert error_code(tool(bridge, run, "write_script", {"targetId": "script", "text": "过期写入"}), 409) == "REVISION_CONFLICT"
            stopped = client.post(f"/projects/{project_id}/agent-runs/{run['id']}/stop", json={"operationId": str(uuid.uuid4())})
            assert stopped.status_code == 200 and stopped.json()["run"]["stopRequested"]
            assert error_code(tool(bridge, {**run, "projectRevision": edited["revision"]}, "write_script", {"targetId": "script", "text": "停止后写入"}), 409) == "RUN_STOPPED"
            snapshot = client.get(f"/projects/{project_id}").json()
            assert snapshot["canvases"][0]["nodes"][0]["metadata"]["content"] == "人工最新正文"


def test_media_quote_tool_waits_without_provider_submission(live_service):
    with closing(registered_client(live_service)[0]) as client:
        prompt = "清晨树林，镜头缓慢向前。"
        envelope = save_canvas(client, create_project(client), [node("idea", "text", "镜头文字", content=prompt), node("config", "config", "视频配置", generationMode="video", composerContent=prompt), node("result", "video", "视频结果")], [connection("idea", "config"), connection("config", "result")])
        project_id = envelope["project"]["id"]
        response, _ = create_run(client, envelope, role="media_producer", targets=["config", "result"])
        assert response.status_code == 202
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            quoted = tool(bridge, run, "submit_generation", {"sourceNodeId": "config", "outputNodeId": "result", "prompt": prompt, "duration": 5, "resolution": "720P"})
            assert quoted.status_code == 200, quoted.text
            generation = quoted.json()["result"]["result"]["generation"]
            assert generation["status"] == "awaiting_confirmation" and generation["reservedCents"] == 0
            assert generation["remoteTaskId"] is None and generation["remoteRequestId"] is None
            persisted = client.get(f"/projects/{project_id}/agent-runs/{run['id']}").json()["run"]
            assert persisted["status"] == "waiting_confirmation"
            resumed = client.post(f"/projects/{project_id}/agent-runs/{run['id']}/resume", json={"operationId": str(uuid.uuid4()), "expectedRevision": envelope["revision"]})
            assert error_code(resumed, 409) == "CONFIRMATION_REQUIRED"


def test_agent_run_and_actual_tool_result_survive_service_restart(tmp_path):
    service = TextTestService(tmp_path)
    try:
        service.start()
        with closing(registered_client(service)[0]) as client:
            envelope = create_project(client)
            project_id = envelope["project"]["id"]
            response, _ = create_run(client, envelope)
            with bridge_client(service, client, project_id)[0] as bridge:
                run = claim_run(bridge, response.json()["run"]["id"])
                written = tool(bridge, run, "write_script", {"text": "真实重启后保留这段剧本。", "title": "恢复验收"})
                assert written.status_code == 200
                before = client.get(f"/projects/{project_id}/agent-runs/{run['id']}").json()["run"]
                service.stop()
                service.start()
                after = client.get(f"/projects/{project_id}/agent-runs/{run['id']}").json()["run"]
                assert after == before
                assert len(after["steps"][0]["toolResults"]) == 1
                assert after["usage"]["costMicros"] is None and after["usage"]["totalTokens"] is None
                project = client.get(f"/projects/{project_id}").json()
                assert project["canvases"][0]["nodes"][0]["metadata"]["content"] == "真实重启后保留这段剧本。"
    finally:
        service.stop()


def test_editor_real_image_split_and_idempotency(live_service, sample_input_media):
    with closing(registered_client(live_service)[0]) as client:
        envelope = create_project(client)
        project_id = envelope["project"]["id"]
        source, mime = sample_input_media["image"]
        with source.open("rb") as stream:
            uploaded = client.post(f"/projects/{project_id}/assets", files={"file": (source.name, stream, mime)})
        assert uploaded.status_code == 201
        envelope = uploaded.json()
        original = envelope["asset"]
        envelope = save_canvas(client, envelope, [node("image", "image", "真实原图", storageKey=original["id"], content=original["url"])])
        response, _ = create_run(client, envelope, role="editor", targets=["image"])
        assert response.status_code == 202
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            operation = str(uuid.uuid4())
            split = tool(bridge, run, "split_image", {"sourceNodeId": "image", "rows": 2, "columns": 2}, operation)
            assert split.status_code == 200, split.text
            result = split.json()["result"]
            assert len(result["result"]["pieces"]) == 4
            assert result["revision"] == envelope["revision"] + 1
            replay = tool(bridge, run, "split_image", {"sourceNodeId": "image", "rows": 2, "columns": 2}, operation)
            assert replay.status_code == 200 and replay.json() == split.json()
            saved = client.get(f"/projects/{project_id}/agent-runs/{run['id']}").json()["run"]
            assert set(saved["createdNodeIds"]) == {piece["nodeId"] for piece in result["result"]["pieces"]}
            assert saved["usage"]["costMicros"] is None and saved["calls"] == []
            download = client.get(f"/assets/{original['id']}/content")
            assert download.status_code == 200 and download.content == source.read_bytes()


def test_editor_local_join_creates_only_zero_cost_task(live_service, sample_input_media):
    with closing(registered_client(live_service)[0]) as client:
        envelope = create_project(client)
        project_id = envelope["project"]["id"]
        source, mime = sample_input_media["video"]
        assets = []
        for _ in range(2):
            with source.open("rb") as stream:
                uploaded = client.post(f"/projects/{project_id}/assets", files={"file": (source.name, stream, mime)})
            assert uploaded.status_code == 201
            envelope = uploaded.json()
            assets.append(envelope["asset"])
        envelope = save_canvas(client, envelope, [node("video1", "video", "视频1", storageKey=assets[0]["id"], content=assets[0]["url"]), node("video2", "video", "视频2", storageKey=assets[1]["id"], content=assets[1]["url"]), node("config", "config", "拼接", generationMode="video", dramaTask="join"), node("result", "video", "成片")], [connection("video1", "config"), connection("video2", "config"), connection("config", "result")])
        response, _ = create_run(client, envelope, role="editor", targets=["config", "result"])
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            joined = tool(bridge, run, "join_videos", {"sourceNodeId": "config", "outputNodeId": "result"})
            assert joined.status_code == 200, joined.text
            generation = joined.json()["result"]["result"]["generation"]
            assert generation["input"]["provider"] == "local-ffmpeg" and generation["status"] == "queued"
            assert generation["quotedCents"] == generation["reservedCents"] == generation["actualCents"] == 0
            assert generation["remoteTaskId"] is None and generation["remoteRequestId"] is None
            assert [item["assetId"] for item in generation["input"]["inputs"]] == [item["id"] for item in assets]


def test_user_can_resume_stopped_unclaimed_run_at_latest_revision(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = create_project(client)
        project_id = envelope["project"]["id"]
        response, _ = create_run(client, envelope)
        run_id = response.json()["run"]["id"]
        stopped = client.post(f"/projects/{project_id}/agent-runs/{run_id}/stop", json={"operationId": str(uuid.uuid4())})
        assert stopped.status_code == 200 and stopped.json()["run"]["status"] == "stopped"
        envelope = save_canvas(client, envelope, [node("manual", "text", "人工新正文", content="人工决定保留。")])
        operation = str(uuid.uuid4())
        payload = {"operationId": operation, "expectedRevision": envelope["revision"]}
        resumed = client.post(f"/projects/{project_id}/agent-runs/{run_id}/resume", json=payload)
        assert resumed.status_code == 200
        run = resumed.json()["run"]
        assert run["status"] == "waiting_connection" and not run["stopRequested"]
        assert run["projectRevision"] == envelope["revision"]
        assert run["result"]["codexResumeOperationId"] == operation
        replay = client.post(f"/projects/{project_id}/agent-runs/{run_id}/resume", json=payload)
        assert replay.status_code == 200 and replay.json() == resumed.json()
        saved = client.get(f"/projects/{project_id}").json()
        assert saved["canvases"][0]["nodes"][0]["metadata"]["content"] == "人工决定保留。"


def test_real_api_history_preserves_model_decisions_and_successful_receipts():
    data = sqlite_database_path(PRODUCT_ROOT / ".intermediate" / "agent-smoke" / "255b0687-4a34-4748-b9e9-e54014798aa5" / "data")
    if not data.exists():
        pytest.skip("需要本产品已有真实角色API验证数据")
    engine = create_engine(f"sqlite:///{data.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        run = session.get(AgentRun, "346a9a45-91d7-45ab-bcc2-9269a3bdd0f3")
        step = session.scalar(select(AgentStep).where(AgentStep.run_id == run.id))
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
        frozen_context = json.loads(calls[-1].input_json["messages"][1]["content"])["context"]
        messages = _role_messages(run, step, step.role, frozen_context, calls, [])
        instruction = json.loads(messages[1]["content"])
        assert len(instruction["completedOperations"]) == len(step.tool_results_json) == 11
        assert sum(item["tool"] == "write_script" for item in instruction["completedOperations"]) == 7
        actual_calls = [call for call in calls if call.step_id == step.id and call.response_json][-4:]
        assert [json.loads(message["content"]) for message in messages if message["role"] == "assistant"] == [call.response_json for call in actual_calls]
        receipts = {receipt["operationId"]: receipt for receipt in step.tool_results_json}
        for call, reply in zip(actual_calls, messages[3::2], strict=True):
            evidence = json.loads(reply["content"])["toolResult"]
            saved = receipts[f"agent-{call.id}"]
            assert evidence["operationId"] == saved["operationId"] and evidence["tool"] == saved["tool"]
            if saved["tool"] == "write_script":
                assert evidence["result"] == saved["result"]
        assert "action=complete" in messages[0]["content"]
        assert sum(len(message["content"].encode()) for message in messages) > sum(len(message["content"].encode()) for message in messages[:2])
    engine.dispose()


def test_real_completed_production_has_independent_read_only_artifact_proof():
    data_dir = PRODUCT_ROOT / ".intermediate" / "agent-smoke" / "442333b4-b449-4447-8e45-4f83727af6c3" / "data"
    data = sqlite_database_path(data_dir)
    if not data.exists():
        pytest.skip("需要本产品已有第二轮真实角色API数据")
    engine = create_engine(f"sqlite:///{data.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        run = session.get(AgentRun, "55a443d9-e14d-4f68-bd20-78a4139e7f79")
        step = session.scalar(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.index == 0))
        project = session.get(Project, run.project_id)
        canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.scene_id == run.scene_id))
        assert step.status == "completed" and run.error_code == "AGENT_NO_TOOL_RESULTS"
        proof = validate_artifacts(session, run, step, project, canvas, Settings(data_dir=data_dir))
        assert proof["valid"] is True and proof["readOnly"] is True
        assert proof["verifiedRevision"] == project.revision == 4
        assert all(artifact["sourceStepId"] == step.id and artifact["sourceRevision"] == 4 for artifact in proof["artifacts"])
        call = session.scalar(select(AgentCall).where(AgentCall.run_id == run.id, AgentCall.step_id.is_(None)))
        legacy = AgentDecision.model_validate_json(call.raw_output).model_dump(by_alias=True)
        assert legacy == AgentDecision.model_validate(call.response_json).model_dump(by_alias=True)
        with pytest.raises(ValidationError):
            ExecutionDecision.model_validate_json(call.raw_output)
    engine.dispose()


def test_real_execution_cannot_reenter_the_planning_response_contract():
    data = sqlite_database_path(PRODUCT_ROOT / ".intermediate" / "agent-smoke" / "96ccf1f6-7462-45d3-8d2b-e3d4a79ae588" / "data")
    if not data.exists():
        pytest.skip("需要本产品已有阶段决策真实API数据")
    engine = create_engine(f"sqlite:///{data.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        run = session.get(AgentRun, "ec351683-28f3-4a5a-bcdc-686d64952b68")
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
        assert len(calls) == 2 and run.error_code == "AGENT_DECISION_INVALID"
        assert calls[0].input_json["planning"] is True and calls[1].input_json["planning"] is False
        assert PlanningDecision.model_validate_json(calls[0].raw_output).action == "plan"
        with pytest.raises(ValidationError):
            StepDecision.model_validate_json(calls[1].raw_output)
        for call in calls:
            assert AgentDecision.model_validate_json(call.raw_output).model_dump(by_alias=True) == AgentDecision.model_validate(call.response_json).model_dump(by_alias=True)
        planning_prompt = agent_prompt("writer", tool_catalog("writer"), True)
        execution_prompt = agent_prompt("writer", tool_catalog("writer"), False)
        assert "计划格式" in planning_prompt and "工具例子" not in planning_prompt
        assert "工具例子" in execution_prompt and "计划格式" not in execution_prompt
        assert StepDecision.model_json_schema()["properties"]["action"]["enum"] == ["tool", "complete"]
        assert PlanningDecision.model_json_schema()["properties"]["action"]["const"] == "plan"
    engine.dispose()


def test_real_extra_field_repair_preserves_decision_and_freezes_original_revision():
    data = sqlite_database_path(PRODUCT_ROOT / ".intermediate" / "agent-smoke" / "8a6e4d4d-d8ff-45bb-b5ad-0d7ee586e665" / "data")
    if not data.exists():
        pytest.skip("需要本产品已有额外字段真实API数据")
    before = hashlib.sha256(data.read_bytes()).hexdigest()
    engine = create_engine(f"sqlite:///{data.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        run = session.get(AgentRun, "d5d8c3e1-5679-4f70-b5db-7a3613470e10")
        call = session.get(AgentCall, "007b12aa-655a-4a0d-b609-b87e8cb857da")
        assert run.error_code == "AGENT_RESULT_INVALID" and call.status == "failed"
        assert call.cost_micros == 6661 and call.usage_json["settled"] is True
        with pytest.raises(ValidationError):
            StepDecision.model_validate_json(call.raw_output)
        info = _checked_json_repair(session, run, call, run.project_revision)
        assert info["repairOf"] == call.id and info["jsonRepairAttempt"] == 1
        assert info["repairProjectRevision"] == run.project_revision == 4
        assert info["extraFieldPaths"] == [["planNote"]]
        original = json.loads(call.raw_output)
        corrected = {key: value for key, value in original.items() if key != "planNote"}
        assert payload_digest(StepDecision.model_validate(corrected).model_dump(by_alias=True)) == info["repairExpectedSha256"]
        assert all(corrected[key] == original[key] for key in ("action", "tool", "arguments", "plan", "summary"))
        with pytest.raises(AppError) as stale:
            _checked_json_repair(session, run, call, run.project_revision + 1)
        assert stale.value.code == "AGENT_JSON_REPAIR_STALE"
        repaired_messages = _repair_messages(call.input_json["messages"], call, info)
        assert repaired_messages[-2] == {"role": "assistant", "content": call.raw_output}
        assert repaired_messages[:2] == call.input_json["messages"][:2]
        assert json.loads(repaired_messages[-1]["content"])["jsonRepair"]["originalCallId"] == call.id
        assert _json_repair_info(session.scalar(select(AgentCall).where(AgentCall.run_id == run.id, AgentCall.status == "ready").limit(1))) is None
    engine.dispose()
    assert hashlib.sha256(data.read_bytes()).hexdigest() == before
