import asyncio
import binascii
import json
import logging
import lzma
import subprocess
import time
import uuid
import zipfile
import zlib

from pydantic import ValidationError
from sqlalchemy import or_, select

from app.core.errors import AppError
from app.services.asset_ingest import register_asset
from app.services.deployment_queue import DispatchMarker, epoch_milliseconds
from app.services.deployment_schedule import WakeDecision, retire_dispatch, wake_decision
from app.services.direct_uploads import delete_asset_staging, ensure_import_source, inspect_upload
from app.services.durable_files import materialize_file
from app.services.project_transfers import apply_import, build_archive, file_sha256, transfer_root
from app.services.projects import lock_write, payload_digest, require_project
from app.services.transfer_archives import import_preview
from app.services.transfer_dispatch import acknowledge_transfer_marker, commit_transfer, transfer_queue
from app.transfer_models import TransferJob


LEASE_SECONDS = 30
logger = logging.getLogger(__name__)


def claim(factory, worker_id, settings=None, *, identifier=None, marker=None):
    with factory() as session:
        lock_write(session)
        if marker is not None:
            decision = wake_decision(session, marker, epoch_milliseconds())
            if decision is not WakeDecision.DUE:
                session.commit()
                queue = transfer_queue(settings)
                if decision is WakeDecision.STALE and queue is not None:
                    acknowledge_transfer_marker(factory, queue, marker)
                return None
        if identifier is None:
            value = session.scalar(select(TransferJob).where(or_(TransferJob.status == "queued",
                (TransferJob.status == "processing") & (TransferJob.lease_expires_at <= time.time())))
                .order_by(TransferJob.created_at).limit(1))
        else:
            value = session.get(TransferJob, identifier)
            if value is not None and value.status != "queued" and (
                value.status != "processing" or (value.lease_expires_at or 0) > time.time()
            ):
                commit_transfer(session, value, settings)
                return None
        if value is None:
            if marker is not None:
                retire_dispatch(session, marker)
            session.commit()
            if marker is not None and (queue := transfer_queue(settings)) is not None:
                acknowledge_transfer_marker(factory, queue, marker)
            return None
        value.status, value.lease_owner, value.lease_expires_at = "processing", worker_id, time.time() + LEASE_SECONDS
        value.updated_at = time.time()
        if settings is None:
            session.commit()
        else:
            commit_transfer(session, value, settings)
        return value.id


def renew_lease(factory, identifier, worker_id):
    with factory() as session:
        lock_write(session)
        value = session.get(TransferJob, identifier)
        if value is None or value.lease_owner != worker_id or value.status != "processing":
            session.commit()
            return False
        value.lease_expires_at = time.time() + LEASE_SECONDS
        session.commit()
        return True


def process_transfer(factory, settings, identifier, worker_id):
    try:
        with factory() as session:
            lock_write(session)
            value = session.get(TransferJob, identifier)
            if value is None or value.lease_owner != worker_id or value.status != "processing":
                session.commit()
                return
            value.lease_expires_at = time.time() + LEASE_SECONDS
            phase, kind = value.request_json["phase"], value.kind
            session.flush()
            session.expunge(value)
            session.commit()
        if kind == "import" and phase == "preview":
            if payload_digest({"sha256": value.request_json["archiveSha256"]}) != value.identity_sha256:
                raise AppError(409, "IMPORT_SOURCE_CHANGED", "导入源摘要与保存记录不一致")
            if "directUpload" in value.request_json:
                source = ensure_import_source(settings, value)
            else:
                source = materialize_file(settings, transfer_root(settings, identifier) / "source.zip",
                    max_bytes=settings.max_upload_bytes, sha256=value.request_json["archiveSha256"])
            if file_sha256(source) != value.request_json["archiveSha256"]:
                raise AppError(409, "IMPORT_SOURCE_CHANGED", "导入源文件发生变化")
            result = import_preview(identifier, source, settings)
        elif kind == "import":
            result = None
        elif kind == "asset_upload":
            result = inspect_upload(settings, value)
        else:
            result = build_archive(value, settings, str(uuid.uuid4()))
        with factory() as session:
            lock_write(session)
            value = session.get(TransferJob, identifier)
            if value is None or value.lease_owner != worker_id or value.status != "processing":
                session.commit()
                return
            if kind == "import" and phase == "preview":
                value.private_json, value.result_json, value.source_digest = result, result["view"], result["sourceDigest"]
                value.confirmation_hash = payload_digest({"id": value.id, "source": value.source_digest, "preview": value.result_json})
                value.status = "blocked" if value.result_json["issues"] else "awaiting_confirmation"
            elif kind == "import":
                apply_import(session, value, settings)
                value.status = "ready"
            elif kind == "asset_upload":
                project = require_project(session, value.project_id, value.owner_id)
                asset = register_asset(session, project, result)
                value.result_json, value.status = {"assetId": asset.id}, "ready"
            else:
                value.private_json = {**value.private_json, **result}
                value.status = "ready"
            value.lease_owner, value.lease_expires_at, value.updated_at = None, None, time.time()
            commit_transfer(session, value, settings)
        if kind == "asset_upload":
            try:
                delete_asset_staging(settings, value)
            except AppError as failure:
                logger.warning("清理已登记素材的上传暂存对象失败（%s）", failure.code)
    except (AppError, OSError, ValidationError, json.JSONDecodeError, UnicodeDecodeError, zipfile.BadZipFile, zipfile.LargeZipFile, subprocess.TimeoutExpired, ValueError, binascii.Error, zlib.error, lzma.LZMAError, EOFError) as failure:
        with factory() as session:
            lock_write(session)
            value = session.get(TransferJob, identifier)
            if value is not None and value.lease_owner == worker_id and value.status == "processing":
                value.status = "failed"
                if isinstance(failure, AppError):
                    value.error_code, value.error_message = failure.code, failure.message
                elif isinstance(failure, ValidationError):
                    value.error_code = "IMPORT_SCHEMA_INVALID"
                    details = failure.errors(include_input=False, include_url=False)
                    value.error_message = "项目结构未通过检查：" + "；".join(f"{'.'.join(str(part) for part in item['loc'])}：{item['msg']}" for item in details[:10])
                elif isinstance(failure, (json.JSONDecodeError, UnicodeDecodeError)):
                    value.error_code, value.error_message = "IMPORT_JSON_INVALID", "项目清单无法作为完整UTF-8 JSON读取"
                elif isinstance(failure, (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, lzma.LZMAError, EOFError)):
                    value.error_code, value.error_message = "IMPORT_ARCHIVE_INVALID", "压缩包结构或文件校验未通过"
                elif isinstance(failure, subprocess.TimeoutExpired):
                    value.error_code, value.error_message = "GAME_RENDER_TIMEOUT", "游戏播放器生成超时，当前操作已终止"
                elif isinstance(failure, (ValueError, binascii.Error)):
                    value.error_code, value.error_message = "IMPORT_MEDIA_INVALID", "项目内嵌媒体无法完整读取"
                else:
                    value.error_code, value.error_message = "TRANSFER_STORAGE_FAILED", "项目文件无法保存，请检查磁盘空间及目录权限"
                value.lease_owner, value.lease_expires_at, value.updated_at = None, None, time.time()
                commit_transfer(session, value, settings)


async def run_transfer(settings, factory, identifier, worker_id):
    task = asyncio.create_task(asyncio.to_thread(process_transfer, factory, settings, identifier, worker_id))
    while not task.done():
        done, _ = await asyncio.wait({task}, timeout=5)
        if done or not await asyncio.to_thread(renew_lease, factory, identifier, worker_id):
            break
    await task


class HostedTransferWorker:
    def __init__(self, settings, factory):
        self.settings = settings
        self.factory = factory
        self._running = asyncio.Lock()

    async def _run(self, identifier, marker=None):
        worker_id = str(uuid.uuid4())
        try:
            claimed = await asyncio.to_thread(claim, self.factory, worker_id, self.settings,
                identifier=identifier, marker=marker)
        except AppError as error:
            if error.code != "TRANSFER_QUEUE_UNAVAILABLE":
                raise
            logger.warning("传输任务保留，等待队列连接恢复")
            return
        if claimed is not None:
            await run_transfer(self.settings, self.factory, claimed, worker_id)

    async def run_now(self, identifier: str):
        async with self._running:
            await self._run(identifier)

    async def wake(self, markers: tuple[DispatchMarker, ...]):
        async with self._running:
            for marker in markers:
                await self._run(str(marker.job_id), marker)


async def transfer_worker_loop(settings, factory, stop_event):
    while not stop_event.is_set():
        worker_id = str(uuid.uuid4())
        identifier = await asyncio.to_thread(claim, factory, worker_id)
        if identifier:
            await run_transfer(settings, factory, identifier, worker_id)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.5)
        except TimeoutError:
            continue
