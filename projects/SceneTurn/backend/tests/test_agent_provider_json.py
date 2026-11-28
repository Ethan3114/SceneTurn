import asyncio

import pytest
from jsonschema import Draft202012Validator

from app.core.config import Settings
from app.core.errors import AppError
from app.services.agent_provider import (
    DEFAULT_AGENT_PROTOCOL, JSON_AGENT_PROTOCOL, MATERIAL_JSON_AGENT_PROTOCOL, THINKING_JSON_AGENT_PROTOCOL, NATIVE_AGENT_PROTOCOL,
    SUPPORTED_JSON_PROTOCOLS, SUPPORTED_NATIVE_PROTOCOLS, SUPPORTED_PLATFORM_PROTOCOLS,
    agent_business_tools, agent_request_body, agent_response_schema, agent_tools,
    complete_agent, is_json_protocol, protocol_kind, provider_endpoint,
)
from app.services.prompts.agent import ROLE_TITLES
from app.tools.project_tools import LEGACY_TOOL_CATALOG_VERSION


@pytest.mark.parametrize("role", ROLE_TITLES)
@pytest.mark.parametrize("planning", [True, False])
@pytest.mark.parametrize("protocol", sorted(SUPPORTED_JSON_PROTOCOLS))
def test_json_role_request_preserves_each_frozen_thinking_mode_without_native_tools(role, planning, protocol):
    settings = Settings()
    messages = [{"role": "system", "content": "按当前岗位完整Schema输出JSON对象。"}]
    tools = agent_tools(role, planning, protocol=protocol)
    body = agent_request_body(messages, 32768, tools, planning, settings, protocol=protocol)
    assert DEFAULT_AGENT_PROTOCOL == THINKING_JSON_AGENT_PROTOCOL == "json-tools-v5-thinking"
    assert tools == [] and "tools" not in body
    assert body["messages"] == messages
    assert body["response_format"] == {"type": "json_object"}
    assert body["tool_choice"] == "none"
    if protocol == JSON_AGENT_PROTOCOL:
        assert body["thinking"] == {"type": "disabled"} and body["temperature"] == 0.4
        assert "reasoning_effort" not in body
    else:
        assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "high"
        assert "temperature" not in body
        assert agent_request_body(messages, 32768, tools, planning, settings) == body
    assert body["stream"] is False
    assert body["max_tokens"] == 32768
    assert provider_endpoint() == "https://api.deepseek.com/chat/completions"
    schema = agent_response_schema(planning)
    Draft202012Validator.check_schema(schema)
    if planning:
        assert set(schema["required"]) == {"action", "plan"}
        assert set(schema["properties"]) == {"action", "plan"}
        assert schema["properties"]["plan"]["minItems"] == 1
    else:
        tool = schema["$defs"]["JsonToolDecision"]
        complete = schema["$defs"]["JsonCompleteDecision"]
        assert set(tool["properties"]) == set(tool["required"]) == {"action", "tool", "arguments"}
        assert set(complete["properties"]) == set(complete["required"]) == {"action", "summary"}
        assert tool["additionalProperties"] is False and complete["additionalProperties"] is False


@pytest.mark.parametrize("protocol", sorted(SUPPORTED_NATIVE_PROTOCOLS))
@pytest.mark.parametrize("planning", [True, False])
def test_native_protocols_keep_explicit_original_request_contract(protocol, planning):
    tools = agent_tools("writer", planning, protocol)
    body = agent_request_body([{"role": "user", "content": "检查原生协议，不发送HTTP请求。"}], 32768, tools, planning, Settings(), protocol)
    assert protocol_kind(protocol) == "native" and not is_json_protocol(protocol)
    assert "response_format" not in body
    assert body["tools"] == tools
    assert body["tool_choice"] == ({"type": "function", "function": {"name": "submit_plan"}} if planning else "required")
    assert body["thinking"] == {"type": "disabled"} and body["temperature"] == 0.4
    assert "reasoning_effort" not in body
    if protocol == "native-tools-v1":
        assert tools == agent_business_tools("writer", planning, LEGACY_TOOL_CATALOG_VERSION)
        assert provider_endpoint(protocol) == "https://api.deepseek.com/chat/completions"
    else:
        assert provider_endpoint(protocol) == "https://api.deepseek.com/beta/chat/completions"
        assert all(tool["function"]["strict"] is True for tool in tools)


def test_protocol_catalog_preserves_all_native_versions():
    assert NATIVE_AGENT_PROTOCOL == "native-tools-v3-strict-ref"
    assert SUPPORTED_NATIVE_PROTOCOLS == {"native-tools-v1", "native-tools-v2-strict", "native-tools-v3-strict-ref", "native-tools-v4-materials-strict"}
    assert JSON_AGENT_PROTOCOL == "json-tools-v4"
    assert MATERIAL_JSON_AGENT_PROTOCOL == "json-tools-v6-materials"
    assert SUPPORTED_JSON_PROTOCOLS == {JSON_AGENT_PROTOCOL, THINKING_JSON_AGENT_PROTOCOL, MATERIAL_JSON_AGENT_PROTOCOL}
    assert SUPPORTED_PLATFORM_PROTOCOLS == SUPPORTED_NATIVE_PROTOCOLS | SUPPORTED_JSON_PROTOCOLS
    assert agent_response_schema(True, JSON_AGENT_PROTOCOL) == agent_response_schema(True, THINKING_JSON_AGENT_PROTOCOL)
    assert agent_response_schema(False, JSON_AGENT_PROTOCOL) == agent_response_schema(False, THINKING_JSON_AGENT_PROTOCOL)
    assert protocol_kind() == "json" and is_json_protocol()


def test_json_rejects_native_tools_and_missing_json_instruction_before_request():
    with pytest.raises(AppError) as native:
        agent_request_body([{"role": "system", "content": "JSON"}], 32768, agent_business_tools("writer", True), True, Settings())
    assert native.value.code == "AGENT_JSON_TOOLS_INVALID"
    with pytest.raises(AppError) as missing:
        agent_request_body([{"role": "system", "content": "创作本次完整角色产物。"}], 32768, [], False, Settings())
    assert missing.value.code == "AGENT_JSON_MESSAGES_INVALID"


def test_unconfigured_json_key_fails_before_network_request():
    with pytest.raises(AppError) as error:
        asyncio.run(complete_agent("", [{"role": "system", "content": "JSON"}], 32768, [], True, Settings()))
    assert error.value.code == "TEXT_NOT_CONFIGURED"
