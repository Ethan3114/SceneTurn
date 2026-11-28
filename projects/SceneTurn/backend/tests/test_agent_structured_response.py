import json

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.creative_artifacts import artifact_model
from app.services.agent_provider import MATERIAL_JSON_AGENT_PROTOCOL, agent_request_body, decode_structured_scene_closure, provider_endpoint
from app.services.text_provider import ENDPOINT, MODEL, RESPONSES_ENDPOINT, StructuredResponse
from test_agent_professional_artifacts import manual_artifact


def test_structured_material_request_uses_scene_schema_and_responses_endpoint():
    schema = artifact_model("scene_designer").model_json_schema(by_alias=True)
    messages = [{"role": "system", "content": "输出场景JSON"}]
    body = agent_request_body(messages, 128, [], False, Settings(), protocol=MATERIAL_JSON_AGENT_PROTOCOL,
        structured_response_schema=schema)
    assert body["model"] == MODEL
    assert body["input"] == messages
    assert body["text"]["format"] == {"type": "json_schema", "name": "material_artifact", "schema": schema}
    assert body["max_output_tokens"] == 128 and body["tool_choice"] == "none"
    assert "tools" not in body and "response_format" not in body
    assert provider_endpoint(MATERIAL_JSON_AGENT_PROTOCOL, structured_response=True) == RESPONSES_ENDPOINT
    assert provider_endpoint(MATERIAL_JSON_AGENT_PROTOCOL) == ENDPOINT
    with pytest.raises(AppError, match="结构化产物请求需要当前素材协议"):
        agent_request_body(messages, 128, [], False, Settings(), structured_response_schema=schema)


def response_payload():
    return {"object": "response", "id": "contract-example", "model": MODEL, "status": "completed",
        "output": [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "内部推理"}]},
            {"type": "message", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": "{\"title\":\"场景\"}"}]}],
        "usage": {"input_tokens": 128, "input_tokens_details": {"cached_tokens": 32}, "output_tokens": 26,
            "output_tokens_details": {"reasoning_tokens": 18}, "total_tokens": 154}}


def test_structured_response_preserves_documented_usage_and_only_returns_assistant_text():
    completion = StructuredResponse.model_validate(response_payload()).completion()
    assert completion.id == "contract-example"
    assert completion.choices[0].finish_reason == "stop"
    assert json.loads(completion.choices[0].message.content) == {"title": "场景"}
    assert completion.usage.prompt_cache_hit_tokens == 32
    assert completion.usage.prompt_cache_miss_tokens == 96
    assert completion.usage.completion_tokens == 26 and completion.usage.total_tokens == 154


@pytest.mark.parametrize("case,finish_reason", [
    ("incomplete", "incomplete"), ("failed", "failed"), ("tool_call", "unexpected_output"),
])
def test_structured_response_blocks_unfinished_or_unexpected_outputs_without_losing_usage(case, finish_reason):
    payload = response_payload()
    if case == "tool_call":
        payload["output"].append({"type": "function_call"})
    else:
        payload["status"] = case
    completion = StructuredResponse.model_validate(payload).completion()
    assert completion.choices[0].finish_reason == finish_reason
    assert completion.usage.total_tokens == 154


def test_structured_response_rejects_unaccountable_usage():
    payload = response_payload()
    payload["usage"]["input_tokens_details"]["cached_tokens"] = 129
    with pytest.raises(ValidationError):
        StructuredResponse.model_validate(payload).completion()


def test_scene_closure_restores_only_the_exact_missing_end_delimiters():
    artifact = manual_artifact("scene_designer", "project")
    scenes = artifact.pop("scenes")
    artifact.update(schemaVersion=1, scenes=scenes)
    valid = json.dumps(artifact)
    message = {"role": "assistant", "content": valid[:-2] + "\n"}
    repaired, suffix = decode_structured_scene_closure(message)
    assert suffix == "]}"
    assert message["content"] == valid[:-2] + "\n"
    assert repaired["tool"] == "write_artifact"
    assert repaired["arguments"]["artifact"] == artifact == json.loads(message["content"] + suffix)
    assert decode_structured_scene_closure({"role": "assistant", "content": valid}) is None
    assert decode_structured_scene_closure({"role": "assistant", "content": valid[1:-2]}) is None
    invalid_artifact = {**artifact, "title": 1}
    assert decode_structured_scene_closure({"role": "assistant", "content": json.dumps(invalid_artifact)[:-2]}) is None
