import asyncio
import binascii
import json
import lzma
import subprocess
import time
import uuid
import zipfile
import zlib

from pydantic import ValidationError
from sqlalchemy import or_, select, update

from app.core.errors import AppError
from app.services.project_transfers import apply_import, build_archive, file_sha256, transfer_root
from app.services.projects import lock_write, payload_digest
from app.services.transfer_archives import import_preview
from app.transfer_models import TransferJob


LEASE_SECONDS = 30


def claim(factory, worker_id):
    with factory() as session:
        lock_write(session)
        value = session.scalar(select(TransferJob).where(or_(TransferJob.status == "queued",
            (TransferJob.status == "processing") & (TransferJob.lease_expires_at <= time.time()))).order_by(TransferJob.created_at).limit(1))
        if value is None:
            session.commit()
            return None
        value.status, value.lease_owner, value.lease_expires_at = "processing", worker_id, time.time() + LEASE_SECONDS
        value.updated_at = time.time()
        session.commit()
        return value.id


def process_transfer(factory, settings, identifier, worker_id):
    try:
        with factory() as session:
            value = session.get(TransferJob, identifier)
            if value.lease_owner != worker_id:
                raise AppError(409, "TRANSFER_LEASE_LOST", "导入或导出执行权限已过期")
            phase, kind = value.request_json["phase"], value.kind
            if kind == "import" and phase == "preview":
                source = transfer_root(settings, identifier) / "source.zip"
                if payload_digest({"sha256": value.request_json["archiveSha256"]}) != value.identity_sha256:
                    raise AppError(409, "IMPORT_SOURCE_CHANGED", "导入源摘要与保存记录不一致")
                if file_sha256(source) != value.request_json["archiveSha256"]:
                    raise AppError(409, "IMPORT_SOURCE_CHANGED", "导入源文件发生变化")
                result = import_preview(identifier, source, settings)
            elif kind == "import":
                result = None
            else:
                result = build_archive(value, settings, str(uuid.uuid4()))
        with factory() as session:
            lock_write(session)
            value = session.get(TransferJob, identifier)
            if value.lease_owner != worker_id or value.status != "processing" or value.lease_expires_at <= time.time():
                raise AppError(409, "TRANSFER_LEASE_LOST", "导入或导出执行权限已过期")
            if kind == "import" and phase == "preview":
                value.private_json, value.result_json, value.source_digest = result, result["view"], result["sourceDigest"]
                value.confirmation_hash = payload_digest({"id": value.id, "source": value.source_digest, "preview": value.result_json})
                value.status = "blocked" if value.result_json["issues"] else "awaiting_confirmation"
            elif kind == "import":
                apply_import(session, value, settings)
                value.status = "ready"
            else:
                value.private_json = {**value.private_json, **result}
                value.status = "ready"
            value.lease_owner, value.lease_expires_at, value.updated_at = None, None, time.time()
            session.commit()
    except (AppError, OSError, ValidationError, json.JSONDecodeError, UnicodeDecodeError, zipfile.BadZipFile, zipfile.LargeZipFile, subprocess.TimeoutExpired, ValueError, binascii.Error, zlib.error, lzma.LZMAError, EOFError) as failure:
        with factory() as session:
            lock_write(session)
            value = session.get(TransferJob, identifier)
            if value.lease_owner == worker_id:
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
                session.commit()


async def transfer_worker_loop(settings, factory, stop_event):
    worker_id = str(uuid.uuid4())
    while not stop_event.is_set():
        identifier = claim(factory, worker_id)
        if identifier:
            task = asyncio.create_task(asyncio.to_thread(process_transfer, factory, settings, identifier, worker_id))
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=5)
                if done:
                    break
                with factory() as session:
                    result = session.execute(update(TransferJob).where(TransferJob.id == identifier, TransferJob.lease_owner == worker_id,
                        TransferJob.status == "processing", TransferJob.lease_expires_at > time.time()).values(lease_expires_at=time.time() + LEASE_SECONDS))
                    session.commit()
                    if result.rowcount != 1:
                        raise AppError(409, "TRANSFER_LEASE_LOST", "导入或导出任务租约已失效")
            await task
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.5)
        except TimeoutError:
            continue
