import hashlib
import json
import sqlite3
import uuid
from contextlib import closing

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent_models import AgentCall, AgentRun, AgentStep
from app.agent_schemas import RunLimits
from app.api.text_generations import TextCreate
from app.core.config import Settings
from app.core.errors import AppError
from app.services.agent import _native_messages, _native_tool_error, _safe_native_rejection
from app.services.agent_provider import MATERIAL_NATIVE_AGENT_PROTOCOL, NATIVE_AGENT_PROTOCOL, SUPPORTED_NATIVE_PROTOCOLS, agent_business_tools, agent_request_body, agent_tools, decode_agent_message, provider_endpoint
from app.services.context import retrieve_context
from app.services.text_provider import cost_micros
from app.tools.project_tools import MemoryArguments, ROLE_TOOLS, allowed_tools, execute_tool, tool_arguments
from conftest import PRODUCT_ROOT
from test_agent_runtime import bridge_client, claim_run, create_run, tool
from test_api import create_project, registered_client
from test_generation_canvas import node, save_canvas


def test_codex_scope_is_separate_from_platform_native_protocol_and_schemas_are_complete(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("target", "text", "授权资料", content="本人已保存的钟表店剧情")])
        response, _ = create_run(client, envelope, targets=["target"], delivery_mode="professional")
        assert response.status_code == 202
        project_id = envelope["project"]["id"]
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            engine = create_engine(f"sqlite:///{live_service.data_dir / 'openfmv.sqlite3'}")
            with Session(engine) as session:
                actual = session.get(AgentRun, run["id"])
                step = session.scalar(select(AgentStep).where(AgentStep.run_id == actual.id))
                assert "providerProtocol" not in actual.limits_json
                context = retrieve_context(session, project_id, actual.owner_id, actual.scene_id, target_ids=["target"])
                messages = _native_messages(actual, step, actual.role, context, [], [])
                assert json.loads(messages[1]["content"])["context"] == context
                assert len(messages) == 2 and all(item["role"] in {"system", "user"} for item in messages)
                for planning in (True, False):
                    tools = agent_tools(actual.role, planning, protocol=NATIVE_AGENT_PROTOCOL)
                    body = agent_request_body(messages, 8192, tools, planning, Settings(), protocol=NATIVE_AGENT_PROTOCOL)
                    assert body["messages"] == messages and body["tools"] == tools
                    assert "response_format" not in body and body["thinking"] == {"type": "disabled"}
                    assert body["tool_choice"] == ({"type": "function", "function": {"name": "submit_plan"}} if planning else "required")
                    upper = len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False, separators=(",", ":")).encode()) + 2048
                    assert upper > len(json.dumps(messages, ensure_ascii=False).encode())
            engine.dispose()


def test_actual_parameter_rejection_reports_validation_paths_without_input(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = create_project(client)
        response, _ = create_run(client, envelope)
        project_id = envelope["project"]["id"]
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            engine = create_engine(f"sqlite:///{live_service.data_dir / 'openfmv.sqlite3'}")
            with Session(engine) as session:
                actual = session.get(AgentRun, run["id"])
                with pytest.raises(AppError) as failure:
                    execute_tool(session, actual, actual.role, "write_script", {"text": 7}, str(uuid.uuid4()), actual.project_revision, Settings(data_dir=live_service.data_dir))
                assert failure.value.code == "AGENT_TOOL_ARGUMENTS_INVALID"
                errors = _native_tool_error(failure.value)
                assert errors["businessExecuted"] is False
                assert errors["details"][0]["loc"] == ("text",)
                assert errors["details"][0]["type"] == "string_type"
                assert all("input" not in detail and "ctx" not in detail for detail in errors["details"])
                assert _safe_native_rejection(failure.value, "write_script") is True
                assert session.scalars(select(AgentCall).where(AgentCall.run_id == actual.id)).all() == []
            engine.dispose()


@pytest.mark.parametrize("status", [401, 402, 403, 404, 409, 500])
def test_status_outside_safe_parameter_errors_cannot_trigger_correction(status):
    assert _safe_native_rejection(AppError(status, "SOURCE_REJECTED", "请求未能执行"), "write_artifact") is False


@pytest.mark.parametrize("tool", ["split_image", "join_videos", "adopt_asset", "submit_generation"])
def test_actual_file_or_media_failure_cannot_trigger_automatic_correction(tool):
    assert _safe_native_rejection(AppError(422, "MEDIA_PROCESSING_FAILED", "实际媒体处理失败"), tool) is False


def test_genuine_legacy_response_remains_outside_native_protocol_and_original_data_is_unchanged():
    database = PRODUCT_ROOT / ".intermediate" / "agent-smoke" / "e81dcbec-c8a9-4ce1-8b5b-30c5e3d46155" / "data" / "openfmv.sqlite3"
    if not database.exists():
        pytest.skip("需要本产品已有专业角色真实API记录")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        value = connection.execute("SELECT response_json FROM agent_calls WHERE run_id=? ORDER BY created_at DESC LIMIT 1", ("18e0a87c-600b-4415-afaf-17f2d93dd69c",)).fetchone()[0]
    actual = json.loads(value)
    with pytest.raises(AppError) as failure:
        decode_agent_message(actual, False, "writer")
    assert failure.value.code == "AGENT_NATIVE_CALL_INVALID"
    assert _safe_native_rejection(failure.value, "write_artifact") is False
    assert all("input" not in detail and "ctx" not in detail for detail in _native_tool_error(failure.value)["details"])
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def native_database():
    for directory in sorted((PRODUCT_ROOT / ".intermediate" / "agent-smoke").iterdir(), reverse=True):
        database = directory / "data" / "openfmv.sqlite3"
        if not database.exists():
            continue
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT run_id,input_json FROM agent_calls WHERE provider='deepseek' AND status IN ('ready','tool_rejected')").fetchall()
        for run_id, frozen in rows:
            if json.loads(frozen).get("protocol") in SUPPORTED_NATIVE_PROTOCOLS:
                return database, run_id
    pytest.skip("原生供应商正向记录由统一真实验证产生后再核验")


def test_genuine_native_history_keeps_actual_assistant_calls_and_real_tool_replies():
    database, run_id = native_database()
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        run = session.get(AgentRun, run_id)
        assert run.executor == "platform" and run.limits_json["providerProtocol"] in SUPPORTED_NATIVE_PROTOCOLS
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
        completed = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.status == "completed").order_by(AgentStep.index)).all()
        step = session.scalar(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.status != "completed").order_by(AgentStep.index))
        available = [call for call in calls if call.input_json.get("protocol") in SUPPORTED_NATIVE_PROTOCOLS and call.usage_json.get("toolCallId") and (call.usage_json.get("toolExecutionDone") or call.status == "tool_rejected")]
        if run.limits_json["providerProtocol"] == MATERIAL_NATIVE_AGENT_PROTOCOL:
            step = step or (completed[-1] if completed else None)
            available = [call for call in available if call.step_id == (step.id if step else None)]
        if not available:
            pytest.skip("原生实际请求尚未取得真实工具结果")
        frozen = json.loads(available[-1].input_json["messages"][1]["content"])
        messages = _native_messages(run, step, step.role if step else run.role, frozen["context"], available, completed)
        assistant = [item for item in messages if item["role"] == "assistant"]
        results = [item for item in messages if item["role"] == "tool"]
        assert assistant == [json.loads(call.raw_output) for call in available]
        assert [item["tool_call_id"] for item in results] == [call.usage_json["toolCallId"] for call in available]
        for call, result in zip(available, results, strict=True):
            assert call.remote_request_id and call.usage_json["settled"] and call.cost_micros is not None
            frozen = call.input_json
            names = {item["function"]["name"] for item in frozen["tools"]}
            version = frozen.get("toolCatalogVersion", "R1")
            assert names == ({"submit_plan"} if frozen["planning"] else allowed_tools(call.role, version) | {"complete_step"})
            assert frozen["requestBody"]["tools"] == frozen["tools"] and frozen["requestBody"]["messages"] == frozen["messages"]
            if frozen["protocol"] in {NATIVE_AGENT_PROTOCOL, MATERIAL_NATIVE_AGENT_PROTOCOL}:
                assert frozen["providerEndpoint"] == provider_endpoint(frozen["protocol"])
                assert frozen["businessTools"] == agent_business_tools(call.role, frozen["planning"], catalog_version=version)
                assert frozen["tools"] == agent_tools(call.role, frozen["planning"], protocol=frozen["protocol"], catalog_version=version)
            if call.status == "tool_rejected":
                assert json.loads(result["content"])["error"] == call.usage_json["toolError"]
            else:
                assert decode_agent_message(json.loads(call.raw_output), call.input_json["planning"], call.role, version, protocol=frozen["protocol"])[0] == call.response_json
    engine.dispose()


def test_expanded_capacity_and_finite_timeout_keep_old_explicit_requests():
    settings = Settings(_env_file=None, text_max_output_tokens=32768, text_timeout=600)
    assert Settings.model_fields["text_max_output_tokens"].default == 32768
    assert Settings.model_fields["text_timeout"].default == 600
    assert settings.text_timeout == 600 and RunLimits().max_tokens == 32768
    tools = agent_tools("character_designer", False, protocol=NATIVE_AGENT_PROTOCOL)
    for maximum in (512, 2048, 8192, 8193, 32768):
        body = agent_request_body([{"role": "user", "content": "本人要求保留完整角色设计与九段生图提示词。"}], maximum, tools, False, settings, protocol=NATIVE_AGENT_PROTOCOL)
        assert body["max_tokens"] == maximum and RunLimits(maxTokens=maximum).max_tokens == maximum
        text_request = TextCreate(scene_id="scene", source_node_id="source", output_node_id="output", prompt="保留完整专业提示词",
            expected_revision=1, operation_id="capacity-check", max_tokens=maximum)
        assert text_request.max_tokens == maximum
    with pytest.raises(ValidationError):
        RunLimits(maxTokens=32769)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, text_max_output_tokens=32769)
    with pytest.raises(ValidationError):
        TextCreate(scene_id="scene", source_node_id="source", output_node_id="output", prompt="输出限制检查",
            expected_revision=1, operation_id="capacity-check", max_tokens=32769)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, text_timeout=601)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, text_timeout=4)
    with pytest.raises(AppError) as failure:
        agent_request_body([{"role": "user", "content": "输出限制检查"}], 32769, tools, False, settings, protocol=NATIVE_AGENT_PROTOCOL)
    assert failure.value.code == "AGENT_NATIVE_TOKEN_LIMIT"


def test_real_native_request_price_reserves_the_full_new_output_capacity():
    database, run_id = native_database()
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        frozen_json, cost = connection.execute("SELECT input_json,cost_micros FROM agent_calls WHERE run_id=? AND status='ready' ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
    frozen = json.loads(frozen_json)
    price = frozen["price"]
    original = cost_micros(0, frozen["inputTokenUpperBound"], 8192, price)
    expanded = cost_micros(0, frozen["inputTokenUpperBound"], 32768, price)
    assert expanded > original >= cost
    expected_extra = (32768 - 8192) * price["outputMicrosPerMillion"] / 1000000
    assert abs((expanded - original) - expected_extra) < 1
    assert RunLimits(maxTokens=32768, maxCostMicros=2000000).max_cost_micros == 2000000


@pytest.mark.parametrize("protocol", ["native-tools-v1", "native-tools-v2-strict"])
@pytest.mark.parametrize("planning", [True, False])
def test_native_protocols_keep_separate_endpoints_and_complete_business_contract(protocol, planning):
    tools = agent_tools("writer", planning, protocol=protocol)
    business = agent_business_tools("writer", planning, catalog_version="R1")
    messages = [{"role": "user", "content": "按本人已确认设定完成专业剧本。"}]
    body = agent_request_body(messages, 32768, tools, planning, Settings(), protocol=protocol)
    assert body["tools"] == tools and body["messages"] == messages
    assert {item["function"]["name"] for item in tools} == {item["function"]["name"] for item in business}
    if protocol == "native-tools-v2-strict":
        assert "/beta/" in provider_endpoint(protocol)
        assert all(item["function"]["strict"] is True for item in tools)
        complete = next(item for item in tools if item["function"]["name"] == ("submit_plan" if planning else "complete_step"))
        schema = complete["function"]["parameters"]
        assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
    else:
        assert "/beta/" not in provider_endpoint(protocol) and tools == business
    if not planning:
        artifact = next(item for item in business if item["function"]["name"] == "write_artifact")
        assert artifact["function"]["parameters"]["$defs"]["WriterArtifact"]["properties"]["role"]["const"] == "writer"
    assert messages == [{"role": "user", "content": "按本人已确认设定完成专业剧本。"}]


@pytest.mark.parametrize("revision_as_text", [False, True])
def test_typed_memory_sources_preserve_valid_wire_shape_and_actual_operation_replay(live_service, revision_as_text):
    with closing(registered_client(live_service)[0]) as client:
        envelope = create_project(client)
        response, _ = create_run(client, envelope)
        project_id = envelope["project"]["id"]
        revision = str(envelope["revision"]) if revision_as_text else envelope["revision"]
        arguments = {"kind": "setting", "content": "本人确认林岚穿深灰工作服。", "sources": [{"kind": "project", "id": project_id, "revision": revision}]}
        validated = MemoryArguments.model_validate(arguments)
        assert tool_arguments(validated, arguments)["sources"] == arguments["sources"]
        assert validated.sources[0].revision == envelope["revision"]
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            operation = str(uuid.uuid4())
            written = tool(bridge, run, "write_memory", arguments, operation)
            assert written.status_code == 200, written.text
            assert tool(bridge, run, "write_memory", arguments, operation).json() == written.json()
            saved = client.get(f"/projects/{project_id}/agent-runs/{run['id']}").json()["run"]
            receipt = next(item for item in saved["steps"][0]["toolResults"] if item["tool"] == "write_memory")
            assert receipt["arguments"]["sources"] == arguments["sources"]
            memory = written.json()["result"]["result"]["memory"]
            assert memory["valid"] is True and memory["sources"][0]["revision"] == envelope["revision"]
        schema = MemoryArguments.model_json_schema(by_alias=True)
        assert schema["properties"]["sources"]["items"]["$ref"].endswith("/ContextSource")
        assert schema["$defs"]["ContextSource"]["additionalProperties"] is False
