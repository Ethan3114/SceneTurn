import json
import sqlite3

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent_models import AgentCall, AgentRun
from app.agent_schemas import RoleId, RunCreate
from app.core.config import Settings
from app.services.agent import _new_run_limits, _run_model, _validate_frozen_model
from app.services.agent_provider import DEFAULT_AGENT_PROTOCOL, JSON_AGENT_PROTOCOL, THINKING_JSON_AGENT_PROTOCOL, agent_request_body
from app.services.text_provider import MODEL, PRO_MODEL, cost_micros, current_price, peak_price
from conftest import PRODUCT_ROOT


def test_new_platform_model_and_cost_ceiling_are_frozen_independently_from_codex():
    body = RunCreate(operation_id="model-choice", expected_revision=1, scene_id="scene", goal="本人要求完成本岗位专业产物。",
        executor="platform", limits={"maxTokens": 32768, "maxCostMicros": 10000000})
    frozen = _new_run_limits(body)
    assert frozen["providerProtocol"] == DEFAULT_AGENT_PROTOCOL == THINKING_JSON_AGENT_PROTOCOL
    assert frozen["model"] == MODEL == "deepseek-flash"
    assert frozen["model"] != PRO_MODEL and frozen["maxCostMicros"] == 10000000
    body.executor = "codex"
    personal = _new_run_limits(body)
    assert "model" not in personal and "providerProtocol" not in personal


def test_model_and_pro_peak_price_are_consistent_in_actual_request_configuration():
    body = agent_request_body([{"role": "system", "content": "仅输出符合本次要求的json对象。"}], 32768, [], False, Settings(),
        protocol=JSON_AGENT_PROTOCOL, model=PRO_MODEL)
    assert body["model"] == PRO_MODEL and body["response_format"] == {"type": "json_object"}
    assert "tools" not in body and body["tool_choice"] == "none"
    pro, flash = peak_price(PRO_MODEL), peak_price(MODEL)
    assert (pro["cacheHitMicrosPerMillion"], pro["cacheMissMicrosPerMillion"], pro["outputMicrosPerMillion"]) == (300000, 9000000, 27000000)
    assert (flash["cacheHitMicrosPerMillion"], flash["cacheMissMicrosPerMillion"], flash["outputMicrosPerMillion"]) == (40000, 2000000, 8000000)
    actual_body_upper = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) + 2048
    reserve = cost_micros(0, actual_body_upper, 32768, pro)
    assert reserve > cost_micros(0, actual_body_upper, 32768, flash)
    assert cost_micros(0, 0, 32768, pro) == 884736
    assert reserve < 10000000


def test_existing_json_without_model_and_native_records_keep_actual_flash_prices():
    checked = 0
    for database in (PRODUCT_ROOT / ".intermediate" / "agent-smoke").glob("*/data/openfmv.sqlite3"):
        engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
        with Session(engine) as session:
            runs = session.scalars(select(AgentRun).where(AgentRun.executor == "platform")).all()
            for run in runs:
                if "model" in run.limits_json:
                    continue
                assert _run_model(run) == MODEL
                for call in session.scalars(select(AgentCall).where(AgentCall.run_id == run.id)):
                    if "price" in call.input_json:
                        assert call.input_json["price"] == current_price(call.created_at)
                        assert call.input_json["price"] == current_price(call.created_at, model=MODEL)
                        if call.remote_request_id is not None:
                            _validate_frozen_model(run, call)
                        checked += 1
        engine.dispose()
    assert checked > 0


@pytest.mark.parametrize("role", ["director", "writer", "character_designer", "scene_designer", "storyboarder", "media_producer", "editor", "interaction_designer", "sound_designer"])
@pytest.mark.parametrize("delivery_mode", ["professional", "text_edit", "tool_action"])
def test_all_new_platform_role_limits_freeze_authorized_flash(role: RoleId, delivery_mode):
    body = RunCreate(operation_id="authorized-model", expected_revision=1, scene_id="scene", role=role,
                     goal="本人授权的本岗位任务。", executor="platform", delivery_mode=delivery_mode)
    frozen = _new_run_limits(body)
    assert frozen["model"] == MODEL == "deepseek-flash"
    request = agent_request_body([{"role": "system", "content": "仅输出json对象。"}], body.limits.max_tokens, [], False,
                                 Settings(), protocol=frozen["providerProtocol"], model=frozen["model"], catalog_version=frozen["toolCatalogVersion"])
    assert request["model"] == MODEL and request["model"] != PRO_MODEL


@pytest.mark.parametrize("placement", ["request", "limits"])
def test_client_cannot_select_pro_for_new_platform_tasks(placement):
    payload = {"operationId": "model-boundary", "expectedRevision": 1, "sceneId": "scene", "goal": "本人授权的专业任务。", "executor": "platform"}
    if placement == "request":
        payload["model"] = PRO_MODEL
    else:
        payload["limits"] = {"model": PRO_MODEL}
    with pytest.raises(ValidationError) as failure:
        RunCreate.model_validate(payload)
    assert all(error["type"] == "extra_forbidden" for error in failure.value.errors())


def test_existing_real_pro_records_keep_their_frozen_model_price_and_reply():
    checked = 0
    for database in (PRODUCT_ROOT / ".intermediate" / "agent-smoke").glob("*/data/openfmv.sqlite3"):
        engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
        with Session(engine) as session:
            for run in session.scalars(select(AgentRun).where(AgentRun.executor == "platform")):
                if run.limits_json.get("model") != PRO_MODEL:
                    continue
                assert _run_model(run) == PRO_MODEL
                for call in session.scalars(select(AgentCall).where(AgentCall.run_id == run.id)):
                    assert call.input_json["model"] == call.input_json["requestBody"]["model"] == PRO_MODEL
                    assert call.input_json["price"] == current_price(call.created_at, model=PRO_MODEL)
                    if call.remote_request_id is not None:
                        _validate_frozen_model(run, call)
                    checked += 1
        engine.dispose()
    assert checked > 0, "需要既有真实Pro历史记录供只读复核"
