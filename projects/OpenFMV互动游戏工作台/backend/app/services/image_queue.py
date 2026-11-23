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
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from pydantic import ValidationError

from app.agent_models import AgentRun
from app.core.errors import AppError
from app.core.security import decode_secret
from app.image_models import ImageAccount, ImageJob
from app.models import Asset, Budget, BudgetReservation, new_id
from app.services.ark import image_request as ark_image_request, seedream_cost_micros, submit_image as submit_ark_image
from app.services.canvas_images import bind_output
from app.services.media import resolve_asset_path
from app.services.material_references import validate_reference_assets
from app.services.projects import iso_timestamp, lock_write, payload_digest, record_project_version, require_project, require_scene
from app.services.sensetime import IMAGE_RECEIPT_TIMEOUT_SECONDS, MAX_IMAGE_BYTES, MODEL, decode_image, submit_image
from app.services.providers import frozen_key


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


def uses_codex_image(job):
    return job.request_json.get("modelProfile", {}).get("providerId") == "codex-local"


def _cloud_image_job():
    return func.coalesce(ImageJob.request_json["modelProfile"]["providerId"].as_string(), "") != "codex-local"


def image_queue_awaits_result(session):
    return session.scalar(select(ImageAccount.id).where(
        ImageAccount.status == "quarantined", ImageAccount.active_job_id.is_not(None)).limit(1)) is not None or session.scalar(
        select(ImageJob.id).where(_cloud_image_job(), ImageJob.account_id.is_(None), ImageJob.status == "submission_unknown").limit(1)) is not None


def _paid_image(job):
    return job.request_json.get("modelProfile", {}).get("providerId") == "volcengine-ark"


def _settle_image(session, job, actual):
    reservation = session.get(BudgetReservation, job.request_json.get("budgetReservationId"))
    if reservation is None:
        raise ValueError("付费图片缺少原始费用预留")
    if reservation.status == "settled":
        if reservation.cost_micros != actual:
            raise ValueError("图片费用与原始结算不一致")
        return
    if actual < 0 or actual > reservation.reserved_micros:
        raise ValueError("图片费用超过已确认的预留")
    changed = session.execute(update(Budget).where(Budget.id == "global", Budget.text_reserved_micros >= reservation.reserved_micros).values(
        text_reserved_micros=Budget.text_reserved_micros - reservation.reserved_micros,
        text_spent_micros=Budget.text_spent_micros + actual))
    if changed.rowcount != 1:
        raise ValueError("图片费用预留与账目不一致")
    reservation.reserved_micros, reservation.cost_micros, reservation.status, reservation.updated_at = 0, actual, "settled", time.time()


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
        run = session.get(AgentRun, job.run_id) if job.run_id else None
        job.status = "queued"
        if run and (run.stop_requested or run.status == "stopped"):
            job.status = "cancelled"
        elif run and run.status in {"completed", "failed"}:
            job.status = "failed"
        job.error_code, job.updated_at = "IMAGE_RESPONSE_TIMEOUT", now
        job.finished_at = now if job.status != "queued" else None
    if expired:
        session.flush()


def shared_image_request_busy(session):
    now = time.time()
    active_image = session.scalar(select(ImageJob.id).where(ImageJob.status.in_((
        "claimed", "submitting", "submission_unknown", "response_saved", "waiting_connection")),
        or_(_cloud_image_job(), ImageJob.updated_at >= now - IMAGE_RECEIPT_TIMEOUT_SECONDS)).limit(1))
    if active_image is not None:
        return True
    last_result = session.scalar(select(func.max(func.coalesce(ImageJob.finished_at, ImageJob.updated_at))).where(
        ImageJob.submitted_at.is_not(None), or_(ImageJob.finished_at.is_not(None), ImageJob.status.in_(("queued", "cancelled")))))
    return last_result is not None and now < last_result + IMAGE_REQUEST_GAP_SECONDS


def claim_job(session):
    lock_write(session)
    _release_expired_requests(session, time.time())
    if session.scalar(select(ImageAccount.id).where(ImageAccount.active_job_id.is_not(None)).limit(1)) is not None:
        session.commit()
        return None
    if shared_image_request_busy(session):
        session.commit()
        return None
    queued = session.scalars(select(ImageJob).where(_cloud_image_job(), ImageJob.status == "queued").order_by(ImageJob.created_at, ImageJob.id)).all()
    for job in queued:
        if job.run_id and session.get(AgentRun, job.run_id).stop_requested:
            job.status = "cancelled"
            continue
        dependencies = [session.get(ImageJob, identifier) for identifier in job.dependency_ids]
        if any(item is None or item.owner_id != job.owner_id or item.run_id != job.run_id for item in dependencies):
            raise ValueError("图片依赖不属于当前任务")
        if any(item.status != "ready" for item in dependencies):
            continue
        if _paid_image(job):
            job.status, job.updated_at = "claimed", time.time()
            session.commit()
            return job.id
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
    for job in session.scalars(select(ImageJob).where(ImageJob.account_id.is_(None), ImageJob.status.in_(("claimed", "submitting", "submission_unknown")))):
        if not _paid_image(job):
            continue
        if job.status == "claimed":
            job.status = "queued"
        else:
            saved_path = settings.data_dir.resolve() / "image-responses" / f"{job.id}.json" if settings else None
            if saved_path and saved_path.is_file():
                job.status, job.response_relpath = "response_saved", f"image-responses/{job.id}.json"
            elif job.status == "submitting":
                job.status, job.error_code = "submission_unknown", "IMAGE_SUBMISSION_UNKNOWN"
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
    if _paid_image(job):
        job.status, job.error_code, job.updated_at = "submission_unknown" if unknown else "failed", code, time.time()
        job.finished_at = None if unknown else time.time()
        if not unknown and job.request_json.get("budgetReservationId"):
            _settle_image(session, job, 0)
        session.commit()
        return
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
    run = None
    if job.run_id:
        require_scene(project, job.scene_id)
        run = session.get(AgentRun, job.run_id)
        session.refresh(run)
        if (run.owner_id, run.project_id, run.scene_id) != (job.owner_id, job.project_id, job.scene_id):
            raise ValueError("图片任务归属与创作任务不一致")
    expected_path = settings.data_dir.resolve() / "image-responses" / f"{job.id}.json"
    if (settings.data_dir.resolve() / job.response_relpath).resolve() != expected_path:
        raise ValueError("图片响应文件位置无效")
    response = json.loads(expected_path.read_bytes())
    if _paid_image(job) and response.get("model") not in {None, job.request_json["modelProfile"]["model"]}:
        raise ValueError("图片响应模型与已确认模型不一致")
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
    account = None
    if not _paid_image(job):
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
        metadata_json={"provider": "volcengine-ark" if _paid_image(job) else "sensetime", "model": job.request_json["modelProfile"]["model"] if _paid_image(job) else MODEL, "imageJobId": job.id,
            "billingSource": "confirmed_provider_price" if _paid_image(job) else "user_confirmed_free_token_plan", "frameKey": job.frame_key})
    session.add(asset)
    session.flush()
    snapshot = copy.deepcopy(project.data_json)
    snapshot["assets"].append({"id": asset_id, "type": "image", "name": job.label,
        "path": f"/api/v1/assets/{asset_id}/content", "relativePath": f"assets/{asset_id}",
        "importedAt": iso_timestamp(time.time()), "metadata": {"width": job.request_json["width"], "height": job.request_json["height"]}})
    snapshot["updatedAt"] = iso_timestamp(time.time())
    if job.run_id is None:
        bind_output(session, project, job, asset)
    if run and run.project_revision == project.revision:
        run.project_revision += 1
    project.data_json, project.revision, project.updated_at = snapshot, project.revision + 1, time.time()
    job.asset_id, job.status, job.updated_at, job.error_code = asset_id, "ready", time.time(), None
    if account is not None:
        account.active_job_id = None
    else:
        _settle_image(session, job, job.request_json.get("chargedMicros", job.request_json["modelProfile"]["price"]["microsPerImage"]))
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


def _submitted_ark_job(session, job_id, submission):
    job = session.get(ImageJob, job_id)
    if (job is None or not _paid_image(job) or job.status not in {"submitting", "submission_unknown"} or job.account_id is not None
            or job.attempt_count != submission["attemptCount"] or job.submitted_at != submission["submittedAt"]
            or _submission_digest(job) != submission["inputSha256"]):
        raise ValueError("付费图片提交状态已经变化")
    return job


async def _execute_ark_job(job_id, settings, session_factory):
    with session_factory() as session:
        lock_write(session)
        job = session.get(ImageJob, job_id)
        if job.status != "claimed" or job.confirmed_at is None or job.account_id is not None:
            raise ValueError("付费图片尚未确认或已经提交")
        run = session.get(AgentRun, job.run_id) if job.run_id else None
        if run and (run.stop_requested or run.status == "stopped"):
            job.status, job.updated_at, job.finished_at = "cancelled", time.time(), time.time()
            session.commit()
            return
        project = require_project(session, job.project_id, job.owner_id)
        frozen = job.request_json.get("referenceAssets", [])
        validate_reference_assets(session, project, [item["assetId"] for item in frozen], settings, frozen)
        identifiers = list(job.reference_asset_ids)
        for identifier in job.dependency_ids:
            dependency = session.get(ImageJob, identifier)
            if dependency is None or (dependency.owner_id, dependency.run_id, dependency.status) != (job.owner_id, job.run_id, "ready") or not dependency.asset_id:
                raise ValueError("参考图片尚未生成或不属于当前任务")
            identifiers.append(dependency.asset_id)
        references = []
        for identifier in dict.fromkeys(identifiers):
            asset = session.get(Asset, identifier)
            if asset is None or (asset.owner_id, asset.project_id) != (job.owner_id, job.project_id) or not asset.mime_type.startswith("image/") or asset.bytes > MAX_IMAGE_BYTES:
                raise ValueError("参考图片无法使用")
            raw = resolve_asset_path(settings, asset.relpath).read_bytes()
            if len(raw) != asset.bytes or len(raw) > MAX_IMAGE_BYTES or hashlib.sha256(raw).hexdigest() != asset.sha256:
                raise ValueError("参考图片内容发生改变")
            references.append((asset.mime_type, raw))
        selection = job.request_json["modelProfile"]
        key = frozen_key(session, job.owner_id, selection, settings)
        request = copy.deepcopy(job.request_json)
        body = ark_image_request(request, references)
        amount = seedream_cost_micros(request, len(references))
        if amount > settings.max_generation_cents * 10000:
            raise AppError(422, "IMAGE_COST_LIMIT", "单张图片报价超过已授权的单次费用上限")
        if job.request_json.get("budgetReservationId"):
            raise ValueError("付费图片已有费用预留，不能重新提交")
        changed = session.execute(update(Budget).where(Budget.id == "global",
            (Budget.spent_cents + Budget.reserved_cents) * 10000 + Budget.text_spent_micros + Budget.text_reserved_micros + amount <= Budget.limit_cents * 10000,
            (Budget.spent_cents + Budget.reserved_cents) * 10000 + Budget.text_spent_micros + Budget.text_reserved_micros + amount <= settings.budget_cents * 10000,
        ).values(text_reserved_micros=Budget.text_reserved_micros + amount))
        if changed.rowcount != 1:
            raise AppError(402, "BUDGET_EXCEEDED", "本次图片请求将超过累计验证预算，已停止新增收费请求")
        reservation = BudgetReservation(id=new_id(), purpose="image-model", reserved_micros=amount, status="reserved",
            details_json={"jobId": job.id, "ownerId": job.owner_id, "projectId": job.project_id, "modelProfile": selection})
        session.add(reservation)
        job.request_json = {**job.request_json, "budgetReservationId": reservation.id, "chargedMicros": amount, "requestBody": body}
        job.status, job.updated_at, job.attempt_count = "submitting", time.time(), job.attempt_count + 1
        job.submitted_at, job.error_code = time.time(), None
        submission = {"attemptCount": job.attempt_count, "submittedAt": job.submitted_at, "inputSha256": _submission_digest(job)}
        session.commit()
    try:
        async with asyncio.timeout(IMAGE_RECEIPT_TIMEOUT_SECONDS):
            response = await submit_ark_image(key, request, references)
    except (TimeoutError, httpx.TransportError):
        with session_factory() as session:
            lock_write(session)
            job = _submitted_ark_job(session, job_id, submission)
            _finish_failure(session, job, "IMAGE_SUBMISSION_UNKNOWN", unknown=True)
        return
    with session_factory() as session:
        lock_write(session)
        job = _submitted_ark_job(session, job_id, submission)
        if response.status_code >= 300:
            _private_write(settings.data_dir.resolve() / "image-responses" / f"{job.id}-error.json", response.content)
            _finish_failure(session, job, "IMAGE_PROVIDER_REJECTED", unknown=not (400 <= response.status_code < 500 and response.status_code != 408))
            return
        response_relpath = f"image-responses/{job.id}.json"
        _private_write(settings.data_dir.resolve() / response_relpath, response.content)
        job.response_relpath, job.status, job.updated_at = response_relpath, "response_saved", time.time()
        session.commit()
    _finalize_saved(job_id, settings, session_factory)


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
        paid = _paid_image(job)
    if saved:
        _finalize_saved(job_id, settings, session_factory)
        return
    if paid:
        await _execute_ark_job(job_id, settings, session_factory)
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
