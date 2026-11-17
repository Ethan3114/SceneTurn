import copy
import hashlib
import time
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.models import Asset, Canvas, new_id
from app.services.media import resolve_asset_path
from app.services.projects import get_operation, iso_timestamp, lock_write, project_envelope, record_project_version, require_project, require_revision, require_scene, save_operation


def split_cuts(lines: list[float] | None, count: int, size: int) -> list[int]:
    if lines is not None and (len(lines) != count - 1 or any(not 0 < value < 1 for value in lines) or lines != sorted(set(lines))):
        raise AppError(422, "SPLIT_LINES_INVALID", "切线需要按顺序排列，数量与行列一致，并位于图片内部")
    cuts = [0, *[round(value * size) for value in lines], size] if lines is not None else [index * size // count for index in range(count + 1)]
    if any(right <= left for left, right in zip(cuts, cuts[1:])):
        raise AppError(422, "SPLIT_PIECE_EMPTY", "切线过于接近，无法生成至少一个像素的切片")
    return cuts


def split_image(session: Session, project_id: str, owner_id: str, payload: dict, settings: Settings, *, manage_transaction: bool = True) -> dict:
    if manage_transaction:
        lock_write(session)
    project = require_project(session, project_id, owner_id)
    replay = get_operation(session, owner_id, project_id, payload["operationId"], "image.split", payload)
    if replay is not None:
        return replay
    require_revision(project, payload["expectedRevision"])
    require_scene(project, payload["sceneId"])
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project_id, Canvas.owner_id == owner_id, Canvas.scene_id == payload["sceneId"]))
    if canvas is None:
        raise AppError(404, "CANVAS_NOT_FOUND", "图片所属的剧情画布不存在")
    data = copy.deepcopy(canvas.data_json)
    source = next((node for node in data["nodes"] if node["id"] == payload["sourceNodeId"]), None)
    if source is None or source["type"] != "image":
        raise AppError(422, "SPLIT_SOURCE_INVALID", "请选择当前剧情画布中的图片节点")
    asset_id = source.get("metadata", {}).get("storageKey")
    asset = session.scalar(select(Asset).where(Asset.id == asset_id, Asset.owner_id == owner_id, Asset.project_id == project_id)) if asset_id else None
    if asset is None or not asset.mime_type.startswith("image/"):
        raise AppError(422, "SPLIT_ASSET_INVALID", "请先将完整图片保存为当前项目的素材")
    path = resolve_asset_path(settings, asset.relpath)
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original)
        if image.width * image.height > 64000000:
            raise AppError(413, "SPLIT_IMAGE_TOO_LARGE", "图片切片支持最多6400万个像素")
        image.load()
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    x_cuts = split_cuts(payload.get("verticalLines"), payload["columns"], image.width)
    y_cuts = split_cuts(payload.get("horizontalLines"), payload["rows"], image.height)
    directory = settings.data_dir / "media"
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = copy.deepcopy(project.data_json)
    pieces = []
    timestamp = iso_timestamp(time.time())
    tile_width = 260
    tile_height = max(120, min(360, tile_width * image.height / image.width))
    anchor_x = source["position"]["x"] + source["width"] + 96
    anchor_y = source["position"]["y"]
    current_nodes = data["nodes"]
    area_right = anchor_x + payload["columns"] * (tile_width + 32)
    area_bottom = anchor_y + payload["rows"] * (tile_height + 64)
    overlaps = [node for node in current_nodes if node["type"] != "group" and node["position"]["x"] < area_right and node["position"]["x"] + node["width"] > anchor_x and node["position"]["y"] < area_bottom and node["position"]["y"] + node["height"] > anchor_y]
    if overlaps:
        anchor_y = max(node["position"]["y"] + node["height"] for node in current_nodes) + 64
    for row in range(payload["rows"]):
        for column in range(payload["columns"]):
            crop_box = (x_cuts[column], y_cuts[row], x_cuts[column + 1], y_cuts[row + 1])
            piece = image.crop(crop_box)
            identifier = new_id()
            destination = directory / f"{identifier}.png"
            piece.save(destination, format="PNG")
            destination.chmod(0o600)
            with destination.open("rb") as stream:
                sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
            name = f"{Path(asset.name).stem} · {row + 1}-{column + 1}.png"
            metadata = {"sourceAssetId": asset.id, "sourceNodeId": source["id"], "row": row, "column": column, "cropBox": list(crop_box)}
            child = Asset(id=identifier, owner_id=owner_id, project_id=project_id, name=name, relpath=destination.relative_to(settings.data_dir).as_posix(), mime_type="image/png", bytes=destination.stat().st_size, sha256=sha256, duration_ms=0, width=piece.width, height=piece.height, metadata_json=metadata)
            session.add(child)
            node_id = new_id()
            url = f"/api/v1/assets/{identifier}/content"
            data["nodes"].append({
                "id": node_id, "type": "image", "title": name,
                "position": {"x": anchor_x + column * (tile_width + 32), "y": anchor_y + row * (tile_height + 64)},
                "width": tile_width, "height": tile_height,
                "metadata": {"content": url, "storageKey": identifier, "status": "success", "naturalWidth": piece.width, "naturalHeight": piece.height, "mimeType": "image/png", "bytes": child.bytes},
            })
            data["connections"].append({"id": new_id(), "fromNodeId": source["id"], "toNodeId": node_id})
            snapshot["assets"].append({"id": identifier, "type": "image", "name": name, "path": url, "relativePath": f"assets/{identifier}", "importedAt": timestamp, "metadata": {**metadata, "width": piece.width, "height": piece.height, "size": child.bytes, "mimeType": "image/png"}})
            pieces.append({"nodeId": node_id, "assetId": identifier, "row": row, "column": column})
    canvas.data_json = data
    canvas.updated_at = time.time()
    snapshot["updatedAt"] = timestamp
    project.data_json = snapshot
    project.revision += 1
    project.updated_at = time.time()
    record_project_version(session, project)
    result = {**project_envelope(session, project), "pieces": pieces}
    save_operation(session, owner_id, project_id, payload["operationId"], "image.split", payload, result)
    if manage_transaction:
        session.commit()
    return result
