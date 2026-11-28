import copy
import hashlib
import json
import mimetypes
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

from sqlalchemy import select

from app.core.errors import AppError
from app.models import Asset, Base, Canvas, Project, new_id
from app.schemas import ProjectSnapshot
from app.services.durable_files import persist_file
from app.services.transfer_dispatch import commit_transfer
from app.services.media import resolve_asset_path
from app.services.media_adoptions import conflict_issues
from app.services.media_ranges import required_media_end
from app.services.projects import iso_timestamp, lock_write, payload_digest, project_envelope, record_project_version, require_project, require_revision
from app.services.transfer_archives import checked_member, media_references, rewrite_references, without_credentials
from app.transfer_models import TransferJob
from app.transfer_schemas import BACKUP_FORMAT, BACKUP_SOURCE_FORMAT


ROOT = Path(__file__).resolve().parents[3]
HISTORY_TABLES = {"project_versions", "project_memories", "agent_runs", "agent_steps", "agent_calls", "project_conversations", "conversation_messages",
    "material_conversations", "material_messages", "generations", "text_generations", "text_usage_ledger", "image_jobs", "media_sequences", "media_adoptions", "operations"}
CLIP_FIELDS = {"id", "type", "startTime", "duration", "sourceStart", "sourceDuration", "enabled", "hidden", "opacity", "rotation", "linkGroupId", "src", "assetId",
    "poster", "playbackId", "muted", "volume", "playbackRate", "preservePitch", "freezeFrameTime", "fit", "sourceAudioEnabled", "sourceVideoClipId", "mode", "label", "pauseOnShow"}
STYLE_FIELDS = {"preset", "shape", "fillColor", "textColor", "borderColor", "fillOpacity", "borderOpacity", "borderWidth", "shadow", "backgroundImageAssetId", "backgroundImageSrc", "backgroundImageFit"}


def file_sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def public_timeline(source):
    value = {"version": source["version"], "duration": source["duration"], "tracks": [], "bookmarks": []}
    for original in source["tracks"]:
        track = {key: copy.deepcopy(item) for key, item in original.items() if key in {"id", "type", "mediaRole", "hidden", "muted"}}
        track["clips"] = []
        for clip in original["clips"]:
            output = {key: copy.deepcopy(item) for key, item in clip.items() if key in CLIP_FIELDS}
            for key, fields in {"rect": {"x", "y", "width", "height"}, "qte": {"input", "prompt", "clickCount", "keyLabel", "showCountdown", "showCueLabel"}, "style": STYLE_FIELDS}.items():
                if isinstance(clip.get(key), dict):
                    output[key] = {field: copy.deepcopy(item) for field, item in clip[key].items() if field in fields}
            if "keyframes" in clip:
                output["keyframes"] = [{key: copy.deepcopy(item) for key, item in frame.items() if key in {"id", "property", "time", "value", "interpolation"}} for frame in clip["keyframes"]]
            track["clips"].append(output)
        value["tracks"].append(track)
    return value


def require_transfer(session, identifier, owner_id):
    value = session.scalar(select(TransferJob).where(TransferJob.id == identifier, TransferJob.owner_id == owner_id,
        TransferJob.kind.in_(("import", "backup", "game")), TransferJob.status != "awaiting_upload"))
    if value is None:
        raise AppError(404, "TRANSFER_NOT_FOUND", "导入或导出记录不存在或无权访问")
    return value


def transfer_view(value):
    result = value.result_json
    return {"id": value.id, "kind": value.kind, "status": value.status, "projectId": value.project_id, "sourceRevision": value.source_revision,
        "sourceFormat": result.get("sourceFormat"), "projects": result.get("projects", []), "mappings": result.get("mappings", []), "issues": result.get("issues", []),
        "confirmationHash": value.confirmation_hash if value.status == "awaiting_confirmation" else None,
        "downloadUrl": f"/api/v1/transfers/{value.id}/download" if value.status == "ready" and value.kind != "import" else None,
        "previewUrl": f"/api/v1/transfers/{value.id}/play/index.html" if value.status == "ready" and value.kind == "game" else None,
        "errorCode": value.error_code, "errorMessage": value.error_message,
        "createdAt": iso_timestamp(value.created_at), "updatedAt": iso_timestamp(value.updated_at)}


def matching_job(session, owner_id, kind, operation_id, identity):
    operation = session.scalar(select(TransferJob).where(TransferJob.owner_id == owner_id, TransferJob.operation_id == operation_id))
    if operation is not None:
        if operation.kind != kind or operation.identity_sha256 != identity:
            raise AppError(409, "OPERATION_CONFLICT", "同一操作标识已绑定其他导入或导出")
        return operation
    return session.scalar(select(TransferJob).where(TransferJob.owner_id == owner_id, TransferJob.kind == kind, TransferJob.identity_sha256 == identity))


def history_snapshot(session, project):
    result = {}
    run_ids = select(Base.metadata.tables["agent_runs"].c.id).where(Base.metadata.tables["agent_runs"].c.project_id == project.id)
    conversations = {"conversation_messages": "project_conversations", "material_messages": "material_conversations"}
    for name in sorted(HISTORY_TABLES):
        table = Base.metadata.tables[name]
        if "project_id" in table.c:
            query = select(table).where(table.c.project_id == project.id)
        elif "run_id" in table.c:
            query = select(table).where(table.c.run_id.in_(run_ids))
        else:
            parent = Base.metadata.tables[conversations[name]]
            query = select(table).where(table.c.conversation_id.in_(select(parent.c.id).where(parent.c.project_id == project.id)))
        result[name] = [without_credentials(dict(row)) for row in session.execute(query).mappings()]
    source_id = project.data_json.get("metadata", {}).get("importId")
    if source_id:
        source = require_transfer(session, source_id, project.owner_id)
        result["sourceArchive"] = source.private_json.get("history", {})
    return result


def export_asset_record(asset):
    return {"sourceId": asset.id, "path": f"assets/{asset.id}{Path(asset.relpath).suffix.lower()}", "name": asset.name, "mimeType": asset.mime_type,
        "bytes": asset.bytes, "sha256": asset.sha256, "relpath": asset.relpath, "posterRelpath": asset.poster_relpath,
        "durationMs": asset.duration_ms, "width": asset.width, "height": asset.height}


def checked_asset(asset, settings):
    try:
        path = resolve_asset_path(settings, asset.relpath)
    except AppError as error:
        if error.status != 404:
            raise
        raise AppError(404, "EXPORT_MEDIA_MISSING", f"媒体文件缺失：{asset.name}") from error
    if path.stat().st_size != asset.bytes or file_sha256(path) != asset.sha256:
        raise AppError(409, "EXPORT_MEDIA_CHANGED", f"媒体文件与保存记录不一致：{asset.name}")
    return export_asset_record(asset)


def game_snapshot(session, project, settings):
    assets = session.scalars(select(Asset).where(Asset.project_id == project.id, Asset.owner_id == project.owner_id)).all()
    return game_snapshot_from_records(project.data_json, project.revision, assets,
        conflict_issues(session, project.id, project.owner_id), settings, verify_source=True)


def game_snapshot_from_records(source, revision, assets, conflicts, settings, *, verify_source):
    issues = [{"code": item["code"], "message": "项目仍有待处理的素材采用冲突", "sourceId": item.get("clipId")} for item in conflicts]
    by_id, by_path = {asset.id: asset for asset in assets}, {}
    for asset in assets:
        by_path[f"/api/v1/assets/{asset.id}/content"] = (asset, False)
        by_path[f"/api/v1/assets/{asset.id}/poster"] = (asset, True)
    for item in source["assets"]:
        if item["id"] in by_id:
            by_path[item["path"]] = (by_id[item["id"]], False)
            by_path[item["relativePath"]] = (by_id[item["id"]], False)
    graph = {"nodes": [], "edges": [{key: copy.deepcopy(value) for key, value in edge.items() if key in {"id", "source", "target", "sourceHandle", "targetHandle"}} for edge in source["graphData"]["edges"]]}
    node_ids = {node["id"] for node in source["graphData"]["nodes"]}
    for node in source["graphData"]["nodes"]:
        data = {key: copy.deepcopy(value) for key, value in node["data"].items() if key in {"type", "title", "label", "bodyText", "timeline"}}
        if "timeline" in data and data["timeline"]:
            data["timeline"] = public_timeline(data["timeline"])
            for track in data["timeline"]["tracks"]:
                for clip in track["clips"]:
                    if not clip.get("enabled", True):
                        continue
                    if clip["type"] in {"video", "audio", "image"}:
                        asset = by_id.get(clip.get("assetId"))
                        if asset is None:
                            match = by_path.get(clip.get("src"))
                            asset = match[0] if match else None
                        if asset is None:
                            issues.append({"code": "EXPORT_MEDIA_UNREGISTERED", "message": "时间线媒体未完整登记", "sourceId": clip["id"]})
                        elif clip["type"] != "image" and required_media_end(clip) > (asset.duration_ms or 0) / 1000 + 0.001:
                            issues.append({"code": "MEDIA_DURATION_CONFLICT", "message": "时间线裁切超过实际媒体时长", "sourceId": clip["id"]})
                    if clip["type"] == "button":
                        for event in (["click", "timeout"] if clip.get("mode") == "qte" else ["click"]):
                            targets = [edge for edge in graph["edges"] if edge["source"] == node["id"] and edge.get("sourceHandle") == f"button:{clip['id']}:{event}"]
                            if len(targets) != 1 or targets[0]["target"] not in node_ids:
                                issues.append({"code": "INTERACTION_TARGET_INVALID", "message": "按钮或QTE缺少有效的跳转目标", "sourceId": clip["id"]})
        graph["nodes"].append({"id": node["id"], "type": node["type"], "position": copy.deepcopy(node["position"]), "data": data})
    paths, files = {}, {}
    for reference in set(media_references(graph)):
        match = by_path.get(reference)
        if match is None:
            issues.append({"code": "EXPORT_MEDIA_UNREGISTERED", "message": "播放媒体或互动背景图未完整登记", "sourceId": reference[:200]})
            continue
        asset, poster = match
        record = checked_asset(asset, settings) if verify_source else export_asset_record(asset)
        if poster:
            if not asset.poster_relpath:
                issues.append({"code": "EXPORT_POSTER_MISSING", "message": "播放封面文件缺失", "sourceId": asset.id})
                continue
            record = {**record, "path": f"assets/{asset.id}-poster.jpg", "relpath": asset.poster_relpath, "bytes": None, "sha256": None}
            if verify_source:
                poster_path = resolve_asset_path(settings, asset.poster_relpath)
                record.update(bytes=poster_path.stat().st_size, sha256=file_sha256(poster_path))
        files[record["path"]] = record
        paths[reference] = record["path"]
    entry = source["metadata"].get("entryNodeId")
    if entry not in node_ids:
        issues.append({"code": "EXPORT_ENTRY_MISSING", "message": "项目缺少有效的游戏入口", "sourceId": entry})
    game = {"schemaVersion": 1, "title": source["title"], "graphData": rewrite_references(without_credentials(graph), {}, paths), "assets": [],
        "metadata": {"entryNodeId": entry, "locale": "zh-CN", "resolution": {"width": 1280, "height": 720}, "sourceRevision": revision}}
    return game, list(files.values()), issues


def create_export(session, owner_id, project_id, body, settings):
    lock_write(session)
    project = require_project(session, project_id, owner_id)
    identity = payload_digest({"projectId": project_id, "revision": body.expected_revision, "kind": body.kind})
    replay = matching_job(session, owner_id, body.kind, body.operation_id, identity)
    if replay:
        commit_transfer(session, replay, settings)
        return replay
    require_revision(project, body.expected_revision)
    if body.kind == "game":
        content, files, issues = game_snapshot(session, project, settings)
        private = {"game": content, "files": files}
    else:
        envelope = project_envelope(session, project)
        private = {**envelope, "files": [checked_asset(asset, settings) for asset in session.scalars(select(Asset).where(Asset.project_id == project_id, Asset.owner_id == owner_id))],
            "history": history_snapshot(session, project)}
        issues = []
    value = TransferJob(id=new_id(), owner_id=owner_id, project_id=project_id, kind=body.kind, operation_id=body.operation_id,
        identity_sha256=identity, source_revision=project.revision, status="blocked" if issues else "awaiting_confirmation" if body.kind == "game" else "queued",
        request_json={"phase": "build"}, private_json=private,
        result_json={"sourceFormat": BACKUP_SOURCE_FORMAT, "projects": [{"id": project.id, "title": project.title, "sceneCount": sum(node["type"] == "scene" for node in project.data_json["graphData"]["nodes"]),
            "canvasNodeCount": sum(len(canvas.data_json["nodes"]) for canvas in session.scalars(select(Canvas).where(Canvas.project_id == project.id))), "assetCount": len(private["files"])}], "issues": issues})
    value.confirmation_hash = payload_digest({"id": value.id, "revision": project.revision, "private": private})
    session.add(value)
    commit_transfer(session, value, settings)
    return value


def confirm_transfer(session, owner_id, identifier, confirmation_hash, settings):
    lock_write(session)
    value = require_transfer(session, identifier, owner_id)
    if value.confirmation_hash != confirmation_hash:
        raise AppError(409, "TRANSFER_CONFIRMATION_CHANGED", "确认内容已变化，请重新核对")
    if value.status in {"queued", "processing", "ready"}:
        commit_transfer(session, value, settings)
        return value
    if value.status != "awaiting_confirmation" or value.result_json.get("issues"):
        raise AppError(409, "TRANSFER_NOT_CONFIRMABLE", "当前记录尚未通过完整检查")
    if value.kind == "game":
        require_revision(require_project(session, value.project_id, owner_id), value.source_revision)
    value.status = "queued"
    value.request_json = {**value.request_json, "phase": "apply" if value.kind == "import" else "build"}
    value.updated_at = time.time()
    commit_transfer(session, value, settings)
    return value


def apply_import(session, value, settings):
    previous = session.scalar(select(TransferJob).where(TransferJob.owner_id == value.owner_id, TransferJob.kind == "import",
        TransferJob.source_digest == value.source_digest, TransferJob.status == "ready", TransferJob.id != value.id).order_by(TransferJob.created_at))
    if previous:
        value.project_id, value.result_json = previous.project_id, copy.deepcopy(previous.result_json)
        return
    for item in value.private_json["projects"]:
        snapshot = ProjectSnapshot.model_validate(item["project"]).model_dump(by_alias=True, exclude_unset=True)
        project = Project(id=snapshot["id"], owner_id=value.owner_id, title=snapshot["title"], data_json=snapshot, revision=1)
        session.add(project)
        session.flush()
        for record in item["files"]:
            path = resolve_asset_path(settings, record["relpath"])
            if path.stat().st_size != record["bytes"] or file_sha256(path) != record["sha256"]:
                raise AppError(409, "IMPORT_MEDIA_CHANGED", "预览后的媒体文件发生变化，导入已终止")
            session.add(Asset(id=record["id"], owner_id=value.owner_id, project_id=project.id, generation_id=None,
                name=record["name"], relpath=record["relpath"], poster_relpath=record["poster_relpath"], mime_type=record["mime_type"],
                bytes=record["bytes"], sha256=record["sha256"], duration_ms=record["duration_ms"], width=record["width"], height=record["height"],
                metadata_json={**record["metadata_json"], "importId": value.id, "sourceId": record["sourceId"]}))
        for canvas in item["canvases"]:
            session.add(Canvas(owner_id=value.owner_id, project_id=project.id, scene_id=canvas["sceneId"], data_json=canvas))
        record_project_version(session, project)
        value.project_id = value.project_id or project.id


def transfer_root(settings, identifier):
    root = (settings.data_dir / "transfers" / identifier).resolve()
    if not root.is_relative_to(settings.data_dir.resolve() / "transfers"):
        raise AppError(422, "TRANSFER_PATH_INVALID", "导入或导出目录无效")
    return root


def build_archive(value, settings, attempt_id):
    directory = transfer_root(settings, value.id) / attempt_id
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    files = value.private_json["files"]
    for record in files:
        source = resolve_asset_path(settings, record["relpath"])
        sha256 = file_sha256(source)
        # 封面使用独立生成的不可覆盖对象，在导出时保存实际文件摘要。
        pending_poster = (value.kind == "game" and record["relpath"] == record.get("posterRelpath")
                         and record["bytes"] is None and record["sha256"] is None)
        if not pending_poster and (source.stat().st_size != record["bytes"] or sha256 != record["sha256"]):
            raise AppError(409, "EXPORT_MEDIA_CHANGED", f"媒体文件已变化：{record['name']}")
        target = directory / checked_member(record["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if file_sha256(target) != sha256:
            raise AppError(507, "EXPORT_COPY_FAILED", "媒体复制后的摘要不一致")
    def write_json(name, content):
        (directory / name).write_text(json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    if value.kind == "game":
        write_json("game.json", value.private_json["game"])
        result = subprocess.run(["node", str(ROOT / "scripts/render_game.cjs"), str(directory / "game.json"), str(directory / "index.html")], capture_output=True, timeout=60)
        if result.returncode:
            raise AppError(500, "GAME_RENDER_FAILED", "互动游戏播放器生成失败，原项目及已完成版本保留")
    else:
        project = copy.deepcopy(value.private_json["project"])
        paths = {f"/api/v1/assets/{item['sourceId']}/content": item["path"] for item in files}
        for item in project["assets"]:
            if item["id"] in {record["sourceId"] for record in files}:
                record = next(record for record in files if record["sourceId"] == item["id"])
                paths[item["path"]] = record["path"]
                paths[item["relativePath"]] = record["path"]
        write_json("project.json", rewrite_references(without_credentials(project), {}, paths))
        write_json("canvases.json", rewrite_references(without_credentials(value.private_json["canvases"]), {}, paths))
        write_json("history.json", without_credentials(value.private_json["history"]))
        write_json("manifest.json", {"format": BACKUP_FORMAT, "version": 1, "sourceRevision": value.source_revision,
            "assets": [{key: record[key] for key in ("sourceId", "path", "name", "mimeType", "bytes", "sha256")} for record in files]})
    archive = directory.parent / f"{attempt_id}.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as output:
        for source in sorted(directory.rglob("*")):
            if source.is_file():
                output.write(source, source.relative_to(directory).as_posix())
    with zipfile.ZipFile(archive) as check:
        if check.testzip() is not None:
            raise AppError(507, "EXPORT_ARCHIVE_INVALID", "游戏或项目压缩包校验未通过")
    manifest = [{"path": source.relative_to(directory).as_posix(), "bytes": source.stat().st_size,
        "sha256": file_sha256(source)} for source in sorted(directory.rglob("*")) if source.is_file()]
    persist_file(settings, archive, "application/zip")
    if value.kind == "game":
        for record in manifest:
            path = directory / record["path"]
            persist_file(settings, path, mimetypes.guess_type(path)[0] or "application/octet-stream")
    return {"archive": archive.relative_to(settings.data_dir).as_posix(), "directory": directory.relative_to(settings.data_dir).as_posix(),
        "archiveSha256": file_sha256(archive), "archiveBytes": archive.stat().st_size, "fileManifest": manifest}
