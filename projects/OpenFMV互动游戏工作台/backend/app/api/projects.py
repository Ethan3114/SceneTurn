import copy
import time

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from pydantic import Field

from app.api.deps import current_user, get_db
from app.core.errors import AppError
from app.core.config import get_settings
from app.models import Canvas, Project, User, new_id
from app.schemas import CanvasResult, CanvasSave, CanvasSnapshot, ProjectCreate, ProjectEnvelope, ProjectList, ProjectSave, SceneCreate
from app.services.canvas_delivery import material_canvas_ids, public_canvas, public_project_envelope, restore_canvas_private_data
from app.services.projects import create_project_record, empty_canvas, get_operation, iso_timestamp, lock_write, project_envelope, record_project_version, require_project, require_revision, require_scene, save_operation, scene_node, validate_project_assets
from app.tools.story_tools import TimelineEdit, owned_asset, put_media_clip
from app.schemas import ProjectSnapshot


router = APIRouter(tags=["projects"])


class TimelineAssetInput(TimelineEdit):
    operation_id: str
    expected_revision: int
    start_time: float | None = Field(default=None, ge=0, le=86400, allow_inf_nan=False)
    duration: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)


@router.post("/projects/{project_id}/timeline-assets", response_model=ProjectEnvelope, response_model_exclude_unset=True)
def add_timeline_asset(project_id: str, body: TimelineAssetInput, user: User = Depends(current_user), session: Session = Depends(get_db)):
    lock_write(session)
    project = require_project(session, project_id, user.id)
    payload = body.model_dump(by_alias=True)
    replay = get_operation(session, user.id, project_id, body.operation_id, "timeline.asset", payload)
    if replay is not None:
        return public_project_envelope(session, user.id, replay)
    require_revision(project, body.expected_revision)
    require_scene(project, body.scene_id)
    snapshot = copy.deepcopy(project.data_json)
    scene = next(item for item in snapshot["graphData"]["nodes"] if item["id"] == body.scene_id)
    settings = get_settings()
    asset = owned_asset(session, project, body.asset_id, settings)
    start_time = body.start_time if body.start_time is not None else max((clip["startTime"] + clip["duration"] for track in scene["data"]["timeline"]["tracks"] if track["type"] == "media" for clip in track["clips"]), default=0)
    duration = body.duration if body.duration is not None else (5 if asset.mime_type.startswith("image/") else (asset.duration_ms or 0) / 1000)
    edit = TimelineEdit.model_validate({**body.model_dump(exclude={"operation_id", "expected_revision"}), "start_time": start_time, "duration": duration})
    put_media_clip(session, project, snapshot, scene, edit, settings)
    project.data_json = ProjectSnapshot.model_validate(snapshot).model_dump(by_alias=True, exclude_unset=True)
    project.revision += 1
    project.updated_at = time.time()
    record_project_version(session, project)
    result = project_envelope(session, project)
    save_operation(session, user.id, project_id, body.operation_id, "timeline.asset", payload, result)
    session.commit()
    return public_project_envelope(session, user.id, result)


@router.get("/projects", response_model=ProjectList)
def list_projects(user: User = Depends(current_user), session: Session = Depends(get_db)):
    projects = session.scalars(select(Project).where(Project.owner_id == user.id).order_by(Project.updated_at.desc())).all()
    return {"projects": [{"id": p.id, "title": p.title, "revision": p.revision,
                          "createdAt": iso_timestamp(p.created_at), "updatedAt": iso_timestamp(p.updated_at)} for p in projects]}


@router.post("/projects", response_model=ProjectEnvelope, response_model_exclude_unset=True, status_code=201)
def create_project(body: ProjectCreate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    project = create_project_record(session, user.id, body.title)
    result = project_envelope(session, project)
    session.commit()
    return public_project_envelope(session, user.id, result)


@router.get("/projects/{project_id}", response_model=ProjectEnvelope, response_model_exclude_unset=True)
def get_project(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    project = require_project(session, project_id, user.id)
    return public_project_envelope(session, user.id, project_envelope(session, project))


@router.put("/projects/{project_id}", response_model=ProjectEnvelope, response_model_exclude_unset=True)
def save_project(project_id: str, body: ProjectSave, user: User = Depends(current_user), session: Session = Depends(get_db)):
    lock_write(session)
    project = require_project(session, project_id, user.id)
    payload = body.model_dump(by_alias=True, exclude_unset=True)
    replay = get_operation(session, user.id, project_id, body.operation_id, "project.save", payload)
    if replay is not None:
        return public_project_envelope(session, user.id, replay)
    require_revision(project, body.expected_revision)
    snapshot = body.project.model_dump(by_alias=True, exclude_unset=True)
    if snapshot["id"] != project_id:
        raise AppError(422, "PROJECT_ID_MISMATCH", "项目标识与写入目标不一致")
    validate_project_assets(session, project, snapshot)
    snapshot["metadata"].pop("serverRevision", None)
    snapshot["createdAt"] = project.data_json["createdAt"]
    snapshot["updatedAt"] = iso_timestamp(time.time())
    project.title = snapshot["title"]
    project.data_json = snapshot
    project.revision += 1
    project.updated_at = time.time()
    existing_scene_ids = set(session.scalars(select(Canvas.scene_id).where(Canvas.project_id == project_id)).all())
    for node in snapshot["graphData"]["nodes"]:
        if node["type"] == "scene" and node["id"] not in existing_scene_ids:
            session.add(Canvas(owner_id=user.id, project_id=project_id, scene_id=node["id"], data_json=empty_canvas(node["id"])))
    record_project_version(session, project)
    result = project_envelope(session, project)
    save_operation(session, user.id, project_id, body.operation_id, "project.save", payload, result)
    session.commit()
    return public_project_envelope(session, user.id, result)


@router.post("/projects/{project_id}/scenes", response_model=ProjectEnvelope, response_model_exclude_unset=True, status_code=201)
def create_scene(project_id: str, body: SceneCreate, response: Response, user: User = Depends(current_user), session: Session = Depends(get_db)):
    lock_write(session)
    project = require_project(session, project_id, user.id)
    payload = body.model_dump(by_alias=True)
    replay = get_operation(session, user.id, project_id, body.operation_id, "scene.create", payload)
    if replay is not None:
        response.status_code = 200
        return public_project_envelope(session, user.id, replay)
    require_revision(project, body.expected_revision)
    snapshot = project_envelope(session, project)["project"]
    scene_id = new_id()
    snapshot["graphData"]["nodes"].append(scene_node(scene_id, body.title, len(snapshot["graphData"]["nodes"])))
    snapshot["updatedAt"] = iso_timestamp(time.time())
    snapshot["metadata"].pop("serverRevision", None)
    project.data_json = snapshot
    project.revision += 1
    project.updated_at = time.time()
    session.add(Canvas(owner_id=user.id, project_id=project_id, scene_id=scene_id, data_json=empty_canvas(scene_id)))
    record_project_version(session, project)
    result = project_envelope(session, project)
    save_operation(session, user.id, project_id, body.operation_id, "scene.create", payload, result)
    session.commit()
    return public_project_envelope(session, user.id, result)


@router.get("/projects/{project_id}/canvases/{scene_id}", response_model=CanvasResult)
def get_canvas(project_id: str, scene_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    project = require_project(session, project_id, user.id)
    require_scene(project, scene_id)
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project_id, Canvas.scene_id == scene_id))
    if canvas is None:
        raise AppError(404, "CANVAS_NOT_FOUND", "场景画布不存在")
    public_nodes = material_canvas_ids(session, project_id, user.id, scene_id)[scene_id]
    return {"canvas": public_canvas(canvas.data_json, public_nodes), "revision": project.revision}


@router.put("/projects/{project_id}/canvases/{scene_id}", response_model=CanvasResult)
def save_canvas(project_id: str, scene_id: str, body: CanvasSave, user: User = Depends(current_user), session: Session = Depends(get_db)):
    lock_write(session)
    project = require_project(session, project_id, user.id)
    require_scene(project, scene_id)
    payload = {"sceneId": scene_id, **body.model_dump(by_alias=True, exclude_unset=True)}
    public_nodes = material_canvas_ids(session, project_id, user.id, scene_id)[scene_id]
    replay = get_operation(session, user.id, project_id, body.operation_id, "canvas.save", payload)
    if replay is not None:
        return {**replay, "canvas": public_canvas(replay["canvas"], public_nodes)}
    require_revision(project, body.expected_revision)
    snapshot = CanvasSnapshot(scene_id=scene_id, nodes=body.nodes, connections=body.connections, viewport=body.viewport)
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project_id, Canvas.scene_id == scene_id))
    if canvas is None:
        raise AppError(404, "CANVAS_NOT_FOUND", "场景画布不存在")
    canvas.data_json = restore_canvas_private_data(canvas.data_json, snapshot.model_dump(by_alias=True, exclude_unset=True), public_nodes)
    canvas.updated_at = time.time()
    project.revision += 1
    project.updated_at = time.time()
    record_project_version(session, project)
    result = {"canvas": canvas.data_json, "revision": project.revision}
    save_operation(session, user.id, project_id, body.operation_id, "canvas.save", payload, result)
    session.commit()
    return {**result, "canvas": public_canvas(result["canvas"], public_nodes)}
