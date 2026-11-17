import hashlib

import filetype
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import AppError
from app.image_models import ImageJob
from app.models import Asset
from app.services.media import resolve_asset_path
from app.services.projects import payload_digest, require_project
from app.services.sensetime import MAX_IMAGE_BYTES


IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def validate_reference_assets(session, project, asset_ids, settings, frozen=None):
    if len(asset_ids) > 5 or len(set(asset_ids)) != len(asset_ids):
        raise AppError(422, "MATERIAL_REFERENCE_ASSET_LIMIT", "参考图片最多选择5张，且不能重复")
    expected = {item["assetId"]: item for item in frozen or []}
    if asset_ids and settings is None:
        settings = get_settings()
    result = []
    for identifier in asset_ids:
        asset = session.scalar(select(Asset).where(Asset.id == identifier, Asset.owner_id == project.owner_id, Asset.project_id == project.id))
        if asset is None:
            raise AppError(422, "MATERIAL_REFERENCE_ASSET_INVALID", "参考图片不属于当前用户与项目")
        if asset.mime_type not in IMAGE_MIME_TYPES:
            raise AppError(422, "MATERIAL_REFERENCE_IMAGE_REQUIRED", "参考素材必须是PNG、JPEG、WebP或GIF图片")
        path = resolve_asset_path(settings, asset.relpath)
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise AppError(422, "MATERIAL_REFERENCE_IMAGE_TOO_LARGE", "单张参考图片不能超过32MiB")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != asset.sha256 or path.stat().st_size != asset.bytes or (identifier in expected and digest != expected[identifier]["sha256"]):
            raise AppError(409, "MATERIAL_REFERENCE_ASSET_CHANGED", "参考图片内容已经变化，请重新选择当前版本")
        if filetype.guess_mime(str(path)) != asset.mime_type:
            raise AppError(422, "MATERIAL_REFERENCE_IMAGE_INVALID", "参考图片的真实文件类型与登记类型不一致")
        try:
            with Image.open(path) as image:
                if Image.MIME.get(image.format) != asset.mime_type:
                    raise AppError(422, "MATERIAL_REFERENCE_IMAGE_INVALID", "参考图片的真实文件类型与登记类型不一致")
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
            raise AppError(422, "MATERIAL_REFERENCE_IMAGE_INVALID", "参考图片文件无法完整读取") from error
        result.append(expected.get(identifier, {"assetId": asset.id, "name": asset.name, "sha256": digest}))
    return result


def frozen_reference_assets(session, run, settings):
    frozen = run.limits_json.get("materialContext", {}).get("referenceAssets", [])
    if not frozen:
        return []
    project = require_project(session, run.project_id, run.owner_id)
    return validate_reference_assets(session, project, [item["assetId"] for item in frozen], settings, frozen)


def current_round_asset_ids(session, run, canvas):
    allowed_nodes = {*run.target_ids_json, *run.created_ids_json}
    nodes = [node for node in canvas.data_json["nodes"] if node["id"] in allowed_nodes]
    identifiers = {item["assetId"] for item in run.limits_json.get("materialContext", {}).get("referenceAssets", [])}
    identifiers.update(node.get("metadata", {}).get(key) for node in nodes for key in ("assetId", "storageKey"))
    artifacts = {node["id"]: payload_digest(node.get("metadata", {}).get("creativeArtifact")) for node in nodes}
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id, ImageJob.status == "ready")).all()
    identifiers.update(job.asset_id for job in jobs if artifacts.get(job.node_id) == job.artifact_hash)
    return identifiers
