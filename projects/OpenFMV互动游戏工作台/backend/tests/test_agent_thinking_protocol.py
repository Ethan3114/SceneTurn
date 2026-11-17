import asyncio
import copy
import hashlib
import json
import sqlite3
import time
from contextlib import closing

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent_models import AgentCall, AgentRun
from app.agent_schemas import RunCreate
from app.core.config import Settings
from app.services.agent import _new_run_limits, _public_call_usage, _public_run_result
from app.services.agent_provider import (
    DEFAULT_AGENT_PROTOCOL, JSON_AGENT_PROTOCOL, THINKING_JSON_AGENT_PROTOCOL,
    SUPPORTED_NATIVE_PROTOCOLS, _agent_request_deadline, agent_request_body, provider_endpoint,
)
from app.services.text_provider import MODEL, TextProviderFailure, cost_micros
from conftest import PRODUCT_ROOT
from test_agent_runtime import create_run
from test_api import create_project, registered_client


def test_actual_http_unconfigured_platform_stays_unsubmitted_and_new_limits_use_thinking(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = create_project(client)
        response, payload = create_run(client, envelope, executor="platform", delivery_mode="professional")
        assert response.status_code == 402 and response.json()["error"]["code"] == "TEXT_BILLING_NOT_CONFIGURED"
        limits = _new_run_limits(RunCreate.model_validate(payload))
        engine = create_engine(f"sqlite:///{live_service.data_dir / 'openfmv.sqlite3'}")
        with Session(engine) as session:
            assert session.scalars(select(AgentRun).where(AgentRun.project_id == envelope["project"]["id"])).all() == []
            assert session.scalars(select(AgentCall)).all() == []
            assert limits["providerProtocol"] == DEFAULT_AGENT_PROTOCOL == THINKING_JSON_AGENT_PROTOCOL
            assert limits["model"] == MODEL and limits["toolCatalogVersion"] == "R2"
            body = agent_request_body([{"role": "system", "content": "按完整Schema只输出json对象。"}],
                limits["maxTokens"], [], True, Settings(), protocol=limits["providerProtocol"], model=limits["model"], catalog_version="R2")
            assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "high"
            assert "temperature" not in body and "tools" not in body
            upper = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) + 2048
            assert upper > len(json.dumps(body["messages"], ensure_ascii=False).encode())
        engine.dispose()


def test_real_total_deadline_expires_despite_periodic_progress_and_keeps_unknown_outcome():
    progress = []

    async def operation():
        async with _agent_request_deadline(0.03):
            while True:
                await asyncio.sleep(0.003)
                progress.append(time.monotonic())

    started = time.monotonic()
    with pytest.raises(TextProviderFailure) as failure:
        asyncio.run(operation())
    assert progress and 0.02 <= time.monotonic() - started < 1.0
    assert failure.value.unknown is True and failure.value.code == "TEXT_SUBMISSION_UNKNOWN"
    assert isinstance(failure.value.__cause__, TimeoutError)
    assert "预留继续保留" in failure.value.message and "不会自动重发" in failure.value.message


def test_authored_private_field_projection_preserves_input_and_only_exposes_allowed_fields():
    # 公开字段投影处理作者输入字典。
    private = {"candidate": {"providerMessage": {
        "content": "人工编写的公开字段检查", "reasoning_content": "人工编写的私有字段检查", "providerExtra": "人工编写的额外字段检查"}},
        "other": "保留已有结果"}
    original = copy.deepcopy(private)
    public = _public_run_result(private)
    assert public["candidate"]["providerMessage"] == {"content": private["candidate"]["providerMessage"]["content"]}
    assert public["other"] == private["other"] and private == original
    assert _public_run_result(None) is None
    database = PRODUCT_ROOT / ".intermediate/agent-smoke/93a18558-8403-47c1-b4eb-1c5b5857a204/data/openfmv.sqlite3"
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        usage = json.loads(connection.execute("SELECT usage_json FROM agent_calls WHERE total_tokens IS NOT NULL LIMIT 1").fetchone()[0])
    public_usage = _public_call_usage({**usage, "reasoning_content": "人工编写的私有字段检查"})
    assert "reasoning_content" not in public_usage
    assert public_usage == _public_call_usage(usage)
    assert public_usage["completion_tokens"] == usage["completion_tokens"]


def test_all_actual_v4_and_native_request_bytes_usage_and_private_records_remain_unchanged():
    checked, protocols = 0, set()
    for database in (PRODUCT_ROOT / ".intermediate" / "agent-smoke").glob("*/data/openfmv.sqlite3"):
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT input_json,usage_json,cost_micros,output_tokens,raw_output FROM agent_calls").fetchall()
        for row in rows:
            frozen, usage = json.loads(row["input_json"]), json.loads(row["usage_json"])
            protocol = frozen.get("protocol")
            if protocol not in SUPPORTED_NATIVE_PROTOCOLS | {JSON_AGENT_PROTOCOL} or "requestBody" not in frozen:
                continue
            body = agent_request_body(frozen["messages"], frozen["maxTokens"], frozen["tools"], frozen["planning"], Settings(),
                protocol=protocol, model=frozen.get("model", MODEL), catalog_version=frozen.get("toolCatalogVersion", "R1"))
            assert json.dumps(body, ensure_ascii=False) == json.dumps(frozen["requestBody"], ensure_ascii=False)
            if "providerEndpoint" in frozen:
                assert provider_endpoint(protocol) == frozen["providerEndpoint"]
            assert body["thinking"] == {"type": "disabled"} and body["temperature"] == 0.4
            assert "reasoning_effort" not in body
            if usage.get("settled") and row["output_tokens"] is not None and "prompt_cache_hit_tokens" in usage:
                assert row["cost_micros"] == cost_micros(usage["prompt_cache_hit_tokens"], usage["prompt_cache_miss_tokens"], row["output_tokens"], frozen["price"])
            protocols.add(protocol)
            checked += 1
        assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert checked > 0 and protocols == SUPPORTED_NATIVE_PROTOCOLS | {JSON_AGENT_PROTOCOL}
