import copy
import time

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select, true
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.errors import AppError
from app.core.config import get_settings
from app.db import json_array_items
from app.models import Canvas, Project, ProjectVersion, User, new_id
from app.schemas import CanvasNode, CanvasResult, CanvasSave, CanvasSnapshot, ProjectCreate, ProjectEnvelope, ProjectList, ProjectSave, SceneCreate
from app.services.canvas_delivery import material_canvas_ids, public_canvas, public_project_envelope, restore_canvas_private_data
from app.services.cloudbase_canvases import CloudBaseCanvasRepository
from app.services.cloudbase_database import gateway_enabled
from app.services.cloudbase_projects import CloudBaseProjectRepository
from app.services.cloudbase_timeline import CloudBaseTimelineRepository
from app.services.projects import create_project_record, empty_canvas, get_operation, iso_timestamp, lock_write, project_envelope, record_project_version, require_project, require_revision, require_scene, save_operation, scene_node, validate_project_assets
from app.tools.story_tools import TimelineAssetInput, attach_timeline_asset, owned_asset
from app.schemas import ProjectSnapshot


router = APIRouter(tags=["projects"])


@router.get("/projects/{project_id}/canvases/{scene_id}/nodes/{node_id}/versions")
def canvas_node_versions(project_id: str, scene_id: str, node_id: str, before: int | None = Query(None, ge=1),
                         user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseCanvasRepository(settings).versions(user.id, project_id, scene_id, node_id, before)
    project = require_project(session, project_id, user.id)
    require_scene(project, scene_id)
    scenes = json_array_items(session, ProjectVersion.canvases_json, name="saved_scene")
    nodes = json_array_items(session, scenes.c.value["nodes"], name="saved_node")
    query = (select(ProjectVersion.revision, nodes.c.value).select_from(ProjectVersion).join(scenes, true()).join(nodes, true())
             .where(ProjectVersion.project_id == project_id, ProjectVersion.owner_id == user.id,
                    scenes.c.value["sceneId"].as_string() == scene_id, nodes.c.value["id"].as_string() == node_id))
    if before is not None:
        query = query.where(ProjectVersion.revision < before)
    records = session.execute(query.order_by(ProjectVersion.revision.desc()).limit(13)).all()
    allowed = material_canvas_ids(session, project_id, user.id, scene_id)[scene_id]
    items = []
    for revision, value in records[:12]:
        node = CanvasNode.model_validate(value).model_dump(by_alias=True, exclude_unset=True)
        page = public_canvas({"nodes": [node], "connections": []}, allowed)
        if page["nodes"]:
            items.append({"revision": revision, "node": page["nodes"][0]})
    return {"versions": items, "before": records[11].revision if len(records) > 12 else None}


@router.post("/projects/{project_id}/timeline-assets", response_model=ProjectEnvelope, response_model_exclude_unset=True)
def add_timeline_asset(project_id: str, body: TimelineAssetInput, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseTimelineRepository(settings).attach(user.id, project_id, body)
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
    asset = owned_asset(session, project, body.asset_id, settings)
    attach_timeline_asset(snapshot, scene, body, asset)
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
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseProjectRepository(settings).list(user.id)
    projects = session.scalars(select(Project).where(Project.owner_id == user.id).order_by(Project.updated_at.desc())).all()
    items = []
    for project in projects:
        archived_at = project.data_json.get("metadata", {}).get("archivedAt")
        items.append({
            "id": project.id, "title": project.title, "revision": project.revision,
            "createdAt": iso_timestamp(project.created_at), "updatedAt": iso_timestamp(project.updated_at),
            "archivedAt": archived_at if isinstance(archived_at, str) and archived_at.strip() else None,
        })
    return {"projects": items}


@router.post("/projects", response_model=ProjectEnvelope, response_model_exclude_unset=True, status_code=201)
def create_project(body: ProjectCreate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseProjectRepository(settings).create(user.id, body.title)
    project = create_project_record(session, user.id, body.title)
    result = project_envelope(session, project)
    session.commit()
    return public_project_envelope(session, user.id, result)


@router.get("/projects/{project_id}", response_model=ProjectEnvelope, response_model_exclude_unset=True)
def get_project(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseProjectRepository(settings).get(user.id, project_id)
    project = require_project(session, project_id, user.id)
    return public_project_envelope(session, user.id, project_envelope(session, project))


@router.put("/projects/{project_id}", response_model=ProjectEnvelope, response_model_exclude_unset=True)
def save_project(project_id: str, body: ProjectSave, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseProjectRepository(settings).save(user.id, project_id, body)
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
    settings = get_settings()
    if gateway_enabled(settings):
        result = CloudBaseProjectRepository(settings).create_scene(user.id, project_id, body)
        if result.replayed:
            response.status_code = 200
        return result.public_envelope()
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
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseCanvasRepository(settings).read(user.id, project_id, scene_id)
    project = require_project(session, project_id, user.id)
    require_scene(project, scene_id)
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project_id, Canvas.scene_id == scene_id))
    if canvas is None:
        raise AppError(404, "CANVAS_NOT_FOUND", "场景画布不存在")
    public_nodes = material_canvas_ids(session, project_id, user.id, scene_id)[scene_id]
    return {"canvas": public_canvas(canvas.data_json, public_nodes), "revision": project.revision}


@router.put("/projects/{project_id}/canvases/{scene_id}", response_model=CanvasResult)
def save_canvas(project_id: str, scene_id: str, body: CanvasSave, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseCanvasRepository(settings).save(user.id, project_id, scene_id, body)
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
