import copy
import time
from typing import Annotated, Literal

from pydantic import Field
from sqlalchemy import select

from app.agent_models import AgentRun
from app.core.errors import AppError
from app.models import Canvas, ProjectVersion, new_id
from app.schemas import CanvasSnapshot, ProjectSnapshot, WireModel
from app.services.projects import payload_digest, record_project_version
from app.tools.story_tools import scoped_scene, timeline


class MoveNode(WireModel):
    action: Literal["move"]
    node_id: str
    x: float = Field(allow_inf_nan=False, ge=-100000, le=100000)
    y: float = Field(allow_inf_nan=False, ge=-100000, le=100000)


class LinkNode(WireModel):
    action: Literal["connect"]
    source_id: str
    target_id: str


class ArrangeStory(WireModel):
    operations: list[Annotated[MoveNode | LinkNode, Field(discriminator="action")]] = Field(min_length=1, max_length=200)


class MoveClip(WireModel):
    scene_id: str
    clip_id: str
    start_time: float = Field(ge=0, le=86400, allow_inf_nan=False)
    track_id: str | None = None
    enabled: bool | None = None


class ButtonRect(WireModel):
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)
    width: float = Field(gt=0, le=1, allow_inf_nan=False)
    height: float = Field(gt=0, le=1, allow_inf_nan=False)


class PlaceInteraction(WireModel):
    scene_id: str
    clip_id: str
    rect: ButtonRect


class UndoEdit(WireModel):
    run_id: str


EDITING_SCHEMAS = {"arrange_story": ArrangeStory, "move_timeline_clip": MoveClip, "place_interaction": PlaceInteraction, "undo_project_edit": UndoEdit}
EDITING_DESCRIPTIONS = {
    "arrange_story": "批量移动总画布节点或设置普通剧情连接；连接使用node:default并保留按钮/QTE连接。一次调用保存为一个项目版本。",
    "move_timeline_clip": "移动授权场景中的实际片段，保持源媒体裁切和播放参数；可指定同类型轨道和启用状态。",
    "place_interaction": "调整真实按钮或QTE的归一化画面位置与尺寸，保留时间、内容和跳转。",
    "undo_project_edit": "撤销同一项目对话中指定的已完成Codex消息产生的整组修改；有后续人工编辑时拒绝覆盖。保留所有历史和媒体文件。",
}


def execute_editing_tool(session, run, project, name, body):
    if run.limits_json.get("workspace") != "project" or not run.limits_json.get("conversationId"):
        raise AppError(403, "PROJECT_CHAT_REQUIRED", "该操作需要当前项目的Codex对话")
    data = copy.deepcopy(project.data_json)
    if name == "undo_project_edit":
        previous = session.get(AgentRun, body.run_id)
        if previous is None or previous.id == run.id or previous.owner_id != run.owner_id or previous.project_id != project.id or previous.status != "completed" or previous.limits_json.get("conversationId") != run.limits_json["conversationId"]:
            raise AppError(404, "UNDO_TARGET_NOT_FOUND", "只能撤销此项目对话中已完成的编辑")
        if previous.project_revision == previous.initial_revision:
            raise AppError(409, "UNDO_NO_CHANGES", "该条消息没有修改项目")
        if project.revision != previous.project_revision or run.initial_revision != previous.project_revision:
            raise AppError(409, "UNDO_EDIT_CONFLICT", "这次操作之后已有其他编辑，请保留当前内容并指定需要调整的对象")
        version = session.scalar(select(ProjectVersion).where(ProjectVersion.project_id == project.id, ProjectVersion.owner_id == run.owner_id, ProjectVersion.revision == previous.initial_revision))
        if version is None:
            raise AppError(404, "PROJECT_VERSION_NOT_FOUND", "编辑前的项目版本不存在")
        data = copy.deepcopy(version.data_json)
        data["metadata"].pop("serverRevision", None)
        for snapshot in version.canvases_json:
            canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.owner_id == run.owner_id, Canvas.scene_id == snapshot["sceneId"]))
            if canvas is None:
                raise AppError(409, "CANVAS_NOT_FOUND", "原素材画布不存在，撤销已停止")
            canvas.data_json = CanvasSnapshot.model_validate(snapshot).model_dump(by_alias=True)
        result = {"undoneRunId": previous.id, "restoredFromRevision": previous.initial_revision}
    elif name == "arrange_story":
        nodes = {node["id"]: node for node in data["graphData"]["nodes"]}
        for operation in body.operations:
            source_id = operation.node_id if isinstance(operation, MoveNode) else operation.source_id
            node = nodes.get(source_id)
            if node is None:
                raise AppError(404, "STORY_NODE_NOT_FOUND", "剧情节点已不存在")
            if node["type"] == "scene":
                scoped_scene(run, data, source_id)
            if isinstance(operation, MoveNode):
                node["position"] = {**node["position"], "x": operation.x, "y": operation.y}
            else:
                if operation.target_id not in nodes or source_id == operation.target_id or node["type"] == "end" or nodes[operation.target_id]["type"] == "start":
                    raise AppError(422, "STORY_CONNECTION_INVALID", "剧情连接的起点或终点无效")
                edges = data["graphData"]["edges"]
                edges[:] = [edge for edge in edges if not (edge["source"] == source_id and edge.get("sourceHandle", "node:default") == "node:default")]
                edges.append({"id": new_id(), "source": source_id, "target": operation.target_id, "sourceHandle": "node:default"})
        result = {"operations": body.model_dump(by_alias=True)["operations"]}
    else:
        scene = scoped_scene(run, data, body.scene_id)
        value = timeline(scene)
        located = next(((track, clip) for track in value["tracks"] for clip in track["clips"] if clip["id"] == body.clip_id), None)
        if located is None:
            raise AppError(404, "CLIP_NOT_FOUND", "选中的片段已不存在")
        track, clip = located
        if name == "place_interaction":
            if clip["type"] != "button" or body.rect.x + body.rect.width > 1.000001 or body.rect.y + body.rect.height > 1.000001:
                raise AppError(422, "BUTTON_RECT_INVALID", "互动位置必须完整位于画面内")
            clip["rect"] = body.rect.model_dump()
        else:
            target = next((item for item in value["tracks"] if item["id"] == (body.track_id or track["id"])), None)
            if target is None or target["type"] != track["type"]:
                raise AppError(422, "TRACK_TYPE_MISMATCH", "目标轨道必须与原片段类型相同")
            if target is not track:
                track["clips"].remove(clip)
                target["clips"].append(clip)
            clip["startTime"] = body.start_time
            if body.enabled is not None:
                clip["enabled"] = body.enabled
            value["duration"] = max(value["duration"], clip["startTime"] + clip["duration"])
        result = {"sceneId": body.scene_id, "clipId": body.clip_id, "clip": copy.deepcopy(clip)}
    project.data_json = ProjectSnapshot.model_validate(data).model_dump(by_alias=True, exclude_unset=True)
    project.revision += 1
    project.updated_at, run.project_revision = time.time(), project.revision
    record_project_version(session, project)
    return {**result, "persisted": True, "revision": project.revision, "snapshotSha256": payload_digest(project.data_json)}
