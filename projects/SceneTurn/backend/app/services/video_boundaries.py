import base64
import hashlib
import io

from PIL import Image
from sqlalchemy import select

from app.agent_models import AgentRun
from app.core.errors import AppError
from app.image_models import ImageJob
from app.models import Asset, Canvas
from app.services.media import resolve_asset_path
from app.services.projects import payload_digest
from app.services.material_references import reusable_boundary_frames


def _approved_review(run, storyboard_node_id, artifact_hash):
    reviews = [*(run.result_json or {}).get("materialReviewHistory", []), (run.result_json or {}).get("materialReview", {})]
    return next((review for review in reversed(reviews) if review.get("nodeId") == storyboard_node_id and review.get("artifactHash") == artifact_hash and review.get("status") == "approved"), None)


def _storyboard_artifact(session, run, storyboard_node_id):
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == run.project_id, Canvas.scene_id == run.scene_id, Canvas.owner_id == run.owner_id))
    node = next((item for item in canvas.data_json["nodes"] if item["id"] == storyboard_node_id), None) if canvas else None
    artifact = node.get("metadata", {}).get("creativeArtifact") if node else None
    if not artifact or artifact.get("role") != "storyboarder" or artifact.get("schemaVersion") not in {2, 3}:
        raise AppError(422, "VIDEO_BOUNDARIES_REQUIRED", "请先完成连续首尾帧分镜")
    return artifact


def approved_storyboard_source(session, run, storyboard_node_id, artifact_hash):
    approved = _approved_review(run, storyboard_node_id, artifact_hash)
    if approved:
        return run, approved
    previous = session.scalars(select(AgentRun).where(AgentRun.owner_id == run.owner_id,
        AgentRun.project_id == run.project_id, AgentRun.scene_id == run.scene_id, AgentRun.id != run.id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()))
    for source in previous:
        approved = _approved_review(source, storyboard_node_id, artifact_hash)
        if approved:
            return source, approved
    return None, None


def approved_video_segments(session, run, storyboard_node_id, artifact):
    digest = payload_digest(artifact)
    source, approved = approved_storyboard_source(session, run, storyboard_node_id, digest)
    if source is None:
        return []
    assets = {frame["frameKey"]: frame["assetId"] for frame in approved.get("reusedFrames", [])}
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == source.id, ImageJob.owner_id == run.owner_id,
        ImageJob.node_id == storyboard_node_id, ImageJob.artifact_hash == digest, ImageJob.status == "ready"))
    assets.update((job.frame_key, job.asset_id) for job in jobs if job.asset_id)
    return [{"storyboardNodeId": storyboard_node_id, "shotKey": shot["key"], "durationSeconds": shot["durationSeconds"],
        "firstFrameAssetId": assets[shot["firstFrameKey"]], "lastFrameAssetId": assets[shot["lastFrameKey"]]}
        for shot in artifact["shots"] if shot["firstFrameKey"] in assets and shot["lastFrameKey"] in assets]


def freeze_adopted_boundaries(session, run, storyboard_node_id, shot_key, settings):
    artifact = _storyboard_artifact(session, run, storyboard_node_id)
    source, _ = approved_storyboard_source(session, run, storyboard_node_id, payload_digest(artifact))
    if source is None:
        raise AppError(409, "VIDEO_FRAMES_UNCONFIRMED", "请先确认当前分镜图片成果")
    return freeze_boundaries(session, source, storyboard_node_id, shot_key, settings)


def freeze_boundaries(session, run, storyboard_node_id, shot_key, settings):
    artifact = _storyboard_artifact(session, run, storyboard_node_id)
    artifact_hash = payload_digest(artifact)
    approved = _approved_review(run, storyboard_node_id, artifact_hash)
    if approved is None:
        raise AppError(409, "VIDEO_FRAMES_UNCONFIRMED", "请先确认当前分镜图片成果")
    reused = reusable_boundary_frames(session, run, artifact, settings)
    if reused != approved.get("reusedFrames", []):
        raise AppError(409, "STORYBOARD_EXISTING_FRAME_CHANGED", "复用边界图片与已确认版本不一致")
    reused_by_key = {frame["frameKey"]: frame for frame in reused}
    shot = next((item for item in artifact["shots"] if item["key"] == shot_key), None)
    if shot is None:
        raise AppError(422, "VIDEO_SEGMENT_INVALID", "视频片段不属于当前分镜")
    jobs = session.scalars(select(ImageJob).where(ImageJob.owner_id == run.owner_id, ImageJob.run_id == run.id,
        ImageJob.node_id == storyboard_node_id, ImageJob.artifact_hash == artifact_hash, ImageJob.status == "ready")).all()
    by_key = {job.frame_key: job for job in jobs}
    frames = []
    for kind, key in (("first_frame", shot["firstFrameKey"]), ("last_frame", shot["lastFrameKey"])):
        job = by_key.get(key)
        existing = reused_by_key.get(key)
        asset = session.get(Asset, existing["assetId"] if existing else job.asset_id) if existing or job else None
        if asset is None or asset.owner_id != run.owner_id or asset.project_id != run.project_id:
            raise AppError(409, "VIDEO_FRAME_NOT_READY", "分镜图片尚未生成完成")
        raw = resolve_asset_path(settings, asset.relpath).read_bytes()
        if len(raw) > 20 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != asset.sha256:
            raise AppError(409, "VIDEO_FRAME_INVALID", "分镜图片内容或大小不符合要求")
        with Image.open(io.BytesIO(raw)) as image:
            if image.mode not in {"RGB", "L"} or not (240 <= min(image.size) and max(image.size) <= 8000):
                raise AppError(422, "VIDEO_FRAME_FORMAT_INVALID", "首尾帧必须为有效的不透明图片")
            image.verify()
        frames.append({"type": kind, "frameKey": key, "assetId": asset.id, "sha256": asset.sha256})
    return {"runId": run.id, "storyboardNodeId": storyboard_node_id, "artifactHash": artifact_hash,
        "shotKey": shot_key, "duration": shot["durationSeconds"], "frames": frames}


def validate_frozen_boundaries(session, owner_id, frozen, settings):
    if frozen.get("sourceType") == "media_sequence":
        from app.media_models import MediaSequence
        from app.services.media_sequences import freeze_sequence
        sequence = session.get(MediaSequence, frozen["sequenceId"])
        if sequence is None or sequence.owner_id != owner_id:
            raise AppError(404, "VIDEO_BOUNDARIES_NOT_FOUND", "首尾帧来源不存在")
        if freeze_sequence(session, sequence, frozen["shotKey"], settings) != frozen:
            raise AppError(409, "VIDEO_BOUNDARIES_CHANGED", "首尾图片或视频序列已经变化，请重新确认")
        return
    run = session.get(AgentRun, frozen["runId"])
    if run is None or run.owner_id != owner_id:
        raise AppError(404, "VIDEO_BOUNDARIES_NOT_FOUND", "首尾帧来源不存在")
    current = freeze_boundaries(session, run, frozen["storyboardNodeId"], frozen["shotKey"], settings)
    if current != frozen:
        raise AppError(409, "VIDEO_BOUNDARIES_CHANGED", "分镜首尾帧已有更新，请重新确认视频任务")


def media_payload(session, owner_id, frozen, settings):
    validate_frozen_boundaries(session, owner_id, frozen, settings)
    return [{"type": item["type"], "url": f"data:{asset.mime_type};base64,{base64.b64encode(resolve_asset_path(settings, asset.relpath).read_bytes()).decode()}"}
        for item in frozen["frames"] for asset in [session.get(Asset, item["assetId"])]]
