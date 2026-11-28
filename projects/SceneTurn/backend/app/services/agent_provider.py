import asyncio
import json
from copy import deepcopy
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError, model_validator
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from app.agent_schemas import ExecutionPlanItem, MaterialDirectorBrief, MaterialDirectorQuestion, MaterialPlanningDecision, PlanningDecision, StepDecision
from app.core.config import Settings
from app.core.errors import AppError
from app.creative_artifacts import ArtifactSource, artifact_model, validate_artifact
from app.schemas import WireModel
from app.services.text_provider import ENDPOINT, RESPONSES_ENDPOINT, STRICT_ENDPOINT, MODEL, PRO_MODEL, Completion, Message, TextProviderFailure, complete_chat_request, complete_structured_request, model_aliases
from app.tools.project_tools import (
    LEGACY_TOOL_CATALOG_VERSION, MATERIAL_SOP_ROLE_TOOLS, MATERIAL_SOP_TOOL_CATALOG_VERSION, MATERIAL_TOOL_CATALOG_VERSION,
    MATERIAL_CATALOG_VERSIONS, ROLE_TOOLS, SCHEMAS, SUPPORTED_TOOL_CATALOG_VERSIONS,
    TOOL_CATALOG_VERSION, ArtifactPatch, PatchArtifactArguments, allowed_tools, tool_catalog, tool_schema,
)


NATIVE_AGENT_PROTOCOL = "native-tools-v3-strict-ref"
MATERIAL_NATIVE_AGENT_PROTOCOL = "native-tools-v4-materials-strict"
JSON_AGENT_PROTOCOL = "json-tools-v4"
THINKING_JSON_AGENT_PROTOCOL = "json-tools-v5-thinking"
MATERIAL_JSON_AGENT_PROTOCOL = "json-tools-v6-materials"
MATERIAL_STRUCTURED_RECOVERY_ROLES = frozenset({"character_designer", "storyboarder"})
THINKING_JSON_PROTOCOLS = frozenset({THINKING_JSON_AGENT_PROTOCOL, MATERIAL_JSON_AGENT_PROTOCOL})
SUPPORTED_JSON_PROTOCOLS = frozenset({JSON_AGENT_PROTOCOL, *THINKING_JSON_PROTOCOLS})
DEFAULT_AGENT_PROTOCOL = THINKING_JSON_AGENT_PROTOCOL
SUPPORTED_NATIVE_PROTOCOLS = frozenset({"native-tools-v1", "native-tools-v2-strict", NATIVE_AGENT_PROTOCOL, MATERIAL_NATIVE_AGENT_PROTOCOL})
SUPPORTED_PLATFORM_PROTOCOLS = SUPPORTED_NATIVE_PROTOCOLS | SUPPORTED_JSON_PROTOCOLS
SUPPORTED_AGENT_PROTOCOLS = SUPPORTED_PLATFORM_PROTOCOLS
REQUEST_ENDPOINT = {"native-tools-v1": ENDPOINT, "native-tools-v2-strict": STRICT_ENDPOINT, NATIVE_AGENT_PROTOCOL: STRICT_ENDPOINT,
    MATERIAL_NATIVE_AGENT_PROTOCOL: STRICT_ENDPOINT, **{protocol: ENDPOINT for protocol in SUPPORTED_JSON_PROTOCOLS}}
FunctionName = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
CallId = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^\S+$")]


class NativeModel(WireModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class SubmitPlanArguments(NativeModel):
    plan: list[ExecutionPlanItem] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def check_dependencies(self):
        for index, step in enumerate(self.plan):
            if len(set(step.depends_on)) != len(step.depends_on) or any(dependency < 0 or dependency >= index for dependency in step.depends_on):
                raise ValueError("计划依赖必须唯一且引用前面的步骤")
            if step.kind == "verify" and (not step.depends_on or any(self.plan[dependency].kind != "produce" for dependency in step.depends_on)):
                raise ValueError("验证步骤必须依赖前面已列出的产物步骤")
        return self


class CompleteStepArguments(NativeModel):
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]


class SubmitMaterialPlanArguments(SubmitPlanArguments):
    plan: list[ExecutionPlanItem] = Field(max_length=8)
    director_brief: MaterialDirectorBrief
    questions: list[MaterialDirectorQuestion] = Field(max_length=5)


class JsonPlanningDecision(SubmitPlanArguments):
    action: Literal["plan"]


class JsonMaterialPlanningDecision(SubmitMaterialPlanArguments):
    action: Literal["plan"]


class JsonToolDecision(NativeModel):
    action: Literal["tool"]
    tool: FunctionName
    arguments: dict


class JsonCompleteDecision(NativeModel):
    action: Literal["complete"]
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)]


JSON_STEP_ADAPTER = TypeAdapter(Annotated[JsonToolDecision | JsonCompleteDecision, Field(discriminator="action")])


class NativeFunctionDefinition(NativeModel):
    name: FunctionName
    description: str
    parameters: dict
    strict: bool | None = None


class NativeToolDefinition(NativeModel):
    type: Literal["function"]
    function: NativeFunctionDefinition


class NativeFunctionCall(NativeModel):
    name: FunctionName
    arguments: str = Field(min_length=2, max_length=500000)


class NativeToolCall(NativeModel):
    id: CallId
    index: int | None = Field(default=None, ge=0)
    type: Literal["function"]
    function: NativeFunctionCall


class NativeAssistantMessage(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", hide_input_in_errors=True)
    role: Literal["assistant"]
    content: str | None
    tool_calls: list[NativeToolCall] = Field(min_length=1, max_length=1)


class NativeBatchMessage(NativeAssistantMessage):
    tool_calls: list[NativeToolCall] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def unique_call_ids(self):
        identifiers = [call.id for call in self.tool_calls]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("函数调用标识必须唯一")
        return self


def native_batch_ids(message: dict, protocol: str) -> list[str]:
    if protocol != MATERIAL_NATIVE_AGENT_PROTOCOL:
        return []
    try:
        parsed = NativeBatchMessage.model_validate(message, strict=True)
    except ValidationError:
        return []
    return [call.id for call in parsed.tool_calls]


def protocol_kind(protocol: str = DEFAULT_AGENT_PROTOCOL) -> Literal["json", "native"]:
    if protocol not in SUPPORTED_PLATFORM_PROTOCOLS:
        raise AppError(422, "AGENT_NATIVE_PROTOCOL_INVALID", "当前原生角色协议未获支持")
    return "json" if protocol in SUPPORTED_JSON_PROTOCOLS else "native"


def is_json_protocol(protocol: str = DEFAULT_AGENT_PROTOCOL) -> bool:
    return protocol_kind(protocol) == "json"


def provider_endpoint(protocol: str = DEFAULT_AGENT_PROTOCOL, *, structured_response: bool = False) -> str:
    protocol_kind(protocol)
    if structured_response:
        if protocol != MATERIAL_JSON_AGENT_PROTOCOL:
            raise AppError(422, "AGENT_STRUCTURED_OUTPUT_INVALID", "结构化产物请求需要当前素材协议")
        return RESPONSES_ENDPOINT
    return REQUEST_ENDPOINT[protocol]


def resolve_tool_catalog_version(protocol: str = DEFAULT_AGENT_PROTOCOL, catalog_version: str | None = None) -> str:
    kind = protocol_kind(protocol)
    if catalog_version is not None:
        version = catalog_version
    elif protocol == MATERIAL_JSON_AGENT_PROTOCOL:
        version = MATERIAL_SOP_TOOL_CATALOG_VERSION
    elif protocol == MATERIAL_NATIVE_AGENT_PROTOCOL:
        version = MATERIAL_TOOL_CATALOG_VERSION
    else:
        version = TOOL_CATALOG_VERSION if kind == "json" else LEGACY_TOOL_CATALOG_VERSION
    if version not in SUPPORTED_TOOL_CATALOG_VERSIONS:
        raise AppError(422, "AGENT_TOOL_CATALOG_INVALID", "工具目录版本不可用")
    if protocol == MATERIAL_JSON_AGENT_PROTOCOL and version != MATERIAL_SOP_TOOL_CATALOG_VERSION:
        raise AppError(422, "AGENT_TOOL_CATALOG_PROTOCOL_INVALID", "素材JSON协议需要当前素材工具目录")
    if protocol == MATERIAL_NATIVE_AGENT_PROTOCOL and version not in MATERIAL_CATALOG_VERSIONS:
        raise AppError(422, "AGENT_TOOL_CATALOG_PROTOCOL_INVALID", "素材严格协议需要有效的素材工具目录")
    if kind == "native" and protocol != MATERIAL_NATIVE_AGENT_PROTOCOL and version != LEGACY_TOOL_CATALOG_VERSION:
        raise AppError(422, "AGENT_TOOL_CATALOG_PROTOCOL_INVALID", "既有原生协议只支持其冻结的R1工具目录")
    return version


def agent_response_schema(planning: bool, protocol: str = DEFAULT_AGENT_PROTOCOL) -> dict:
    if is_json_protocol(protocol):
        if not planning:
            return JSON_STEP_ADAPTER.json_schema(by_alias=True)
        schema = JsonMaterialPlanningDecision if protocol == MATERIAL_JSON_AGENT_PROTOCOL else JsonPlanningDecision
        return schema.model_json_schema(by_alias=True)
    return (PlanningDecision if planning else StepDecision).model_json_schema(by_alias=True)


_SCHEMA_KEYS = {"$schema", "$defs", "$def", "$ref", "title", "description", "type", "properties", "required", "additionalProperties", "items", "anyOf", "enum", "const", "default", "pattern", "format", "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
_MOVED_LIMITS = {"minLength": "最少字符数", "maxLength": "最多字符数", "minItems": "最少数组项数", "maxItems": "最多数组项数"}
_FORMATS = {"email", "hostname", "ipv4", "ipv6", "uuid"}


class MaterialArtifactPatch(ArtifactPatch):
    value: str | int | float | bool | None | list[str | int | float | bool | None] | list[ArtifactSource]


class MaterialPatchArguments(PatchArtifactArguments):
    patches: list[MaterialArtifactPatch] = Field(min_length=1, max_length=200)


def _strict_node(schema: dict, references: dict[str, str], root: bool = False) -> dict:
    if not isinstance(schema, dict) or not schema or set(schema) - _SCHEMA_KEYS:
        raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "原生严格工具Schema含未支持的结构或关键字")
    if not root and ("$defs" in schema or "$def" in schema):
        raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具仅支持根目录中的共享Schema定义")
    if "$schema" in schema and schema["$schema"] not in {"https://json-schema.org/draft/2020-12/schema", "https://json-schema.org/draft/2020-12/schema#"}:
        raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具Schema声明了未支持的规范版本")
    result = {}
    notes = [schema[key] for key in ("title", "description") if key in schema]
    notes.extend(f"服务端要求{label}：{schema[key]}。" for key, label in _MOVED_LIMITS.items() if key in schema)
    kind = schema.get("type")
    if kind is not None and not isinstance(kind, str):
        raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具类型须为明确的单个类型或anyOf组合")
    if any(key in schema and kind != expected for key, expected in (("minLength", "string"), ("maxLength", "string"), ("minItems", "array"), ("maxItems", "array"))):
        raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具的长度及数组限制必须属于对应业务类型")
    if "const" in schema and "enum" in schema:
        raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具常量与枚举的组合未获支持")
    if "default" in schema:
        if kind in {"number", "integer"} and type(schema["default"]) in {int, float}:
            result["default"] = schema["default"]
        else:
            notes.append("原业务默认值：" + json.dumps(schema["default"], ensure_ascii=False, allow_nan=False) + "；字段必填时填写符合本字段类型的实际值。")
    if notes:
        result["description"] = "\n".join(notes)
    if "$ref" in schema:
        if schema["$ref"] not in references or any(key in schema for key in ("type", "anyOf", "properties", "items", "const", "enum")):
            raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具Schema引用未定义或具有不支持的组合")
        result["$ref"] = references[schema["$ref"]]
    elif "anyOf" in schema:
        if "type" in schema or not isinstance(schema["anyOf"], list) or not schema["anyOf"]:
            raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具anyOf结构无效")
        result["anyOf"] = [_strict_node(item, references) for item in schema["anyOf"]]
    elif kind == "object":
        if not isinstance(schema.get("properties"), dict) or schema.get("additionalProperties") is not False:
            raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具对象必须已有明确字段及禁止额外字段的业务定义")
        result.update(type="object", properties={key: _strict_node(value, references) for key, value in schema["properties"].items()},
                      required=list(schema["properties"]), additionalProperties=False)
    elif kind == "array":
        if not isinstance(schema.get("items"), dict):
            raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具数组必须已有明确的元素Schema")
        result.update(type="array", items=_strict_node(schema["items"], references))
    elif kind in {"string", "number", "integer", "boolean", "null"}:
        result["type"] = kind
    else:
        raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具Schema缺少已支持的明确类型")
    if "enum" in schema:
        result["enum"] = deepcopy(schema["enum"])
    if "const" in schema:
        if kind in {"string", "boolean", "null"}:
            result["enum"] = [schema["const"]]
        elif kind in {"number", "integer"}:
            result["const"] = schema["const"]
        else:
            raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具常量类型未获支持")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        if key in schema:
            if kind not in {"number", "integer"}:
                raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具数值约束必须属于数值类型")
            result[key] = schema[key]
    for key in ("pattern", "format"):
        if key in schema:
            if kind != "string" or (key == "format" and schema[key] not in _FORMATS):
                raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具字符串约束未获支持")
            result[key] = schema[key]
    definition_key = "$defs" if "$defs" in schema else "$def" if "$def" in schema else None
    if definition_key:
        result["$def"] = {key: _strict_node(value, references) for key, value in schema[definition_key].items()}
    Draft202012Validator.check_schema(result)
    return result


def _expand_union_refs(schema: dict, resolver, expanding: frozenset[str] = frozenset()) -> dict:
    result = deepcopy(schema)
    for key in ("properties", "$def"):
        if key in schema:
            result[key] = {name: _expand_union_refs(value, resolver, expanding) for name, value in schema[key].items()}
    if "items" in schema:
        result["items"] = _expand_union_refs(schema["items"], resolver, expanding)
    if "anyOf" in schema:
        branches = []
        for branch in schema["anyOf"]:
            if "$ref" in branch:
                reference = branch["$ref"]
                if reference in expanding or set(branch) - {"$ref", "description"}:
                    raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格联合类型包含未支持的递归引用或附加约束")
                resolved = resolver.lookup(reference)
                if not isinstance(resolved.contents, dict) or not isinstance(resolved.contents.get("type"), str):
                    raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格联合类型引用必须指向明确类型的完整Schema")
                expanded = deepcopy(resolved.contents)
                if "description" in branch:
                    expanded["description"] = "\n".join(value for value in (expanded.get("description"), branch["description"]) if value)
                branches.append(_expand_union_refs(expanded, resolved.resolver, expanding | {reference}))
            else:
                branches.append(_expand_union_refs(branch, resolver, expanding))
        result["anyOf"] = branches
    return result


def strict_schema(schema: dict, protocol: str = NATIVE_AGENT_PROTOCOL) -> dict:
    provider_endpoint(protocol)
    if protocol not in {"native-tools-v2-strict", NATIVE_AGENT_PROTOCOL, MATERIAL_NATIVE_AGENT_PROTOCOL}:
        raise AppError(422, "AGENT_NATIVE_PROTOCOL_INVALID", "原生v1协议保留完整业务Schema，不使用严格Schema适配")
    if protocol == MATERIAL_NATIVE_AGENT_PROTOCOL and schema == PatchArtifactArguments.model_json_schema(by_alias=True):
        schema = MaterialPatchArguments.model_json_schema(by_alias=True)
    try:
        Draft202012Validator.check_schema(schema)
        if "$defs" in schema and "$def" in schema:
            raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具共享定义目录重复")
        definitions = schema.get("$defs", schema.get("$def", {}))
        if not isinstance(definitions, dict):
            raise AppError(422, "AGENT_STRICT_SCHEMA_UNSUPPORTED", "严格工具共享定义目录无效")
        references = {f"#/{folder}/{name}": f"#/$def/{name}" for folder in ("$defs", "$def") for name in definitions}
        adapted = _strict_node(schema, references, True)
        if protocol == "native-tools-v2-strict":
            return adapted
        identifier = "urn:sceneturn:strict-tool-schema"
        resolver = Registry().with_resource(identifier, DRAFT202012.create_resource(adapted)).resolver(identifier)
        expanded = _expand_union_refs(adapted, resolver)
        Draft202012Validator.check_schema(expanded)
        return expanded
    except SchemaError as error:
        raise AppError(422, "AGENT_STRICT_SCHEMA_INVALID", "原生严格工具Schema未通过标准JSON Schema校验") from error
    except Unresolvable as error:
        raise AppError(422, "AGENT_STRICT_SCHEMA_INVALID", "原生严格联合类型的Schema引用无法解析") from error


def agent_business_tools(role: str, planning: bool, catalog_version: str = TOOL_CATALOG_VERSION) -> list[dict]:
    if role not in ROLE_TOOLS:
        raise AppError(422, "AGENT_NATIVE_ROLE_INVALID", "当前原生工具岗位无效")
    allowed_tools(role, catalog_version)
    if planning:
        if catalog_version == MATERIAL_SOP_TOOL_CATALOG_VERSION:
            catalog = [{"name": "submit_plan", "description": "提交总编面向创作者的简报及本轮安排。需要创作者决定时先提交questions，plan为空；需求明确后questions为空，填写必要岗位和前置依赖。", "inputSchema": SubmitMaterialPlanArguments.model_json_schema(by_alias=True)}]
        else:
            catalog = [{"name": "submit_plan", "description": "提交本次任务的唯一完整执行计划；只填写plan，按岗位与交付类型列出必要步骤、kind和真实前置依赖。", "inputSchema": SubmitPlanArguments.model_json_schema(by_alias=True)}]
    else:
        catalog = [*tool_catalog(role, catalog_version), {"name": "complete_step", "description": "当前步骤所需实际写入或工具操作完成并核实后提交完成说明；规划、只读资料或未执行的建议不能作为实际交付。", "inputSchema": CompleteStepArguments.model_json_schema(by_alias=True)}]
    return [{"type": "function", "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["inputSchema"]}} for tool in catalog]


def agent_tools(role: str, planning: bool, protocol: str = DEFAULT_AGENT_PROTOCOL, model: str = MODEL, catalog_version: str | None = None) -> list[dict]:
    provider_endpoint(protocol)
    model_aliases(model)
    version = resolve_tool_catalog_version(protocol, catalog_version)
    if is_json_protocol(protocol):
        if role not in ROLE_TOOLS:
            raise AppError(422, "AGENT_NATIVE_ROLE_INVALID", "当前原生工具岗位无效")
        return []
    tools = agent_business_tools(role, planning, version)
    if protocol == "native-tools-v1":
        return tools
    if protocol == MATERIAL_NATIVE_AGENT_PROTOCOL and version == MATERIAL_SOP_TOOL_CATALOG_VERSION:
        for tool in tools:
            if tool["function"]["name"] == "patch_artifact":
                tool["function"]["parameters"] = PatchArtifactArguments.model_json_schema(by_alias=True)
    return [{"type": "function", "function": {**tool["function"], "parameters": strict_schema(tool["function"]["parameters"], protocol), "strict": True}} for tool in tools]


def agent_request_body(messages: list[dict], max_tokens: int, tools: list[dict], planning: bool, settings: Settings, protocol: str = DEFAULT_AGENT_PROTOCOL, model: str = MODEL, catalog_version: str | None = None, *, structured_response_schema: dict | None = None) -> dict:
    provider_endpoint(protocol, structured_response=structured_response_schema is not None)
    model_aliases(model)
    version = resolve_tool_catalog_version(protocol, catalog_version)
    if type(max_tokens) is not int or max_tokens < 64 or max_tokens > settings.text_max_output_tokens:
        raise AppError(422, "AGENT_NATIVE_TOKEN_LIMIT", "本次原生角色请求的输出Token限制无效")
    if not messages or any(not isinstance(message, dict) for message in messages):
        raise AppError(422, "AGENT_NATIVE_MESSAGES_INVALID", "本次原生角色请求缺少有效消息")
    if is_json_protocol(protocol):
        if tools != []:
            raise AppError(422, "AGENT_JSON_TOOLS_INVALID", "JSON角色协议不发送原生函数定义")
        if not any(isinstance(message.get("content"), str) and "json" in message["content"].lower() for message in messages):
            raise AppError(422, "AGENT_JSON_MESSAGES_INVALID", "JSON角色协议的实际提示词必须明确要求JSON输出")
        if structured_response_schema is not None:
            if planning or model != MODEL or structured_response_schema.get("type") != "object":
                raise AppError(422, "AGENT_STRUCTURED_OUTPUT_INVALID", "结构化产物请求需要当前执行步骤和对象结构")
            if any(message.get("role") not in {"system", "user", "assistant"} or not isinstance(message.get("content"), str) for message in messages):
                raise AppError(422, "AGENT_JSON_MESSAGES_INVALID", "结构化产物请求需要明确的文本消息")
            try:
                Draft202012Validator.check_schema(structured_response_schema)
            except SchemaError as error:
                raise AppError(422, "AGENT_STRUCTURED_OUTPUT_INVALID", "结构化产物定义未通过JSON Schema校验") from error
            return {"model": model, "input": [{"role": message["role"], "content": message["content"]} for message in messages],
                    "reasoning": {"effort": "high"}, "tool_choice": "none", "stream": False, "max_output_tokens": max_tokens,
                    "text": {"format": {"type": "json_schema", "name": "material_artifact", "schema": structured_response_schema}}}
        body = {"model": model, "messages": messages, "thinking": {"type": "disabled"}, "temperature": 0.4,
                "response_format": {"type": "json_object"}, "stream": False, "max_tokens": max_tokens, "tool_choice": "none"}
        if protocol in THINKING_JSON_PROTOCOLS:
            body["thinking"] = {"type": "enabled"}
            del body["temperature"]
            body["reasoning_effort"] = "high"
        return body
    try:
        definitions = [NativeToolDefinition.model_validate(tool, strict=True) for tool in tools]
    except ValidationError as error:
        raise AppError(422, "AGENT_NATIVE_TOOLS_INVALID", "本次原生角色工具定义不符合协议") from error
    names = [tool.function.name for tool in definitions]
    if protocol != "native-tools-v1":
        if any(tool.function.strict is not True or tool.function.parameters != strict_schema(tool.function.parameters, protocol) for tool in definitions):
            raise AppError(422, "AGENT_STRICT_TOOLS_INVALID", "严格协议必须发送全部strict函数及其已适配的完整供应商Schema")
    elif any(tool.function.strict is not None for tool in definitions):
        raise AppError(422, "AGENT_NATIVE_TOOLS_INVALID", "原生v1工具不使用严格Beta标记")
    if not names or len(names) != len(set(names)):
        raise AppError(422, "AGENT_NATIVE_TOOLS_INVALID", "原生角色工具名称必须有效且唯一")
    if planning and names != ["submit_plan"]:
        raise AppError(422, "AGENT_NATIVE_TOOLS_INVALID", "规划阶段只允许submit_plan函数")
    registered = set().union(*MATERIAL_SOP_ROLE_TOOLS.values()) if version == MATERIAL_SOP_TOOL_CATALOG_VERSION else allowed_tools("director", version)
    if not planning and ("complete_step" not in names or not set(names) <= registered | {"complete_step"}):
        raise AppError(422, "AGENT_NATIVE_TOOLS_INVALID", "执行阶段仅允许已注册的项目函数与complete_step")
    return {"model": model, "messages": messages, "thinking": {"type": "disabled"}, "temperature": 0.4, "stream": False,
            "max_tokens": max_tokens, "tools": tools,
            "tool_choice": {"type": "function", "function": {"name": "submit_plan"}} if planning else "required"}


@asynccontextmanager
async def _agent_request_deadline(seconds: float):
    try:
        async with asyncio.timeout(seconds):
            yield
    except TimeoutError as error:
        raise TextProviderFailure("TEXT_SUBMISSION_UNKNOWN", "文本请求总时限已到，费用预留继续保留，请核实原请求，系统不会自动重发", True) from error


async def complete_agent(api_key: str, messages: list[dict], max_tokens: int, tools: list[dict], planning: bool, settings: Settings, protocol: str = DEFAULT_AGENT_PROTOCOL, model: str = MODEL, catalog_version: str | None = None, *, structured_response_schema: dict | None = None) -> Completion:
    if not api_key.strip():
        raise AppError(503, "TEXT_NOT_CONFIGURED", "平台文字配置尚未完成")
    body = agent_request_body(messages, max_tokens, tools, planning, settings, protocol, model, catalog_version,
        structured_response_schema=structured_response_schema)
    async with _agent_request_deadline(settings.text_timeout):
        if structured_response_schema is not None:
            return await complete_structured_request(api_key, body, settings)
        return await complete_chat_request(api_key, body, settings, endpoint=provider_endpoint(protocol))


def json_syntax_details(error: json.JSONDecodeError) -> dict:
    start = max(0, min(error.pos - 80, len(error.doc) - 160))
    return {"loc": [], "type": "json_invalid", "msg": error.msg,
            "lineno": error.lineno, "colno": error.colno, "pos": error.pos,
            "excerptStart": start, "excerptErrorOffset": error.pos - start,
            "excerpt": error.doc[start:start + 160]}


def decode_json_agent_message(message: dict, planning: bool, role: str, catalog_version: str = TOOL_CATALOG_VERSION, *, scene_artifact_only: bool = False, material_artifact_only: bool = False) -> tuple[dict, None]:
    if role not in ROLE_TOOLS:
        raise AppError(422, "AGENT_NATIVE_ROLE_INVALID", "当前角色岗位无效")
    allowed = allowed_tools(role, catalog_version)
    try:
        parsed = Message.model_validate(message, strict=True)
    except ValidationError as error:
        raise AppError(422, "AGENT_JSON_MESSAGE_INVALID", "JSON角色响应缺少有效的供应商助手消息") from error
    if parsed.tool_calls:
        raise AppError(422, "AGENT_JSON_CALL_FORBIDDEN", "JSON角色响应不能同时包含原生函数调用")
    if parsed.content is None or not parsed.content.strip():
        raise AppError(422, "AGENT_JSON_CONTENT_EMPTY", "供应商返回了空白JSON内容，尚未产生有效角色决策")
    try:
        values = json.loads(parsed.content)
        json.dumps(values, allow_nan=False)
    except ValueError as error:
        raise AppError(422, "AGENT_JSON_ARGUMENTS_INVALID", "JSON角色响应未通过标准JSON语法校验") from error
    if scene_artifact_only or material_artifact_only:
        roles = {"scene_designer"} if scene_artifact_only else MATERIAL_STRUCTURED_RECOVERY_ROLES
        if planning or role not in roles or catalog_version != MATERIAL_SOP_TOOL_CATALOG_VERSION:
            raise AppError(422, "AGENT_JSON_FUNCTION_FORBIDDEN", "直接结构化成果仅用于已授权的素材制作岗位")
        values = {"action": "tool", "tool": "write_artifact", "arguments": {"artifact": values}}
    try:
        if planning:
            schema = JsonMaterialPlanningDecision if catalog_version == MATERIAL_SOP_TOOL_CATALOG_VERSION else JsonPlanningDecision
            compact = schema.model_validate(values, strict=True)
            if role != "director" and any(step.role != role for step in compact.plan):
                raise AppError(422, "AGENT_JSON_FUNCTION_FORBIDDEN", "本岗位计划不能安排其他岗位的工作")
            if isinstance(compact, JsonMaterialPlanningDecision):
                decision = MaterialPlanningDecision(action="plan", plan=compact.plan, director_brief=compact.director_brief, questions=compact.questions)
            else:
                decision = PlanningDecision(action="plan", plan=compact.plan)
        else:
            compact = JSON_STEP_ADAPTER.validate_python(values, strict=True)
            if isinstance(compact, JsonCompleteDecision):
                decision = StepDecision(action="complete", summary=compact.summary)
            else:
                if compact.tool not in allowed:
                    raise AppError(422, "AGENT_JSON_FUNCTION_FORBIDDEN", "JSON角色响应包含本岗位未授权的项目工具")
                arguments = tool_schema(compact.tool, catalog_version).model_validate(compact.arguments, strict=True)
                normalized = arguments.model_dump(mode="json", by_alias=True, exclude_unset=True)
                if compact.tool == "write_artifact":
                    if catalog_version == MATERIAL_SOP_TOOL_CATALOG_VERSION:
                        contract = artifact_model(role, reusable_frames=True, material_video_plan=True)
                        normalized["artifact"] = contract.model_validate(arguments.artifact, strict=True).model_dump(mode="json", by_alias=True)
                    else:
                        normalized["artifact"] = validate_artifact(role, arguments.artifact)
                decision = StepDecision(action="tool", tool=compact.tool, arguments=normalized)
    except ValidationError as error:
        raise AppError(422, "AGENT_JSON_ARGUMENTS_INVALID", "JSON角色决策未通过当前阶段、岗位及工具参数的严格结构或语义校验") from error
    return decision.model_dump(mode="json", by_alias=True), None


def decode_platform_message(message: dict, planning: bool, role: str, protocol: str = DEFAULT_AGENT_PROTOCOL, catalog_version: str | None = None, *, scene_artifact_only: bool = False, material_artifact_only: bool = False) -> tuple[dict, str | None]:
    version = resolve_tool_catalog_version(protocol, catalog_version)
    if (scene_artifact_only or material_artifact_only) and protocol != MATERIAL_JSON_AGENT_PROTOCOL:
        raise AppError(422, "AGENT_JSON_FUNCTION_FORBIDDEN", "直接结构化成果只适用于素材JSON响应")
    if is_json_protocol(protocol):
        return decode_json_agent_message(message, planning, role, version, scene_artifact_only=scene_artifact_only,
            material_artifact_only=material_artifact_only)
    return decode_agent_message(message, planning, role, version, protocol=protocol)


def decode_structured_scene_closure(message: dict) -> tuple[dict, str] | None:
    content = message.get("content")
    if not isinstance(content, str):
        return None
    trimmed = content.rstrip()
    if not trimmed.endswith("}]}"):
        return None
    try:
        json.loads(trimmed)
    except json.JSONDecodeError as error:
        if error.pos != len(trimmed) or error.msg != "Expecting ',' delimiter":
            return None
    else:
        return None
    suffix = "]}"
    try:
        decision, _ = decode_platform_message({**message, "content": content + suffix}, False, "scene_designer",
            MATERIAL_JSON_AGENT_PROTOCOL, scene_artifact_only=True)
    except AppError:
        return None
    return decision, suffix


def decode_agent_message(message: dict, planning: bool, role: str, catalog_version: str = LEGACY_TOOL_CATALOG_VERSION, *, protocol: str = "native-tools-v1") -> tuple[dict, str]:
    if role not in ROLE_TOOLS:
        raise AppError(422, "AGENT_NATIVE_ROLE_INVALID", "当前原生工具岗位无效")
    version = resolve_tool_catalog_version(protocol, catalog_version)
    try:
        parsed = NativeAssistantMessage.model_validate(message, strict=True)
    except ValidationError as error:
        if native_batch_ids(message, protocol):
            raise AppError(422, "AGENT_NATIVE_BATCH_NOT_EXECUTED", "本轮多个操作均未执行，请每次只调用一个函数并等待结果") from error
        raise AppError(422, "AGENT_NATIVE_CALL_INVALID", "原生角色响应必须包含唯一有效函数调用及真实调用标识") from error
    call = parsed.tool_calls[0]
    name = call.function.name
    allowed = {"submit_plan"} if planning else allowed_tools(role, version) | {"complete_step"}
    if name not in allowed:
        raise AppError(422, "AGENT_NATIVE_FUNCTION_FORBIDDEN", "原生角色响应调用了当前阶段或岗位未授权的函数")
    try:
        arguments = json.loads(call.function.arguments)
        json.dumps(arguments, allow_nan=False)
    except ValueError as error:
        raise AppError(422, "AGENT_NATIVE_ARGUMENTS_INVALID", "原生角色函数参数必须是完整有效的JSON") from error
    try:
        if planning:
            if version == MATERIAL_SOP_TOOL_CATALOG_VERSION:
                body = SubmitMaterialPlanArguments.model_validate(arguments, strict=True)
            else:
                body = SubmitPlanArguments.model_validate(arguments, strict=True)
            if role != "director" and any(step.role != role for step in body.plan):
                raise AppError(422, "AGENT_NATIVE_FUNCTION_FORBIDDEN", "本岗位计划不能安排其他岗位的工作")
            if isinstance(body, SubmitMaterialPlanArguments):
                decision = MaterialPlanningDecision(action="plan", **body.model_dump())
            else:
                decision = PlanningDecision(action="plan", plan=body.plan)
        elif name == "complete_step":
            body = CompleteStepArguments.model_validate(arguments, strict=True)
            decision = StepDecision(action="complete", summary=body.summary)
        else:
            schema = PatchArtifactArguments if name == "patch_artifact" and version == MATERIAL_SOP_TOOL_CATALOG_VERSION else tool_schema(name, version)
            body = schema.model_validate(arguments, strict=True)
            values = body.model_dump(mode="json", by_alias=True, exclude_unset=True)
            if name == "write_artifact":
                values["artifact"] = validate_artifact(role, body.artifact)
            decision = StepDecision(action="tool", tool=name, arguments=values)
    except ValidationError as error:
        raise AppError(422, "AGENT_NATIVE_ARGUMENTS_INVALID", "原生角色函数参数未通过当前岗位的严格结构或语义校验") from error
    return decision.model_dump(mode="json", by_alias=True), call.id
