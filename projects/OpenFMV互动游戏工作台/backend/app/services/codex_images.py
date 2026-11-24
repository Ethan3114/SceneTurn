import copy
import hashlib
import os
import time
from fractions import Fraction
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun
from app.codex_image_models import CodexImageJob
from app.connection_models import CodexConnection
from app.core.errors import AppError
from app.image_models import ImageJob
from app.models import Asset
from app.services.connections import ONLINE_WINDOW_SECONDS, assert_connection_active
from app.services.canvas_images import bind_output
from app.services.durable_files import persist_file
from app.services.image_queue import shared_image_request_busy, uses_codex_image
from app.services.material_references import validate_reference_assets
from app.services.media import StoredMedia, asset_view, resolve_asset_path
from app.services.projects import get_operation, iso_timestamp, lock_write, record_project_version, require_project, require_scene, save_operation


TERMINAL = {"ready", "failed", "stopped"}
MAX_IMAGE_BYTES = 32 * 1024 * 1024


def job_view(session: Session, job: CodexImageJob) -> dict:
    asset = session.get(Asset, job.asset_id) if job.asset_id else None
    return {"id": job.id, "projectId": job.project_id, "status": job.status,
        "name": job.input_json["name"], "prompt": job.input_json["prompt"],
        "referenceAssets": job.input_json["referenceAssets"], "transparentBackground": job.input_json["transparentBackground"],
        "requestedModel": "gpt-image-2", "connectionId": job.connection_id,
        "threadId": job.thread_id, "turnId": job.turn_id, "stopRequested": job.stop_requested,
        "asset": asset_view(asset) if asset else None, "costStatus": "personal_account_unknown",
        "error": {"code": job.error_code, "message": job.error_message} if job.error_code else None,
        "createdAt": iso_timestamp(job.created_at), "updatedAt": iso_timestamp(job.updated_at)}


def owned_job(session: Session, owner_id: str, project_id: str, identifier: str, *, include_material=False) -> CodexImageJob:
    require_project(session, project_id, owner_id)
    job = session.scalar(select(CodexImageJob).where(CodexImageJob.id == identifier, CodexImageJob.owner_id == owner_id, CodexImageJob.project_id == project_id))
    if job is None or not include_material and (job.input_json.get("materialRunId") or job.input_json.get("canvasImageJob")):
        raise AppError(404, "CODEX_IMAGE_NOT_FOUND", "图片任务不存在或无权访问")
    return job


def create_job(session: Session, owner_id: str, project_id: str, body: dict, settings) -> dict:
    lock_write(session)
    project = require_project(session, project_id, owner_id)
    replay = get_operation(session, owner_id, project_id, body["operationId"], "codex.image.create", body)
    if replay is not None:
        return job_view(session, owned_job(session, owner_id, project_id, replay["jobId"]))
    if project.revision != body["expectedRevision"]:
        raise AppError(409, "REVISION_CONFLICT", "项目已有编辑，请读取当前版本再创建图片任务")
    if not body["prompt"].strip():
        raise AppError(422, "CODEX_IMAGE_PROMPT_REQUIRED", "请填写图片要求")
    references = validate_reference_assets(session, project, body["referenceAssetIds"], settings)
    job = CodexImageJob(owner_id=owner_id, project_id=project_id,
        input_json={"prompt": body["prompt"], "name": body["name"], "referenceAssets": references,
            "transparentBackground": body["transparentBackground"], "projectRevision": project.revision,
            "protocolVersion": "codex-image-v1", "requestedModel": "gpt-image-2"})
    session.add(job)
    session.flush()
    save_operation(session, owner_id, project_id, body["operationId"], "codex.image.create", body, {"jobId": job.id})
    session.commit()
    return job_view(session, job)


def confirm_job(session: Session, owner_id: str, project_id: str, identifier: str, body: dict) -> dict:
    lock_write(session)
    job = owned_job(session, owner_id, project_id, identifier)
    payload = {"jobId": identifier, **body}
    replay = get_operation(session, owner_id, project_id, body["operationId"], "codex.image.confirm", payload)
    if replay is not None:
        return job_view(session, job)
    if job.status != "awaiting_confirmation":
        raise AppError(409, "CODEX_IMAGE_ALREADY_CONFIRMED", "图片任务已确认，请查看当前进度")
    connection = session.scalar(select(CodexConnection).where(CodexConnection.id == body["connectionId"], CodexConnection.owner_id == owner_id, CodexConnection.project_id == project_id))
    if connection is None:
        raise AppError(404, "CONNECTION_NOT_FOUND", "请选择本人当前项目的连接")
    assert_connection_active(connection)
    if "native_image_v1" not in connection.capabilities_json or time.time() - connection.last_seen_at > ONLINE_WINDOW_SECONDS:
        raise AppError(409, "CODEX_IMAGE_CONNECTION_UNAVAILABLE", "请启动支持图片任务的本人Codex连接并保持在线")
    if require_project(session, project_id, owner_id).revision != job.input_json["projectRevision"]:
        raise AppError(409, "REVISION_CONFLICT", "项目已有编辑，请重新核对当前图片任务")
    job.connection_id, job.status, job.updated_at = connection.id, "queued", time.time()
    job.result_json = {"accountUsageConfirmedAt": time.time(), "costStatus": "personal_account_unknown"}
    save_operation(session, owner_id, project_id, body["operationId"], "codex.image.confirm", payload, {"jobId": job.id})
    session.commit()
    return job_view(session, job)


def stop_job(session: Session, owner_id: str, project_id: str, identifier: str, operation_id: str) -> dict:
    lock_write(session)
    job = owned_job(session, owner_id, project_id, identifier)
    payload = {"jobId": identifier, "operationId": operation_id}
    if get_operation(session, owner_id, project_id, operation_id, "codex.image.stop", payload) is None:
        if job.status not in TERMINAL:
            job.stop_requested, job.status, job.updated_at = True, "stopped", time.time()
        save_operation(session, owner_id, project_id, operation_id, "codex.image.stop", payload, {"jobId": job.id})
        session.commit()
    return job_view(session, job)


def material_connection_status(session: Session, job: ImageJob) -> str:
    connection = session.get(CodexConnection, job.request_json.get("codexConnectionId"))
    now = time.time()
    if connection is None or (connection.owner_id, connection.project_id) != (job.owner_id, job.project_id) or connection.revoked_at is not None or connection.expires_at <= now:
        return "authorization_ended"
    if connection.last_seen_at < now - ONLINE_WINDOW_SECONDS or "native_image_v1" not in connection.capabilities_json:
        return "offline"
    return "online"


def _material_image(session: Session, job: CodexImageJob) -> ImageJob | None:
    run_id = job.input_json.get("materialRunId")
    if run_id is None and not job.input_json.get("canvasImageJob"):
        return None
    material = session.get(ImageJob, job.id)
    if (material is None or (material.owner_id, material.project_id, material.run_id) != (job.owner_id, job.project_id, run_id)
            or not uses_codex_image(material) or material.request_json.get("codexConnectionId") != job.connection_id):
        raise AppError(409, "CODEX_IMAGE_MATERIAL_SCOPE_INVALID", "图片任务与原素材制作记录不一致")
    return material


def _claim_material_job(session: Session, connection: CodexConnection, settings) -> CodexImageJob | None:
    if shared_image_request_busy(session):
        return None
    jobs = session.scalars(select(ImageJob).where(ImageJob.owner_id == connection.owner_id, ImageJob.project_id == connection.project_id,
        ImageJob.request_json["codexConnectionId"].as_string() == connection.id, ImageJob.status == "queued")
        .order_by(ImageJob.created_at, ImageJob.id)).all()
    for material in jobs:
        if not uses_codex_image(material) or material.confirmed_at is None:
            raise AppError(409, "CODEX_IMAGE_MATERIAL_NOT_CONFIRMED", "本机 Codex 图片任务需要原素材授权")
        if material.run_id is not None:
            run = session.get(AgentRun, material.run_id)
            if run is None or (run.owner_id, run.project_id, run.scene_id) != (material.owner_id, material.project_id, material.scene_id):
                raise AppError(409, "CODEX_IMAGE_MATERIAL_SCOPE_INVALID", "图片任务与原素材制作记录不一致")
            review = (run.result_json or {}).get("materialReview", {})
            if run.stop_requested or run.status != "waiting_confirmation" or review.get("status") != "generating" or material.id not in review.get("imageJobIds", []):
                material.status, material.updated_at, material.finished_at = "cancelled", time.time(), time.time()
                continue
        dependencies = [session.get(ImageJob, identifier) for identifier in material.dependency_ids]
        if any(item is None or (item.owner_id, item.project_id, item.run_id) != (material.owner_id, material.project_id, material.run_id) for item in dependencies):
            raise AppError(409, "CODEX_IMAGE_MATERIAL_SCOPE_INVALID", "上一帧记录不属于当前素材任务")
        if any(item.status in {"failed", "cancelled"} for item in dependencies):
            material.status, material.error_code = "cancelled", "CODEX_IMAGE_DEPENDENCY_FAILED"
            material.updated_at = material.finished_at = time.time()
            continue
        if any(item.status != "ready" or item.asset_id is None for item in dependencies):
            continue
        project = require_project(session, material.project_id, material.owner_id)
        require_scene(project, material.scene_id)
        request = material.request_json
        identifiers = list(dict.fromkeys([*material.reference_asset_ids, *[item.asset_id for item in dependencies]]))
        try:
            references = validate_reference_assets(session, project, identifiers, settings, request.get("referenceAssets"))
        except AppError as error:
            material.status, material.error_code = "failed", error.code
            material.updated_at = material.finished_at = time.time()
            continue
        ratio = Fraction(request["width"], request["height"])
        prompt = f'{request["prompt"]}\n\n输出一张完整画面，画幅 {ratio.numerator}:{ratio.denominator}，主体与关键细节应保留在画面内。'
        # 使用素材任务标识关联本机回合，重启后仍恢复同一图片任务。
        job = CodexImageJob(id=material.id, owner_id=material.owner_id, project_id=material.project_id, connection_id=connection.id,
            status="queued", input_json={"prompt": prompt, "name": material.label[:150], "referenceAssets": references,
                "transparentBackground": False, "projectRevision": project.revision, "materialRunId": material.run_id,
                "canvasImageJob": material.run_id is None,
                "outputSize": [request["width"], request["height"]], "protocolVersion": "codex-image-v1", "requestedModel": "gpt-image-2"},
            result_json={"accountUsageConfirmedAt": material.confirmed_at, "costStatus": "personal_account_unknown"})
        session.add(job)
        return job
    return None


def stop_material_images(session: Session, run: AgentRun) -> None:
    native_jobs = session.scalars(select(CodexImageJob).join(ImageJob, ImageJob.id == CodexImageJob.id).where(
        ImageJob.run_id == run.id, CodexImageJob.owner_id == run.owner_id, CodexImageJob.project_id == run.project_id,
        CodexImageJob.status.not_in(TERMINAL))).all()
    for job in native_jobs:
        material = _material_image(session, job)
        if material:
            now = time.time()
            job.stop_requested, job.status, job.updated_at = True, "stopped", now
            material.status, material.finished_at, material.updated_at = "cancelled", now, now


def connection_job(session: Session, connection: CodexConnection, identifier: str, completed: bool = False) -> CodexImageJob:
    session.refresh(connection)
    assert_connection_active(connection)
    job = owned_job(session, connection.owner_id, connection.project_id, identifier, include_material=True)
    session.refresh(job)
    if job.connection_id != connection.id or "native_image_v1" not in connection.capabilities_json:
        raise AppError(403, "CODEX_IMAGE_SCOPE_INVALID", "连接没有此图片任务的授权")
    if completed and job.status == "ready":
        return job
    if job.status in TERMINAL or job.lease_expires_at is None or job.lease_expires_at <= time.time():
        raise AppError(409, "CODEX_IMAGE_LEASE_LOST", "图片执行授权已变化，请重新读取当前任务")
    return job


def claim_job(session: Session, connection: CodexConnection, settings=None) -> dict | None:
    lock_write(session)
    session.refresh(connection)
    assert_connection_active(connection)
    if "native_image_v1" not in connection.capabilities_json:
        raise AppError(403, "CODEX_IMAGE_CAPABILITY_REQUIRED", "当前连接程序没有报告图片任务能力")
    job = session.scalar(select(CodexImageJob).where(CodexImageJob.connection_id == connection.id,
        CodexImageJob.owner_id == connection.owner_id, CodexImageJob.project_id == connection.project_id,
        CodexImageJob.status.in_(["queued", "running", "waiting_connection", "submission_unknown"]))
        .order_by(CodexImageJob.status == "queued", CodexImageJob.created_at, CodexImageJob.id).limit(1))
    if job is None:
        job = _claim_material_job(session, connection, settings)
    if job is None:
        session.commit()
        return None
    now = time.time()
    job.status, job.lease_expires_at, job.updated_at = "running", now + 90, now
    material = _material_image(session, job)
    if material:
        material.status = "submission_unknown" if job.result_json.get("submissionAttempted") and not job.turn_id else "submitting"
        material.updated_at = now
    connection.last_seen_at = now
    session.commit()
    return {"job": job_view(session, job), "submissionAttempted": bool(job.result_json.get("submissionAttempted")),
        "imageCompleted": bool(job.result_json.get("imageCompleted")), "itemId": job.item_id,
        "protocolVersion": "codex-image-v1"}


def event_job(session: Session, connection: CodexConnection, identifier: str, body: dict) -> dict:
    lock_write(session)
    job = connection_job(session, connection, identifier, completed=True)
    if job.status == "ready":
        if body.get("threadId") != job.thread_id or body.get("turnId") != job.turn_id:
            raise AppError(409, "CODEX_IMAGE_RESULT_MISMATCH", "图片进度不属于已保存的会话和回合")
        if body.get("usage") is not None:
            job.result_json = {**job.result_json, "usage": body["usage"]}
            session.commit()
        return job_view(session, job)
    kind = body["eventType"]
    material = _material_image(session, job)
    for name, attr in (("threadId", "thread_id"), ("turnId", "turn_id"), ("itemId", "item_id")):
        value = body.get(name)
        previous = getattr(job, attr)
        if name == "threadId" and kind == "thread_started" and not job.result_json.get("submissionAttempted") and not job.turn_id and not job.item_id:
            previous = None
        if value and previous and value != previous:
            job.status, job.error_code, job.error_message = "submission_unknown", "CODEX_IMAGE_IDENTITY_CHANGED", "同一图片任务报告了不同的会话或图片调用，已停止自动执行"
            if material:
                material.status, material.error_code, material.updated_at = "submission_unknown", job.error_code, time.time()
            session.commit()
            raise AppError(409, "CODEX_IMAGE_IDENTITY_CHANGED", job.error_message)
        if value:
            setattr(job, attr, value)
    if kind == "turn_requested":
        if not job.thread_id:
            raise AppError(409, "CODEX_IMAGE_THREAD_REQUIRED", "请先保存真实图片线程")
        if job.result_json.get("submissionAttempted"):
            raise AppError(409, "CODEX_IMAGE_ALREADY_SUBMITTED", "此任务已经请求过本机回合，只能恢复原请求")
        if job.status != "running":
            raise AppError(409, "CODEX_IMAGE_STATE_CHANGED", "当前状态不允许提交新的图片回合")
        job.result_json = {**job.result_json, "submissionAttempted": True}
    if kind in {"image_started", "image_completed"} and (not job.thread_id or not job.turn_id or not job.item_id or not job.result_json.get("submissionAttempted")):
        raise AppError(409, "CODEX_IMAGE_EVENT_ORDER_INVALID", "图片事件缺少已保存的线程、回合或调用标识")
    if kind == "image_completed":
        expected = sorted(item["sha256"] for item in job.input_json["referenceAssets"])
        actual = sorted(item.get("sha256", "") for item in body["inputImages"])
        if expected != actual:
            raise AppError(409, "CODEX_IMAGE_REFERENCE_MISMATCH", "图片实际引用与本人确认的参考素材不一致，原任务已保留")
        job.result_json = {**job.result_json, "imageCompleted": True, "inputImageHashes": actual}
    if body.get("usage") is not None:
        job.result_json = {**job.result_json, "usage": body["usage"]}
    if kind in {"failed", "interrupted", "disconnected"}:
        job.status = "failed" if kind == "failed" else "stopped" if kind == "interrupted" else "waiting_connection"
        job.error_code = body.get("errorCode") or "CODEX_IMAGE_CONNECTION_INTERRUPTED"
        job.error_message = "本机图片进度已保存，请核对连接；未知请求不会自动重新提交"
    if kind == "turn_completed" and not job.result_json.get("imageCompleted"):
        job.status, job.error_code, job.error_message = "failed", "CODEX_IMAGE_RESULT_MISSING", "Codex回合已结束，但没有可核验的图片生成结果"
    job.updated_at, job.lease_expires_at, connection.last_seen_at = time.time(), time.time() + 90, time.time()
    if material:
        if kind == "turn_requested":
            material.submitted_at, material.attempt_count = job.updated_at, 1
        material.status = {"stopped": "cancelled", "failed": "failed", "waiting_connection": "waiting_connection"}.get(job.status, "submitting")
        if job.status == "waiting_connection" and job.result_json.get("submissionAttempted") and not job.turn_id:
            material.status = "submission_unknown"
        material.error_code, material.updated_at = job.error_code, job.updated_at
        if job.status in TERMINAL:
            material.finished_at = job.updated_at
    session.commit()
    return job_view(session, job)


def check_result(session: Session, connection: CodexConnection, identifier: str, metadata: dict) -> CodexImageJob:
    job = connection_job(session, connection, identifier, completed=True)
    if (metadata["threadId"], metadata["turnId"], metadata["itemId"]) != (job.thread_id, job.turn_id, job.item_id):
        raise AppError(409, "CODEX_IMAGE_RESULT_MISMATCH", "图片不属于已保存的本次会话和调用")
    if not job.result_json.get("imageCompleted"):
        raise AppError(409, "CODEX_IMAGE_RESULT_UNCONFIRMED", "尚无实际图片完成事件")
    if job.asset_id:
        asset = session.get(Asset, job.asset_id)
        if asset is None or (asset.owner_id, asset.project_id) != (job.owner_id, job.project_id) or job.result_json.get("sourceSha256", asset.sha256) != metadata["sha256"]:
            raise AppError(409, "CODEX_IMAGE_RESULT_CHANGED", "同一图片调用不能替换已保存的图片")
    return job


def prepare_result_media(job: CodexImageJob, metadata: dict, media: StoredMedia, settings) -> StoredMedia:
    if media.sha256 != metadata["sha256"] or media.mime_type not in {"image/png", "image/jpeg", "image/webp"} or media.bytes > MAX_IMAGE_BYTES:
        raise AppError(422, "CODEX_IMAGE_BYTES_INVALID", "图片字节、格式或校验值无效")
    size = job.input_json.get("outputSize")
    if size is None:
        return media
    with Image.open(resolve_asset_path(settings, media.relpath)) as original:
        if original.width * original.height > 64_000_000:
            raise AppError(422, "CODEX_IMAGE_DIMENSIONS_INVALID", "本机 Codex 返回的图片像素超出允许范围")
        upright = ImageOps.exif_transpose(original)
        if list(upright.size) == size and upright.mode == "RGB" and original.getexif().get(274, 1) == 1:
            return media
        image = ImageOps.fit(upright.convert("RGBA"), tuple(size), Image.Resampling.LANCZOS)
        flattened = Image.new("RGB", tuple(size), "white")
        flattened.paste(image, mask=image.getchannel("A"))
        with NamedTemporaryFile(mode="w+b", suffix=".png", dir=settings.data_dir / "media", delete=False) as output:
            destination = Path(output.name)
            try:
                flattened.save(output, format="PNG")
                output.flush()
                os.fsync(output.fileno())
                written = output.tell()
                output.seek(0)
                digest = hashlib.file_digest(output, "sha256").hexdigest()
            except OSError:
                destination.unlink(missing_ok=True)
                raise
    persisted = False
    try:
        persist_file(settings, destination, "image/png")
        persisted = True
    finally:
        if not persisted:
            destination.unlink(missing_ok=True)
    return StoredMedia(name=media.name, relpath=destination.relative_to(settings.data_dir).as_posix(), poster_relpath="",
        mime_type="image/png", bytes=written, sha256=digest, duration_ms=0, width=size[0], height=size[1])


def persist_result(session: Session, connection: CodexConnection, identifier: str, metadata: dict, source: StoredMedia, media: StoredMedia) -> dict:
    lock_write(session)
    job = check_result(session, connection, identifier, metadata)
    if job.asset_id:
        session.commit()
        return job_view(session, job)
    if source.sha256 != metadata["sha256"] or source.mime_type not in {"image/png", "image/jpeg", "image/webp"} or source.bytes > MAX_IMAGE_BYTES:
        raise AppError(422, "CODEX_IMAGE_BYTES_INVALID", "图片字节、格式或校验值无效")
    project = require_project(session, job.project_id, job.owner_id)
    provenance = {"provider": "codex-local", "codexImageJobId": job.id, "threadId": job.thread_id, "turnId": job.turn_id,
        "itemId": job.item_id, "costStatus": "personal_account_unknown", "source": metadata["source"]}
    material = _material_image(session, job)
    run = None
    if material:
        if material.run_id is not None:
            run = session.get(AgentRun, material.run_id)
            if run is None or (run.project_id, run.owner_id, run.scene_id) != (material.project_id, material.owner_id, material.scene_id) or run.stop_requested:
                raise AppError(409, "CODEX_IMAGE_MATERIAL_SCOPE_INVALID", "图片任务与原素材制作记录不一致")
            require_scene(project, material.scene_id)
        if [media.width, media.height] != job.input_json["outputSize"]:
            raise AppError(422, "CODEX_IMAGE_DIMENSIONS_INVALID", "图片与已确认的交付尺寸不符")
        provenance.update(imageJobId=material.id, frameKey=material.frame_key, model=material.request_json["modelProfile"]["model"],
            sourceSha256=source.sha256, sourceWidth=source.width, sourceHeight=source.height)
    asset = Asset(owner_id=job.owner_id, project_id=job.project_id, generation_id=None, name=job.input_json["name"],
        relpath=media.relpath, poster_relpath=media.poster_relpath, mime_type=media.mime_type, bytes=media.bytes,
        sha256=media.sha256, width=media.width, height=media.height, duration_ms=0, metadata_json=provenance)
    session.add(asset)
    session.flush()
    snapshot = copy.deepcopy(project.data_json)
    snapshot["assets"].append({"id": asset.id, "type": "image", "name": asset.name, "path": f"/api/v1/assets/{asset.id}/content",
        "relativePath": f"assets/{asset.id}", "importedAt": iso_timestamp(time.time()),
        "metadata": {**provenance, "duration": 0, "width": asset.width, "height": asset.height,
            **({"poster": f"/api/v1/assets/{asset.id}/poster"} if media.poster_relpath else {})}})
    snapshot["updatedAt"] = iso_timestamp(time.time())
    if material and material.run_id is None:
        bind_output(session, project, material, asset)
    project.data_json, project.revision, project.updated_at = snapshot, project.revision + 1, time.time()
    record_project_version(session, project)
    job.asset_id, job.status, job.updated_at = asset.id, "ready", time.time()
    job.error_code, job.error_message = None, None
    job.result_json = {**job.result_json, "sha256": media.sha256, "sourceSha256": source.sha256, "bytes": media.bytes, "width": media.width, "height": media.height}
    if material:
        if run and run.project_revision == project.revision - 1:
            run.project_revision = project.revision
        material.asset_id, material.status, material.error_code = asset.id, "ready", None
        material.updated_at = material.finished_at = job.updated_at
    session.commit()
    return job_view(session, job)
