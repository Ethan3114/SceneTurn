import copy
import hashlib
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent_models import AgentCall, AgentRun, AgentStep
from app.core.errors import AppError
from app.services.agent import _json_messages, _native_correction_info, _native_tool_error
from app.services.agent_provider import decode_platform_message, json_syntax_details
from app.core.config import sqlite_database_path
from conftest import PRODUCT_ROOT


DATABASE = sqlite_database_path(PRODUCT_ROOT / ".intermediate/agent-smoke/93a18558-8403-47c1-b4eb-1c5b5857a204/data")


def test_real_flash_media_json_rejections_include_exact_position_and_bounded_original_excerpt():
    before = hashlib.sha256(DATABASE.read_bytes()).hexdigest()
    engine = create_engine(f"sqlite:///{DATABASE.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        calls = session.scalars(select(AgentCall).where(AgentCall.role == "media_producer", AgentCall.status == "tool_rejected")).all()
        assert len(calls) == 3
        for call in calls:
            original_usage = copy.deepcopy(call.usage_json)
            raw_message = json.loads(call.raw_output)
            with pytest.raises(AppError) as failure:
                decode_platform_message(raw_message, call.input_json["planning"], call.role,
                    protocol=call.input_json["protocol"], catalog_version=call.input_json["toolCatalogVersion"])
            cause = failure.value.__cause__
            assert isinstance(cause, json.JSONDecodeError)
            assert (cause.lineno, cause.colno, cause.pos) == (1, 8252, 8251)
            detail = _native_tool_error(failure.value)["details"][0]
            assert detail == json_syntax_details(cause)
            assert (detail["lineno"], detail["colno"], detail["pos"]) == (1, 8252, 8251)
            assert len(detail["excerpt"]) <= 160
            assert detail["excerpt"] == raw_message["content"][detail["excerptStart"]:detail["excerptStart"] + 160]
            assert detail["excerptStart"] + detail["excerptErrorOffset"] == cause.pos
            assert "input" not in detail and "doc" not in detail
            assert call.usage_json == original_usage
    engine.dispose()
    assert hashlib.sha256(DATABASE.read_bytes()).hexdigest() == before


def test_real_rejection_history_keeps_actual_messages_and_adds_only_derived_diagnostics():
    before = hashlib.sha256(DATABASE.read_bytes()).hexdigest()
    engine = create_engine(f"sqlite:///{DATABASE.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        run = session.scalar(select(AgentRun).where(AgentRun.role == "media_producer"))
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
        step = session.scalar(select(AgentStep).where(AgentStep.run_id == run.id))
        original_errors = {call.id: copy.deepcopy(call.usage_json.get("toolError")) for call in calls}
        context = json.loads(calls[-1].input_json["messages"][1]["content"])["context"]
        messages = _json_messages(run, step, run.role, context, calls, [])
        assert [m["content"] for m in messages if m["role"] == "assistant"] == [json.loads(call.raw_output)["content"] for call in calls]
        results = [json.loads(m["content"])["toolResult"] for m in messages if m["role"] == "user" and "toolResult" in json.loads(m["content"])]
        corrections = [r for r in results if "jsonCorrection" in r["result"]]
        assert len(corrections) == 3
        for result in corrections:
            assert result["result"]["error"] == original_errors[result["callId"]]
            correction = result["result"]["jsonCorrection"]
            assert correction["sourceCallId"] == result["callId"]
            assert correction["origin"] == "standard_json_diagnostics"
            assert correction["location"]["pos"] == 8251 and len(correction["location"]["excerpt"]) <= 160
            assert "禁止原样重复同一非法回复" in correction["instruction"]
        assert all(call.usage_json.get("toolError") == original_errors[call.id] for call in calls)
        with pytest.raises(AppError) as limit:
            _native_correction_info(session, run, step.id, calls)
        assert limit.value.code == "AGENT_NATIVE_CORRECTION_LIMIT"
        assert json.loads(messages[-2]["content"])["taskReminder"]["goal"] == run.goal
        reminder = json.loads(messages[-1]["content"])["jsonCorrectionReminder"]
        assert reminder == corrections[-1]["result"]["jsonCorrection"]
        assert "完整Schema检查当前完整对象的字段归属" in reminder["instruction"]
    engine.dispose()
    assert hashlib.sha256(DATABASE.read_bytes()).hexdigest() == before
