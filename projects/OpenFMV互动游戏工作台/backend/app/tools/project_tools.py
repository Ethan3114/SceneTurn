import copy
import hashlib
import time
from typing import Literal

import jsonpatch
from jsonpointer import JsonPointer, JsonPointerException
from pydantic import Field, JsonValue, ValidationError, field_validator
from referencing import Registry
from referencing.jsonschema import DRAFT202012
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun, AgentStep
from app.connection_models import CodexConnection
from app.context_schemas import ContextSource
from app.core.config import Settings
from app.core.errors import AppError
from app.creative_artifacts import ARTIFACT_CONTRACT_VERSION, VideoMotionPrompt, artifact_arguments_schema, artifact_model, image_prompt_items, render_artifact, validate_artifact
from app.models import Asset, Canvas, Generation, Project, ProjectVersion, new_id
from app.schemas import CanvasSnapshot, WireModel
from app.services.context import SourceResolver, retrieve_context, source_scope, write_memory
from app.services.generation import adopt_generation, create_join_generation, generation_view, quote_generation, require_generation
from app.services.image_split import split_image
from app.services.media import resolve_asset_path
from app.services.projects import get_operation, iso_timestamp, lock_write, payload_digest, project_envelope, record_project_version, require_project, require_revision, require_scene, save_operation
from app.services.prompts.agent import ROLE_DUTIES, ROLE_TITLES
from app.tools.story_tools import STORY_DESCRIPTIONS, STORY_SCHEMAS, execute_story_tool
from app.tools.editing_tools import EDITING_DESCRIPTIONS, EDITING_SCHEMAS, execute_editing_tool


class EmptyArguments(WireModel):
    pass


class ContextArguments(WireModel):
    max_chars: int = Field(default=16000, ge=1024, le=24000, description="本次返回正文与来源的字符预算。")
    target_ids: list[str] | None = Field(default=None, max_length=100, description="仅填写画布文字节点ID。省略时读取授权文字目标，空列表读取当前场景与共同祖先文本，显式节点ID读取当前分支允许的指定正文。项目记忆按场景和分支自动返回，读取记忆时省略本参数；记忆ID用于产物sources.referenceId。此参数不扩展写入授权。")


class ScriptArguments(WireModel):
    target_id: str | None = Field(default=None, max_length=100)
    text: str = Field(min_length=1, max_length=20000)
    title: str = Field(default="创作文本", min_length=1, max_length=200)


class ArtifactArguments(WireModel):
    target_id: str | None = Field(default=None, min_length=1, max_length=100)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    artifact: dict


class ReadArtifactArguments(WireModel):
    target_id: str = Field(min_length=1, max_length=100)


class ArtifactPatch(WireModel):
    op: Literal["test", "replace"]
    path: str = Field(max_length=1000, description="RFC6901 JSON Pointer，明确指向已有字段；禁止整个专业产物、实体对象或实体集合替换。")
    value: JsonValue

    @field_validator("value")
    @classmethod
    def finite_json_value(cls, value):
        payload_digest(value)
        return value


class PatchArtifactArguments(ReadArtifactArguments):
    expected_revision: int = Field(ge=1)
    base_artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    patches: list[ArtifactPatch] = Field(min_length=1, max_length=200)


class RemoveImagePromptPatch(WireModel):
    op: Literal["remove"]
    path: str = Field(max_length=1000, pattern=r"^/(characters|scenes)/(0|[1-9][0-9]*)/imagePrompts/(0|[1-9][0-9]*)$",
        description="修订确认指定的成果时，使用read_artifact返回的可移除图片路径。至少保留一张；同一列表按序号从大到小移除。")


class MaterialSopPatchArtifactArguments(PatchArtifactArguments):
    patches: list[ArtifactPatch | RemoveImagePromptPatch] = Field(min_length=1, max_length=200)


TOOL_CATALOG_VERSION = "R2"
WORKSPACE_TOOL_CATALOG_VERSION = "R3"
CONVERSATION_TOOL_CATALOG_VERSION = "R4"
MATERIAL_TOOL_CATALOG_VERSION = "R5"
BOUNDARY_TOOL_CATALOG_VERSION = "R6"
REUSABLE_BOUNDARY_TOOL_CATALOG_VERSION = "R7"
MATERIAL_SOP_TOOL_CATALOG_VERSION = "R8"
REUSABLE_BOUNDARY_CATALOG_VERSIONS = frozenset({REUSABLE_BOUNDARY_TOOL_CATALOG_VERSION, MATERIAL_SOP_TOOL_CATALOG_VERSION})
BOUNDARY_CATALOG_VERSIONS = frozenset({BOUNDARY_TOOL_CATALOG_VERSION, *REUSABLE_BOUNDARY_CATALOG_VERSIONS})
MATERIAL_CATALOG_VERSIONS = frozenset({MATERIAL_TOOL_CATALOG_VERSION, *BOUNDARY_CATALOG_VERSIONS})
LEGACY_TOOL_CATALOG_VERSION = "R1"
SUPPORTED_TOOL_CATALOG_VERSIONS = frozenset({LEGACY_TOOL_CATALOG_VERSION, TOOL_CATALOG_VERSION, WORKSPACE_TOOL_CATALOG_VERSION, CONVERSATION_TOOL_CATALOG_VERSION, *MATERIAL_CATALOG_VERSIONS})


class ArtifactSourceBinding(ContextSource):
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    binding_mode: Literal["live", "input_snapshot"] = "live"


class CanvasOperation(WireModel):
    action: Literal["add_text", "add_config", "add_media", "update_text", "move_node", "connect", "disconnect"]
    node_id: str | None = None
    target_id: str | None = None
    text: str | None = Field(default=None, max_length=20000)
    title: str | None = Field(default=None, max_length=200)
    x: float | None = Field(default=None, allow_inf_nan=False)
    y: float | None = Field(default=None, allow_inf_nan=False)
    generation_mode: Literal["text", "image", "video"] | None = None
    drama_task: Literal["text", "summary", "character", "prop", "scene", "plot", "shot", "clip", "join"] | None = None
    media_type: Literal["image", "video", "audio"] | None = None


class CanvasArguments(WireModel):
    operations: list[CanvasOperation] = Field(min_length=1, max_length=30)


class GenerationArguments(WireModel):
    source_node_id: str
    output_node_id: str
    prompt: str = Field(max_length=20000)
    duration: int = Field(default=5, ge=2, le=30)
    resolution: Literal["480P", "720P", "1080P"] = "720P"
    clip_id: str | None = None


class GenerationId(WireModel):
    generation_id: str = Field(min_length=1, max_length=100)


class BoundaryGenerationArguments(GenerationArguments):
    storyboard_node_id: str = Field(min_length=1, max_length=100)
    shot_key: str = Field(min_length=1, max_length=100)


class MaterialVideoGenerationArguments(WireModel):
    video_artifact_node_id: str = Field(min_length=1, max_length=100, description="当前视频师保存的专业产物节点。")
    shot_key: str = Field(min_length=1, max_length=100, description="该产物中已绑定实际首尾帧的镜头代号。")


class SplitArguments(WireModel):
    source_node_id: str
    rows: int = Field(ge=1, le=12)
    columns: int = Field(ge=1, le=12)
    horizontal_lines: list[float] | None = Field(default=None, max_length=11)
    vertical_lines: list[float] | None = Field(default=None, max_length=11)


class JoinArguments(WireModel):
    source_node_id: str
    output_node_id: str


class MemoryArguments(WireModel):
    kind: Literal["setting", "decision", "story_event", "creation_event", "candidate", "summary"]
    content: str = Field(min_length=1, max_length=12000)
    sources: list[ContextSource] = Field(min_length=1, max_length=50)
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    supersedes_id: str | None = Field(default=None, max_length=100)
    story_time: str | None = Field(default=None, max_length=200)


SCHEMAS = {
    "read_project": EmptyArguments, "retrieve_context": ContextArguments,
    "write_script": ScriptArguments, "write_artifact": ArtifactArguments, "apply_canvas_ops": CanvasArguments,
    "submit_generation": GenerationArguments, "inspect_generation": GenerationId,
    "adopt_asset": GenerationId, "validate_scene": EmptyArguments, "write_memory": MemoryArguments,
    "split_image": SplitArguments, "join_videos": JoinArguments,
    "validate_artifacts": EmptyArguments,
    "read_artifact": ReadArtifactArguments, "patch_artifact": PatchArtifactArguments,
}
DESCRIPTIONS = {
    "read_project": "读取本任务项目、当前场景画布、版本和实际素材；不含凭据。",
    "retrieve_context": "读取当前场景与分支的有效设定、历史和来源；保留条件。",
    "write_script": "修改授权文字节点正文，targetId省略时创建独立文字节点；保留历史。",
    "write_artifact": "严格保存当前角色完整专业结构、完整中文正文及可直接使用的生图提示词；targetId省略时创建文字节点，保留来源、哈希及历史，不调用媒体模型。",
    "apply_canvas_ops": "执行授权范围内的文字、配置节点、位置及直接连接操作；不删除节点。",
    "submit_generation": "创建当前视频模型报价并等待本人确认；本工具不确认或提交收费生成。",
    "inspect_generation": "读取同项目同场景实际生成任务、费用与媒体状态。",
    "adopt_asset": "将本任务生成的已完成真实视频采用至授权片段，保留时间线与旧版本。",
    "validate_scene": "检查已保存场景、引用、实际素材文件及时间范围。",
    "write_memory": "保存带真实来源、版本、场景和分支的项目记忆。",
    "split_image": "将授权的真实图片按行列或切线切片，保存所有原图、切片、引用及历史，无模型费用。",
    "join_videos": "按授权拼接配置的真实连线顺序创建本地FFmpeg任务，保留原片，无模型费用。",
    "validate_artifacts": "只读核对本步骤或明确前置产物的当前内容、哈希、保存版本与实际文件；不创建产物。",
    "read_artifact": "读取授权文字节点的当前有效专业结构或与原正文一致的归档草稿，返回准确字段、版本、基准哈希及引用范围；归档来源尚未验证。",
    "patch_artifact": "对read_artifact读到的本岗位结构逐字段test/replace；保留实体key与未修改内容，完整严格校验来源后重新保存中文正文、提示词、版本与历史。",
}
READ_TOOLS = {"read_project", "retrieve_context", "inspect_generation", "validate_scene", "validate_artifacts", "read_artifact"}
ROLE_TOOLS = {
    "director": set(SCHEMAS),
    "writer": READ_TOOLS | {"write_artifact", "write_script", "write_memory", "apply_canvas_ops"},
    "character_designer": READ_TOOLS | {"write_artifact", "write_script", "write_memory", "apply_canvas_ops"},
    "scene_designer": READ_TOOLS | {"write_artifact", "write_script", "write_memory", "apply_canvas_ops"},
    "storyboarder": READ_TOOLS | {"write_artifact", "write_script", "write_memory", "apply_canvas_ops"},
    "media_producer": READ_TOOLS | {"write_artifact", "submit_generation", "adopt_asset", "apply_canvas_ops"},
    "editor": READ_TOOLS | {"write_artifact", "adopt_asset", "apply_canvas_ops", "write_script", "split_image", "join_videos"},
    "interaction_designer": READ_TOOLS | {"write_artifact", "write_script", "write_memory"},
    "sound_designer": READ_TOOLS | {"write_artifact", "write_script", "write_memory"},
}
for _role_tools in ROLE_TOOLS.values():
    _role_tools.update({"read_artifact", "patch_artifact"})

PROJECT_ROLE_TOOLS = {
    "director": set(STORY_SCHEMAS),
    "writer": {"read_story", "apply_story_ops", "prepare_scene_media", "validate_story"},
    "editor": {"read_story", "edit_timeline", "validate_story"},
    "interaction_designer": {"read_story", "edit_interaction", "validate_story"},
}
SCENE_ROLES = {"writer", "character_designer", "scene_designer", "storyboarder", "media_producer", "sound_designer"}
MATERIAL_ROLES = SCENE_ROLES | {"director"}
MATERIAL_SOP_READ_TOOLS = frozenset({"read_project", "retrieve_context", "read_artifact", "validate_artifacts"})
MATERIAL_ARTIFACT_TOOLS = MATERIAL_SOP_READ_TOOLS | {"write_artifact", "patch_artifact"}
MATERIAL_SOP_ROLE_TOOLS = {
    **{role: MATERIAL_ARTIFACT_TOOLS for role in MATERIAL_ROLES},
    "media_producer": MATERIAL_ARTIFACT_TOOLS | {"submit_generation", "inspect_generation"},
}
SCHEMAS.update(STORY_SCHEMAS)
DESCRIPTIONS.update(STORY_DESCRIPTIONS)
SCHEMAS.update(EDITING_SCHEMAS)
DESCRIPTIONS.update(EDITING_DESCRIPTIONS)
READ_TOOLS.update({"read_story", "validate_story"})


def allowed_tools(role: str, catalog_version: str = TOOL_CATALOG_VERSION) -> set[str]:
    if catalog_version not in SUPPORTED_TOOL_CATALOG_VERSIONS:
        raise AppError(422, "AGENT_TOOL_CATALOG_INVALID", "工具目录版本不可用")
    if catalog_version == CONVERSATION_TOOL_CATALOG_VERSION:
        return set(STORY_SCHEMAS) | set(EDITING_SCHEMAS) if role == "director" else set()
    if catalog_version == MATERIAL_SOP_TOOL_CATALOG_VERSION:
        return set(MATERIAL_SOP_ROLE_TOOLS.get(role, ()))
    if catalog_version in MATERIAL_CATALOG_VERSIONS:
        if role not in MATERIAL_ROLES:
            return set()
        return ROLE_TOOLS[role] - {"adopt_asset", "join_videos"}
    if catalog_version == WORKSPACE_TOOL_CATALOG_VERSION:
        return PROJECT_ROLE_TOOLS.get(role, set())
    return ROLE_TOOLS[role] - ({"read_artifact", "patch_artifact"} if catalog_version == LEGACY_TOOL_CATALOG_VERSION else set())


def tool_catalog(role: str, catalog_version: str = TOOL_CATALOG_VERSION) -> list[dict]:
    explicit_null = catalog_version in MATERIAL_CATALOG_VERSIONS
    descriptions = DESCRIPTIONS
    if catalog_version == MATERIAL_SOP_TOOL_CATALOG_VERSION:
        descriptions = {**DESCRIPTIONS,
            "write_artifact": "保存当前岗位的完整成果，标明上游已采用版本。新建成果时targetId与title填null，服务端保存来源和历史。",
            "submit_generation": "根据当前视频成品方案的节点和镜头代号，固定使用已采用的分镜首尾图与保存的运动要求，准备待本人确认的视频任务。",
        }
    elif explicit_null:
        descriptions = {**DESCRIPTIONS,
            "write_artifact": "严格保存当前角色完整专业结构、完整中文正文及可直接使用的生图提示词；创建文字节点时targetId填写null，保留来源、哈希及历史，不调用媒体模型。",
        }
    return [{"name": name, "description": descriptions[name],
        "inputSchema": artifact_arguments_schema(role, catalog_version == BOUNDARY_TOOL_CATALOG_VERSION, explicit_null,
            catalog_version in REUSABLE_BOUNDARY_CATALOG_VERSIONS, catalog_version == MATERIAL_SOP_TOOL_CATALOG_VERSION) if name == "write_artifact" else tool_schema(name, catalog_version).model_json_schema(by_alias=True)} for name in sorted(allowed_tools(role, catalog_version))]


def tool_schema(name, catalog_version):
    if name == "patch_artifact" and catalog_version == MATERIAL_SOP_TOOL_CATALOG_VERSION:
        return MaterialSopPatchArtifactArguments
    if name == "submit_generation":
        if catalog_version == MATERIAL_SOP_TOOL_CATALOG_VERSION:
            return MaterialVideoGenerationArguments
        if catalog_version in BOUNDARY_CATALOG_VERSIONS:
            return BoundaryGenerationArguments
    return SCHEMAS[name]


def role_catalog(workspace: str | None = None) -> list[dict]:
    reasons = {"character_designer": "角色文字设定可用；真实角色图片在阶段2-3接入",
               "scene_designer": "场景与道具文字设计可用；真实图片在阶段2-3接入",
               "media_producer": "当前支持真实Wan视频报价、查询及采用；图片生成在阶段2-3接入",
               "editor": "真实图片切片与本地视频拼接可用，保留原始素材及历史",
               "interaction_designer": "互动文字方案可用；完整按钮、QTE与分支编辑在阶段2-4验收",
               "sound_designer": "声音文字方案及当前场景资料读取可用，音频模型依据真实配置开放"}
    tools = PROJECT_ROLE_TOOLS if workspace == "project" else MATERIAL_SOP_ROLE_TOOLS if workspace == "materials" else ROLE_TOOLS
    roles = SCENE_ROLES if workspace == "scene" else tools.keys()
    return [{"id": role, "title": title, "description": ROLE_DUTIES[role], "tools": sorted(tools[role]), "available": True,
             "reason": ("直接编辑故事总画布中的场景、时间线和互动对象" if workspace == "project" else reasons.get(role))} for role, title in ROLE_TITLES.items()
            if role in roles]


def require_canvas(session: Session, run: AgentRun, project: Project) -> Canvas:
    require_scene(project, run.scene_id)
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == run.project_id, Canvas.owner_id == run.owner_id, Canvas.scene_id == run.scene_id))
    if canvas is None:
        raise AppError(404, "CANVAS_NOT_FOUND", "任务所属画布不存在")
    return canvas


def authorized(run: AgentRun, node_id: str) -> None:
    if node_id not in {*run.target_ids_json, *run.created_ids_json}:
        raise AppError(403, "AGENT_TARGET_NOT_ALLOWED", "该对象不在本次任务授权范围内")


def check_codex_connection(session: Session, owner_id: str, project_id: str, connection_id: str | None, tool_name: str | None = None) -> CodexConnection:
    connection = session.get(CodexConnection, connection_id) if connection_id else None
    if connection is None:
        raise AppError(403, "AGENT_CONNECTION_MISMATCH", "本机任务需要有效的连接授权")
    session.refresh(connection)
    if connection.owner_id != owner_id or connection.project_id != project_id:
        raise AppError(403, "AGENT_CONNECTION_MISMATCH", "连接不属于本次项目任务")
    if connection.revoked_at is not None or connection.expires_at <= time.time():
        raise AppError(403, "CONNECTION_REVOKED", "连接授权已撤销或到期")
    if tool_name is not None and tool_name not in connection.tools_json:
        raise AppError(403, "TOOL_NOT_ALLOWED", "连接授权不包含该工具")
    return connection


def find_node(canvas: dict, node_id: str) -> dict:
    node = next((item for item in canvas["nodes"] if item["id"] == node_id), None)
    if node is None:
        raise AppError(409, "TARGET_CHANGED", "任务目标已删除，候选及历史继续保留")
    if node["type"] == "agent":
        raise AppError(403, "AGENT_CONTROL_NODE_PROTECTED", "角色任务不能修改Agent控制节点")
    return node


def require_run_action(session: Session, run: AgentRun, expected_revision: int) -> Project:
    project = require_project(session, run.project_id, run.owner_id)
    if run.stop_requested or run.status in {"stopped", "stopping", "failed", "completed", "submission_unknown"}:
        raise AppError(409, "RUN_STOPPED", "当前任务已停止新增操作")
    if run.status in {"waiting_connection", "waiting_confirmation", "conflict"}:
        raise AppError(409, "AGENT_RUN_WAITING", "任务需要恢复或人工确认后继续")
    require_revision(project, expected_revision)
    if run.project_revision != expected_revision:
        raise AppError(409, "REVISION_CONFLICT", "任务读取的项目版本已经变化，请重新读取后恢复")
    return project


def _write_canvas(session: Session, run: AgentRun, project: Project, canvas: Canvas, snapshot: dict) -> None:
    validated = CanvasSnapshot.model_validate(snapshot).model_dump(by_alias=True, exclude_unset=True)
    canvas.data_json, canvas.updated_at = validated, time.time()
    project.revision += 1
    project.updated_at, run.project_revision = time.time(), project.revision
    record_project_version(session, project)


def _write_text(run: AgentRun, data: dict, target_id: str | None, text: str, title: str, *, preserve_title: bool = False) -> list[dict]:
    if not text.strip():
        raise AppError(422, "AGENT_TEXT_EMPTY", "文字节点需要实际正文")
    if target_id:
        authorized(run, target_id)
        node = find_node(data, target_id)
        if node["type"] != "text":
            raise AppError(422, "AGENT_TEXT_TARGET_REQUIRED", "正文操作只允许文字节点")
        metadata = {**node.get("metadata", {}), "content": text}
        if "texts" in metadata:
            if not isinstance(metadata["texts"], list):
                raise AppError(422, "AGENT_TEXT_CANDIDATES_INVALID", "批量文字候选格式无效，请保留已有资料并核对节点")
            candidate = {"id": new_id(), "content": text, "status": "success", "agentRunId": run.id}
            metadata.update(texts=[*metadata["texts"], candidate], primaryTextId=candidate["id"])
        node["metadata"] = metadata
        if not preserve_title:
            node["title"] = title
        return [{"action": "update_text", "nodeId": target_id, "targetId": None}]
    identifier = new_id()
    data["nodes"].append({"id": identifier, "type": "text", "title": title, "metadata": {"content": text},
        "position": {"x": 80 + len(data["nodes"]) * 40, "y": 100}, "width": 340, "height": 260})
    run.created_ids_json = [*run.created_ids_json, identifier]
    return [{"action": "add_text", "nodeId": identifier}]


def _apply_canvas(run: AgentRun, data: dict, operations: list[CanvasOperation]) -> list[dict]:
    results = []
    for operation in operations:
        if operation.action in {"add_text", "add_config", "add_media"}:
            node_id = new_id()
            if operation.action == "add_text":
                if not operation.text or not operation.text.strip():
                    raise AppError(422, "AGENT_TEXT_EMPTY", "新文字节点需要实际正文")
                node = {"id": node_id, "type": "text", "metadata": {"content": operation.text}}
            elif operation.action == "add_config":
                node = {"id": node_id, "type": "config", "metadata": {"generationMode": operation.generation_mode or "text", "composerContent": operation.text or ""}}
                if operation.drama_task:
                    node["metadata"]["dramaTask"] = operation.drama_task
            else:
                node = {"id": node_id, "type": operation.media_type or "video", "metadata": {"status": "idle"}}
            node.update(title=operation.title or "创作内容", position={"x": operation.x if operation.x is not None else 80 + len(data["nodes"]) * 40, "y": operation.y if operation.y is not None else 100}, width=340, height=260)
            data["nodes"].append(node)
            run.created_ids_json = [*run.created_ids_json, node_id]
            results.append({"action": operation.action, "nodeId": node_id})
            continue
        if not operation.node_id:
            raise AppError(422, "AGENT_NODE_REQUIRED", "画布操作缺少目标节点")
        node = find_node(data, operation.node_id)
        if operation.action in {"connect", "disconnect"}:
            if not operation.target_id:
                raise AppError(422, "AGENT_TARGET_REQUIRED", "连接操作需要目标节点")
            find_node(data, operation.target_id)
            authorized(run, operation.target_id)
            matching = [item for item in data["connections"] if item["fromNodeId"] == node["id"] and item["toNodeId"] == operation.target_id]
            if operation.action == "connect" and not matching:
                data["connections"].append({"id": new_id(), "fromNodeId": node["id"], "toNodeId": operation.target_id})
            elif operation.action == "disconnect":
                data["connections"] = [item for item in data["connections"] if item not in matching]
        else:
            authorized(run, node["id"])
            if operation.action == "move_node":
                if operation.x is None or operation.y is None:
                    raise AppError(422, "AGENT_POSITION_REQUIRED", "移动节点需要完整坐标")
                node["position"] = {"x": operation.x, "y": operation.y}
            else:
                if node["type"] != "text" or not operation.text or not operation.text.strip():
                    raise AppError(422, "AGENT_TEXT_TARGET_REQUIRED", "正文操作只允许有效文字节点")
                node["metadata"] = {**node.get("metadata", {}), "content": operation.text}
                if operation.title is not None:
                    node["title"] = operation.title
        results.append({"action": operation.action, "nodeId": node["id"], "targetId": operation.target_id})
    return results


def _connection_snapshot(data: dict, source_id: str, target_id: str) -> dict:
    connections = sorted((item for item in data["connections"] if item["fromNodeId"] == source_id and item["toNodeId"] == target_id), key=lambda item: item["id"])
    return {"fromNodeId": source_id, "toNodeId": target_id, "present": bool(connections), "sha256": payload_digest(connections)}


def artifact_is_current(node: dict) -> bool:
    metadata = node.get("metadata", {})
    artifact = metadata.get("creativeArtifact")
    if not isinstance(artifact, dict) or artifact.get("role") not in ROLE_TOOLS:
        return False
    try:
        normalized = validate_artifact(artifact["role"], artifact)
    except ValidationError:
        return False
    text = render_artifact(normalized)
    candidates = metadata.get("texts")
    if candidates is not None:
        if not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates):
            return False
        primary = metadata.get("primaryTextId") or (candidates[0].get("id") if candidates else None)
        selected = next((item for item in candidates if item.get("id") == primary), {})
        if (selected.get("content") or metadata.get("content")) != text:
            return False
    return (metadata.get("artifactContractVersion") == ARTIFACT_CONTRACT_VERSION
            and metadata.get("content") == text
            and metadata.get("artifactContentSha256") == hashlib.sha256(text.encode()).hexdigest()
            and metadata.get("generationPrompts") == image_prompt_items(normalized))


def _artifact_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _artifact_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _artifact_objects(child)


def _artifact_body_matches(node: dict, artifact: dict) -> bool:
    metadata = node.get("metadata", {})
    text = render_artifact(artifact)
    if metadata.get("content") != text:
        return False
    candidates = metadata.get("texts")
    if candidates is None:
        return True
    if not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates):
        return False
    primary = metadata.get("primaryTextId") or (candidates[0].get("id") if candidates else None)
    selected = next((item for item in candidates if item.get("id") == primary), {})
    return (selected.get("content") or metadata.get("content")) == text


def _patchable_paths(artifact: dict, allow_image_prompt_removal: bool = False) -> list[dict]:
    version = artifact.get("schemaVersion")
    schema = artifact_model(artifact["role"], version == 2, version == 3, version == 2).model_json_schema(by_alias=True)
    resolver = Registry().with_resource("urn:openfmv:artifact", DRAFT202012.create_resource(schema)).resolver("urn:openfmv:artifact")

    def expanded(item: dict) -> dict:
        return expanded(resolver.lookup(item["$ref"]).contents) if "$ref" in item else item

    def scalar(item: dict) -> bool:
        item = expanded(item)
        return all(scalar(part) for part in item["anyOf"]) if "anyOf" in item else item.get("type") in {"string", "integer", "number", "boolean", "null"}

    def visit(value, definition: dict, parts: list[str]):
        definition = expanded(definition)
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"key", "shotKey"} or not parts and key in {"role", "schemaVersion"} or parts and parts[0] == "variables" and key == "name":
                    continue
                yield from visit(child, definition.get("properties", {}).get(key, {}), [*parts, key])
        elif isinstance(value, list):
            item_title = expanded(definition.get("items", {})).get("title")
            source_list = item_title == "ArtifactSource"
            if source_list or scalar(definition.get("items", {})):
                yield {"path": JsonPointer.from_parts(parts).path, "value": copy.deepcopy(value), "sourceList": source_list}
            removable_images = (allow_image_prompt_removal and len(value) > 1 and item_title == "ImagePrompt"
                and len(parts) == 3 and parts[0] in {"characters", "scenes"} and parts[2] == "imagePrompts")
            for index, child in enumerate(value):
                child_path = [*parts, str(index)]
                if removable_images:
                    yield {"path": JsonPointer.from_parts(child_path).path, "removeImagePrompt": True}
                yield from visit(child, definition.get("items", {}), child_path)
        elif parts:
            yield {"path": JsonPointer.from_parts(parts).path, "value": value}

    return list(visit(artifact, schema, []))


def _read_artifact(session: Session, run: AgentRun, project: Project, canvas: Canvas, target_id: str, settings: Settings,
                   step: AgentStep | None = None) -> dict:
    readable = run.limits_json.get("materialContext", {}).get("readableNodeIds", []) if run.limits_json.get("toolCatalogVersion") in MATERIAL_CATALOG_VERSIONS else []
    if target_id not in readable:
        authorized(run, target_id)
    node = find_node(canvas.data_json, target_id)
    if node["type"] != "text":
        raise AppError(422, "AGENT_ARTIFACT_TARGET_REQUIRED", "专业结构读取需要授权文字节点")
    metadata = node.get("metadata", {})
    payload = metadata.get("creativeArtifact")
    if payload is None and metadata.get("artifactDraft") is None and run.limits_json.get("toolCatalogVersion") in MATERIAL_CATALOG_VERSIONS:
        content = metadata.get("content", "")
        return {"kind": "plain_text", "targetId": target_id, "title": node.get("title", "文字素材"), "text": content,
            "revision": project.revision, "contentSha256": hashlib.sha256(content.encode()).hexdigest(), "artifact": None,
            "referenceScope": "plain_text", "professionalAvailable": False, "patchablePaths": [], "sourceKind": "canvas_node",
            "instruction": "这是实际普通文字，可作为canvas_node来源。新专业产物使用write_artifact创建独立节点，targetId填写null；此文字没有可修订的专业字段。"}
    scope = "current"
    if payload is None:
        payload, scope = metadata.get("artifactDraft"), "archive"
    if not isinstance(payload, dict) or payload.get("role") not in ROLE_TOOLS:
        raise AppError(422, "AGENT_ARTIFACT_NOT_AVAILABLE", "该节点没有可读取的专业结构或归档草稿")
    try:
        artifact = validate_artifact(payload["role"], payload)
    except ValidationError as error:
        raise AppError(422, "AGENT_ARTIFACT_INVALID", "专业结构或归档草稿没有通过角色结构校验") from error
    if artifact != payload or not _artifact_body_matches(node, artifact):
        raise AppError(409, "AGENT_ARTIFACT_CHANGED", "专业结构与当前正文已有差异，请保留人工内容并核对后继续")
    if scope == "current":
        _validate_saved_artifact(session, run, project, canvas, node, settings, frozenset({target_id}))
    digest = payload_digest(artifact)
    prompt_removal_review_id = None
    if scope == "current" and step is not None and step.role == artifact["role"] and step.role in {"character_designer", "scene_designer"}:
        from app.services.agent_provider import MATERIAL_JSON_AGENT_PROTOCOL
        limits = run.limits_json
        context = limits.get("materialContext", {})
        review = (run.result_json or {}).get("materialReview", {})
        request = context.get("revisionRequest", {})
        if (limits.get("toolCatalogVersion") == MATERIAL_SOP_TOOL_CATALOG_VERSION and limits.get("workspace") == "materials"
                and limits.get("providerProtocol") == MATERIAL_JSON_AGENT_PROTOCOL and context.get("requireHumanReview")
                and review.get("role") == step.role and review.get("status") == "revision_requested"
                and review.get("id") == request.get("reviewId") and review.get("operationId") == request.get("operationId")
                and review.get("stepId") == request.get("stepId") == step.id
                and review.get("nodeId") == request.get("nodeId") == target_id
                and review.get("artifactHash") == request.get("artifactHash") == digest):
            prompt_removal_review_id = review["id"]
    result = {"artifact": copy.deepcopy(artifact), "artifactSha256": digest, "baseArtifactSha256": digest,
            "revision": project.revision, "targetId": target_id, "referenceScope": scope,
            "sourcesValidated": scope == "current", "artifactContractVersion": ARTIFACT_CONTRACT_VERSION,
            "contentSha256": hashlib.sha256(metadata["content"].encode()).hexdigest(),
            "patchablePaths": _patchable_paths(artifact, prompt_removal_review_id is not None)}
    if prompt_removal_review_id is not None:
        result["imagePromptRemovalReviewId"] = prompt_removal_review_id
    if (scope == "current" and run.limits_json.get("toolCatalogVersion") == MATERIAL_SOP_TOOL_CATALOG_VERSION
            and artifact["role"] == "storyboarder" and artifact["schemaVersion"] in {2, 3}):
        from app.services.video_boundaries import approved_video_segments
        result["approvedVideoSegments"] = approved_video_segments(session, run, target_id, artifact)
    return result


def _patch_artifact(session: Session, run: AgentRun, step: AgentStep | None, project: Project, canvas: Canvas, body: PatchArtifactArguments, role: str, settings: Settings) -> tuple[dict, dict]:
    authorized(run, body.target_id)
    require_revision(project, body.expected_revision)
    baseline = _read_artifact(session, run, project, canvas, body.target_id, settings, step)
    artifact = baseline["artifact"]
    if artifact is None:
        raise AppError(422, "AGENT_ARTIFACT_TARGET_REQUIRED", "普通文字没有专业字段，请保留原文并创建独立专业产物")
    if artifact["role"] != role:
        raise AppError(403, "AGENT_ARTIFACT_ROLE_MISMATCH", "专业修订只能修改当前岗位的结构")
    if baseline["baseArtifactSha256"] != body.base_artifact_sha256:
        raise AppError(409, "AGENT_ARTIFACT_BASE_CONFLICT", "专业结构基准已有变化，请重新读取后修订")
    if step is None or not any(receipt["revision"] == project.revision and (
            receipt["tool"] == "read_artifact" and receipt["result"].get("targetId") == body.target_id
            and receipt["result"].get("baseArtifactSha256") == body.base_artifact_sha256
            or run.limits_json.get("toolCatalogVersion") in MATERIAL_CATALOG_VERSIONS and receipt["tool"] == "patch_artifact"
            and receipt["result"].get("persisted") is True and receipt["result"].get("sourceNodeId") == body.target_id
            and receipt["result"].get("artifactSha256") == body.base_artifact_sha256 and receipt["result"].get("artifact") == artifact)
            for receipt in step.tool_results_json):
        raise AppError(409, "AGENT_ARTIFACT_READ_REQUIRED", "字段修订需要先读取当前版本的完整专业结构")
    allowed = {item["path"]: item for item in baseline["patchablePaths"]}
    operations = [item.model_dump(by_alias=True) for item in body.patches]
    removal_paths = [item["path"] for item in operations if item["op"] == "remove"]
    if removal_paths:
        review_id = baseline.get("imagePromptRemovalReviewId")
        if review_id is None:
            raise AppError(422, "AGENT_ARTIFACT_PATCH_FORBIDDEN", "仅可在当前成果的修订确认中移除图片。")
        removals = {}
        for path in removal_paths:
            if not allowed.get(path, {}).get("removeImagePrompt"):
                raise AppError(422, "AGENT_IMAGE_PROMPT_SELECTION_INVALID", "请选择当前读取结果中可以移除的图片序号。")
            parts = JsonPointer(path).parts
            parent, index = JsonPointer.from_parts(parts[:-1]).path, parts[-1]
            removals.setdefault(parent, []).append(int(index))
        confirmed_reads = (receipt["result"] for receipt in step.tool_results_json
            if receipt["tool"] == "read_artifact" and receipt["revision"] == project.revision
            and receipt["result"].get("targetId") == body.target_id
            and receipt["result"].get("baseArtifactSha256") == body.base_artifact_sha256)
        if not any(result.get("imagePromptRemovalReviewId") == review_id
                or result.get("imagePromptSubsetReviewId") == review_id and removals.keys() <= {
                    item["path"] for item in result["patchablePaths"] if item.get("imagePromptSubset")}
                for result in confirmed_reads):
            raise AppError(409, "AGENT_ARTIFACT_READ_REQUIRED", "减少图片前，请读取当前确认所对应的专业结构。")
        if any(indexes != sorted(set(indexes), reverse=True) or len(indexes) >= len(JsonPointer(parent).resolve(artifact))
                for parent, indexes in removals.items()):
            raise AppError(422, "AGENT_IMAGE_PROMPT_SELECTION_INVALID", "同一列表按序号从大到小移除，不可重复，并且至少保留一张。")
        unchanged = (*removals, "/sources")
        if any(item["op"] == "replace" and any(item["path"] == path or item["path"].startswith(path + "/") for path in unchanged)
                for item in operations):
            raise AppError(422, "AGENT_IMAGE_PROMPT_SELECTION_INVALID", "移除图片时请保持保留图片的完整要求和原有来源。")
    current = copy.deepcopy(artifact)
    changed = []
    for operation in operations:
        try:
            pointer = JsonPointer(operation["path"])
            old = pointer.resolve(current)
            if operation["op"] == "replace":
                value = operation["value"]
                if (pointer.path not in allowed or allowed[pointer.path].get("removeImagePrompt") or isinstance(value, dict)
                        or isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value) and not allowed[pointer.path].get("sourceList")):
                    raise AppError(422, "AGENT_ARTIFACT_PATCH_FORBIDDEN", "字段修订仅允许具体标量、标量列表及完整来源列表，实体与身份保持原值")
                if payload_digest(old) != payload_digest(value):
                    changed.append(pointer.path)
            elif operation["op"] == "remove":
                changed.append(JsonPointer.from_parts(pointer.parts[:-1]).path)
            current = jsonpatch.JsonPatch([operation]).apply(current, in_place=False)
        except jsonpatch.JsonPatchTestFailed as error:
            raise AppError(409, "AGENT_ARTIFACT_PATCH_TEST_FAILED", "字段原值检查失败，请重新读取后修订") from error
        except (jsonpatch.JsonPatchException, JsonPointerException) as error:
            raise AppError(422, "AGENT_ARTIFACT_PATCH_INVALID", f"修订路径{operation['path']}不存在或操作无效。仅可使用read_artifact返回的patchablePaths；扩展标量数组时，请replace已允许的完整数组路径并提供完整数组。") from error
    try:
        patched = validate_artifact(role, current)
    except ValidationError as error:
        raise AppError(422, "AGENT_ARTIFACT_INVALID", "修订后的完整产物没有通过角色结构校验") from error
    changed = [path for path in dict.fromkeys(changed) if payload_digest(JsonPointer(path).resolve(artifact)) != payload_digest(JsonPointer(path).resolve(patched))]
    if not changed or payload_digest(patched) == body.base_artifact_sha256:
        raise AppError(422, "AGENT_ARTIFACT_PATCH_NO_CHANGE", "字段修订需要至少一项实际内容变化")
    return patched, {"baseArtifact": artifact, "baseArtifactSha256": body.base_artifact_sha256,
                     "patches": operations, "changedPaths": list(dict.fromkeys(changed)), "referenceScope": baseline["referenceScope"]}


def _validate_artifact_sources(session: Session, run: AgentRun, project: Project, canvas: Canvas, artifact: dict, settings: Settings, seen: frozenset[str] = frozenset(), input_snapshot_ids: frozenset[str] = frozenset()) -> list[dict]:
    scope = source_scope(session, project.id, run.owner_id, run.scene_id, run.branch_id)
    memory_ids = set(scope["memoryIds"])
    retrieved_nodes = set()
    retrieved_memories = set()
    for receipts in session.scalars(select(AgentStep.tool_results_json).where(AgentStep.run_id == run.id)).all():
        for receipt in receipts:
            if receipt["tool"] == "retrieve_context":
                retrieved_nodes.update(source["id"] for source in receipt["result"].get("sources", []) if source["kind"] == "canvas_node")
                retrieved_memories.update(source["id"] for source in receipt["result"].get("sources", []) if source["kind"] == "memory")
            if receipt["tool"] == "read_artifact" and run.limits_json.get("toolCatalogVersion") in MATERIAL_CATALOG_VERSIONS:
                retrieved_nodes.add(receipt["result"]["targetId"])
    allowed_scenes = set(scope["allowedSceneIds"])
    canvases = session.scalars(select(Canvas).where(Canvas.owner_id == run.owner_id, Canvas.project_id == project.id, Canvas.scene_id.in_(allowed_scenes))).all()
    nodes = {node["id"]: (item.scene_id, node) for item in canvases for node in item.data_json["nodes"] if node["type"] != "agent"}
    current_ids = {node["id"] for node in canvas.data_json["nodes"] if node["type"] != "agent"}
    scene_ids = {scene["id"] for scene in project.data_json["graphData"]["nodes"] if scene["type"] == "scene" and scene["id"] in allowed_scenes}
    resolver = SourceResolver(session, project)
    bindings = {}

    def require_asset(identifier: str) -> Asset:
        source_validation = seen and run.limits_json.get("toolCatalogVersion") == MATERIAL_SOP_TOOL_CATALOG_VERSION
        if run.limits_json.get("materialContext", {}).get("scope") == "current_round" and not source_validation:
            from app.services.material_references import current_round_asset_ids, frozen_reference_assets
            frozen_reference_assets(session, run, settings)
            if identifier not in current_round_asset_ids(session, run, canvas):
                raise AppError(422, "AGENT_ARTIFACT_SOURCE_INVALID", "专业产物只能引用本轮明确选择或已生成的素材")
        asset = session.get(Asset, identifier)
        if asset is None or asset.owner_id != run.owner_id or asset.project_id != project.id:
            raise AppError(422, "AGENT_ARTIFACT_SOURCE_INVALID", "专业产物引用的素材不属于当前用户与项目")
        if not resolve_asset_path(settings, asset.relpath).is_file():
            raise AppError(409, "AGENT_ARTIFACT_SOURCE_MISSING", "专业产物引用的已有素材文件不可用")
        return asset

    for item in _artifact_objects(artifact):
        if "referenceId" in item and "description" in item:
            kind, identifier = item["kind"], item["referenceId"]
            if kind == "project":
                valid = identifier == project.id
            elif kind == "scene":
                valid = identifier in scene_ids
            elif kind in {"canvas_node", "artifact"}:
                valid = identifier in nodes and (identifier in current_ids or identifier in retrieved_nodes or bool(seen))
                if valid and kind == "artifact" and identifier not in input_snapshot_ids:
                    if identifier in seen:
                        raise AppError(422, "AGENT_ARTIFACT_SOURCE_CYCLE", "专业产物来源存在循环引用")
                    source_node = nodes[identifier][1]
                    valid = artifact_is_current(source_node)
                    if valid:
                        _validate_saved_artifact(session, run, project, canvas, source_node, settings, seen | {identifier})
            elif kind == "memory":
                valid = identifier in memory_ids and (identifier in retrieved_memories or bool(seen))
            elif kind == "asset":
                require_asset(identifier)
                valid = True
            else:
                valid = kind == "user_requirement"
            if not valid:
                raise AppError(422, "AGENT_ARTIFACT_SOURCE_INVALID", "专业产物来源不存在、已失效或超出本次项目与分支允许范围")
            if kind != "user_requirement":
                reference = {"kind": "canvas_node" if kind == "artifact" else kind, "id": identifier, "revision": project.revision}
                if kind in {"canvas_node", "artifact"}:
                    reference["sceneId"] = nodes[identifier][0]
                resolved = resolver.resolve(reference, current=True)
                if resolved is None:
                    raise AppError(422, "AGENT_ARTIFACT_SOURCE_INVALID", "专业产物来源未能从当前项目版本读取")
                bindings[(kind, identifier)] = {**reference, "digest": payload_digest(resolved), "bindingMode": "live"}
        if "availability" in item and "assetId" in item:
            if item["availability"] == "existing":
                asset = require_asset(item["assetId"])
                if asset.mime_type.split("/", 1)[0] != item["kind"]:
                    raise AppError(422, "AGENT_ARTIFACT_SOURCE_INVALID", "已有素材的实际类型与专业产物要求不一致")
            elif item["assetId"] is not None:
                raise AppError(422, "AGENT_ARTIFACT_SOURCE_INVALID", "待制作或待上传素材不能填写实际素材标识")
    return list(bindings.values())


def _validate_saved_artifact(session: Session, run: AgentRun, project: Project, canvas: Canvas, node: dict, settings: Settings, seen: frozenset[str] = frozenset()) -> None:
    if not artifact_is_current(node):
        raise AppError(409, "AGENT_ARTIFACT_CHANGED", "专业产物正文、结构、完整提示词或交付版本已有变化")
    metadata = node["metadata"]
    saved_bindings = metadata.get("artifactSources")
    try:
        saved_bindings = [ArtifactSourceBinding.model_validate(item).model_dump(by_alias=True, exclude_none=True) for item in saved_bindings] if isinstance(saved_bindings, list) else None
    except ValidationError as error:
        raise AppError(409, "AGENT_ARTIFACT_SOURCE_CHANGED", "专业产物来源版本记录没有通过结构校验") from error
    snapshots = frozenset(item["id"] for item in saved_bindings or [] if item["bindingMode"] == "input_snapshot")
    current_bindings = _validate_artifact_sources(session, run, project, canvas, metadata["creativeArtifact"], settings, seen, snapshots)
    identity = lambda binding: (binding["kind"], binding["id"], binding.get("sceneId"))
    if not isinstance(saved_bindings, list) or {identity(item) for item in saved_bindings} != {identity(item) for item in current_bindings}:
        raise AppError(409, "AGENT_ARTIFACT_SOURCE_CHANGED", "专业产物缺少完整的实际来源版本记录")
    resolver = SourceResolver(session, project)
    for binding in saved_bindings:
        if binding["bindingMode"] == "input_snapshot":
            if binding["kind"] != "canvas_node" or binding["id"] != node["id"]:
                raise AppError(409, "AGENT_ARTIFACT_SOURCE_CHANGED", "专业产物输入快照只能引用本次被修订节点的原版本")
            reference = {key: binding[key] for key in ("kind", "id", "revision", "sceneId") if key in binding}
            current = resolver.resolve(reference, current=True)
            original = resolver.resolve(reference)
            valid = current is not None and original is not None and payload_digest(original) == binding["digest"]
        else:
            valid = resolver.source_status(binding)[0]
        if not valid:
            raise AppError(409, "AGENT_ARTIFACT_SOURCE_CHANGED", "专业产物引用的原始资料已有新版本或已失效")


def validate_artifacts(session: Session, run: AgentRun, step: AgentStep, project: Project, canvas: Canvas, settings: Settings) -> dict:
    plan = run.plan_json[step.index]
    kind = plan.get("kind", "produce")
    indexes = plan.get("dependsOn", []) if kind == "verify" else [step.index]
    if kind == "verify" and (not indexes or any(type(index) is not int or index < 0 or index >= step.index for index in indexes)):
        raise AppError(422, "AGENT_VERIFICATION_DEPENDENCY_INVALID", "验证步骤必须声明前面的产物步骤")
    sources = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.index.in_(indexes)).order_by(AgentStep.index)).all()
    if len(sources) != len(set(indexes)) or kind == "verify" and any(item.status != "completed" for item in sources):
        raise AppError(409, "AGENT_DEPENDENCY_NOT_READY", "前置产物步骤尚未实际完成")
    artifacts: dict[tuple[str, str], dict] = {}
    for source in sources:
        for receipt in source.tool_results_json:
            result = receipt["result"]
            common = {"sourceStepId": source.id, "operationId": receipt["operationId"], "sourceRevision": receipt["revision"]}
            for saved in result.get("savedTexts", []):
                artifacts[("text", saved["nodeId"])] = {**common, "kind": "text", "id": saved["nodeId"], "sha256": saved["sha256"]}
            if receipt["tool"] in {"write_artifact", "patch_artifact"}:
                identifier = result["sourceNodeId"]
                artifacts[("creative_artifact", identifier)] = {**common, "kind": "creative_artifact", "id": identifier,
                    "sha256": result["artifactSha256"], "contractVersion": result["artifactContractVersion"]}
            if receipt["tool"] == "write_script" and not result.get("savedTexts"):
                for operation in result.get("operations", []):
                    text = receipt["arguments"]["text"]
                    artifacts[("text", operation["nodeId"])] = {**common, "kind": "text", "id": operation["nodeId"], "sha256": hashlib.sha256(text.encode()).hexdigest()}
            for saved in result.get("nodeSnapshots", []):
                if ("text", saved["nodeId"]) not in artifacts:
                    artifacts[("node", saved["nodeId"])] = {**common, "kind": "node", "id": saved["nodeId"], "sha256": saved["sha256"]}
            connection_snapshots = {(item["fromNodeId"], item["toNodeId"]): item for item in result.get("connectionSnapshots", [])}
            for operation in result.get("operations", []):
                if operation["action"] not in {"connect", "disconnect"}:
                    continue
                endpoints = {"fromNodeId": operation["nodeId"], "toNodeId": operation["targetId"]}
                identifier = payload_digest(endpoints)
                saved = connection_snapshots.get((operation["nodeId"], operation["targetId"]))
                artifacts[("connection", identifier)] = {**common, "kind": "connection", "id": identifier, **endpoints,
                    "present": operation["action"] == "connect", **({"sha256": saved["sha256"]} if saved else {})}
            for piece in result.get("pieces", []):
                artifacts[("asset", piece["assetId"])] = {**common, "kind": "asset", "id": piece["assetId"], "nodeId": piece["nodeId"]}
            if receipt["tool"] == "write_memory" and result.get("memory"):
                memory = result["memory"]
                artifacts[("memory", memory["id"])] = {**common, "kind": "memory", "id": memory["id"], "sha256": hashlib.sha256(memory["content"].encode()).hexdigest()}
            if receipt["tool"] in {"join_videos", "submit_generation", "adopt_asset"}:
                generation = result.get("generation")
                identifier = generation["id"] if generation else receipt["arguments"].get("generationId")
                if identifier:
                    artifacts[("generation", identifier)] = {**common, "kind": "generation", "id": identifier}
    if not artifacts:
        raise AppError(409, "AGENT_ARTIFACT_MISSING", "没有可核验的真实前置产物，无法登记验证完成")
    proofs = []
    resolver = SourceResolver(session, project)
    for artifact in artifacts.values():
        history = session.scalar(select(ProjectVersion).where(ProjectVersion.project_id == project.id, ProjectVersion.owner_id == run.owner_id, ProjectVersion.revision == artifact["sourceRevision"]))
        if history is None:
            raise AppError(409, "AGENT_ARTIFACT_HISTORY_MISSING", "产物缺少已保存的项目版本")
        proof = {**artifact, "verifiedRevision": project.revision}
        if artifact["kind"] in {"text", "node", "creative_artifact"}:
            authorized(run, artifact["id"])
            current = find_node(canvas.data_json, artifact["id"])
            original_canvas = next((item for item in history.canvases_json if item["sceneId"] == run.scene_id), None)
            original = find_node(original_canvas, artifact["id"]) if original_canvas else None
            if original is None:
                raise AppError(409, "AGENT_ARTIFACT_HISTORY_MISSING", "产物未出现在原保存版本中")
            if artifact["kind"] == "creative_artifact":
                if not artifact_is_current(current) or not artifact_is_current(original):
                    raise AppError(409, "AGENT_ARTIFACT_CHANGED", "专业产物正文、结构、完整提示词或交付版本已有变化")
                current_hash = payload_digest(current["metadata"]["creativeArtifact"])
                original_hash = payload_digest(original["metadata"]["creativeArtifact"])
                if current["metadata"]["artifactContractVersion"] != artifact["contractVersion"]:
                    raise AppError(409, "AGENT_ARTIFACT_CHANGED", "专业产物交付版本与保存回执不一致")
                if current["metadata"].get("artifactSources") != original["metadata"].get("artifactSources"):
                    raise AppError(409, "AGENT_ARTIFACT_SOURCE_CHANGED", "专业产物实际来源版本记录与原保存版本不一致")
                _validate_saved_artifact(session, run, project, canvas, current, settings, frozenset({current["id"]}))
            else:
                current_hash = hashlib.sha256(current.get("metadata", {}).get("content", "").encode()).hexdigest() if artifact["kind"] == "text" else payload_digest(current)
                original_hash = hashlib.sha256(original.get("metadata", {}).get("content", "").encode()).hexdigest() if artifact["kind"] == "text" else payload_digest(original)
            if current_hash != artifact["sha256"] or original_hash != artifact["sha256"]:
                raise AppError(409, "AGENT_ARTIFACT_CHANGED", "前置产物已有新编辑，保留当前内容并等待重新核对")
            proof["sha256"] = current_hash
        elif artifact["kind"] == "connection":
            authorized(run, artifact["toNodeId"])
            original_canvas = next((item for item in history.canvases_json if item["sceneId"] == run.scene_id), None)
            if original_canvas is None:
                raise AppError(409, "AGENT_ARTIFACT_HISTORY_MISSING", "连接产物未出现在原保存版本中")
            original = _connection_snapshot(original_canvas, artifact["fromNodeId"], artifact["toNodeId"])
            current = _connection_snapshot(canvas.data_json, artifact["fromNodeId"], artifact["toNodeId"])
            if original["present"] != artifact["present"] or original["sha256"] != artifact.get("sha256", original["sha256"]) or current != original:
                raise AppError(409, "AGENT_ARTIFACT_CHANGED", "连接产物已有新编辑，保留当前连接并等待重新核对")
            proof["sha256"] = original["sha256"]
        elif artifact["kind"] == "memory":
            memory = resolver.memory(artifact["id"])
            if memory is None or not resolver.validity(memory)[0] or hashlib.sha256(memory.content.encode()).hexdigest() != artifact["sha256"]:
                raise AppError(409, "AGENT_ARTIFACT_CHANGED", "前置记忆或其来源已有变化")
        else:
            if artifact["kind"] == "generation":
                generation = require_generation(session, artifact["id"], run.owner_id)
                if generation.project_id != run.project_id or generation.scene_id != run.scene_id or generation.status != "ready" or not generation.asset_id:
                    raise AppError(409, "AGENT_ARTIFACT_NOT_READY", "媒体产物尚未可用，完成处理后可恢复任务")
                asset_id = generation.asset_id
            else:
                authorized(run, artifact["nodeId"])
                current = find_node(canvas.data_json, artifact["nodeId"])
                if current.get("metadata", {}).get("storageKey") != artifact["id"]:
                    raise AppError(409, "AGENT_ARTIFACT_CHANGED", "切片节点的素材引用已有变化")
                asset_id = artifact["id"]
            asset = session.get(Asset, asset_id)
            if asset is None or asset.owner_id != run.owner_id or asset.project_id != run.project_id:
                raise AppError(404, "ASSET_NOT_FOUND", "实际产物素材不存在")
            path = resolve_asset_path(settings, asset.relpath)
            with path.open("rb") as content:
                actual_hash = hashlib.file_digest(content, "sha256").hexdigest()
            if actual_hash != asset.sha256 or path.stat().st_size != asset.bytes:
                raise AppError(409, "AGENT_ARTIFACT_CHANGED", "实际媒体文件与保存记录不一致")
            proof.update(assetId=asset.id, sha256=actual_hash, bytes=asset.bytes)
        proofs.append(proof)
    return {"valid": True, "artifacts": proofs, "dependsOn": indexes, "sceneId": run.scene_id, "verifiedRevision": project.revision, "readOnly": True}


def tool_arguments(body: WireModel, original: dict | None = None) -> dict:
    values = body.model_dump(by_alias=True)
    if isinstance(body, MemoryArguments):
        values["sources"] = copy.deepcopy(original["sources"]) if original is not None else [source.model_dump(by_alias=True, exclude_unset=True) for source in body.sources]
    return values


def _material_video_boundary(session: Session, run: AgentRun, step: AgentStep, shot: dict, settings: Settings) -> dict:
    from app.services.video_boundaries import freeze_adopted_boundaries

    inputs = run.limits_json["materialContext"]["handoffs"][step.id]["inputs"]
    source = next((item for item in inputs if item["role"] == "storyboarder" and item["nodeId"] == shot["storyboardNodeId"]), None)
    if source is None:
        raise AppError(422, "VIDEO_STORYBOARD_NOT_HANDED_OFF", "视频只能使用交接给当前岗位的已采用分镜")
    authorized(run, shot["storyboardNodeId"])
    boundary = freeze_adopted_boundaries(session, run, shot["storyboardNodeId"], shot["shotKey"], settings)
    if boundary["artifactHash"] != source["artifactHash"] or boundary["runId"] != source["runId"]:
        raise AppError(409, "AGENT_ARTIFACT_SOURCE_CHANGED", "视频所引用的已采用分镜已经变化")
    if [frame["assetId"] for frame in boundary["frames"]] != [shot["firstFrameAssetId"], shot["lastFrameAssetId"]]:
        raise AppError(422, "VIDEO_FRAMES_MISMATCH", "视频方案必须逐一使用已采用的真实首尾图片")
    if shot["videoPrompt"]["durationSeconds"] != boundary["duration"]:
        raise AppError(422, "VIDEO_DURATION_MISMATCH", "视频时长必须与已确认分镜片段一致")
    return boundary


def _quote_material_video(session: Session, run: AgentRun, step: AgentStep, project: Project, canvas: Canvas,
                          body: MaterialVideoGenerationArguments, operation_id: str, settings: Settings) -> Generation:
    authorized(run, body.video_artifact_node_id)
    node = find_node(canvas.data_json, body.video_artifact_node_id)
    artifact = node.get("metadata", {}).get("creativeArtifact")
    if not artifact or artifact.get("role") != "media_producer" or artifact.get("schemaVersion") != 2:
        raise AppError(422, "VIDEO_ARTIFACT_REQUIRED", "请先保存绑定实际首尾图的视频制作方案")
    digest = payload_digest(artifact)
    review = (run.result_json or {}).get("materialReview", {})
    if (review.get("stepId") != step.id or review.get("nodeId") != node["id"] or review.get("artifactHash") != digest
            or review.get("status") not in {"approved", "not_required"}):
        raise AppError(409, "VIDEO_ARTIFACT_UNCONFIRMED", "请先完成当前视频方案的核对事项")
    shot = next((item for item in artifact["shots"] if item["shotKey"] == body.shot_key), None)
    if shot is None:
        raise AppError(422, "VIDEO_SEGMENT_INVALID", "视频片段不属于当前制作方案")
    _validate_saved_artifact(session, run, project, canvas, node, settings, frozenset({node["id"]}))
    boundary = _material_video_boundary(session, run, step, shot, settings)
    previous = session.scalar(select(Generation).where(
        Generation.owner_id == run.owner_id, Generation.project_id == run.project_id, Generation.scene_id == run.scene_id,
        Generation.input_json["request"]["videoArtifactNodeId"].as_string() == node["id"],
        Generation.input_json["request"]["videoArtifactHash"].as_string() == digest,
        Generation.input_json["boundaryFrames"]["shotKey"].as_string() == body.shot_key,
    ).order_by(Generation.created_at.desc(), Generation.id.desc()).limit(1))
    if (previous is not None and previous.status not in {"failed", "cancelled"}
            and (previous.status != "awaiting_confirmation" or previous.project_revision == project.revision)):
        return previous
    prompt = VideoMotionPrompt.model_validate(shot["videoPrompt"], strict=True)
    prompt_text = prompt.generation_text()
    title = f"{artifact['title']} · {body.shot_key}"[:200]
    data = copy.deepcopy(canvas.data_json)
    source, output = _apply_canvas(run, data, [
        CanvasOperation(action="add_config", generation_mode="video", drama_task="clip", title=title, text=prompt_text),
        CanvasOperation(action="add_media", media_type="video", title=title),
    ])
    find_node(data, source["nodeId"])["metadata"]["materialVideoInput"] = True
    _write_canvas(session, run, project, canvas, data)
    payload = {"sourceNodeId": source["nodeId"], "outputNodeId": output["nodeId"], "prompt": prompt_text,
        "duration": prompt.duration_seconds, "resolution": prompt.resolution, "sceneId": run.scene_id, "operationId": operation_id,
        "boundaryFrames": boundary, "videoArtifactNodeId": node["id"], "videoArtifactHash": digest}
    return quote_generation(session, run.project_id, run.owner_id, payload, settings, manage_transaction=False)


def execute_tool(session: Session, run: AgentRun, role: str, tool_name: str, arguments: dict, operation_id: str, expected_revision: int, settings: Settings, step: AgentStep | None = None, *, connection_id: str | None = None, manage_transaction: bool = True, execution_origin: Literal["model", "runtime"] = "model") -> dict:
    if tool_name not in allowed_tools(role, run.limits_json.get("toolCatalogVersion", LEGACY_TOOL_CATALOG_VERSION)):
        raise AppError(403, "TOOL_NOT_ALLOWED", "当前角色没有调用该工具的权限")
    try:
        body = tool_schema(tool_name, run.limits_json.get("toolCatalogVersion")).model_validate(arguments)
    except ValidationError as error:
        raise AppError(422, "AGENT_TOOL_ARGUMENTS_INVALID", "工具参数没有通过结构校验") from error
    payload = {"runId": run.id, "role": role, "tool": tool_name, "arguments": tool_arguments(body, arguments), "expectedRevision": expected_revision}
    if execution_origin != "model":
        payload["origin"] = execution_origin
    if manage_transaction:
        lock_write(session)
    session.refresh(run)
    if run.executor == "codex":
        check_codex_connection(session, run.owner_id, run.project_id, connection_id, tool_name)
        if run.lease_owner != connection_id or not run.lease_expires_at or run.lease_expires_at <= time.time():
            raise AppError(403, "AGENT_CONNECTION_MISMATCH", "本机连接没有当前任务执行权")
    replay = get_operation(session, run.owner_id, run.project_id, operation_id, "agent.tool", payload)
    if replay is not None:
        if manage_transaction:
            session.commit()
        return replay
    current_step = session.scalar(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.status != "completed").order_by(AgentStep.index).limit(1))
    if step is not None and current_step is not None and step.id != current_step.id:
        raise AppError(403, "AGENT_STEP_NOT_ACTIVE", "本次工具只能操作当前执行步骤")
    step = step or current_step
    if step is not None:
        session.refresh(step)
        if role != step.role:
            raise AppError(403, "AGENT_ROLE_MISMATCH", "工具角色与当前执行步骤不一致")
        if len(step.tool_results_json) >= run.limits_json["maxSteps"]:
            raise AppError(409, "AGENT_STEP_LIMIT", "任务达到允许的工具步骤上限")
    if step is None and run.limits_json.get("toolCatalogVersion") == MATERIAL_SOP_TOOL_CATALOG_VERSION and tool_name not in MATERIAL_SOP_READ_TOOLS:
        raise AppError(422, "MATERIAL_STEP_REQUIRED", "素材制作操作必须属于当前岗位步骤")
    project = require_run_action(session, run, expected_revision)
    canvas = require_canvas(session, run, project)
    if step is not None and run.plan_json[step.index].get("kind", "produce") == "verify" and tool_name not in READ_TOOLS:
        raise AppError(403, "AGENT_VERIFICATION_READ_ONLY", "验证步骤只允许读取并核对真实前置产物")
    if run.limits_json.get("deliveryMode") == "professional" and tool_name == "write_script":
        raise AppError(403, "AGENT_PROFESSIONAL_TOOL_REQUIRED", "完整专业交付需要使用当前岗位的write_artifact结构")
    if run.limits_json.get("deliveryMode") == "text_edit" and tool_name == "write_script" and body.target_id not in run.target_ids_json:
        raise AppError(403, "AGENT_TEXT_EDIT_TARGET_REQUIRED", "局部正文修订需要明确授权已有目标节点")
    service_operation = hashlib.sha256((operation_id + ":service").encode()).hexdigest()
    if tool_name in EDITING_SCHEMAS:
        result = execute_editing_tool(session, run, project, tool_name, body)
    elif tool_name in STORY_SCHEMAS:
        result = execute_story_tool(session, run, project, tool_name, body, settings)
    elif tool_name == "read_project":
        business_canvas = copy.deepcopy(canvas.data_json)
        current_round = run.limits_json.get("materialContext", {}).get("scope") == "current_round"
        scope_ids = {*run.target_ids_json, *run.created_ids_json}
        business_canvas["nodes"] = [node for node in business_canvas["nodes"] if node["type"] != "agent" and (not current_round or node["id"] in scope_ids)]
        for node in business_canvas["nodes"]:
            if "creativeArtifact" in node.get("metadata", {}):
                node["artifactCurrent"] = artifact_is_current(node)
                if node["artifactCurrent"]:
                    try:
                        _validate_saved_artifact(session, run, project, canvas, node, settings, frozenset({node["id"]}))
                    except AppError:
                        node["artifactCurrent"] = False
        business_ids = {node["id"] for node in business_canvas["nodes"]}
        business_canvas["connections"] = [item for item in business_canvas["connections"] if item["fromNodeId"] in business_ids and item["toNodeId"] in business_ids]
        result = {"project": {"id": project.id, "title": project.title, "revision": project.revision},
                  "scene": copy.deepcopy(require_scene(project, run.scene_id)), "canvas": business_canvas,
                  "assets": [{"id": asset.id, "name": asset.name, "mimeType": asset.mime_type, "bytes": asset.bytes,
                              "durationMs": asset.duration_ms, "width": asset.width, "height": asset.height, "sha256": asset.sha256}
                             for asset in session.scalars(select(Asset).where(Asset.owner_id == run.owner_id, Asset.project_id == project.id)).all()],
                  "storyTopology": {"nodes": [{"id": node["id"], "type": node["type"], "title": node["data"].get("title")} for node in project.data_json["graphData"]["nodes"]], "edges": project.data_json["graphData"]["edges"]},
                  "capabilities": {"text": True, "videoQuote": True, "imageGeneration": False, "audioGeneration": False, "publish": False}}
        if current_round:
            from app.services.material_references import frozen_reference_assets
            selected_references = frozen_reference_assets(session, run, settings)
            result["referenceAssets"] = selected_references
            scene = require_scene(project, run.scene_id)
            result["scene"] = {"id": scene["id"], "type": scene["type"], "data": {"title": scene["data"].get("title")}}
            asset_ids = {node.get("metadata", {}).get(key) for node in business_canvas["nodes"] for key in ("assetId", "storageKey")}
            asset_ids.update(item["assetId"] for item in selected_references)
            if run.limits_json.get("materialContext", {}).get("requireHumanReview"):
                from app.image_models import ImageJob
                generated = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id, ImageJob.status == "ready")).all()
                current_artifacts = {node["id"]: payload_digest(node.get("metadata", {}).get("creativeArtifact")) for node in business_canvas["nodes"]}
                generated = [job for job in generated if current_artifacts.get(job.node_id) == job.artifact_hash]
                asset_ids.update(job.asset_id for job in generated)
                result["generatedImages"] = [{"nodeId": job.node_id, "assetId": job.asset_id, "frameKey": job.frame_key, "label": job.label} for job in generated]
                result["capabilities"]["imageGeneration"] = True
            result["assets"] = [asset for asset in result["assets"] if asset["id"] in asset_ids]
            result["storyTopology"] = {"nodes": [node for node in result["storyTopology"]["nodes"] if node["id"] == run.scene_id], "edges": []}
    elif tool_name == "retrieve_context":
        identifiers = {node["id"] for node in canvas.data_json["nodes"] if node["type"] != "agent"}
        targets = body.target_ids if body.target_ids is not None else [target for target in run.target_ids_json if target in identifiers]
        current_round = run.limits_json.get("materialContext", {}).get("scope") == "current_round"
        if current_round:
            targets = body.target_ids if body.target_ids is not None else [target for target in [*run.target_ids_json, *run.created_ids_json] if target in identifiers]
            for target in targets:
                authorized(run, target)
        result = retrieve_context(session, run.project_id, run.owner_id, run.scene_id, run.branch_id, targets, body.max_chars, material_only=current_round)
    elif tool_name == "read_artifact":
        result = _read_artifact(session, run, project, canvas, body.target_id, settings, step)
    elif tool_name in {"write_script", "write_artifact", "patch_artifact", "apply_canvas_ops"}:
        data = copy.deepcopy(canvas.data_json)
        patch_proof = None
        if tool_name in {"write_artifact", "patch_artifact"}:
            actual_role = step.role if step is not None else run.role
            reused_frames = []
            if tool_name == "write_artifact" and body.target_id and run.limits_json.get("toolCatalogVersion") in MATERIAL_CATALOG_VERSIONS:
                authorized(run, body.target_id)
                if (run.limits_json.get("toolCatalogVersion") == MATERIAL_SOP_TOOL_CATALOG_VERSION
                        or find_node(data, body.target_id).get("metadata", {}).get("creativeArtifact")):
                    raise AppError(422, "MATERIAL_PATCH_REQUIRED", "修改已有专业产物须先read_artifact，再用patch_artifact修改具体字段；新完整候选的targetId填写null创建")
            if tool_name == "patch_artifact":
                artifact, patch_proof = _patch_artifact(session, run, step, project, canvas, body, actual_role, settings)
            else:
                try:
                    artifact = validate_artifact(actual_role, body.artifact)
                except ValidationError as error:
                    raise AppError(422, "AGENT_ARTIFACT_INVALID", "专业产物没有通过当前角色的严格结构校验") from error
            if run.limits_json.get("toolCatalogVersion") == BOUNDARY_TOOL_CATALOG_VERSION and actual_role == "storyboarder" and artifact.get("schemaVersion") != 2:
                raise AppError(422, "STORYBOARD_BOUNDARIES_REQUIRED", "分镜必须按n个视频片段保存n+1张连续首尾帧")
            if run.limits_json.get("toolCatalogVersion") in REUSABLE_BOUNDARY_CATALOG_VERSIONS and actual_role == "storyboarder" and tool_name == "write_artifact" and artifact.get("schemaVersion") != 3:
                raise AppError(422, "STORYBOARD_REUSABLE_BOUNDARIES_REQUIRED", "当前分镜须使用版本3，明确每张边界复用已有图片或生成新图片")
            if artifact.get("role") == "storyboarder" and artifact.get("schemaVersion") == 3:
                if run.limits_json.get("toolCatalogVersion") not in REUSABLE_BOUNDARY_CATALOG_VERSIONS:
                    raise AppError(422, "STORYBOARD_REUSABLE_CATALOG_REQUIRED", "已有图片复用需要当前版本的分镜任务")
                from app.services.material_references import reusable_boundary_frames
                reused_frames = reusable_boundary_frames(session, run, artifact, settings)
            if run.limits_json.get("toolCatalogVersion") == MATERIAL_SOP_TOOL_CATALOG_VERSION:
                from app.services.material_handoffs import validate_material_handoff
                validate_material_handoff(session, run, step, canvas, artifact)
                if actual_role == "media_producer":
                    if artifact.get("schemaVersion") != 2:
                        raise AppError(422, "MATERIAL_VIDEO_SCHEMA_REQUIRED", "请新建实际首尾帧与视频动作要求的视频方案")
                    for shot in artifact["shots"]:
                        _material_video_boundary(session, run, step, shot, settings)
            elif actual_role == "media_producer" and artifact.get("schemaVersion") == 2:
                raise AppError(422, "MATERIAL_VIDEO_SCHEMA_REQUIRED", "采用首尾帧的视频方案需要当前素材工作流程")
            source_bindings = _validate_artifact_sources(session, run, project, canvas, artifact, settings)
            for binding in source_bindings:
                if binding["kind"] == "canvas_node" and binding["id"] == body.target_id and binding.get("sceneId") == run.scene_id:
                    binding["bindingMode"] = "input_snapshot"
            content = render_artifact(artifact)
            prompts = image_prompt_items(artifact)
            operations = _write_text(run, data, body.target_id, content, (body.title if isinstance(body, ArtifactArguments) else None) or artifact["title"], preserve_title=tool_name == "patch_artifact")
            written = find_node(data, operations[0]["nodeId"])
            written["metadata"] = {**written["metadata"], "creativeArtifact": artifact,
                "artifactContractVersion": ARTIFACT_CONTRACT_VERSION, "generationPrompts": prompts,
                "artifactSources": source_bindings,
                "artifactContentSha256": hashlib.sha256(content.encode()).hexdigest()}
            if run.limits_json.get("workspace") == "materials":
                written["metadata"]["materialArtifact"] = True
            if artifact.get("role") == "storyboarder" and artifact.get("schemaVersion") == 3:
                written["metadata"]["reusedBoundaryFrames"] = reused_frames
        elif tool_name == "write_script":
            operations = _write_text(run, data, body.target_id, body.text, body.title)
        else:
            operations = _apply_canvas(run, data, body.operations)
        _write_canvas(session, run, project, canvas, data)
        saved_texts = []
        for operation in operations:
            if operation["action"] not in {"add_text", "update_text"}:
                continue
            written = find_node(data, operation["nodeId"])
            content = written.get("metadata", {}).get("content", "")
            saved_texts.append({"nodeId": written["id"], "title": written.get("title", ""), "text": content,
                                "characterCount": len(content), "sha256": hashlib.sha256(content.encode()).hexdigest()})
        node_ids = {item["nodeId"] for item in operations if item["action"] not in {"connect", "disconnect"}}
        snapshots = [{"nodeId": identifier, "sha256": payload_digest(find_node(data, identifier)), "snapshot": copy.deepcopy(find_node(data, identifier))} for identifier in node_ids]
        endpoint_pairs = {(item["nodeId"], item["targetId"]) for item in operations if item["action"] in {"connect", "disconnect"}}
        connection_snapshots = [_connection_snapshot(data, source_id, target_id) for source_id, target_id in sorted(endpoint_pairs)]
        result = {"operations": operations, "savedTexts": saved_texts, "nodeSnapshots": snapshots, "sceneId": run.scene_id,
                  "connectionSnapshots": connection_snapshots, "revision": project.revision, "persisted": True}
        if tool_name in {"write_artifact", "patch_artifact"}:
            result.update(artifact=artifact, artifactContractVersion=ARTIFACT_CONTRACT_VERSION,
                artifactSha256=payload_digest(artifact), promptItems=prompts, sourceNodeId=operations[0]["nodeId"], sourceRevision=project.revision,
                sourceBindings=source_bindings)
            if patch_proof is not None:
                result.update(patch_proof)
            if run.limits_json.get("materialContext", {}).get("requireHumanReview"):
                from app.services.material_review import pause_for_review
                pause_for_review(session, run, step, written, artifact, settings)
    elif tool_name == "write_memory":
        memory_payload = {**body.model_dump(by_alias=True, exclude_none=True), "sceneId": run.scene_id, "branchId": run.branch_id, "expectedRevision": expected_revision, "operationId": service_operation}
        result = write_memory(session, run.project_id, run.owner_id, memory_payload, manage_transaction=False)
        run.project_revision = result["revision"]
    elif tool_name == "split_image":
        authorized(run, body.source_node_id)
        find_node(canvas.data_json, body.source_node_id)
        result = split_image(session, run.project_id, run.owner_id, {**body.model_dump(by_alias=True), "sceneId": run.scene_id, "expectedRevision": expected_revision, "operationId": service_operation}, settings, manage_transaction=False)
        run.created_ids_json = [*run.created_ids_json, *[piece["nodeId"] for piece in result["pieces"]]]
        run.project_revision = result["revision"]
    elif tool_name == "join_videos":
        authorized(run, body.source_node_id)
        authorized(run, body.output_node_id)
        find_node(canvas.data_json, body.source_node_id)
        find_node(canvas.data_json, body.output_node_id)
        generation = create_join_generation(session, run.project_id, run.owner_id, {**body.model_dump(by_alias=True), "sceneId": run.scene_id, "expectedRevision": expected_revision, "operationId": service_operation}, settings, manage_transaction=False)
        result = {"generation": generation_view(generation), "localProcessing": True}
    elif tool_name == "submit_generation" and isinstance(body, MaterialVideoGenerationArguments):
        generation = _quote_material_video(session, run, step, project, canvas, body, service_operation, settings)
        result = {"generation": generation_view(generation), "confirmationRequired": generation.status == "awaiting_confirmation"}
        if result["confirmationRequired"]:
            run.status = "waiting_confirmation"
    elif tool_name == "submit_generation":
        authorized(run, body.source_node_id)
        authorized(run, body.output_node_id)
        find_node(canvas.data_json, body.source_node_id)
        find_node(canvas.data_json, body.output_node_id)
        if body.clip_id is not None:
            authorized(run, body.clip_id)
        generation_payload = {**body.model_dump(by_alias=True), "sceneId": run.scene_id, "operationId": service_operation}
        if isinstance(body, BoundaryGenerationArguments):
            from app.services.video_boundaries import freeze_boundaries
            authorized(run, body.storyboard_node_id)
            boundary = freeze_boundaries(session, run, body.storyboard_node_id, body.shot_key, settings)
            if body.duration != boundary["duration"]:
                raise AppError(422, "VIDEO_DURATION_MISMATCH", "视频时长必须与已确认分镜片段一致")
            generation_payload["boundaryFrames"] = boundary
        generation = quote_generation(session, run.project_id, run.owner_id, generation_payload, settings, manage_transaction=False)
        result = {"generation": generation_view(generation), "confirmationRequired": True}
        run.status = "waiting_confirmation"
    elif tool_name in {"inspect_generation", "adopt_asset"}:
        generation = require_generation(session, body.generation_id, run.owner_id)
        if generation.project_id != run.project_id or generation.scene_id != run.scene_id:
            raise AppError(404, "GENERATION_NOT_FOUND", "本任务无法访问该生成任务")
        if tool_name == "adopt_asset":
            authorized(run, generation.source_node_id)
            if generation.input_json.get("replaceExisting"):
                authorized(run, generation.clip_id)
            result = adopt_generation(session, generation.id, run.owner_id, expected_revision, service_operation, settings, manage_transaction=False)
            run.project_revision = result["revision"]
        else:
            result = {"generation": generation_view(generation)}
    elif tool_name == "validate_artifacts":
        if step is None:
            raise AppError(409, "AGENT_STEP_MISSING", "产物验证需要当前执行步骤")
        result = {**validate_artifacts(session, run, step, project, canvas, settings), "executionOrigin": execution_origin}
    else:
        issues = []
        scene = require_scene(project, run.scene_id)
        timeline = scene["data"].get("timeline")
        if not timeline:
            issues.append({"code": "TIMELINE_MISSING", "message": "场景时间线尚未保存"})
        assets = {asset.id: asset for asset in session.scalars(select(Asset).where(Asset.owner_id == run.owner_id, Asset.project_id == run.project_id)).all()}
        for track in (timeline or {}).get("tracks", []):
            for clip in track["clips"]:
                asset_id = clip.get("assetId")
                if asset_id and asset_id not in assets:
                    issues.append({"code": "ASSET_NOT_FOUND", "clipId": clip["id"]})
                elif asset_id:
                    asset = assets[asset_id]
                    resolve_asset_path(settings, asset.relpath)
                    if asset.duration_ms and float(clip.get("sourceStart") or 0) + float(clip.get("sourceDuration") or clip["duration"]) > asset.duration_ms / 1000 + 0.001:
                        issues.append({"code": "MEDIA_DURATION_CONFLICT", "clipId": clip["id"]})
        result = {"sceneId": run.scene_id, "revision": project.revision, "valid": not issues, "issues": issues, "checks": ["saved_structure", "asset_ownership", "media_files", "duration"], "interactivePlaybackVerified": False}
    output = {"result": result, "revision": run.project_revision}
    save_operation(session, run.owner_id, run.project_id, operation_id, "agent.tool", payload, output)
    if step is not None:
        step.tool_results_json = [*step.tool_results_json, {"operationId": operation_id, "tool": tool_name, "arguments": tool_arguments(body, arguments), **output}]
        step.updated_at = time.time()
    run.updated_at = time.time()
    if manage_transaction:
        session.commit()
    return output
