import asyncio
import binascii
import copy
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
from sqlalchemy import select
from pydantic import ValidationError

from app.agent_models import AgentRun
from app.core.errors import AppError
from app.core.security import decode_secret
from app.image_models import ImageAccount, ImageJob
from app.models import Asset, new_id
from app.services.media import resolve_asset_path
from app.services.material_references import validate_reference_assets
from app.services.projects import iso_timestamp, lock_write, record_project_version, require_project
from app.services.sensetime import MAX_IMAGE_BYTES, MODEL, decode_image, submit_image


def public_job(job):
    return {"id": job.id, "nodeId": job.node_id, "label": job.label, "frameKey": job.frame_key,
        "status": job.status, "assetId": job.asset_id,
        "width": job.request_json["width"], "height": job.request_json["height"],
        "errorCode": job.error_code}


def claim_job(session):
    lock_write(session)
    queued = session.scalars(select(ImageJob).where(ImageJob.status == "queued").order_by(ImageJob.created_at, ImageJob.id)).all()
    for job in queued:
        if session.get(AgentRun, job.run_id).stop_requested:
            job.status = "cancelled"
            continue
        dependencies = [session.get(ImageJob, identifier) for identifier in job.dependency_ids]
        if any(item is None or item.owner_id != job.owner_id or item.run_id != job.run_id for item in dependencies):
            raise ValueError("图片依赖不属于当前任务")
        if any(item.status != "ready" for item in dependencies):
            continue
        account = session.scalar(select(ImageAccount).where(ImageAccount.owner_id == job.owner_id,
            ImageAccount.status == "available", ImageAccount.active_job_id.is_(None)).order_by(ImageAccount.last_assigned_at, ImageAccount.slot).limit(1))
        if account is None:
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
        elif job.status == "submitting":
            saved_path = settings.data_dir.resolve() / "image-responses" / f"{job.id}.json" if settings else None
            if saved_path and saved_path.is_file():
                job.status, job.response_relpath = "response_saved", f"image-responses/{job.id}.json"
            else:
                job.status, job.error_code = "submission_unknown", "IMAGE_SUBMISSION_UNKNOWN"
                account.status = "quarantined"
    session.commit()


def _finish_failure(session, job, code, *, unknown=False, quota=False):
    account = session.get(ImageAccount, job.account_id)
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
    expected_path = settings.data_dir.resolve() / "image-responses" / f"{job.id}.json"
    if (settings.data_dir.resolve() / job.response_relpath).resolve() != expected_path:
        raise ValueError("图片响应文件位置无效")
    response = json.loads(expected_path.read_bytes())
    raw = decode_image(response, job.request_json["width"], job.request_json["height"])
    asset_id = new_id()
    relpath = f"media/images/{job.id}.png"
    image_path = settings.data_dir.resolve() / relpath
    if image_path.exists():
        if image_path.read_bytes() != raw:
            raise ValueError("持久图片与响应不一致")
    else:
        _private_write(image_path, raw)
    lock_write(session)
    session.refresh(job)
    if job.status != "response_saved":
        raise ValueError("图片任务状态已改变")
    project = require_project(session, job.project_id, job.owner_id)
    run = session.get(AgentRun, job.run_id)
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
    job.asset_id, job.status, job.updated_at = asset_id, "ready", time.time()
    job.finished_at = time.time()
    session.get(ImageAccount, job.account_id).active_job_id = None
    session.flush()
    record_project_version(session, project)
    session.commit()


async def _execute_job(job_id, settings, session_factory):
    with session_factory() as session:
        lock_write(session)
        job = session.get(ImageJob, job_id)
        if job.status == "cancelled":
            session.commit()
            return
        if job.status == "response_saved":
            session.commit()
            finalize_response(session, job, settings)
            return
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
        request, key = job.request_json, decode_secret(account.api_key_encrypted, settings)
        job.status, job.updated_at, job.attempt_count = "submitting", time.time(), job.attempt_count + 1
        job.submitted_at = time.time()
        session.commit()
        try:
            response = await submit_image(key, request, references)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            lock_write(session)
            session.refresh(job)
            if job.attempt_count < 2:
                job.status, job.account_id = "queued", None
                account.active_job_id = None
                session.commit()
            else:
                _finish_failure(session, job, "IMAGE_CONNECTION_FAILED")
            return
        except httpx.TransportError:
            lock_write(session)
            session.refresh(job)
            _finish_failure(session, job, "IMAGE_SUBMISSION_UNKNOWN", unknown=True)
            return
        if response.status_code >= 400:
            lock_write(session)
            session.refresh(job)
            _finish_failure(session, job, "IMAGE_QUOTA_OR_RATE_LIMIT" if response.status_code in {402, 429} else "IMAGE_PROVIDER_REJECTED",
                unknown=response.status_code >= 500, quota=response.status_code in {402, 429})
            return
        response_relpath = f"image-responses/{job.id}.json"
        _private_write(settings.data_dir.resolve() / response_relpath, response.content)
        lock_write(session)
        session.refresh(job)
        job.response_relpath, job.status, job.updated_at = response_relpath, "response_saved", time.time()
        session.commit()
        finalize_response(session, job, settings)


async def execute_job(job_id, settings, session_factory):
    try:
        await _execute_job(job_id, settings, session_factory)
    except (AppError, ValueError, ValidationError, binascii.Error, OSError):
        with session_factory() as session:
            lock_write(session)
            job = session.get(ImageJob, job_id)
            if job.status in {"cancelled", "ready"}:
                session.commit()
                return
            _finish_failure(session, job, "IMAGE_RESULT_INVALID", unknown=job.status == "submitting")


async def image_worker_loop(settings, session_factory, stop_event):
    with session_factory() as session:
        recover_jobs(session, settings)
        saved = session.scalars(select(ImageJob.id).where(ImageJob.status == "response_saved")).all()
    for identifier in saved:
        await execute_job(identifier, settings, session_factory)
    active = set()
    while not stop_event.is_set():
        for task in list(active):
            if task.done():
                active.remove(task)
                task.result()
        with session_factory() as session:
            identifier = claim_job(session)
        if identifier:
            active.add(asyncio.create_task(execute_job(identifier, settings, session_factory)))
        else:
            await asyncio.sleep(0.5)
    if active:
        await asyncio.gather(*active)
