import asyncio
import copy
import io
import json
import mimetypes
import stat
import time
import uuid
import zipfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from urllib.request import urlopen

from fastapi import UploadFile
from app.core.errors import AppError
from app.schemas import ProjectSnapshot
from app.services.media import save_upload
from app.services.projects import empty_canvas, initial_project, iso_timestamp, payload_digest
from app.transfer_schemas import BACKUP_SOURCE_FORMAT, BackupManifest, CanvasArchive, ImportCanvasSnapshot


SENSITIVE_KEYS = {"apikey", "api_key", "api_key_encrypted", "apikeyencrypted", "authorization", "password_hash", "access_token", "accesstoken", "sessiontoken", "pairingcode", "pairing_code", "token_hash", "privatekey", "private_key", "client_secret", "auth_token"}
MEDIA_FIELDS = {"src", "poster", "thumbnail", "backgroundImageSrc", "path", "relativePath", "url", "content"}
SUPPORTED_CANVAS_TYPES = {"text", "image", "video", "audio", "config", "group", "agent"}
PROJECT_MANIFEST_NAMES = ("project.json", "project.openfmv.json")
# 导入任务重新执行时需要复用原来的项目和素材标识。
TRANSFER_ID_NAMESPACE = "openfmv"


def without_credentials(value, credential_context=False):
    if isinstance(value, dict):
        current_credentials = credential_context or bool(set(value) & {"connectionId", "pairingId", "password_hash", "api_key", "apiKey"})
        return {key: without_credentials(item, credential_context or key.lower() in {"credentials", "authentication", "headers"}) for key, item in value.items()
            if key.lower() not in SENSITIVE_KEYS and not (current_credentials and key.lower() in {"password", "secret", "token", "cookie"})}
    if isinstance(value, list):
        return [without_credentials(item, credential_context) for item in value]
    return value


def checked_member(name: str) -> str:
    path = PurePosixPath(name)
    if not name or "\\" in name or "\x00" in name or path.is_absolute() or any(part in {"..", "."} for part in name.split("/")) or ":" in path.parts[0]:
        raise AppError(422, "ARCHIVE_PATH_UNSAFE", "压缩包包含不安全的文件路径")
    return path.as_posix()


class ArchiveReader:
    def __init__(self, archive, settings):
        self.zip = archive
        self.entries = {}
        size = 0
        if len(self.zip.infolist()) > 50000:
            raise AppError(413, "ARCHIVE_TOO_MANY_FILES", "压缩包文件数量超过50000项")
        for item in self.zip.infolist():
            name = checked_member(item.filename.rstrip("/"))
            if name in self.entries or item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16) or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}:
                raise AppError(422, "ARCHIVE_ENTRY_UNSAFE", "压缩包包含重复路径、加密文件或符号链接")
            size += item.file_size
            if size > settings.max_upload_bytes * 4 or item.file_size > settings.max_upload_bytes or item.file_size > max(1, item.compress_size) * 1000:
                raise AppError(413, "ARCHIVE_EXPANSION_LIMIT", "压缩包解压大小或压缩率超过限制")
            self.entries[name] = item

    def root_file(self, filename: str) -> str | None:
        values = [name for name in self.entries if PurePosixPath(name).name == filename and not name.startswith("__MACOSX/")]
        if len(values) > 1:
            raise AppError(422, "ARCHIVE_ROOT_AMBIGUOUS", "压缩包中存在多个项目清单，请分别导入")
        return values[0] if values else None

    def read_json(self, name: str):
        if self.entries[name].file_size > 64 * 1024 * 1024:
            raise AppError(413, "ARCHIVE_JSON_TOO_LARGE", "项目清单超过64MB")
        value = json.loads(self.zip.read(name).decode("utf-8-sig"))
        try:
            json.dumps(value, allow_nan=False)
        except ValueError as failure:
            raise AppError(422, "IMPORT_JSON_INVALID", "项目清单包含无效数值") from failure
        return value


def stable_id(job_id: str, kind: str, source_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{TRANSFER_ID_NAMESPACE}:{job_id}:{kind}:{source_id}"))


def media_references(value):
    references = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"src", "poster", "backgroundImageSrc"} and isinstance(item, str) and item:
                references.append(item)
            references.extend(media_references(item))
    elif isinstance(value, list):
        for item in value:
            references.extend(media_references(item))
    return references


def rewrite_references(value, identifiers: dict, paths: dict, preserve_text=False):
    if isinstance(value, list):
        return [rewrite_references(item, identifiers, paths, preserve_text) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    storage_key = value.get("storageKey")
    for key, item in value.items():
        if key in {"assetId", "storageKey", "backgroundImageAssetId"} and isinstance(item, str) and item in identifiers:
            result[key] = identifiers[item]
        elif key in MEDIA_FIELDS and isinstance(item, str) and item in paths and (key != "content" or not preserve_text):
            result[key] = paths[item]
        elif key in {"content", "url", "src"} and isinstance(storage_key, str) and storage_key in identifiers and (key != "content" or not preserve_text):
            result[key] = f"/api/v1/assets/{identifiers[storage_key]}/content"
        else:
            result[key] = rewrite_references(item, identifiers, paths, preserve_text or key == "metadata" and value.get("type") in {"text", "config", "agent"})
    if isinstance(storage_key, str) and storage_key in identifiers and "content" not in result and not preserve_text:
        result["content"] = f"/api/v1/assets/{identifiers[storage_key]}/content"
    return result


def restored_canvas(canvas):
    for node in canvas["nodes"]:
        metadata = node.get("metadata")
        if isinstance(metadata, dict):
            references = {key: metadata.pop(key) for key in ("textGenerationId", "videoTaskId", "videoTaskProvider") if key in metadata}
            if references:
                metadata["importedTaskReferences"] = references
                metadata["status"] = "success" if metadata.get("storageKey") or metadata.get("content") else "idle"
                metadata["pendingResult"] = False
            if metadata.get("status") == "loading":
                metadata["importedStatus"] = "loading"
                metadata["status"] = "success" if metadata.get("storageKey") else "idle"
                metadata["pendingResult"] = False
    return canvas


def import_preview(job_id: str, path: Path, settings) -> dict:
    with zipfile.ZipFile(path) as archive:
        reader = ArchiveReader(archive, settings)
        canvas_root = reader.root_file("projects.json")
        project_roots = [root for filename in PROJECT_MANIFEST_NAMES if (root := reader.root_file(filename)) is not None]
        if len(project_roots) > 1 or canvas_root and project_roots:
            raise AppError(422, "ARCHIVE_ROOT_AMBIGUOUS", "压缩包中存在多个项目清单，请分别导入")
        if canvas_root:
            return preview_canvas(reader, canvas_root, job_id, settings)
        if project_roots:
            return preview_project(reader, project_roots[0], job_id, settings)
        raise AppError(422, "PROJECT_MANIFEST_MISSING", "压缩包缺少项目清单 project.json 或无限画布第三版清单 projects.json")


def stored_file(reader, member, source_id, name, job_id, settings, issues, expected_bytes=None, expected_mime=None):
    member = checked_member(member)
    if member not in reader.entries or reader.entries[member].is_dir():
        issues.append({"code": "IMPORT_MEDIA_MISSING", "message": f"缺少媒体文件：{name}", "sourceId": source_id})
        return None
    if expected_bytes is not None and reader.entries[member].file_size != expected_bytes:
        issues.append({"code": "IMPORT_MEDIA_SIZE_CHANGED", "message": f"媒体大小与来源记录不符：{name}", "sourceId": source_id})
        return None
    with reader.zip.open(reader.entries[member]) as source:
        media = asyncio.run(save_upload(UploadFile(filename=PurePosixPath(name).name, file=source), settings))
    if expected_mime not in {None, "", "application/octet-stream", media.mime_type}:
        raise AppError(422, "IMPORT_MEDIA_TYPE_CHANGED", "媒体实际格式与来源记录不符")
    return {"id": stable_id(job_id, "asset", source_id), "sourceId": source_id, "archivePath": member, **asdict(media)}


def asset_snapshot(item):
    identifier = item["id"]
    return {"id": identifier, "type": "text" if item["mime_type"].startswith("text/") else item["mime_type"].split("/")[0],
        "name": item["name"], "path": f"/api/v1/assets/{identifier}/content", "relativePath": f"assets/{identifier}",
        "importedAt": iso_timestamp(time.time()), "metadata": {**item["metadata_json"], "duration": (item["duration_ms"] or 0) / 1000,
            "width": item["width"], "height": item["height"], "poster": f"/api/v1/assets/{identifier}/poster" if item["poster_relpath"] else None}}


def preview_result(source_format, projects, files, issues, mappings, history, semantic):
    summaries = [{"id": item["project"]["id"], "title": item["project"]["title"],
        "sceneCount": sum(node["type"] == "scene" for node in item["project"]["graphData"]["nodes"]),
        "canvasNodeCount": sum(len(canvas["nodes"]) for canvas in item["canvases"]), "assetCount": len(item["files"])} for item in projects]
    return {"sourceFormat": source_format, "projects": projects, "files": files, "history": history,
        "sourceDigest": payload_digest(semantic), "view": {"sourceFormat": source_format, "projects": summaries, "mappings": mappings, "issues": issues}}


def preview_canvas(reader, root, job_id, settings):
    archive = CanvasArchive.model_validate(reader.read_json(root))
    parent = PurePosixPath(root).parent
    issues, mappings, files, projects = [], [], [], []
    source_ids = [item.project.id for item in archive.projects]
    if len(source_ids) != len(set(source_ids)):
        raise AppError(422, "IMPORT_SOURCE_ID_DUPLICATE", "来源项目标识重复")
    for entry in archive.projects:
        source = entry.project.model_dump(by_alias=True, exclude_unset=True)
        project_id = stable_id(job_id, "project", source["id"])
        project, scene_id = initial_project(project_id, source["title"])
        scene_id = stable_id(job_id, "scene", source["id"])
        old_scene_id = project["graphData"]["nodes"][1]["id"]
        project["graphData"]["nodes"][1]["id"] = scene_id
        for edge in project["graphData"]["edges"]:
            for key in ("source", "target"):
                if edge[key] == old_scene_id:
                    edge[key] = scene_id
        identifiers, paths, project_files = {}, {}, []
        for item in entry.files:
            if item.storage_key in identifiers:
                raise AppError(422, "IMPORT_SOURCE_ID_DUPLICATE", "来源媒体标识重复")
            record = stored_file(reader, (parent / checked_member(item.path)).as_posix(), f"{source['id']}:{item.storage_key}", PurePosixPath(item.path).name,
                job_id, settings, issues, item.bytes, item.mime_type)
            if record:
                identifiers[item.storage_key] = record["id"]
                project_files.append(record)
                mappings.append({"kind": "asset", "sourceId": item.storage_key, "targetId": record["id"]})
        for node in source["nodes"]:
            if node["type"] not in SUPPORTED_CANVAS_TYPES:
                issues.append({"code": "IMPORT_NODE_UNSUPPORTED", "message": f"当前工作台无法转换该节点类型：{node['type']}", "sourceId": node["id"]})
            metadata = node.get("metadata", {})
            content = metadata.get("content")
            if node["type"] in {"image", "video", "audio"} and isinstance(content, str) and content and not metadata.get("storageKey"):
                if content.startswith("data:"):
                    with urlopen(content) as response:
                        raw, mime = response.read(), response.headers.get_content_type()
                    suffix = mimetypes.guess_extension(mime)
                    if suffix is None:
                        raise AppError(422, "IMPORT_MEDIA_TYPE_UNSUPPORTED", "内嵌媒体格式不受支持")
                    media = asyncio.run(save_upload(UploadFile(filename=f"内嵌媒体{suffix}", file=io.BytesIO(raw)), settings))
                    if media.mime_type != mime or not media.mime_type.startswith(f"{node['type']}/"):
                        raise AppError(422, "IMPORT_MEDIA_TYPE_CHANGED", "内嵌媒体的实际格式与声明不符")
                    key = f"inline:{source['id']}:{node['id']}"
                    record = {"id": stable_id(job_id, "asset", key), "sourceId": key, "archivePath": root, **asdict(media)}
                    project_files.append(record)
                    identifiers[key] = record["id"]
                    metadata["storageKey"] = key
                    paths[content] = f"/api/v1/assets/{record['id']}/content"
                    mappings.append({"kind": "asset", "sourceId": node["id"], "targetId": record["id"]})
                else:
                    issues.append({"code": "IMPORT_MEDIA_MISSING", "message": "该媒体节点没有可随项目保存的文件", "sourceId": node["id"]})
        def inspect(value):
            if isinstance(value, dict):
                key = value.get("storageKey")
                if isinstance(key, str) and key and key not in identifiers:
                    issues.append({"code": "IMPORT_MEDIA_MISSING", "message": "引用的媒体未包含在导出包中", "sourceId": key})
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)
        inspect(source["nodes"])
        canvas = restored_canvas(ImportCanvasSnapshot.model_validate({"sceneId": scene_id, "nodes": rewrite_references(source["nodes"], identifiers, paths),
            "connections": source["connections"], "viewport": source["viewport"]}).model_dump(by_alias=True, exclude_unset=True))
        project["assets"] = [asset_snapshot(item) for item in project_files]
        project["metadata"]["importId"] = job_id
        projects.append({"project": project, "canvases": [canvas], "files": project_files})
        files.extend(project_files)
        mappings.extend([{"kind": "project", "sourceId": source["id"], "targetId": project_id}, {"kind": "scene", "sourceId": source["id"], "targetId": scene_id}])
        mappings.extend({"kind": "node", "sourceId": node["id"], "targetId": node["id"]} for node in source["nodes"])
    semantic = {"projects": [item.model_dump(by_alias=True, exclude_unset=True) for item in archive.projects], "files": sorted((item["sourceId"], item["sha256"]) for item in files)}
    return preview_result("infinite-canvas-v3", projects, files, issues, mappings, {"sourceProjects": [without_credentials(item.project.model_dump(by_alias=True, exclude_unset=True)) for item in archive.projects]}, without_credentials(semantic))


def preview_project(reader, root, job_id, settings):
    original = reader.read_json(root)
    source = ProjectSnapshot.model_validate(original).model_dump(by_alias=True, exclude_unset=True)
    parent = PurePosixPath(root).parent
    backup_root = (parent / "manifest.json").as_posix()
    backup = BackupManifest.model_validate(reader.read_json(backup_root)).model_dump(by_alias=True) if backup_root in reader.entries else None
    issues, files, identifiers, paths, mappings = [], [], {}, {}, []
    declared = backup["assets"] if backup is not None else [{"sourceId": asset["id"], "path": asset["relativePath"], "name": asset["name"]} for asset in source["assets"]]
    declared_ids = set()
    for item in declared:
        source_id = item["sourceId"]
        if source_id in declared_ids:
            raise AppError(422, "IMPORT_SOURCE_ID_DUPLICATE", "来源素材标识重复")
        declared_ids.add(source_id)
        record = stored_file(reader, (parent / checked_member(item["path"])).as_posix(), source_id, item["name"], job_id, settings, issues, item.get("bytes"), item.get("mimeType"))
        if record:
            if item.get("sha256") is not None and record["sha256"] != item["sha256"]:
                raise AppError(422, "IMPORT_MEDIA_DIGEST_CHANGED", "媒体摘要与备份记录不一致")
            files.append(record)
            identifiers[source_id] = record["id"]
            paths[item["path"]] = f"/api/v1/assets/{record['id']}/content"
            paths[f"/api/v1/assets/{source_id}/content"] = paths[item["path"]]
            if record["poster_relpath"]:
                paths[f"/api/v1/assets/{source_id}/poster"] = f"/api/v1/assets/{record['id']}/poster"
            for old in source["assets"]:
                if old["id"] == source_id:
                    paths[old["path"]] = paths[item["path"]]
                    paths[old["relativePath"]] = paths[item["path"]]
            mappings.append({"kind": "asset", "sourceId": source_id, "targetId": record["id"]})
    for reference in set(media_references(source["graphData"])):
        if reference in paths:
            continue
        if reference.startswith(("/", "http:", "https:", "file:", "blob:", "data:")):
            issues.append({"code": "IMPORT_MEDIA_MISSING", "message": "时间线媒体引用未包含在项目包中", "sourceId": reference[:200]})
            continue
        record = stored_file(reader, (parent / checked_member(reference)).as_posix(), reference, PurePosixPath(reference).name, job_id, settings, issues)
        if record:
            files.append(record)
            paths[reference] = f"/api/v1/assets/{record['id']}/content"
            mappings.append({"kind": "asset", "sourceId": reference, "targetId": record["id"]})
    project_id = stable_id(job_id, "project", source["id"])
    project = rewrite_references(without_credentials(source), identifiers, paths)
    project["id"] = project_id
    project["assets"] = [asset_snapshot(item) for item in files]
    project["metadata"]["importId"] = job_id
    project["metadata"].pop("serverRevision", None)
    canvas_path, history_path = (parent / "canvases.json").as_posix(), (parent / "history.json").as_posix()
    canvases = reader.read_json(canvas_path) if canvas_path in reader.entries else [empty_canvas(node["id"]) for node in project["graphData"]["nodes"] if node["type"] == "scene"]
    source_canvases = copy.deepcopy(canvases)
    canvases = [restored_canvas(ImportCanvasSnapshot.model_validate(rewrite_references(without_credentials(canvas), identifiers, paths)).model_dump(by_alias=True, exclude_unset=True)) for canvas in canvases]
    for canvas in canvases:
        for node in canvas["nodes"]:
            if node["type"] not in SUPPORTED_CANVAS_TYPES:
                issues.append({"code": "IMPORT_NODE_UNSUPPORTED", "message": f"当前工作台无法转换该节点类型：{node['type']}", "sourceId": node["id"]})
    scene_ids = {node["id"] for node in project["graphData"]["nodes"] if node["type"] == "scene"}
    if {canvas["sceneId"] for canvas in canvases} != scene_ids or len(canvases) != len(scene_ids):
        raise AppError(422, "IMPORT_SCENE_MAPPING_INVALID", "项目场景与素材画布的对应关系不完整")
    history = without_credentials(reader.read_json(history_path)) if history_path in reader.entries else {}
    if not isinstance(history, dict):
        raise AppError(422, "IMPORT_HISTORY_INVALID", "项目历史需要完整的结构化对象")
    history = {**history, "sourceProject": without_credentials(original), "sourceCanvases": without_credentials(source_canvases)}
    mappings.append({"kind": "project", "sourceId": source["id"], "targetId": project_id})
    mappings.extend({"kind": "scene", "sourceId": node["id"], "targetId": node["id"]} for node in project["graphData"]["nodes"] if node["type"] == "scene")
    semantic = {"project": without_credentials(source), "canvases": without_credentials(reader.read_json(canvas_path)) if canvas_path in reader.entries else [],
        "history": history, "files": sorted((item["sourceId"], item["sha256"]) for item in files)}
    return preview_result(BACKUP_SOURCE_FORMAT if backup else "sceneturn-project", [{"project": project, "canvases": canvases, "files": files}], files, issues, mappings, history, semantic)
