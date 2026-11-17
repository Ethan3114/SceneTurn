import asyncio
import copy
import hashlib

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent_models import AgentCall, AgentRun
from app.core.config import Settings
from app.core.errors import AppError
from app.services.agent import _public_call_usage, _record_provider_failure, _redact_private_value
from app.services.agent_provider import agent_tools, complete_agent
from app.services.text_provider import TextProviderFailure, provider_rejection_message
from conftest import PRODUCT_ROOT


def test_exact_private_redaction_with_authored_text_and_empty_credential():
    # 人工文字用于检查精确替换，不代表供应商响应或凭据。
    phrase = "本人指定的保密短语"
    authored = {"正文": f"保留前文，{phrase}，保留后文。", "列表": [phrase, 400, None, {phrase: "普通说明"}], "相似文字": "本人指定的保密"}
    original = copy.deepcopy(authored)
    redacted = _redact_private_value(authored, phrase)
    assert phrase not in str(redacted)
    assert redacted["正文"] == "保留前文，[REDACTED]，保留后文。"
    assert redacted["列表"] == ["[REDACTED]", 400, None, {"[REDACTED]": "普通说明"}]
    assert redacted["相似文字"] == authored["相似文字"]
    assert _redact_private_value(authored, "") == original and authored == original


def test_private_diagnostics_are_excluded_from_page_usage_projection():
    private_fields = {"httpStatus": 400, "providerError": {"本人私有说明": "用于检查页面字段过滤"}}
    usage = {"settled": True, "costEstimate": True, "prompt_tokens": 12, **private_fields}
    assert _public_call_usage(usage) == {"settled": True, "costEstimate": True, "prompt_tokens": 12}
    assert {key: usage[key] for key in private_fields} == private_fields


def test_no_credential_is_rejected_before_any_supplier_request():
    with pytest.raises(AppError) as failure:
        asyncio.run(complete_agent("", [{"role": "user", "content": "本人填写的创作要求"}], 32768, agent_tools("writer", True), True, Settings()))
    assert failure.value.code == "TEXT_NOT_CONFIGURED" and failure.value.status == 503


def test_genuine_old_rejected_call_keeps_original_record_without_missing_metadata_invention():
    database = PRODUCT_ROOT / ".intermediate" / "agent-smoke" / "f30f1a24-888b-4c40-ac18-28e9d2ee9daf" / "data" / "openfmv.sqlite3"
    if not database.exists():
        pytest.skip("需要本产品已有严格端点实际拒绝记录")
    original_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        call = session.scalar(select(AgentCall).where(AgentCall.status == "failed").order_by(AgentCall.created_at.desc()))
        assert call is not None and call.usage_json["settled"] and call.cost_micros == 0
        run = session.get(AgentRun, call.run_id)
        before = copy.deepcopy(call.usage_json)
        failure = TextProviderFailure(run.error_code, run.error_message, False)
        _record_provider_failure(call, failure, "")
        assert call.usage_json == before
        assert failure.http_status is None and failure.provider_error is None
    engine.dispose()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == original_hash


@pytest.mark.parametrize("status", [400, 401, 403, 429])
def test_public_rejection_messages_distinguish_parameters_identity_and_limit(status):
    message = provider_rejection_message(status)
    if status == 400:
        assert "参数" in message and "Key" not in message
    elif status in {401, 403}:
        assert "身份或权限" in message and "Key" in message
    else:
        assert "限额" in message and "Key" not in message
