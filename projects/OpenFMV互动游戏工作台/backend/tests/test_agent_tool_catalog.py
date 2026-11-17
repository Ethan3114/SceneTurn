import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
from jsonschema import Draft202012Validator

from app.core.config import Settings
from app.core.errors import AppError
from app.services.agent_provider import (
    JSON_AGENT_PROTOCOL, SUPPORTED_NATIVE_PROTOCOLS, agent_business_tools,
    agent_request_body, agent_tools, decode_platform_message, resolve_tool_catalog_version, strict_schema,
)
from app.services.text_provider import MODEL
from app.tools.project_tools import (
    LEGACY_TOOL_CATALOG_VERSION, ROLE_TOOLS, TOOL_CATALOG_VERSION, tool_catalog,
)
from conftest import PRODUCT_ROOT


@pytest.mark.parametrize("role", sorted(ROLE_TOOLS))
def test_catalog_versions_preserve_existing_business_schema_and_add_only_explicit_tools(role):
    legacy = tool_catalog(role, LEGACY_TOOL_CATALOG_VERSION)
    current = tool_catalog(role, TOOL_CATALOG_VERSION)
    assert [tool for tool in current if tool["name"] not in {"read_artifact", "patch_artifact"}] == legacy
    assert {tool["name"] for tool in current} - {tool["name"] for tool in legacy} == {"read_artifact", "patch_artifact"}
    for tool in current:
        Draft202012Validator.check_schema(tool["inputSchema"])
    assert agent_business_tools(role, True, "R1") == agent_business_tools(role, True, "R2")


@pytest.mark.parametrize("protocol", sorted(SUPPORTED_NATIVE_PROTOCOLS))
@pytest.mark.parametrize("planning", [True, False])
def test_native_protocols_keep_r1_and_reject_r2_before_supplier_request(protocol, planning):
    version = "R5" if protocol == "native-tools-v4-materials-strict" else "R1"
    assert resolve_tool_catalog_version(protocol) == version
    tools = agent_tools("writer", planning, protocol)
    explicit = agent_tools("writer", planning, protocol, MODEL, version)
    assert tools == explicit
    messages = [{"role": "system", "content": "核对历史协议，无模型请求。"}]
    assert agent_request_body(messages, 32768, tools, planning, Settings(), protocol) == agent_request_body(messages, 32768, tools, planning, Settings(), protocol, MODEL, version)
    with pytest.raises(AppError) as failure:
        agent_tools("writer", planning, protocol, MODEL, "R2")
    assert failure.value.code == "AGENT_TOOL_CATALOG_PROTOCOL_INVALID"
    with pytest.raises(AppError) as request_failure:
        agent_request_body(messages, 32768, tools, planning, Settings(), protocol, MODEL, "R2")
    assert request_failure.value.code == "AGENT_TOOL_CATALOG_PROTOCOL_INVALID"


def test_json_r2_keeps_full_json_patch_value_without_sending_native_tools():
    assert resolve_tool_catalog_version() == "R2"
    business = agent_business_tools("character_designer", False, "R2")
    patch = next(tool for tool in business if tool["function"]["name"] == "patch_artifact")
    parameters = patch["function"]["parameters"]
    assert "value" in parameters["$defs"]["ArtifactPatch"]["required"]
    assert "JsonValue" in parameters["$defs"]
    tools = agent_tools("character_designer", False, JSON_AGENT_PROTOCOL, catalog_version="R2")
    body = agent_request_body([{"role": "system", "content": "按完整业务Schema输出JSON对象。"}], 32768, tools, False, Settings(), JSON_AGENT_PROTOCOL, catalog_version="R2")
    assert tools == [] and "tools" not in body
    assert body["response_format"] == {"type": "json_object"} and body["tool_choice"] == "none"
    for protocol in ("native-tools-v2-strict", "native-tools-v3-strict-ref"):
        with pytest.raises(AppError) as failure:
            strict_schema(parameters, protocol)
        assert failure.value.code == "AGENT_STRICT_SCHEMA_UNSUPPORTED"


@pytest.mark.parametrize("group", [
    "c1bcf2c3-4811-4f42-9b68-1d07a886a89a",
    "45b49514-d7c2-4744-8f77-2ea1e46ce815",
    "77088ff1-e364-44b6-beba-0a8256c17efc",
    "bc66cd13-bcfa-455d-9306-e3eac6ff1719",
    "dac0c69c-d212-4a28-95b8-53dc8aa20911",
])
def test_real_frozen_r1_requests_remain_exact_and_database_is_read_only(group):
    database = PRODUCT_ROOT / ".intermediate" / "agent-smoke" / group / "data" / "openfmv.sqlite3"
    if not database.exists():
        pytest.skip("需要本产品已保存的真实供应商请求")
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute("SELECT role,input_json,raw_output,response_json FROM agent_calls WHERE provider='deepseek' AND input_json IS NOT NULL").fetchall()
        assert rows
        for role, serialized, raw_output, recorded_response in rows:
            frozen = json.loads(serialized)
            if "protocol" not in frozen:
                continue
            assert frozen.get("toolCatalogVersion", "R1") == "R1"
            protocol = frozen["protocol"]
            actual = agent_request_body(frozen["messages"], frozen["maxTokens"], frozen["tools"], frozen["planning"], Settings(), protocol, frozen.get("model", MODEL), "R1")
            assert actual == frozen["requestBody"]
            if group in {"bc66cd13-bcfa-455d-9306-e3eac6ff1719", "dac0c69c-d212-4a28-95b8-53dc8aa20911"}:
                assert agent_business_tools(role, frozen["planning"], "R1") == frozen["businessTools"]
            if recorded_response and (canonical := json.loads(recorded_response)).get("action"):
                decision, _ = decode_platform_message(json.loads(raw_output), frozen["planning"], role, protocol, catalog_version="R1")
                assert decision == canonical
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
