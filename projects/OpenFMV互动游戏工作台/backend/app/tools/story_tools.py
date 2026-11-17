import copy
import math
import time
from typing import Literal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun
from app.core.config import Settings
from app.core.errors import AppError
from app.models import Asset, Canvas, Project, new_id
from app.schemas import CanvasSnapshot, ProjectSnapshot, WireModel
from app.services.media import asset_view, resolve_asset_path
from app.services.projects import empty_canvas, payload_digest, record_project_version, scene_node


class StoryRead(WireModel):
    pass


class StoryEdit(WireModel):
    scene_id: str
    title: str = Field(min_length=1, max_length=200)
    body_text: str = Field(max_length=30000)
    create: bool = False


class TimelineEdit(WireModel):
    scene_id: str
    asset_id: str
    clip_id: str | None = None
    start_time: float = Field(ge=0, le=86400, allow_inf_nan=False)
    duration: float = Field(gt=0, le=86400, allow_inf_nan=False)
    source_start: float = Field(default=0, ge=0, le=86400, allow_inf_nan=False)


class InteractionEdit(WireModel):
    scene_id: str
    clip_id: str | None = None
    label: str = Field(min_length=1, max_length=200)
    start_time: float = Field(ge=0, le=86400, allow_inf_nan=False)
    duration: float = Field(gt=0, le=86400, allow_inf_nan=False)
    mode: Literal["normal", "qte"] = "normal"
    target_id: str
    timeout_target_id: str | None = None
    input: Literal["click", "space"] = "click"
    click_count: int = Field(default=1, ge=1, le=20)


class MediaPreparation(WireModel):
    scene_id: str
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=30000)
    media_type: Literal["image", "video"]


STORY_SCHEMAS = {"read_story": StoryRead, "apply_story_ops": StoryEdit, "edit_timeline": TimelineEdit,
                 "edit_interaction": InteractionEdit, "prepare_scene_media": MediaPreparation, "validate_story": StoryRead}
STORY_DESCRIPTIONS = {
    "read_story": "读取故事总画布、全部场景连接、授权场景的真实时间线、素材画布及项目已登记媒体；返回可修改的场景标识。",
    "apply_story_ops": "保存授权场景的标题与剧情正文；create=true时创建新场景及其素材画布。保留全部既有时间线与互动。",
    "edit_timeline": "将项目真实媒体加入授权场景，或修改该场景已有媒体片段的时间与裁切；检查文件、归属和真实时长，保留其他片段与互动。",
    "edit_interaction": "保存授权场景的真实按钮或QTE及其跳转连接；QTE须提供超时目标。直接进入播放器使用的version 2时间线。",
    "prepare_scene_media": "在授权场景的素材小画布创建提示词→生成配置→媒体预览节点并连接；不调用收费模型。图片服务未配置时等待配置，视频仍须本人逐次确认。",
    "validate_story": "只读校验当前故事、授权场景时间线、按钮/QTE连接、媒体归属、真实文件和裁切时长，返回当前版本证据。",
}


def scoped_scene(run: AgentRun, snapshot: dict, scene_id: str) -> dict:
    if run.limits_json.get("workspace") != "project" or scene_id not in {*run.target_ids_json, *run.created_ids_json}:
        raise AppError(403, "AGENT_SCENE_NOT_ALLOWED", "该场景不在总画布任务授权范围内")
    scene = next((item for item in snapshot["graphData"]["nodes"] if item["id"] == scene_id and item["type"] == "scene"), None)
    if scene is None:
        raise AppError(404, "SCENE_NOT_FOUND", "授权场景已不存在")
    return scene


def timeline(scene: dict) -> dict:
    value = scene["data"].get("timeline")
    if not isinstance(value, dict) or value.get("version") != 2:
        raise AppError(422, "TIMELINE_INVALID", "场景需要有效的version 2时间线")
    return value


def owned_asset(session: Session, project: Project, asset_id: str, settings: Settings) -> Asset:
    asset = session.scalar(select(Asset).where(Asset.id == asset_id, Asset.project_id == project.id, Asset.owner_id == project.owner_id))
    if asset is None:
        raise AppError(404, "ASSET_NOT_FOUND", "媒体未登记在当前项目中")
    resolve_asset_path(settings, asset.relpath)
    return asset


def put_media_clip(session: Session, project: Project, snapshot: dict, scene: dict, body: TimelineEdit, settings: Settings) -> dict:
    asset = owned_asset(session, project, body.asset_id, settings)
    kind = asset.mime_type.split("/")[0]
    if kind not in {"video", "image", "audio"}:
        raise AppError(422, "ASSET_TYPE_UNSUPPORTED", "时间线只接受图片、视频和音频")
    value = timeline(scene)
    existing = next(((track, clip) for track in value["tracks"] for clip in track["clips"] if clip["id"] == body.clip_id), None) if body.clip_id else None
    if body.clip_id and (existing is None or existing[0]["type"] != "media"):
        raise AppError(404, "CLIP_NOT_FOUND", "指定媒体片段不属于当前场景")
    if existing and existing[1].get("assetId") != asset.id:
        raise AppError(422, "CLIP_ASSET_REPLACEMENT_REQUIRES_CONFIRMATION", "本次工具只允许裁切原媒体；素材替换使用候选采用流程")
    rate = float(existing[1].get("playbackRate", 1)) if existing else 1
    if not math.isfinite(rate) or rate <= 0:
        raise AppError(422, "MEDIA_PLAYBACK_RATE_INVALID", "原片段播放速度无效，请核对片段设置")
    source_duration = body.duration * rate
    if kind != "image" and (not asset.duration_ms or body.source_start + source_duration > asset.duration_ms / 1000 + 0.001):
        raise AppError(422, "MEDIA_DURATION_CONFLICT", "裁切范围超过媒体真实时长")
    track = existing[0] if existing else next((track for track in value["tracks"] if track["type"] == "media" and (track.get("mediaRole") == "audio") == (kind == "audio")), None)
    if track is None:
        track = {"id": new_id(), "type": "media", "name": "声音" if kind == "audio" else "主画面", "mediaRole": "audio" if kind == "audio" else "main", "clips": []}
        value["tracks"].append(track)
    clip = existing[1] if existing else {"id": new_id(), "enabled": True, "fit": "contain"}
    media = asset_view(asset)
    clip.update(type=kind, assetId=asset.id, src=media["url"], name=asset.name,
                startTime=body.start_time, duration=body.duration, sourceStart=body.source_start,
                sourceDuration=source_duration, playbackRate=rate)
    if not existing:
        track["clips"].append(clip)
    value["duration"] = max(value.get("duration", 0), body.start_time + body.duration)
    if not any(item["id"] == asset.id for item in snapshot["assets"]):
        snapshot["assets"].append({"id": asset.id, "type": kind, "name": asset.name, "path": media["url"], "relativePath": media["url"], "importedAt": media["createdAt"],
                                   "metadata": {"duration": (asset.duration_ms or 0) / 1000, "width": asset.width, "height": asset.height, "mimeType": asset.mime_type, "size": asset.bytes}})
    return {"sceneId": scene["id"], "clipId": clip["id"], "assetId": asset.id, "clip": copy.deepcopy(clip)}


def execute_story_tool(session: Session, run: AgentRun, project: Project, name: str, body: WireModel, settings: Settings) -> dict:
    if run.limits_json.get("workspace") != "project":
        raise AppError(403, "AGENT_WORKSPACE_REQUIRED", "故事编辑工具需要总画布任务")
    snapshot = copy.deepcopy(project.data_json)
    selected = [item for item in snapshot["graphData"]["nodes"] if item["type"] == "scene" and item["id"] in {*run.target_ids_json, *run.created_ids_json}]
    if name == "read_story":
        from app.services.media_adoptions import scene_media_reviews
        canvases = session.scalars(select(Canvas).where(Canvas.project_id == project.id, Canvas.owner_id == project.owner_id, Canvas.scene_id.in_([item["id"] for item in selected]))).all()
        assets = session.scalars(select(Asset).where(Asset.project_id == project.id, Asset.owner_id == project.owner_id)).all()
        return {"revision": project.revision, "graph": snapshot["graphData"], "authorizedSceneIds": [item["id"] for item in selected],
                "canvases": [{**canvas.data_json, "nodes": [node for node in canvas.data_json["nodes"] if node["type"] != "agent"]} for canvas in canvases],
                "assets": [asset_view(asset) for asset in assets],
                "mediaAdoptions": scene_media_reviews(session, project.id, run.owner_id, [item["id"] for item in selected])}
    if name == "validate_story":
        from app.services.media_adoptions import conflict_issues
        from app.services.media_ranges import required_media_end
        issues = conflict_issues(session, project.id, run.owner_id, [scene["id"] for scene in selected])
        node_ids = {node["id"] for node in snapshot["graphData"]["nodes"]}
        for scene in selected:
            value = timeline(scene)
            for track in value["tracks"]:
                for clip in track["clips"]:
                    if clip.get("assetId"):
                        asset = owned_asset(session, project, clip["assetId"], settings)
                        if asset.duration_ms and required_media_end(clip) > asset.duration_ms / 1000 + 0.001:
                            issues.append({"code": "MEDIA_DURATION_CONFLICT", "clipId": clip["id"]})
                    if clip["type"] == "button":
                        for event in (["click", "timeout"] if clip.get("mode") == "qte" else ["click"]):
                            edges = [edge for edge in snapshot["graphData"]["edges"] if edge["source"] == scene["id"] and edge.get("sourceHandle") == f"button:{clip['id']}:{event}"]
                            if len(edges) != 1 or edges[0]["target"] not in node_ids:
                                issues.append({"code": "INTERACTION_TARGET_INVALID", "clipId": clip["id"], "event": event})
        ProjectSnapshot.model_validate(snapshot)
        return {"valid": not issues, "issues": issues, "verifiedRevision": project.revision, "sceneIds": [item["id"] for item in selected], "snapshotSha256": payload_digest(snapshot), "interactivePlaybackVerified": False}
    if name == "apply_story_ops" and body.create:
        if any(item["id"] == body.scene_id for item in snapshot["graphData"]["nodes"]):
            raise AppError(409, "SCENE_ID_EXISTS", "新场景标识已经存在")
        scene = scene_node(body.scene_id, body.title, len(snapshot["graphData"]["nodes"]))
        snapshot["graphData"]["nodes"].append(scene)
        session.add(Canvas(owner_id=run.owner_id, project_id=project.id, scene_id=body.scene_id, data_json=empty_canvas(body.scene_id)))
        run.created_ids_json = [*run.created_ids_json, body.scene_id]
    else:
        scene = scoped_scene(run, snapshot, body.scene_id)
    if name == "apply_story_ops":
        scene["data"].update(title=body.title, bodyText=body.body_text)
        result = {"sceneId": scene["id"], "title": body.title, "bodyText": body.body_text, "created": body.create}
    elif name == "edit_timeline":
        result = put_media_clip(session, project, snapshot, scene, body, settings)
    elif name == "edit_interaction":
        value = timeline(scene)
        node_ids = {item["id"] for item in snapshot["graphData"]["nodes"]}
        targets = [body.target_id, *([body.timeout_target_id] if body.mode == "qte" else [])]
        if any(target not in node_ids or target == scene["id"] for target in targets):
            raise AppError(422, "INTERACTION_TARGET_INVALID", "按钮和QTE需要有效的其他场景或结尾目标")
        if body.start_time + body.duration > value["duration"] + 0.001:
            raise AppError(422, "INTERACTION_TIME_INVALID", "互动范围必须位于场景时间线内")
        existing = next((clip for track in value["tracks"] for clip in track["clips"] if clip["id"] == body.clip_id), None) if body.clip_id else None
        if body.clip_id and (existing is None or existing["type"] != "button"):
            raise AppError(404, "CLIP_NOT_FOUND", "互动片段不属于当前场景")
        clip = existing if existing else {"id": new_id(), "type": "button", "enabled": True, "rect": {"x": 0.3, "y": 0.75, "width": 0.4, "height": 0.12}}
        clip.update(label=body.label, startTime=body.start_time, duration=body.duration, mode=body.mode, pauseOnShow=body.mode == "normal")
        if body.mode == "qte":
            clip["qte"] = {"input": body.input, "prompt": body.label, "clickCount": body.click_count, "showCountdown": True}
        else:
            clip.pop("qte", None)
        if not existing:
            track = next((track for track in value["tracks"] if track["type"] == "interaction"), None)
            if track is None:
                track = {"id": new_id(), "type": "interaction", "name": "互动", "clips": []}
                value["tracks"].append(track)
            track["clips"].append(clip)
        prefix = f"button:{clip['id']}:"
        edges = snapshot["graphData"]["edges"]
        edges[:] = [edge for edge in edges if not (edge["source"] == scene["id"] and edge.get("sourceHandle", "").startswith(prefix))]
        for event, target in zip(["click", "timeout"], targets):
            edges.append({"id": new_id(), "source": scene["id"], "target": target, "sourceHandle": prefix + event, "targetHandle": None})
        result = {"sceneId": scene["id"], "clipId": clip["id"], "clip": copy.deepcopy(clip), "edges": [edge for edge in edges if edge["source"] == scene["id"] and edge.get("sourceHandle", "").startswith(prefix)]}
    else:
        canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.owner_id == run.owner_id, Canvas.scene_id == scene["id"]))
        if canvas is None:
            raise AppError(404, "CANVAS_NOT_FOUND", "场景素材画布不存在")
        data = copy.deepcopy(canvas.data_json)
        ids = [new_id() for _ in range(3)]
        x = max((node.get("position", {}).get("x", 0) + node.get("width", 360) for node in data["nodes"]), default=0) + 80
        data["nodes"].extend([
            {"id": ids[0], "type": "text", "title": body.title + "提示词", "position": {"x": x, "y": 120}, "width": 360, "height": 320, "metadata": {"content": body.prompt}},
            {"id": ids[1], "type": "config", "title": body.title, "position": {"x": x + 440, "y": 120}, "width": 400, "height": 460, "metadata": {"generationMode": body.media_type, "generationType": "generation", "dramaTask": "clip", "composerContent": body.prompt, "seconds": "5", "vquality": "720P"}},
            {"id": ids[2], "type": body.media_type, "title": body.title + "候选", "position": {"x": x + 920, "y": 120}, "width": 400, "height": 300, "metadata": {}},
        ])
        data["connections"].extend([{"id": new_id(), "fromNodeId": ids[0], "toNodeId": ids[1]}, {"id": new_id(), "fromNodeId": ids[1], "toNodeId": ids[2]}])
        canvas.data_json = CanvasSnapshot.model_validate(data).model_dump(by_alias=True, exclude_unset=True)
        canvas.updated_at = time.time()
        result = {"sceneId": scene["id"], "nodeIds": ids, "confirmationRequired": True, "submitted": False}
    project.data_json = ProjectSnapshot.model_validate(snapshot).model_dump(by_alias=True, exclude_unset=True)
    project.revision += 1
    project.updated_at = time.time()
    run.project_revision = project.revision
    record_project_version(session, project)
    return {**result, "revision": project.revision, "persisted": True}
