import copy
import hashlib
import time

from PIL import Image
from sqlalchemy import select

from app.core.errors import AppError
from app.media_models import MediaAdoption, MediaSequence
from app.models import Asset, Canvas, Generation, new_id
from app.services.media import resolve_asset_path
from app.services.providers import LEGACY_DEFAULTS, profile, project_defaults
from app.services.projects import get_operation, iso_timestamp, lock_write, payload_digest, record_project_version, require_project, require_revision, require_scene, save_operation


def checked_frame(session, asset_id, owner_id, project_id, settings):
    asset = session.get(Asset, asset_id)
    if asset is None or asset.owner_id != owner_id or asset.project_id != project_id or not asset.mime_type.startswith("image/"):
        raise AppError(404, "FRAME_NOT_FOUND", "首尾图片不存在或不属于当前项目")
    path = resolve_asset_path(settings, asset.relpath)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise AppError(422, "FRAME_TOO_LARGE", "首尾图片不得超过20MB")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != asset.sha256:
        raise AppError(409, "FRAME_FILE_CHANGED", "首尾图片文件已经变化，请重新选择")
    with Image.open(path) as image:
        width, height = image.size
        if image.format not in {"JPEG", "PNG", "BMP", "WEBP"} or image.mode not in {"RGB", "L"} or not (240 <= min(width, height) and max(width, height) <= 8000):
            raise AppError(422, "FRAME_FORMAT_INVALID", "请选择有效的不透明图片，单边240至8000像素")
        if width * 9 != height * 16:
            raise AppError(422, "FRAME_ASPECT_INVALID", "当前连续视频使用16:9，请选择相同比例的首尾图片")
        image.verify()
    return {"assetId": asset.id, "name": asset.name, "sha256": asset.sha256, "width": width, "height": height}


def require_sequence(session, project_id, sequence_id, owner_id):
    require_project(session, project_id, owner_id)
    sequence = session.scalar(select(MediaSequence).where(MediaSequence.id == sequence_id,
        MediaSequence.project_id == project_id, MediaSequence.owner_id == owner_id))
    if sequence is None:
        raise AppError(404, "MEDIA_SEQUENCE_NOT_FOUND", "连续视频序列不存在或无权访问")
    if payload_digest(sequence.input_json) != sequence.input_sha256:
        raise AppError(409, "MEDIA_SEQUENCE_INVALID", "连续视频序列摘要不一致")
    return sequence


def sequence_view(session, sequence):
    values = session.scalars(select(Generation).where(Generation.owner_id == sequence.owner_id,
        Generation.project_id == sequence.project_id, Generation.scene_id == sequence.scene_id).order_by(Generation.created_at)).all()
    frames = sequence.input_json["frames"]
    return {"id": sequence.id, "projectId": sequence.project_id, "sceneId": sequence.scene_id,
        "title": sequence.input_json["title"], "frames": frames, "createdAt": iso_timestamp(sequence.created_at),
        "segments": [{"key": item["key"], "title": item["title"], "duration": item["duration"], "resolution": item["resolution"],
            "clipId": item["clipId"], "firstFrame": frames[index], "lastFrame": frames[index + 1],
            "generationIds": [value.id for value in values if value.input_json.get("boundaryFrames", {}).get("sequenceId") == sequence.id and value.input_json["boundaryFrames"].get("shotKey") == item["key"]]}
            for index, item in enumerate(sequence.input_json["segments"])]}


def create_sequence(session, project_id, scene_id, owner_id, body, settings):
    lock_write(session)
    project = require_project(session, project_id, owner_id)
    scene = require_scene(project, scene_id)
    payload = {"sceneId": scene_id, **body.model_dump(by_alias=True)}
    replay = get_operation(session, owner_id, project_id, body.operation_id, "media.sequence", payload)
    if replay:
        return require_sequence(session, project_id, replay["sequenceId"], owner_id)
    require_revision(project, body.expected_revision)
    run = None
    if body.conversation_id:
        from app.services.materials import active_production, require_material
        conversation = require_material(session, project_id, scene_id, body.conversation_id, owner_id)
        run, _ = active_production(session, conversation)
    selected_id = run.limits_json.get("modelSelection", {}).get("video", LEGACY_DEFAULTS["video"]) if run else project_defaults(project)["video"]
    selected = profile(selected_id, "video")
    capability = selected["capabilities"]
    if not capability["lastFrame"]:
        raise AppError(422, "MODEL_LAST_FRAME_UNSUPPORTED", "当前连续视频模型需要支持首尾帧")
    if any(item.resolution not in capability["resolutions"] or not capability["minDuration"] <= item.duration <= capability["maxDuration"] for item in body.segments):
        raise AppError(422, "MODEL_VIDEO_PARAMETERS_UNSUPPORTED", "所选模型不支持当前连续视频的清晰度或时长")
    frames = [checked_frame(session, identifier, owner_id, project_id, settings) for identifier in body.frame_asset_ids]
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project_id, Canvas.scene_id == scene_id, Canvas.owner_id == owner_id))
    if canvas is None:
        raise AppError(404, "CANVAS_NOT_FOUND", "当前场景素材画布不存在")
    snapshot = copy.deepcopy(canvas.data_json)
    y = max((node.get("position", {}).get("y", 0) + node.get("height", 300) for node in snapshot["nodes"]), default=0) + 100
    sequence_id, segments = new_id(), []
    clip_ids = {clip["id"] for track in scene["data"]["timeline"]["tracks"] for clip in track["clips"] if clip["type"] in {"video", "image"}}
    if len({item.clip_id for item in body.segments if item.clip_id}) != sum(bool(item.clip_id) for item in body.segments):
        raise AppError(422, "SEQUENCE_TARGET_DUPLICATE", "同一序列的多个片段不能替换同一个目标片段")
    for index, item in enumerate(body.segments):
        if item.clip_id and item.clip_id not in clip_ids:
            raise AppError(404, "SEQUENCE_TARGET_NOT_FOUND", "选择的目标片段不属于当前场景")
        source_id, output_id = new_id(), new_id()
        segment = {**item.model_dump(by_alias=True), "key": f"segment-{index + 1}", "clipId": item.clip_id or new_id(),
            "replaceExisting": bool(item.clip_id), "sourceNodeId": source_id, "outputNodeId": output_id}
        segments.append(segment)
        snapshot["nodes"].extend([
            {"id": source_id, "type": "config", "title": item.title, "position": {"x": index * 920, "y": y}, "width": 400, "height": 300,
                "metadata": {"generationMode": "video", "dramaTask": "clip", "prompt": item.prompt, "content": item.prompt,
                    "frameSequenceId": sequence_id, "frameSegmentKey": segment["key"], "duration": item.duration, "resolution": item.resolution}},
            {"id": output_id, "type": "video", "title": item.title, "position": {"x": index * 920 + 450, "y": y}, "width": 400, "height": 280,
                "metadata": {"status": "idle", "frameSequenceId": sequence_id, "frameSegmentKey": segment["key"]}},
        ])
        snapshot["connections"].append({"id": new_id(), "fromNodeId": source_id, "toNodeId": output_id, "fromHandle": "right", "toHandle": "left"})
    frozen = {"schemaVersion": 1, "title": body.title, "frames": frames, "segments": segments,
        "conversationId": body.conversation_id, "runId": run.id if run else None, "modelProfileId": selected_id}
    sequence = MediaSequence(id=sequence_id, owner_id=owner_id, project_id=project_id, scene_id=scene_id,
        input_json=frozen, input_sha256=payload_digest(frozen))
    session.add(sequence)
    canvas.data_json = snapshot
    project.revision, project.updated_at = project.revision + 1, time.time()
    project.data_json = {**project.data_json, "updatedAt": iso_timestamp(project.updated_at)}
    session.flush()
    record_project_version(session, project)
    save_operation(session, owner_id, project_id, body.operation_id, "media.sequence", payload, {"sequenceId": sequence.id})
    session.commit()
    return sequence


def freeze_sequence(session, sequence, shot_key, settings):
    project = require_project(session, sequence.project_id, sequence.owner_id)
    require_scene(project, sequence.scene_id)
    if payload_digest(sequence.input_json) != sequence.input_sha256:
        raise AppError(409, "MEDIA_SEQUENCE_INVALID", "连续视频序列摘要不一致")
    index = next((index for index, item in enumerate(sequence.input_json["segments"]) if item["key"] == shot_key), None)
    if index is None:
        raise AppError(404, "MEDIA_SEGMENT_NOT_FOUND", "连续视频片段不存在")
    segment = sequence.input_json["segments"][index]
    frames = []
    for kind, item in zip(("first_frame", "last_frame"), sequence.input_json["frames"][index:index + 2], strict=True):
        current = checked_frame(session, item["assetId"], sequence.owner_id, sequence.project_id, settings)
        if current != item:
            raise AppError(409, "VIDEO_BOUNDARIES_CHANGED", "连续视频的首尾图片已有变化，请重新准备序列")
        frames.append({"type": kind, "assetId": item["assetId"], "sha256": item["sha256"]})
    return {"sourceType": "media_sequence", "sequenceId": sequence.id, "sequenceSha256": sequence.input_sha256,
        "shotKey": shot_key, "sceneId": sequence.scene_id, "duration": segment["duration"], "frames": frames}


def quote_segment(session, project_id, sequence_id, shot_key, owner_id, body, settings):
    from app.services.generation import _clip, quote_generation

    lock_write(session)
    sequence = require_sequence(session, project_id, sequence_id, owner_id)
    payload = {"sequenceId": sequence_id, "shotKey": shot_key, **body.model_dump(by_alias=True)}
    replay = get_operation(session, owner_id, project_id, body.operation_id, "media.segment.quote", payload)
    if replay:
        return session.get(Generation, replay["generationId"])
    project = require_project(session, project_id, owner_id)
    require_revision(project, body.expected_revision)
    boundary = freeze_sequence(session, sequence, shot_key, settings)
    segment = next(item for item in sequence.input_json["segments"] if item["key"] == shot_key)
    clip = _clip(require_scene(project, sequence.scene_id), segment["clipId"])
    previously_applied = session.scalar(select(MediaAdoption.id).where(MediaAdoption.project_id == project_id,
        MediaAdoption.scene_id == sequence.scene_id, MediaAdoption.clip_id == segment["clipId"], MediaAdoption.status == "applied"))
    if clip is None and (segment["replaceExisting"] or previously_applied):
        raise AppError(409, "GENERATION_TARGET_MISSING", "原目标片段已经删除，请选择新的目标")
    request = {"sceneId": sequence.scene_id, "sourceNodeId": segment["sourceNodeId"], "outputNodeId": segment["outputNodeId"],
        "prompt": segment["prompt"], "duration": segment["duration"], "resolution": segment["resolution"],
        "boundaryFrames": boundary, "reservedClipId": segment["clipId"],
        "operationId": hashlib.sha256(f"sequence:{body.operation_id}".encode()).hexdigest(),
        "modelProfileId": sequence.input_json.get("modelProfileId", "wan-3-0")}
    if clip is not None:
        request["clipId"] = clip["id"]
    generation = quote_generation(session, project_id, owner_id, request, settings, manage_transaction=False)
    save_operation(session, owner_id, project_id, body.operation_id, "media.segment.quote", payload, {"generationId": generation.id})
    session.commit()
    return generation
