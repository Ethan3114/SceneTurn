import asyncio

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.errors import AppError
from app.services.text_provider import Choice, Completion, TextProviderFailure, Usage, provider_rejection_message
from app.services.wan import endpoint as workspace_endpoint


MODEL = "qwen3.8-flash"


def endpoint(workspace_id, region="cn-beijing"):
    return workspace_endpoint(workspace_id, region) + "/compatible-mode/v1/chat/completions"


def request_body(messages, max_tokens):
    if type(max_tokens) is not int or not 64 <= max_tokens <= 32768:
        raise AppError(422, "QWEN_TOKEN_LIMIT", "千问输出Token上限无效")
    def valid_message(message):
        content = message.get("content")
        if isinstance(content, str):
            return message.get("role") in {"system", "user", "assistant"}
        return message.get("role") == "user" and isinstance(content, list) and bool(content) and all(
            isinstance(part, dict) and (part.get("type") == "text" and isinstance(part.get("text"), str)
            or part.get("type") == "image_url" and isinstance(part.get("image_url"), dict)
            and str(part["image_url"].get("url", "")).startswith("data:image/jpeg;base64,")) for part in content)

    if not messages or not all(valid_message(message) for message in messages):
        raise AppError(422, "QWEN_MESSAGES_INVALID", "千问生成需要明确的文本消息")
    return {"model": MODEL, "messages": [{"role": item["role"], "content": item["content"]} for item in messages],
        "enable_thinking": False, "preserve_thinking": False, "response_format": {"type": "json_object"},
        "stream": False, "max_tokens": max_tokens}


class QwenUsage(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    prompt_tokens: int = Field(ge=1)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=1)
    prompt_tokens_details: dict | None = None

    @model_validator(mode="after")
    def correct_total(self):
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("千问Token总量不一致")
        return self


class QwenCompletion(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    id: str = Field(min_length=1, max_length=200)
    model: str
    choices: list[Choice] = Field(min_length=1, max_length=1)
    usage: QwenUsage


def parse_response(payload, max_tokens):
    value = QwenCompletion.model_validate(payload)
    if value.model != MODEL or value.usage.completion_tokens > max_tokens:
        raise ValueError("千问响应模型或Token用量与请求不一致")
    cached = (value.usage.prompt_tokens_details or {}).get("cached_tokens")
    if cached is not None and (type(cached) is not int or not 0 <= cached <= value.usage.prompt_tokens):
        raise ValueError("千问缓存用量无效")
    hit = cached if cached is not None else 0
    usage = Usage(prompt_tokens=value.usage.prompt_tokens, completion_tokens=value.usage.completion_tokens,
        total_tokens=value.usage.total_tokens, prompt_cache_hit_tokens=hit, prompt_cache_miss_tokens=value.usage.prompt_tokens - hit,
        providerUsage=value.usage.model_dump(), cacheUsageReported=cached is not None,
        costPolicy="reported_cache" if cached is not None else "all_input_as_uncached_estimate")
    return Completion(id=value.id, model=value.model, choices=value.choices, usage=usage)


async def complete_qwen(api_key, workspace_id, region, messages, max_tokens, settings):
    if not api_key.strip():
        raise AppError(503, "QWEN_NOT_CONFIGURED", "请先保存千问API Key")
    address, body = endpoint(workspace_id, region), request_body(messages, max_tokens)
    try:
        async with asyncio.timeout(settings.text_timeout):
            async with httpx.AsyncClient(timeout=settings.text_timeout, trust_env=False, follow_redirects=False) as client:
                response = await client.post(address, headers={"Authorization": f"Bearer {api_key}"}, json=body)
    except (TimeoutError, httpx.TransportError) as error:
        raise TextProviderFailure("TEXT_SUBMISSION_UNKNOWN", "千问请求结果待核实，费用预留继续保留，系统不会自动重发", True) from error
    if response.status_code != 200:
        unknown = response.status_code == 408 or response.status_code >= 500 or 300 <= response.status_code < 400
        raise TextProviderFailure("TEXT_SUBMISSION_UNKNOWN" if unknown else "TEXT_PROVIDER_REJECTED", provider_rejection_message(response.status_code), unknown, http_status=response.status_code)
    try:
        return parse_response(response.json(), max_tokens)
    except (ValueError, ValidationError) as error:
        raise TextProviderFailure("TEXT_USAGE_UNKNOWN", "千问响应或用量未通过校验，原预留继续保留", True) from error
