import json
import hashlib
import sqlite3
from contextlib import closing

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent_models import AgentCall, AgentRun, AgentStep
from app.core.config import Settings, sqlite_database_path
from app.core.errors import AppError
from app.creative_artifacts import ARTIFACT_MODELS, artifact_model, image_prompt_items, render_artifact, validate_artifact
from app.services.agent import _json_messages, _native_correction_info, _validate_frozen_model
from app.services.agent_provider import DEFAULT_AGENT_PROTOCOL, JSON_AGENT_PROTOCOL, agent_business_tools, agent_request_body, agent_response_schema, agent_tools, decode_platform_message
from app.services.context import retrieve_context
from app.services.prompts.agent import _json_format_examples, json_agent_prompt
from app.services.projects import payload_digest
from app.services.text_provider import PRO_MODEL, current_price
from app.models import BudgetReservation, Project
from app.tools.project_tools import require_canvas, validate_artifacts
from conftest import PRODUCT_ROOT, recorded_databases
from test_agent_runtime import bridge_client, claim_run, create_run
from test_api import create_project, registered_client
from test_generation_canvas import node, save_canvas


@pytest.mark.parametrize("role", ARTIFACT_MODELS)
@pytest.mark.parametrize("planning", [True, False])
def test_json_prompt_uses_only_current_role_fields_and_compact_phase_contract(role, planning):
    business = agent_business_tools(role, planning)
    schema = agent_response_schema(planning, protocol=JSON_AGENT_PROTOCOL)
    prompt = json_agent_prompt(role, business, planning, "professional", schema)
    for name, field in artifact_model(role).model_fields.items():
        assert f'"name": "{field.alias or name}"' in prompt
    if "production_handoff" not in artifact_model(role).model_fields:
        assert "productionHandoff" not in prompt
    assert '"name": "submit_plan"' not in prompt and '"name": "complete_step"' not in prompt
    assert "官方JSON Output" in prompt and "json" in prompt.lower()
    assert agent_tools(role, planning, protocol=JSON_AGENT_PROTOCOL) == []
    body = agent_request_body([{"role": "system", "content": prompt}], 32768, [], planning, Settings(), protocol=JSON_AGENT_PROTOCOL)
    assert body["response_format"] == {"type": "json_object"} and body["tool_choice"] == "none"
    assert "tools" not in body and body["thinking"] == {"type": "disabled"}
    assert "格式示例文档" in prompt and "示例均未执行" in prompt
    for example in _json_format_examples(role, planning, "professional"):
        Draft202012Validator(schema).validate(example)
        assert json.dumps(example, ensure_ascii=False) in prompt
        if planning:
            assert example["plan"][0]["role"] == role
        elif example["action"] == "tool":
            assert example["tool"] == "read_project" and example["arguments"] == {}


@pytest.mark.parametrize("role,paths", [
    ("character_designer", ["characters[].imagePrompts"]), ("scene_designer", ["scenes[].imagePrompts"]),
    ("storyboarder", ["shots[].imagePrompt"]), ("media_producer", ["shots[].imagePrompt", "shots[].videoPrompt"]),
])
def test_visual_prompts_name_exact_required_nested_paths_without_changing_text_edit_scope(role, paths):
    schema = agent_response_schema(False, protocol=JSON_AGENT_PROTOCOL)
    business = agent_business_tools(role, False)
    prompt = json_agent_prompt(role, business, False, "professional", schema)
    assert all(path in prompt for path in paths)
    for field in ("subject", "action", "scene", "composition", "style", "lighting", "details", "negativePrompt", "outputSpec"):
        assert f'"{field}"' in prompt
    assert "textPolicy" in prompt and "至少三项" in prompt
    local_edit = json_agent_prompt(role, business, False, "text_edit", schema)
    assert "write_artifact的artifact内必填嵌套路径" not in local_edit


@pytest.mark.parametrize("planning", [True, False])
def test_character_prompt_requires_independent_complete_objects_for_every_requested_person(planning):
    business = agent_business_tools("character_designer", planning)
    schema = agent_response_schema(planning, protocol=JSON_AGENT_PROTOCOL)
    prompt = json_agent_prompt("character_designer", business, planning, "professional", schema)
    assert "每名人物必须各有一个characters[]独立对象" in prompt
    assert "逐人完整填写身份、外观锚点、服装、表情动作和连续性要求" in prompt
    assert "imagePrompts中的每项只描述所属人物" in prompt
    assert "characters数组实际数量与各项身份逐一对应" in prompt
    assert "替代另一人物的独立对象" in prompt
    local_edit = json_agent_prompt("character_designer", business, planning, "text_edit", schema)
    assert "每名人物必须各有一个characters[]独立对象" not in local_edit


@pytest.mark.parametrize("role", ["character_designer", "scene_designer", "storyboarder", "media_producer"])
def test_visual_self_check_preserves_attachment_negative_text_and_static_consistency(role):
    prompt = json_agent_prompt(role, agent_business_tools(role, False), False, "professional", agent_response_schema(False))
    assert "具体部位、左右基准、持握方式、固定点或接触点" in prompt
    assert "正面、侧面、背面" in prompt and "真实附着关系" in prompt
    assert "正向物品清单与negativePrompt逐项核对" in prompt
    assert "可见文字、数字、刻度与符号设计须和textPolicy、negativePrompt逐项一致" in prompt
    assert "静帧仅描述拍摄瞬间可见的状态" in prompt
    if role == "storyboarder":
        assert "shots[].motionRequirements" in prompt
    elif role == "media_producer":
        assert "shots[].videoPrompt的actionTimeline、cameraMovement" in prompt
    else:
        assert "时间变化需求在本岗位既有" in prompt


def test_json_message_builder_keeps_real_http_context_without_native_ids(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("authored", "text", "本人填写的资料", content="林岚在旧钟表店检查银色怀表。")])
        response, _ = create_run(client, envelope, role="writer", targets=["authored"], delivery_mode="professional")
        assert response.status_code == 202
        with bridge_client(live_service, client, envelope["project"]["id"])[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            engine = create_engine(f"sqlite:///{live_service.data_dir / 'sceneturn.sqlite3'}")
            with Session(engine) as session:
                actual = session.get(AgentRun, run["id"])
                step = session.scalar(select(AgentStep).where(AgentStep.run_id == actual.id))
                context = retrieve_context(session, actual.project_id, actual.owner_id, actual.scene_id, target_ids=["authored"])
                messages = _json_messages(actual, step, actual.role, context, [], [])
                assert [message["role"] for message in messages] == ["system", "user", "user"]
                assert json.loads(messages[1]["content"])["context"] == context
                reminder = json.loads(messages[-1]["content"])["taskReminder"]
                assert reminder == {"origin": "original_user_goal", "goal": actual.goal, "stepInstruction": step.instruction, "authorizedTargetIds": actual.target_ids_json}
                assert all("tool_call_id" not in message and message["role"] != "tool" for message in messages)
            engine.dispose()


@pytest.mark.parametrize("planning", [True, False])
def test_writer_prompt_requires_explicit_dialogues_defined_references_and_separate_branch_time(planning):
    prompt = json_agent_prompt("writer", agent_business_tools("writer", planning), planning, "professional", agent_response_schema(planning))
    assert "无对白时填写dialogues=[]" in prompt
    assert "branches[].fromScene属于已有scenes.key" in prompt
    assert "branches[].destination属于已有scenes.key或endings.key" in prompt
    assert "计划中准备制作、只在说明提及或留给后续的代号尚未定义" in prompt
    assert "选择等待和分支后续使用独立时间范围" in prompt
    assert "括号均完整闭合" in prompt and "相邻对象属性与数组项之间填写逗号" in prompt


@pytest.mark.parametrize("call_id,kind,paths", [
    ("98f7e632-77a0-46d7-b709-f75773e5ba82", "json_invalid", set()),
    ("d3366f44-a019-4b8f-8e0c-17ff35c2ddbc", "missing", {("scenes", 1, "dialogues"), ("scenes", 3, "dialogues")}),
    ("7564c150-5b86-466b-827b-26f143f0766e", "value_error", {("branches", 0, "destination"), ("branches", 1, "destination")}),
])
def test_original_flash_rejections_remain_strict_after_prompt_revision(call_id, kind, paths):
    database = sqlite_database_path(PRODUCT_ROOT / ".intermediate/agent-smoke/12e9e2bf-c347-4fb9-852c-ca65d1a1b581/data")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        raw, frozen = connection.execute("select raw_output,input_json from agent_calls where id=?", (call_id,)).fetchone()
    frozen = json.loads(frozen)
    with pytest.raises(AppError) as failure:
        decode_platform_message(json.loads(raw), frozen["planning"], "writer", protocol=frozen["protocol"], catalog_version=frozen["toolCatalogVersion"])
    assert failure.value.code == "AGENT_JSON_ARGUMENTS_INVALID"
    if kind == "json_invalid":
        assert isinstance(failure.value.__cause__, json.JSONDecodeError)
    else:
        errors = failure.value.__cause__.errors()
        assert {error["type"] for error in errors} == {kind}
        assert {error["loc"] for error in errors} == paths
        if kind == "value_error":
            raw_decision = json.loads(json.loads(raw)["content"])
            artifact = raw_decision["arguments"]["artifact"]
            for index, error in enumerate(errors):
                assert artifact["branches"][index]["key"] in error["msg"]
                assert artifact["branches"][index]["destination"] in error["msg"]
                assert all(scene["key"] in error["msg"] for scene in artifact["scenes"])
                assert all(ending["key"] in error["msg"] for ending in artifact["endings"])
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_genuine_rejected_requests_enforce_two_corrections_without_model_call():
    found = False
    for database in recorded_databases(PRODUCT_ROOT / ".intermediate" / "agent-smoke"):
        engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
        with Session(engine) as session:
            run = session.scalar(select(AgentRun).where(AgentRun.error_code == "AGENT_NATIVE_CORRECTION_LIMIT"))
            if run is not None:
                calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
                last = next(call for call in reversed(calls) if call.status == "tool_rejected")
                with pytest.raises(AppError) as failure:
                    _native_correction_info(session, run, last.step_id, calls)
                assert failure.value.code == "AGENT_NATIVE_CORRECTION_LIMIT"
                assert all(call.usage_json.get("settled") and call.remote_request_id for call in calls if call.status == "tool_rejected")
                found = True
        engine.dispose()
        if found:
            break
    assert found, "需要本产品已有两次参数修正的真实拒绝记录"


def test_genuine_json_records_keep_actual_content_and_only_real_user_tool_results():
    source = None
    for database in recorded_databases(PRODUCT_ROOT / ".intermediate" / "agent-smoke"):
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT run_id,input_json FROM agent_calls WHERE status='ready'").fetchall()
        match = next((run_id for run_id, frozen in rows if json.loads(frozen).get("protocol") == JSON_AGENT_PROTOCOL), None)
        if match:
            source = database, match
            break
    if source is None:
        pytest.skip("等待统一真实JSON Output验证记录")
    database, run_id = source
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        run = session.get(AgentRun, run_id)
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
        available = [call for call in calls if call.input_json.get("protocol") == JSON_AGENT_PROTOCOL and (call.usage_json.get("toolExecutionDone") or call.status == "tool_rejected")]
        if not available:
            pytest.skip("真实JSON请求尚无已保存的实际处理结果")
        completed = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.status == "completed").order_by(AgentStep.index)).all()
        step = session.scalar(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.status != "completed").order_by(AgentStep.index))
        frozen = json.loads(available[-1].input_json["messages"][1]["content"])
        messages = _json_messages(run, step, step.role if step else run.role, frozen["context"], available, completed)
        assistants = [item["content"] for item in messages if item["role"] == "assistant"]
        user_messages = [json.loads(item["content"]) for item in messages[2:] if item["role"] == "user"]
        results = [item["toolResult"] for item in user_messages if "toolResult" in item]
        assert all(set(item) in ({"toolResult"}, {"taskReminder"}, {"jsonCorrectionReminder"}) for item in user_messages)
        assert assistants == [json.loads(call.raw_output)["content"] for call in available]
        assert [item["callId"] for item in results] == [call.id for call in available]
        assert all(item["role"] != "tool" and "tool_call_id" not in item for item in messages)
        for call in available:
            assert "toolCallId" not in call.usage_json and "nativeFunction" not in call.usage_json
            assert call.usage_json["providerFinishReason"] == "stop" and call.usage_json["settled"]
            assert call.input_json["tools"] == [] and "tools" not in call.input_json["requestBody"]
            if call.status == "ready":
                decision, identifier = decode_platform_message(json.loads(call.raw_output), call.input_json["planning"], call.role, protocol=JSON_AGENT_PROTOCOL)
                assert identifier is None and decision == call.response_json
    engine.dispose()


def test_genuine_pro_write_is_valid_independently_from_cost_limited_run_status():
    source = PRODUCT_ROOT / ".intermediate" / "agent-smoke" / "bc66cd13-bcfa-455d-9306-e3eac6ff1719"
    database = sqlite_database_path(source / "data")
    assert database.exists(), "需要本产品已实际保存的Pro专业产物记录"
    files = [path for path in (database, source / "proof.json", source / "requests.json") if path.exists()]
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        call = next(item for item in session.scalars(select(AgentCall).where(AgentCall.status == "ready"))
            if item.input_json.get("protocol") == JSON_AGENT_PROTOCOL and (item.response_json or {}).get("tool") == "write_artifact")
        run = session.get(AgentRun, call.run_id)
        assert run.status == "failed" and run.error_code == "AGENT_COST_LIMIT"
        assert run.limits_json["maxCostMicros"] == 2000000
        assert run.limits_json["model"] == call.input_json["model"] == call.input_json["requestBody"]["model"] == PRO_MODEL
        _validate_frozen_model(run, call)
        message = json.loads(call.raw_output)
        decision, identifier = decode_platform_message(message, False, call.role, protocol=JSON_AGENT_PROTOCOL)
        assert identifier is None and decision == call.response_json
        assert call.usage_json["providerFinishReason"] == "stop" and call.usage_json["toolExecutionDone"]
        assert call.remote_request_id and call.usage_json["settled"]
        assert "toolCallId" not in call.usage_json and "nativeFunction" not in call.usage_json
        assert call.input_json["price"] == current_price(call.created_at, model=PRO_MODEL)
        reservation = session.get(BudgetReservation, call.id)
        assert reservation.status == "settled" and reservation.reserved_micros == 0 and reservation.cost_micros == call.cost_micros
        step = session.get(AgentStep, call.step_id)
        receipt = next(item for item in step.tool_results_json if item["operationId"] == f"agent-{call.id}")
        artifact = validate_artifact(call.role, decision["arguments"]["artifact"])
        assert receipt["result"]["artifact"] == artifact
        assert receipt["result"]["artifactSha256"] == payload_digest(artifact)
        rendered = render_artifact(artifact)
        assert receipt["result"]["savedTexts"]
        assert all(saved["text"] == rendered and saved["sha256"] == hashlib.sha256(rendered.encode()).hexdigest() for saved in receipt["result"]["savedTexts"])
        assert receipt["result"]["promptItems"] == image_prompt_items(artifact)
        project = session.get(Project, run.project_id)
        evidence = validate_artifacts(session, run, step, project, require_canvas(session, run, project), Settings(data_dir=source / "data"))
        assert evidence["valid"] and evidence["readOnly"]
        assert any(item["kind"] == "creative_artifact" for item in evidence["artifacts"])
        actual_validation = next(item for item in step.tool_results_json if item["tool"] == "validate_artifacts")
        assert actual_validation["result"]["valid"] is True
    engine.dispose()
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files} == hashes
