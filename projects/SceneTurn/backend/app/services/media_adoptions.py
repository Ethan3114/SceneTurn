import copy
import hashlib
import hmac
import time

from sqlalchemy import inspect, select

from app.core.errors import AppError
from app.media_models import MediaAdoption
from app.models import Asset, Generation, new_id
from app.services.media import asset_view, resolve_asset_path
from app.services.media_ranges import required_media_end
from app.services.projects import get_operation, iso_timestamp, lock_write, payload_digest, project_envelope, record_project_version, require_project, require_revision, require_scene, save_operation


def find_clip(scene, clip_id):
    return next((clip for track in scene["data"]["timeline"]["tracks"] for clip in track["clips"] if clip["id"] == clip_id), None)


def adoption_view(value, revision=None):
    proposal = value.proposal_json
    status = "stale" if value.status == "ready" and revision is not None and revision != value.project_revision else value.status
    return {"id": value.id, "projectId": value.project_id, "sceneId": value.scene_id, "assetId": value.asset_id,
        "generationId": value.generation_id, "clipId": value.clip_id, "projectRevision": value.project_revision,
        "confirmationHash": value.confirmation_hash, "status": status, "targetName": proposal["targetName"],
        "availableSeconds": proposal["availableSeconds"], "requiredSeconds": proposal["requiredSeconds"],
        "sourceStart": proposal["sourceStart"], "message": proposal["message"], "appliedRevision": value.applied_revision}


def owned_adoption(session, project_id, adoption_id, owner_id):
    require_project(session, project_id, owner_id)
    value = session.scalar(select(MediaAdoption).where(MediaAdoption.id == adoption_id,
        MediaAdoption.project_id == project_id, MediaAdoption.owner_id == owner_id))
    if value is None:
        raise AppError(404, "MEDIA_ADOPTION_NOT_FOUND", "素材采用记录不存在或无权访问")
    return value


def asset_file_state(asset, settings):
    if asset.duration_ms is None or asset.duration_ms <= 0 or not asset.mime_type.startswith("video/"):
        raise AppError(422, "MEDIA_DURATION_INVALID", "请选择已检查真实时长的视频素材")
    try:
        path = resolve_asset_path(settings, asset.relpath)
    except AppError as error:
        if error.code != "ASSET_FILE_NOT_FOUND":
            raise
        return "MEDIA_FILE_MISSING"
    if hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256:
        return "MEDIA_FILE_CHANGED"
    return None


def check_adoption(session, project_id, owner_id, body, settings, *, manage_transaction=True):
    if manage_transaction:
        lock_write(session)
    project = require_project(session, project_id, owner_id)
    payload = body.model_dump(by_alias=True)
    replay = get_operation(session, owner_id, project_id, body.operation_id, "media.adoption.check", payload)
    if replay:
        return owned_adoption(session, project_id, replay["adoptionId"], owner_id)
    require_revision(project, body.expected_revision)
    scene = require_scene(project, body.scene_id)
    asset = session.get(Asset, body.asset_id)
    if asset is None or asset.owner_id != owner_id or asset.project_id != project_id:
        raise AppError(404, "ASSET_NOT_FOUND", "候选素材不属于当前用户与项目")
    generation = session.get(Generation, body.generation_id) if body.generation_id else None
    if body.generation_id and (generation is None or generation.owner_id != owner_id or generation.project_id != project_id or generation.scene_id != body.scene_id or generation.asset_id != asset.id or generation.status != "ready"):
        raise AppError(409, "GENERATION_NOT_READY", "候选视频任务尚未完成或归属不一致")
    clip_id = body.clip_id or (generation.clip_id if generation else new_id())
    clip = find_clip(scene, clip_id)
    if clip is None and body.mode == "replace":
        raise AppError(409, "GENERATION_TARGET_MISSING", "原目标片段已删除，候选继续保留在项目中")
    if clip is not None and body.mode == "append" and clip.get("assetId") != asset.id:
        raise AppError(409, "GENERATION_TARGET_CONFLICT", "预分配片段已经被其他编辑使用，请重新选择目标")
    if clip is not None and clip["type"] not in {"video", "image"}:
        raise AppError(409, "GENERATION_TARGET_INVALID", "候选视频只能替换视频或图片片段")
    file_error = asset_file_state(asset, settings)
    available = asset.duration_ms / 1000
    source_start = float(clip.get("sourceStart") or 0) if clip else 0
    source_duration = float(clip.get("sourceDuration") or float(clip["duration"]) * float(clip.get("playbackRate") or 1)) if clip else available
    required = required_media_end(clip) if clip else available
    status = "blocked" if file_error else "duration_conflict" if required > available + 0.001 else "ready"
    message = "候选文件不可用，请恢复原文件或重新选择素材。" if file_error else "新视频不足以覆盖当前裁切范围，请调整片段后重新检查，或保留当前版本。" if status == "duration_conflict" else "确认后仅更换目标片段的媒体，保留时间、裁切、互动和故事分支。"
    proposal = {"mode": body.mode, "assetSha256": asset.sha256, "targetName": clip.get("name") or "视频片段" if clip else "新视频片段",
        "availableSeconds": available, "requiredSeconds": required, "sourceStart": source_start,
        "sourceDuration": source_duration, "fileError": file_error, "message": message}
    identifier = new_id()
    confirmation = payload_digest({"id": identifier, "projectId": project_id, "revision": project.revision,
        "sceneId": body.scene_id, "clipId": clip_id, "assetId": asset.id, "target": clip, "proposal": proposal})
    for old in session.scalars(select(MediaAdoption).where(MediaAdoption.project_id == project_id,
        MediaAdoption.owner_id == owner_id, MediaAdoption.scene_id == body.scene_id, MediaAdoption.clip_id == clip_id,
        MediaAdoption.status.in_(["ready", "duration_conflict", "blocked"]))):
        old.status, old.updated_at = "superseded", time.time()
    value = MediaAdoption(id=identifier, owner_id=owner_id, project_id=project_id, scene_id=body.scene_id,
        asset_id=asset.id, generation_id=generation.id if generation else None, clip_id=clip_id,
        operation_id=body.operation_id, project_revision=project.revision, target_json=copy.deepcopy(clip),
        proposal_json=proposal, confirmation_hash=confirmation, status=status)
    session.add(value)
    session.flush()
    save_operation(session, owner_id, project_id, body.operation_id, "media.adoption.check", payload, {"adoptionId": value.id})
    if manage_transaction:
        session.commit()
    return value


def apply_adoption(session, project_id, adoption_id, owner_id, body, settings, *, manage_transaction=True):
    if manage_transaction:
        lock_write(session)
    value = owned_adoption(session, project_id, adoption_id, owner_id)
    payload = {"adoptionId": adoption_id, **body.model_dump(by_alias=True)}
    replay = get_operation(session, owner_id, project_id, body.operation_id, "media.adoption.apply", payload)
    if replay:
        return {"adoption": adoption_view(value), "project": replay["project"]}
    project = require_project(session, project_id, owner_id)
    if body.expected_revision != value.project_revision or project.revision != value.project_revision:
        raise AppError(409, "PROJECT_REVISION_CONFLICT", "项目已有新编辑，请重新检查候选与目标片段")
    if not hmac.compare_digest(value.confirmation_hash, body.confirmation_hash):
        raise AppError(409, "MEDIA_ADOPTION_CONFIRMATION_INVALID", "采用确认与本次候选不一致")
    actual_confirmation = payload_digest({"id": value.id, "projectId": project_id, "revision": value.project_revision,
        "sceneId": value.scene_id, "clipId": value.clip_id, "assetId": value.asset_id, "target": value.target_json, "proposal": value.proposal_json})
    if not hmac.compare_digest(actual_confirmation, value.confirmation_hash):
        raise AppError(409, "MEDIA_ADOPTION_CHANGED", "已保存的采用输入与确认摘要不一致")
    if value.status != "ready":
        raise AppError(409, "MEDIA_DURATION_CONFLICT" if value.status == "duration_conflict" else "MEDIA_ADOPTION_BLOCKED", value.proposal_json["message"])
    snapshot = copy.deepcopy(project.data_json)
    scene = next((node for node in snapshot["graphData"]["nodes"] if node["id"] == value.scene_id and node["type"] == "scene"), None)
    if scene is None:
        raise AppError(409, "GENERATION_TARGET_MISSING", "目标场景已经删除，候选继续保留")
    clip = find_clip(scene, value.clip_id)
    if clip != value.target_json:
        raise AppError(409, "MEDIA_TARGET_CHANGED", "目标片段已有变化，请重新检查")
    asset = session.get(Asset, value.asset_id)
    if asset is None or asset.owner_id != owner_id or asset.project_id != project_id or asset.sha256 != value.proposal_json["assetSha256"] or asset_file_state(asset, settings):
        raise AppError(409, "MEDIA_FILE_CHANGED", "候选文件已经变化或不可用，请重新检查")
    media = asset_view(asset)
    timeline = scene["data"]["timeline"]
    if clip is not None:
        clip.update(type="video", assetId=asset.id, src=media["url"], poster=media["posterUrl"])
    else:
        track = next((item for item in timeline["tracks"] if item["type"] == "media" and item.get("mediaRole") == "main"), None)
        if track is None:
            track = {"id": new_id(), "type": "media", "name": "主媒体", "mediaRole": "main", "clips": []}
            timeline["tracks"].append(track)
        start = max((float(item["startTime"]) + float(item["duration"]) for item in track["clips"]), default=0)
        duration = asset.duration_ms / 1000
        track["clips"].append({"id": value.clip_id, "type": "video", "startTime": start, "duration": duration,
            "sourceStart": 0, "sourceDuration": duration, "name": asset.name, "enabled": True,
            "assetId": asset.id, "src": media["url"], "poster": media["posterUrl"], "fit": "contain"})
        timeline["duration"] = max(float(timeline["duration"]), start + duration)
    if not any(item["id"] == asset.id for item in snapshot["assets"]):
        snapshot["assets"].append({"id": asset.id, "type": "video", "name": asset.name, "path": media["url"], "relativePath": media["url"],
            "importedAt": iso_timestamp(asset.created_at), "metadata": {"duration": asset.duration_ms / 1000,
                "width": asset.width, "height": asset.height, "size": asset.bytes, "mimeType": asset.mime_type, "poster": media["posterUrl"], "generationId": value.generation_id}})
    snapshot["updatedAt"] = iso_timestamp(time.time())
    project.data_json, project.revision, project.updated_at = snapshot, project.revision + 1, time.time()
    value.status, value.applied_revision, value.updated_at = "applied", project.revision, time.time()
    record_project_version(session, project)
    response = project_envelope(session, project)
    save_operation(session, owner_id, project_id, body.operation_id, "media.adoption.apply", payload, {"project": response})
    if manage_transaction:
        session.commit()
    return {"adoption": adoption_view(value), "project": response}


def keep_current(session, project_id, adoption_id, owner_id, body):
    lock_write(session)
    value = owned_adoption(session, project_id, adoption_id, owner_id)
    payload = {"adoptionId": adoption_id, **body.model_dump(by_alias=True)}
    if get_operation(session, owner_id, project_id, body.operation_id, "media.adoption.keep", payload):
        return value
    if value.status not in {"ready", "duration_conflict", "blocked"}:
        raise AppError(409, "MEDIA_ADOPTION_CLOSED", "这次采用请求已经处理")
    value.status, value.updated_at = "kept_current", time.time()
    save_operation(session, owner_id, project_id, body.operation_id, "media.adoption.keep", payload, {"adoptionId": value.id})
    session.commit()
    return value


def conflict_issues(session, project_id, owner_id, scene_ids=None):
    if not inspect(session.get_bind()).has_table("media_adoptions"):
        return []
    query = select(MediaAdoption).where(MediaAdoption.project_id == project_id, MediaAdoption.owner_id == owner_id,
        MediaAdoption.status.in_(["duration_conflict", "blocked"]))
    if scene_ids is not None:
        query = query.where(MediaAdoption.scene_id.in_(scene_ids))
    return [{"code": "MEDIA_DURATION_CONFLICT" if value.status == "duration_conflict" else value.proposal_json["fileError"],
        "sceneId": value.scene_id, "clipId": value.clip_id, "adoptionId": value.id} for value in session.scalars(query)]


def scene_media_reviews(session, project_id, owner_id, scene_ids):
    if not inspect(session.get_bind()).has_table("media_adoptions"):
        return []
    values = session.scalars(select(MediaAdoption).where(MediaAdoption.project_id == project_id,
        MediaAdoption.owner_id == owner_id, MediaAdoption.scene_id.in_(scene_ids),
        MediaAdoption.status.in_(["ready", "duration_conflict", "blocked"]))).all()
    return [adoption_view(value) for value in values]
