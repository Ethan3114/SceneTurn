import copy
import hashlib
import json
import time
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import Asset, Canvas, Operation, Project, ProjectVersion, new_id
from app.schemas import CanvasSnapshot, ProjectSnapshot


def iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def payload_digest(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def lock_write(session: Session) -> None:
    session.execute(text("BEGIN IMMEDIATE"))


def require_project(session: Session, project_id: str, owner_id: str) -> Project:
    project = session.scalar(select(Project).where(Project.id == project_id, Project.owner_id == owner_id))
    if project is None:
        raise AppError(404, "PROJECT_NOT_FOUND", "项目不存在或无权访问")
    return project


def require_scene(project: Project, scene_id: str) -> dict:
    nodes = project.data_json["graphData"]["nodes"]
    for node in nodes:
        if node["id"] == scene_id and node["type"] == "scene":
            return node
    raise AppError(404, "SCENE_NOT_FOUND", "目标剧情场景不存在")


def require_revision(project: Project, expected_revision: int) -> None:
    if project.revision != expected_revision:
        raise AppError(409, "REVISION_CONFLICT", "项目已有新版本，请重新读取并保留当前修改")


def project_envelope(session: Session, project: Project) -> dict:
    snapshot = copy.deepcopy(project.data_json)
    snapshot["metadata"]["serverRevision"] = project.revision
    scene_ids = {node["id"] for node in snapshot["graphData"]["nodes"] if node["type"] == "scene"}
    canvases = session.scalars(select(Canvas).where(Canvas.project_id == project.id).order_by(Canvas.scene_id)).all()
    return {
        "project": snapshot,
        "revision": project.revision,
        "canvases": [copy.deepcopy(canvas.data_json) for canvas in canvases if canvas.scene_id in scene_ids],
    }


def record_project_version(session: Session, project: Project) -> None:
    session.flush()
    envelope = project_envelope(session, project)
    session.add(ProjectVersion(
        project_id=project.id,
        owner_id=project.owner_id,
        revision=project.revision,
        data_json=envelope["project"],
        canvases_json=envelope["canvases"],
    ))


def get_operation(session: Session, owner_id: str, project_id: str, operation_id: str, kind: str, payload: dict) -> dict | None:
    operation = session.scalar(select(Operation).where(
        Operation.owner_id == owner_id,
        Operation.project_id == project_id,
        Operation.operation_id == operation_id,
    ))
    if operation is None:
        return None
    if operation.kind != kind or operation.payload_hash != payload_digest(payload):
        raise AppError(409, "OPERATION_CONFLICT", "同一操作标识已绑定其他输入")
    return copy.deepcopy(operation.response_json)


def save_operation(session: Session, owner_id: str, project_id: str, operation_id: str, kind: str, payload: dict, response: dict) -> None:
    session.add(Operation(
        owner_id=owner_id,
        project_id=project_id,
        operation_id=operation_id,
        kind=kind,
        payload_hash=payload_digest(payload),
        response_json=response,
    ))


def validate_project_assets(session: Session, project: Project, snapshot: dict) -> None:
    requested_ids = {asset["id"] for asset in snapshot["assets"]}
    for node in snapshot["graphData"]["nodes"]:
        timeline = node["data"].get("timeline")
        if timeline:
            for track in timeline["tracks"]:
                for clip in track["clips"]:
                    if clip.get("assetId"):
                        requested_ids.add(clip["assetId"])
    if not requested_ids:
        return
    owned = set(session.scalars(select(Asset.id).where(
        Asset.project_id == project.id, Asset.owner_id == project.owner_id, Asset.id.in_(requested_ids)
    )).all())
    if owned != requested_ids:
        raise AppError(422, "ASSET_NOT_REGISTERED", "素材引用无效，请先将完整媒体登记到当前项目")


def empty_canvas(scene_id: str) -> dict:
    return CanvasSnapshot(scene_id=scene_id).model_dump(by_alias=True)


def scene_node(scene_id: str, title: str, index: int = 0) -> dict:
    return {
        "id": scene_id, "type": "scene", "position": {"x": 320 + index * 280, "y": 200},
        "data": {"type": "scene", "title": title, "timeline": {
            "version": 2, "duration": 0, "tracks": [
                {"id": new_id(), "type": "media", "name": "主画面", "mediaRole": "main", "clips": []},
                {"id": new_id(), "type": "interaction", "name": "互动", "clips": []},
            ], "bookmarks": [],
        }},
    }


def initial_project(project_id: str, title: str) -> tuple[dict, str]:
    start_id, scene_id, end_id = new_id(), new_id(), new_id()
    timestamp = iso_timestamp(time.time())
    snapshot = {
        "schemaVersion": 1, "id": project_id, "title": title,
        "graphData": {"nodes": [
            {"id": start_id, "type": "start", "position": {"x": 40, "y": 200}, "data": {"type": "start", "label": "开始"}},
            scene_node(scene_id, "第一幕"),
            {"id": end_id, "type": "end", "position": {"x": 640, "y": 200}, "data": {"type": "end", "label": "结束"}},
        ], "edges": [
            {"id": new_id(), "source": start_id, "target": scene_id, "sourceHandle": "node:default"},
            {"id": new_id(), "source": scene_id, "target": end_id, "sourceHandle": "node:default"},
        ]},
        "assets": [], "metadata": {"entryNodeId": start_id}, "createdAt": timestamp, "updatedAt": timestamp,
    }
    ProjectSnapshot.model_validate(snapshot)
    return snapshot, scene_id


def create_project_record(session: Session, owner_id: str, title: str) -> Project:
    project_id = new_id()
    snapshot, scene_id = initial_project(project_id, title)
    project = Project(id=project_id, owner_id=owner_id, title=title, data_json=snapshot, revision=1)
    session.add(project)
    session.flush()
    session.add(Canvas(owner_id=owner_id, project_id=project_id, scene_id=scene_id, data_json=empty_canvas(scene_id)))
    record_project_version(session, project)
    return project
