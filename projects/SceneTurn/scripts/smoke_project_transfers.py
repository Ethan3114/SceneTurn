import argparse
import asyncio
import hashlib
import json
import sys
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app.main
from fastapi import UploadFile
from sqlalchemy import select
from app.api.transfers import upload_import
from app.core.config import get_settings
from app.db import SessionLocal
from app.image_models import ImageAccount
from app.models import Project, User
from app.services.project_transfers import confirm_transfer, create_export, transfer_view
from app.services.projects import project_envelope, payload_digest
from app.transfer_models import TransferJob
from app.transfer_schemas import ExportCreate
from agent_smoke_evidence import write_private_json
from smoke import validate_media, verify_local_data
from smoke_registry import EvidenceStore


DIRECTORY = ROOT / ".intermediate/project-transfer-smoke-20261116"
SOURCE = ROOT / ".intermediate/media-sequence-smoke-20261116/request.json"


def prepare():
    DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (DIRECTORY / "request.json").exists():
        raise ValueError("原项目导出验证已准备，请使用原记录")
    source = json.loads(SOURCE.read_bytes())
    browser = json.loads((DIRECTORY / "browser-proof.json").read_bytes())
    with SessionLocal() as session:
        project = session.get(Project, browser["projectId"])
        jobs = []
        for kind in ("backup", "game"):
            value = create_export(session, project.owner_id, project.id, ExportCreate(kind=kind, operation_id=str(uuid.uuid4()), expected_revision=project.revision), get_settings())
            if kind == "game":
                if value.status == "awaiting_confirmation":
                    value = confirm_transfer(session, project.owner_id, value.id, value.confirmation_hash)
                elif value.status not in {"queued", "processing", "ready"}:
                    raise ValueError("固定游戏版本没有通过完整检查")
            jobs.append(value.id)
        write_private_json(DIRECTORY / "request.json", {"projectId": project.id, "sourceRevision": project.revision,
            "jobIds": jobs, "generationIds": [item["id"] for item in source["generations"]]})
    print("已准备真实项目备份和私有固定版本；没有新增模型请求。")


def ready_jobs(session, ids):
    values = [session.get(TransferJob, identifier) for identifier in ids]
    if any(value is None or value.status != "ready" for value in values):
        raise ValueError("导入或导出记录尚未全部完成")
    return values


def deliver():
    request = json.loads((DIRECTORY / "request.json").read_bytes())
    original = json.loads(SOURCE.read_bytes())
    with SessionLocal() as session:
        owner_id = session.get(Project, original["projectId"]).owner_id
        if "reviewImportId" not in request:
            jobs = ready_jobs(session, request["jobIds"])
            backup = next(value for value in jobs if value.kind == "backup")
            user = session.get(User, owner_id)
            session.commit()
            with (get_settings().data_dir / backup.private_json["archive"]).open("rb") as stream:
                result = asyncio.run(upload_import(file=UploadFile(filename="互动游戏验证项目.zip", file=stream), operation_id=f"review-import-{backup.id}", user=user, session=session))
            request["reviewImportId"] = result["transfer"]["id"]
            write_private_json(DIRECTORY / "request.json", request)
            print("已为本人账号准备独立验证项目，等待导入检查。")
            return
        value = session.get(TransferJob, request["reviewImportId"])
        if value.owner_id != owner_id:
            raise ValueError("验证项目导入不属于本人账号")
        if value.status == "awaiting_confirmation":
            session.commit()
            confirm_transfer(session, owner_id, value.id, value.confirmation_hash)
            print("独立验证项目已通过完整检查并确认导入。")
            return
        if value.status in {"queued", "processing"}:
            print("独立验证项目正在处理。")
            return
        if value.status != "ready":
            raise ValueError("独立验证项目尚未完整导入")
        project = session.get(Project, value.project_id)
        request["reviewProjectId"] = project.id
        exports = []
        session.commit()
        for kind in ("backup", "game"):
            exported = create_export(session, owner_id, project.id, ExportCreate(kind=kind, expected_revision=project.revision,
                operation_id=f"review-{kind}-{project.id}-{project.revision}"), get_settings())
            if exported.status == "awaiting_confirmation":
                exported = confirm_transfer(session, owner_id, exported.id, exported.confirmation_hash)
            if exported.status not in {"queued", "processing", "ready"}:
                raise ValueError("本人验证项目的固定版本或备份未通过完整检查")
            exports.append(exported.id)
        request["reviewExportIds"] = exports
        write_private_json(DIRECTORY / "request.json", request)
        print(json.dumps({"reviewUrl": f"http://127.0.0.1:3210/zh-CN/editor?id={project.id}", "projectId": project.id}))


def snapshot():
    request = json.loads((DIRECTORY / "request.json").read_bytes())
    browser = json.loads((DIRECTORY / "browser-proof.json").read_bytes())
    with SessionLocal() as session:
        jobs = ready_jobs(session, list(dict.fromkeys([*request["jobIds"], *browser["jobIds"], request["reviewImportId"], *request["reviewExportIds"]])))
        project_ids = {request["projectId"], browser["projectId"], browser["importedProjectId"], request["reviewProjectId"]}
        projects = {identifier: project_envelope(session, session.get(Project, identifier)) for identifier in sorted(project_ids)}
        owner = session.get(Project, json.loads(SOURCE.read_bytes())["projectId"]).owner_id
        accounts = [{"slot": value.slot, "status": value.status, "activeJobId": value.active_job_id}
            for value in session.scalars(select(ImageAccount).where(ImageAccount.owner_id == owner).order_by(ImageAccount.slot))]
        artifacts = []
        for value in jobs:
            entry = {"view": transfer_view(value), "confirmationHash": value.confirmation_hash, "sourceDigest": value.source_digest,
                "privateSha256": payload_digest(value.private_json)}
            if value.kind != "import":
                path = get_settings().data_dir / value.private_json["archive"]
                entry["archiveSha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                if entry["archiveSha256"] != value.private_json["archiveSha256"]:
                    raise ValueError("成品包摘要与原导出记录不一致")
            artifacts.append(entry)
        return {"projects": projects, "artifacts": artifacts, "imageAccounts": accounts}


def record():
    request = json.loads((DIRECTORY / "request.json").read_bytes())
    restart = json.loads((DIRECTORY / "after-restart.json").read_bytes())
    browser = json.loads((DIRECTORY / "browser-proof.json").read_bytes())
    if restart.get("restartVerified") is not True or any(browser.get(key) is not True for key in ("offlinePlayback", "normalButton", "qteSuccess", "qteTimeout", "projectImport", "duplicateImport", "onlyPackageFiles", "editorWebPlay", "projectWebPlay", "fixedVersionWebPlay", "webQteSuccess", "webQteTimeout", "webReload", "anonymousWebAccessDenied")) or browser.get("webDownloadCount") != 0:
        raise ValueError("前端网页游玩、项目导入、独立游戏包或重启验证没有通过")
    with SessionLocal() as session:
        jobs = ready_jobs(session, request["jobIds"])
        game = next(value for value in jobs if value.kind == "game")
        backup = next(value for value in jobs if value.kind == "backup")
        with zipfile.ZipFile(get_settings().data_dir / game.private_json["archive"]) as archive:
            if archive.testzip() is not None or {name for name in archive.namelist() if not name.startswith("assets/")} != {"game.json", "index.html"}:
                raise ValueError("独立游戏包文件集合或完整性不符合要求")
            content = json.loads(archive.read("game.json"))
            if content["metadata"]["sourceRevision"] != request["sourceRevision"] or "/api/v1/" in json.dumps(content):
                raise ValueError("固定游戏版本或媒体引用不正确")
            hashes = {hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist() if name.startswith("assets/")}
        with zipfile.ZipFile(get_settings().data_dir / backup.private_json["archive"]) as archive:
            if archive.testzip() is not None or not {"project.json", "canvases.json", "history.json", "manifest.json"} <= set(archive.namelist()):
                raise ValueError("完整项目备份不完整")
    store = EvidenceStore(ROOT, stage="阶段2-4")
    records = []
    for identifier in request["generationIds"]:
        generation, media_path = verify_local_data(ROOT / "data", identifier)
        media = validate_media(media_path)
        if media["sha256"] not in hashes:
            raise ValueError("真实模型媒体没有完整进入独立游戏包")
        value = {"verifiedAt": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"), "generationId": generation.id,
            "remoteTaskId": generation.remoteTaskId, "remoteRequestId": generation.remoteRequestId, "projectId": generation.projectId,
            "sceneId": generation.sceneId, "clipId": generation.clipId, "assetId": generation.assetId, "model": generation.input["model"],
            "usage": generation.usage, "retryCount": generation.retryCount, "startedAt": generation.startedAt.isoformat(),
            "finishedAt": generation.finishedAt.isoformat(), "elapsedSeconds": (generation.finishedAt - generation.startedAt).total_seconds(),
            "media": media, "restartVerified": True}
        store.register(value)
        records.append(value)
    proof = {"projectSource": "本工作台实际保存的验证项目", "generationIds": request["generationIds"], "exportJobIds": request["jobIds"],
        "reviewProjectId": request["reviewProjectId"], "reviewExportIds": request["reviewExportIds"],
        "sourceRevision": request["sourceRevision"], "restartVerified": True, "browserProofSha256": hashlib.sha256((DIRECTORY / "browser-proof.json").read_bytes()).hexdigest(),
        "restartProofSha256": hashlib.sha256((DIRECTORY / "after-restart.json").read_bytes()).hexdigest(),
        "frontendWebPlayVerified": True, "webDownloadCount": browser["webDownloadCount"], "newModelRequests": 0}
    store.revalidate(records, proof)
    write_private_json(DIRECTORY / "proof.json", proof)
    print("真实模型媒体、完整项目、固定版本、前端网页游玩和重启恢复复核通过。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "deliver", "status", "before", "after", "record"])
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    elif args.action == "deliver":
        deliver()
    elif args.action == "status":
        request = json.loads((DIRECTORY / "request.json").read_bytes())
        with SessionLocal() as session:
            print(json.dumps({"statuses": [session.get(TransferJob, identifier).status for identifier in request["jobIds"]]}))
    elif args.action == "before":
        write_private_json(DIRECTORY / "before-restart.json", snapshot())
        print("真实项目、导入映射和成品文件的重启前状态已保存。")
    elif args.action == "after":
        value = snapshot()
        if value != json.loads((DIRECTORY / "before-restart.json").read_bytes()):
            raise ValueError("实际重启后的项目、导入或成品文件与原记录不一致")
        write_private_json(DIRECTORY / "after-restart.json", {"restartVerified": True, "snapshotSha256": payload_digest(value)})
        print("真实项目、导入、固定版本和成品文件在重启后保持一致。")
    else:
        record()


if __name__ == "__main__":
    main()
