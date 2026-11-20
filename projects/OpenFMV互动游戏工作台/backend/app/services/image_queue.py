import asyncio
import binascii
import copy
import hashlib
import json
import logging
import os
import time
from pathlib import Path

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError

from app.agent_models import AgentRun
from app.core.errors import AppError
from app.core.security import decode_secret
from app.image_models import ImageAccount, ImageJob
from app.models import Asset, new_id
from app.services.media import resolve_asset_path
from app.services.material_references import validate_reference_assets
from app.services.projects import iso_timestamp, lock_write, payload_digest, record_project_version, require_project, require_scene
from app.services.sensetime import IMAGE_RECEIPT_TIMEOUT_SECONDS, MAX_IMAGE_BYTES, MODEL, decode_image, submit_image


logger = logging.getLogger("openfmv")
IMAGE_REQUEST_GAP_SECONDS = 5


def require_image_worker(task):
    if task is not None and task.done() and (task.cancelled() or task.exception() is not None):
        raise AppError(503, "IMAGE_WORKER_FAILED", "图片处理服务已停止，请先恢复已保存的图片")


def image_worker_done(task):
    if task.cancelled():
        logger.error("图片处理服务已停止，错误类型：CancelledError")
    elif (error := task.exception()) is not None:
        logger.error("图片处理服务已停止，错误类型：%s", type(error).__name__)


async def wait_image_worker(task):
    try:
        await task
    except (Exception, asyncio.CancelledError):
        raise AppError(503, "IMAGE_WORKER_FAILED", "图片处理服务已停止，请先恢复已保存的图片") from None


def public_job(job):
    return {"id": job.id, "nodeId": job.node_id, "label": job.label, "frameKey": job.frame_key,
        "status": job.status, "assetId": job.asset_id,
        "width": job.request_json["width"], "height": job.request_json["height"],
        "errorCode": job.error_code}


def image_queue_awaits_result(session):
    return session.scalar(select(ImageAccount.id).where(
        ImageAccount.status == "quarantined", ImageAccount.active_job_id.is_not(None)).limit(1)) is not None


def _release_expired_requests(session, now):
    expired = session.execute(select(ImageAccount, ImageJob).join(ImageJob, ImageAccount.active_job_id == ImageJob.id).where(
        ImageJob.status == "submission_unknown",
        func.coalesce(ImageJob.submitted_at, ImageJob.updated_at) <= now - IMAGE_RECEIPT_TIMEOUT_SECONDS)).all()
    for account, job in expired:
        if (job.account_id, job.owner_id) != (account.id, account.owner_id):
            raise ValueError("图片账号占用记录损坏")
        attempts = job.request_json.get("expiredAttempts", [])
        job.request_json = {**job.request_json, "expiredAttempts": [*attempts, {
            "accountId": account.id, "attempt": job.attempt_count, "submittedAt": job.submitted_at,
            "releasedAt": now, "errorCode": job.error_code}]}
        account.active_job_id, account.status = None, "available"
        run = session.get(AgentRun, job.run_id)
        job.status = "queued"
        if run.stop_requested or run.status == "stopped":
            job.status = "cancelled"
        elif run.status in {"completed", "failed"}:
            job.status = "failed"
        job.error_code, job.updated_at = "IMAGE_RESPONSE_TIMEOUT", now
        job.finished_at = now if job.status != "queued" else None
    if expired:
        session.flush()


def claim_job(session):
    lock_write(session)
    _release_expired_requests(session, time.time())
    if session.scalar(select(ImageAccount.id).where(ImageAccount.active_job_id.is_not(None)).limit(1)) is not None:
        session.commit()
        return None
    queued = session.scalars(select(ImageJob).where(ImageJob.status == "queued").order_by(ImageJob.created_at, ImageJob.id)).all()
    if queued:
        last_result = session.scalar(select(func.max(func.coalesce(ImageJob.finished_at, ImageJob.updated_at))).where(
            ImageJob.submitted_at.is_not(None),
            or_(ImageJob.finished_at.is_not(None), ImageJob.status.in_(("queued", "cancelled")))))
        if last_result is not None and time.time() < last_result + IMAGE_REQUEST_GAP_SECONDS:
            session.commit()
            return None
    for job in queued:
        if session.get(AgentRun, job.run_id).stop_requested:
            job.status = "cancelled"
            continue
        dependencies = [session.get(ImageJob, identifier) for identifier in job.dependency_ids]
        if any(item is None or item.owner_id != job.owner_id or item.run_id != job.run_id for item in dependencies):
            raise ValueError("图片依赖不属于当前任务")
        if any(item.status != "ready" for item in dependencies):
            continue
        timed_out_accounts = {attempt["accountId"] for attempt in job.request_json.get("expiredAttempts", [])}
        candidates = select(ImageAccount).where(ImageAccount.owner_id == job.owner_id,
            ImageAccount.status == "available", ImageAccount.active_job_id.is_(None))
        if timed_out_accounts:
            candidates = candidates.where(ImageAccount.id.not_in(timed_out_accounts))
        account = session.scalar(candidates.order_by(ImageAccount.last_assigned_at, ImageAccount.slot).limit(1))
        if account is None:
            if timed_out_accounts:
                job.status, job.finished_at = "failed", job.updated_at
                untried = session.scalar(select(ImageAccount.id).where(ImageAccount.owner_id == job.owner_id,
                    ImageAccount.id.not_in(timed_out_accounts)).limit(1))
                if untried is not None:
                    job.error_code = "IMAGE_ALTERNATE_ACCOUNT_UNAVAILABLE"
            continue
        account.active_job_id, account.last_assigned_at = job.id, time.time()
        job.account_id, job.status, job.updated_at = account.id, "claimed", time.time()
        session.commit()
        return job.id
    session.commit()
    return None


def recover_jobs(session, settings=None):
    lock_write(session)
    for account in session.scalars(select(ImageAccount).where(ImageAccount.active_job_id.is_not(None))):
        job = session.get(ImageJob, account.active_job_id)
        if job is None or job.account_id != account.id:
            raise ValueError("图片账号占用记录损坏")
        if job.status == "claimed":
            job.status, job.account_id, account.active_job_id = "queued", None, None
        elif job.status in {"submitting", "submission_unknown"}:
            saved_path = settings.data_dir.resolve() / "image-responses" / f"{job.id}.json" if settings else None
            if saved_path and saved_path.is_file():
                job.status, job.response_relpath = "response_saved", f"image-responses/{job.id}.json"
                account.status = "available"
            elif job.status == "submitting":
                job.status, job.error_code = "submission_unknown", "IMAGE_SUBMISSION_UNKNOWN"
                account.status = "quarantined"
    session.commit()


def _finish_failure(session, job, code, *, unknown=False, quota=False):
    account = session.get(ImageAccount, job.account_id)
    session.refresh(account)
    if account.active_job_id != job.id:
        raise ValueError("图片任务已经失去账号占用权")
    job.status = "submission_unknown" if unknown else "failed"
    job.error_code, job.updated_at = code, time.time()
    job.finished_at = None if unknown else time.time()
    if unknown:
        account.status = "quarantined"
    else:
        account.active_job_id = None
        account.status = "quota_exhausted" if quota else "available"
    session.commit()


def _private_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def finalize_response(session, job, settings):
    lock_write(session)
    session.refresh(job)
    if job.status not in {"response_saved", "ready"}:
        raise ValueError("图片任务状态已改变")
    project = require_project(session, job.project_id, job.owner_id)
    session.refresh(project)
    require_scene(project, job.scene_id)
    run = session.get(AgentRun, job.run_id)
    session.refresh(run)
    if (run.owner_id, run.project_id, run.scene_id) != (job.owner_id, job.project_id, job.scene_id):
        raise ValueError("图片任务归属与创作任务不一致")
    expected_path = settings.data_dir.resolve() / "image-responses" / f"{job.id}.json"
    if (settings.data_dir.resolve() / job.response_relpath).resolve() != expected_path:
        raise ValueError("图片响应文件位置无效")
    response = json.loads(expected_path.read_bytes())
    raw = decode_image(response, job.request_json["width"], job.request_json["height"])
    relpath = f"media/images/{job.id}.png"
    image_path = settings.data_dir.resolve() / relpath
    if job.status == "ready":
        asset = session.get(Asset, job.asset_id)
        if (asset is None or (asset.owner_id, asset.project_id, asset.relpath, asset.mime_type) != (job.owner_id, job.project_id, relpath, "image/png")
                or asset.sha256 != hashlib.sha256(raw).hexdigest() or asset.bytes != len(raw)
                or (asset.width, asset.height) != (job.request_json["width"], job.request_json["height"])
                or not image_path.is_file() or image_path.read_bytes() != raw):
            raise ValueError("已登记图片与原响应不一致")
        session.commit()
        return
    account = session.get(ImageAccount, job.account_id)
    if account is None:
        raise ValueError("图片任务没有账号占用记录")
    session.refresh(account)
    if account.owner_id != job.owner_id or account.active_job_id != job.id or account.status != "available":
        raise ValueError("图片任务已经失去账号占用权")
    frozen = job.request_json.get("referenceAssets", [])
    if frozen:
        validate_reference_assets(session, project, [item["assetId"] for item in frozen], settings, frozen)
    if image_path.exists():
        if image_path.read_bytes() != raw:
            raise ValueError("持久图片与响应不一致")
    else:
        _private_write(image_path, raw)
    asset_id = new_id()
    asset = Asset(id=asset_id, owner_id=job.owner_id, project_id=job.project_id,
        generation_id=None, name=f"{job.label[:180]}.png", relpath=relpath, poster_relpath=None,
        mime_type="image/png", bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
        duration_ms=None, width=job.request_json["width"], height=job.request_json["height"],
        metadata_json={"provider": "sensetime", "model": MODEL, "imageJobId": job.id,
            "billingSource": "user_confirmed_free_token_plan", "frameKey": job.frame_key})
    session.add(asset)
    session.flush()
    snapshot = copy.deepcopy(project.data_json)
    snapshot["assets"].append({"id": asset_id, "type": "image", "name": job.label,
        "path": f"/api/v1/assets/{asset_id}/content", "relativePath": f"assets/{asset_id}",
        "importedAt": iso_timestamp(time.time()), "metadata": {"width": job.request_json["width"], "height": job.request_json["height"]}})
    snapshot["updatedAt"] = iso_timestamp(time.time())
    if run.project_revision == project.revision:
        run.project_revision += 1
    project.data_json, project.revision, project.updated_at = snapshot, project.revision + 1, time.time()
    job.asset_id, job.status, job.updated_at, job.error_code = asset_id, "ready", time.time(), None
    account.active_job_id = None
    session.flush()
    record_project_version(session, project)
    job.finished_at = time.time()
    session.commit()


def _submission_digest(job):
    return payload_digest({"request": job.request_json, "referenceAssetIds": job.reference_asset_ids,
        "dependencyIds": job.dependency_ids, "artifactHash": job.artifact_hash})


def _submitted_job(session, job_id, submission):
    job = session.get(ImageJob, job_id)
    account = session.get(ImageAccount, submission["accountId"])
    if (job is None or account is None or job.status != "submitting" or job.account_id != account.id
            or account.owner_id != job.owner_id or account.active_job_id != job.id or account.status != "available"
            or job.attempt_count != submission["attemptCount"] or job.submitted_at != submission["submittedAt"]
            or _submission_digest(job) != submission["inputSha256"]):
        raise ValueError("图片提交状态或账号占用已经变化")
    return job, account


def _finalize_saved(job_id, settings, session_factory):
    with session_factory() as session:
        finalize_response(session, session.get(ImageJob, job_id), settings)


async def _execute_job(job_id, settings, session_factory):
    with session_factory() as session:
        lock_write(session)
        job = session.get(ImageJob, job_id)
        if job.status == "cancelled":
            session.commit()
            return
        if job.status in {"response_saved", "ready"}:
            session.commit()
            saved = True
        else:
            saved = False
    if saved:
        _finalize_saved(job_id, settings, session_factory)
        return
    with session_factory() as session:
        lock_write(session)
        job = session.get(ImageJob, job_id)
        account = session.get(ImageAccount, job.account_id)
        if job.status != "claimed" or account.active_job_id != job.id:
            raise ValueError("图片任务未持有独占账号")
        frozen = job.request_json.get("referenceAssets", [])
        if frozen:
            project = require_project(session, job.project_id, job.owner_id)
            validate_reference_assets(session, project, [item["assetId"] for item in frozen], settings, frozen)
        reference_ids = [*job.reference_asset_ids]
        for identifier in job.dependency_ids:
            dependency = session.get(ImageJob, identifier)
            if dependency.status != "ready" or not dependency.asset_id:
                raise ValueError("参考图片尚未生成")
            reference_ids.append(dependency.asset_id)
        references = []
        for identifier in dict.fromkeys(reference_ids):
            asset = session.get(Asset, identifier)
            if asset is None or asset.owner_id != job.owner_id or asset.project_id != job.project_id or not asset.mime_type.startswith("image/") or asset.bytes > MAX_IMAGE_BYTES:
                raise ValueError("参考图片无法使用")
            raw = resolve_asset_path(settings, asset.relpath).read_bytes()
            if len(raw) != asset.bytes or len(raw) > MAX_IMAGE_BYTES or hashlib.sha256(raw).hexdigest() != asset.sha256:
                raise ValueError("参考图片内容发生改变")
            references.append((asset.mime_type, raw))
        if len(references) > 5:
            raise ValueError("参考图片超过5张")
        request, key = copy.deepcopy(job.request_json), decode_secret(account.api_key_encrypted, settings)
        job.status, job.updated_at, job.attempt_count = "submitting", time.time(), job.attempt_count + 1
        job.error_code = None
        job.submitted_at = time.time()
        submission = {"accountId": account.id, "attemptCount": job.attempt_count, "submittedAt": job.submitted_at,
            "inputSha256": _submission_digest(job)}
        session.commit()
    try:
        async with asyncio.timeout(IMAGE_RECEIPT_TIMEOUT_SECONDS):
            response = await submit_image(key, request, references)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        with session_factory() as session:
            lock_write(session)
            job, account = _submitted_job(session, job_id, submission)
            if job.attempt_count < 2:
                job.status, job.account_id = "queued", None
                job.updated_at = time.time()
                account.active_job_id = None
                session.commit()
            else:
                _finish_failure(session, job, "IMAGE_CONNECTION_FAILED")
        return
    except (TimeoutError, httpx.TransportError) as error:
        code = "IMAGE_RESPONSE_TIMEOUT" if isinstance(error, (TimeoutError, httpx.ReadTimeout)) else "IMAGE_SUBMISSION_UNKNOWN"
        with session_factory() as session:
            lock_write(session)
            job, _account = _submitted_job(session, job_id, submission)
            _finish_failure(session, job, code, unknown=True)
        return
    if response.status_code >= 400:
        with session_factory() as session:
            lock_write(session)
            job, _account = _submitted_job(session, job_id, submission)
            _finish_failure(session, job, "IMAGE_QUOTA_OR_RATE_LIMIT" if response.status_code in {402, 429} else "IMAGE_PROVIDER_REJECTED",
                unknown=response.status_code >= 500, quota=response.status_code in {402, 429})
        return
    with session_factory() as session:
        lock_write(session)
        job, _account = _submitted_job(session, job_id, submission)
        response_relpath = f"image-responses/{job.id}.json"
        response_path = settings.data_dir.resolve() / response_relpath
        if response_path.exists():
            if response_path.read_bytes() != response.content:
                raise ValueError("已保存响应与本次真实返回不一致")
        else:
            _private_write(response_path, response.content)
        job.response_relpath, job.status, job.updated_at = response_relpath, "response_saved", time.time()
        session.commit()
    _finalize_saved(job_id, settings, session_factory)


async def execute_job(job_id, settings, session_factory):
    try:
        await _execute_job(job_id, settings, session_factory)
    except (AppError, ValueError, ValidationError, binascii.Error, OSError, SQLAlchemyError) as error:
        with session_factory() as session:
            lock_write(session)
            job = session.get(ImageJob, job_id)
            if job.status in {"cancelled", "ready"}:
                session.commit()
                return
            if job.status == "response_saved":
                job.error_code, job.updated_at = "IMAGE_REGISTRATION_FAILED", time.time()
                session.commit()
                raise
            if isinstance(error, SQLAlchemyError):
                session.commit()
                raise
            if job.status not in {"claimed", "submitting"}:
                session.commit()
                raise
            _finish_failure(session, job, "IMAGE_RESULT_INVALID", unknown=job.status == "submitting")


async def image_worker_loop(settings, session_factory, stop_event):
    with session_factory() as session:
        recover_jobs(session, settings)
        saved = session.scalars(select(ImageJob.id).where(ImageJob.status == "response_saved")).all()
    for identifier in saved:
        await execute_job(identifier, settings, session_factory)
    while not stop_event.is_set():
        with session_factory() as session:
            identifier = claim_job(session)
        if identifier is None:
            await asyncio.sleep(0.5)
        else:
            await execute_job(identifier, settings, session_factory)
