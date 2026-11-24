import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from smoke import PRODUCT_ROOT, validate_media, verify_local_data
from smoke_record_text import ServiceIdentity, fingerprint, service_identity
from smoke_registry import EvidenceStore, record_kind, write_private_json


sys.path.insert(0, str(PRODUCT_ROOT / "backend"))
from app.models import Asset, Canvas, Generation, Project, TextGeneration
from app.transfer_models import TransferJob


INTERMEDIATE = PRODUCT_ROOT / ".intermediate"
TRANSFER_FLAGS = (
    "offlinePlayback", "normalButton", "qteSuccess", "qteTimeout", "projectImport", "duplicateImport",
    "onlyPackageFiles", "editorWebPlay", "projectWebPlay", "fixedVersionWebPlay", "webQteSuccess",
    "webQteTimeout", "webReload", "anonymousWebAccessDenied",
)


class RestartProof(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    capturedAt: datetime
    service: ServiceIdentity
    validationSha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def confined(path: Path, base: Path, *, exists: bool = True) -> Path:
    resolved = (PRODUCT_ROOT / path).resolve(strict=exists)
    if not resolved.is_relative_to(base) or resolved == base:
        raise ValueError("复核文件必须位于指定的本项目隔离目录")
    return resolved


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_browser_proof(path: Path) -> tuple[dict, dict]:
    resolved = confined(path, INTERMEDIATE)
    value = json.loads(resolved.read_bytes())
    verified = datetime.fromisoformat(value["verifiedAt"])
    if verified.tzinfo is None or verified > datetime.now(timezone.utc):
        raise ValueError("浏览器实测文件缺少有效时间")
    return value, {"path": str(resolved.relative_to(PRODUCT_ROOT)), "sha256": file_sha256(resolved), "verifiedAt": value["verifiedAt"]}


def require_current_browser_proofs(*proofs: dict) -> None:
    production_paths = ("backend/app", "backend/migrations", "frontend/app")
    code_time = max(file.stat().st_mtime for root in production_paths for file in (PRODUCT_ROOT / root).rglob("*")
        if file.suffix in {".py", ".ts", ".tsx", ".js", ".css"} and file.is_file())
    if any(datetime.fromisoformat(proof["verifiedAt"]).timestamp() < code_time for proof in proofs):
        raise ValueError("浏览器实测早于应用代码，请使用最新版本重新验证")


def verify_originals() -> list[dict]:
    store = EvidenceStore(PRODUCT_ROOT, "阶段2-4")
    registry = store.load()
    records = registry["records"]
    if not registry.get("reverification") or len(records) != 3:
        raise ValueError("缺少已经复核的三段原始真实成片记录")
    for record in records:
        if record_kind(record) != "wan" or record.get("restartVerified") is not True:
            raise ValueError("原始记录缺少真实万相调用及重启证据")
        job, media_path = verify_local_data(Path("data"), record["generationId"])
        actual = {
            "generationId": job.id, "remoteTaskId": job.remoteTaskId, "remoteRequestId": job.remoteRequestId, "projectId": job.projectId,
            "sceneId": job.sceneId, "clipId": job.clipId, "assetId": job.assetId, "model": job.input["model"], "usage": job.usage,
            "startedAt": job.startedAt.isoformat(), "finishedAt": job.finishedAt.isoformat(), "media": validate_media(media_path),
        }
        if any(record[key] != value for key, value in actual.items()):
            raise ValueError("原调用时间、供应商回执或成片与已登记的真实记录不同")
    return records


def verify_playback(session: Session, directory: Path, playback: dict, records: list[dict]) -> dict:
    project = session.get(Project, playback["projectId"])
    results = playback["results"]
    source_by_id = {record["generationId"]: record for record in records}
    if (project is None or playback.get("reloadVerified") is not True or len(results) != len(records)
            or {item["generationId"] for item in results} != set(source_by_id)):
        raise ValueError("本次浏览器播放与三段原始成片不对应")
    canvases = session.scalars(select(Canvas).where(Canvas.project_id == project.id, Canvas.owner_id == project.owner_id)).all()
    stored_ids = {node.get("metadata", {}).get("storageKey") for canvas in canvases for node in canvas.data_json["nodes"]}
    for item in results:
        source = source_by_id[item["generationId"]]
        media = source["media"]
        played = item["playback"]
        uploaded = session.get(Asset, item["uploadedAssetId"])
        if (item["sourceAssetId"] != source["assetId"] or item["sha256"] != media["sha256"]
                or played["error"] is not None or played["currentTime"] <= 0.1 or abs(played["duration"] - media["durationSeconds"]) > 0.1
                or (played["width"], played["height"]) != (media["width"], media["height"])):
            raise ValueError("实际浏览器播放结果与供应商成片规格不匹配")
        if uploaded is None or uploaded.id not in stored_ids or (uploaded.project_id, uploaded.owner_id, uploaded.sha256) != (
            project.id, project.owner_id, media["sha256"],
        ) or file_sha256(confined(directory / uploaded.relpath, directory / "media")) != media["sha256"]:
            raise ValueError("浏览器上传视频的文件、归属或画布节点未正确保存")
    return {"projectId": project.id, "revision": project.revision, "assetIds": [item["uploadedAssetId"] for item in results]}


def verify_archive(job: TransferJob, directory: Path, original_hashes: set[str]) -> str:
    archive_path = confined(directory / job.private_json["archive"], directory / "transfers")
    checksum = file_sha256(archive_path)
    if (checksum, archive_path.stat().st_size) != (job.private_json["archiveSha256"], job.private_json["archiveBytes"]):
        raise ValueError("保存的归档文件与导出任务的大小或摘要不同")
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise ValueError("导出文件不完整")
        names = set(archive.namelist())
        if job.kind == "game":
            if {name for name in names if not name.startswith("assets/")} != {"game.json", "index.html"}:
                raise ValueError("固定游戏包缺少播放器及剧本数据")
            payload = json.loads(archive.read("game.json"))
            media_names = {name for name in names if name.startswith("assets/")}
            if payload["metadata"]["sourceRevision"] != job.source_revision or "/api/v1/" in json.dumps(payload):
                raise ValueError("固定游戏包引用了错误的项目版本或在线素材地址")
        else:
            if not {"project.json", "canvases.json", "history.json", "manifest.json"} <= names:
                raise ValueError("完整项目备份缺少项目、画布或历史")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest["sourceRevision"] != job.source_revision:
                raise ValueError("完整备份的版本与原始项目不一致")
            media_names = {entry["path"] for entry in manifest["assets"]}
        if not original_hashes <= {hashlib.sha256(archive.read(name)).hexdigest() for name in media_names}:
            raise ValueError("导出归档没有保留所有真实成片")
    return checksum


def verify_transfers(session: Session, directory: Path, transfer: dict, records: list[dict]) -> dict:
    if any(transfer.get(flag) is not True for flag in TRANSFER_FLAGS) or transfer.get("webDownloadCount") != 0:
        raise ValueError("本次前端浏览器的网页、导出、恢复或权限复核未完成")
    jobs = [session.get(TransferJob, identifier) for identifier in transfer["jobIds"]]
    if len(jobs) != 3 or any(job is None or job.status != "ready" for job in jobs):
        raise ValueError("隔离库中缺少本次实际就绪的导入与导出记录")
    jobs_by_kind = {job.kind: job for job in jobs}
    if set(jobs_by_kind) != {"backup", "game", "import"}:
        raise ValueError("隔离库中缺少完整备份、固定游戏包或导入")
    original, restored = (
        session.get(Project, identifier) for identifier in (transfer["projectId"], transfer["importedProjectId"])
    )
    if original is None or restored is None or original.id == restored.id or any(job.owner_id != original.owner_id for job in jobs):
        raise ValueError("项目备份与恢复的归属不一致")
    backup, game, imported = (jobs_by_kind[name] for name in ("backup", "game", "import"))
    if backup.project_id != original.id or game.project_id != original.id or imported.project_id != restored.id or restored.owner_id != original.owner_id:
        raise ValueError("项目与导入、导出任务的保存目标不一致")
    project_mapping = {"kind": "project", "sourceId": original.id, "targetId": restored.id}
    if project_mapping not in imported.result_json["mappings"]:
        raise ValueError("已恢复项目没有保存原作到新项目的映射")
    hashes = {record["media"]["sha256"] for record in records}
    originals = {asset.id: asset for asset in session.scalars(select(Asset).where(Asset.project_id == original.id, Asset.owner_id == original.owner_id))}
    mapped = {entry["targetId"]: originals[entry["sourceId"]].sha256 for entry in imported.result_json["mappings"]
        if entry["kind"] == "asset" and entry["sourceId"] in originals}
    restored_hashes = {asset.sha256 for asset in session.scalars(select(Asset).where(Asset.project_id == restored.id, Asset.owner_id == restored.owner_id))
        if mapped.get(asset.id) == asset.sha256 and file_sha256(confined(directory / asset.relpath, directory / "media")) == asset.sha256}
    if not hashes <= restored_hashes:
        raise ValueError("实际导入项目缺少原始成片或其摘要不一致")
    archives = {job.kind: verify_archive(job, directory, hashes) for job in (backup, game)}
    if file_sha256(confined(Path(transfer["gameFile"]), INTERMEDIATE)) != archives["game"]:
        raise ValueError("浏览器实际下载的固定版本与隔离服务保存的归档不同")
    return {"projectId": original.id, "restoredProjectId": restored.id, "jobIds": transfer["jobIds"], "archiveSha256": archives,
        "restoredRevision": restored.revision, "restoredAssets": mapped}


def verify_isolated(directory: Path, playback: dict, transfer: dict, records: list[dict]) -> dict:
    database = confined(directory / "openfmv.sqlite3", directory)
    engine = create_engine(f"sqlite:///file:{database}?mode=ro&uri=true")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA query_only = ON")
        with Session(bind=connection, autoflush=False) as session:
            if session.scalar(select(Generation.id).where(Generation.remote_task_id.is_not(None)).limit(1)) or session.scalar(
                select(TextGeneration.id).where(TextGeneration.remote_request_id.is_not(None)).limit(1)
            ):
                raise ValueError("隔离回归库出现新的模型请求回执")
            result = {"playback": verify_playback(session, directory, playback, records),
                "transfers": verify_transfers(session, directory, transfer, records)}
    engine.dispose()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="以已保存的实际万相成片验证发布阶段，全程只读业务库，不调用模型")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--playback-proof", required=True, type=Path)
    parser.add_argument("--transfer-proof", required=True, type=Path)
    parser.add_argument("--snapshot-file", type=Path)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--capture-before-restart", action="store_true")
    actions.add_argument("--verify-after-restart", action="store_true")
    parser.add_argument("--register", action="store_true")
    args = parser.parse_args()
    if args.register and not args.verify_after_restart:
        parser.error("正式登记前须核对隔离服务实际重启")
    directory = confined(args.data_dir, INTERMEDIATE)
    records = verify_originals()
    playback, playback_proof = read_browser_proof(args.playback_proof)
    transfer, transfer_proof = read_browser_proof(args.transfer_proof)
    require_current_browser_proofs(playback_proof, transfer_proof)
    proof = {
        "originalGenerationIds": [record["generationId"] for record in records], "browserPlayback": playback_proof,
        "browserTransfers": transfer_proof, "isolatedResults": verify_isolated(directory, playback, transfer, records),
        "newModelRequests": 0,
    }
    if args.capture_before_restart or args.verify_after_restart:
        path = confined(args.snapshot_file or directory.parent / "before-release-restart.json", INTERMEDIATE, exists=False)
        current = RestartProof(capturedAt=datetime.now(timezone.utc), service=service_identity(args.base_url, directory),
            validationSha256=fingerprint(proof))
        if args.capture_before_restart:
            if path.exists():
                raise ValueError("已保存该路径的重启基线，请核对或指定独立文件")
            write_private_json(path, current.model_dump(mode="json"))
            proof["restartSnapshot"] = str(path.relative_to(PRODUCT_ROOT))
            proof["beforePid"] = current.service.pid
        else:
            if path.stat().st_mode & 0o077:
                raise ValueError("重启基线文件权限须为 0600")
            previous = RestartProof.model_validate_json(path.read_bytes())
            before, after = previous.service, current.service
            if (before.baseUrl, before.dataDirectory, before.healthVersion, previous.validationSha256) != (
                after.baseUrl, after.dataDirectory, after.healthVersion, current.validationSha256,
            ) or before.pid == after.pid or before.processStarted == after.processStarted:
                raise ValueError("隔离服务尚无实际重启证据，或媒体和项目状态有变化")
            proof.update(restartVerified=True, beforePid=before.pid, afterPid=after.pid, restartSnapshotSha256=file_sha256(path))
            if args.register:
                EvidenceStore(PRODUCT_ROOT, "阶段3-2").revalidate(records, proof)
                proof["registered"] = True
    print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    main()
