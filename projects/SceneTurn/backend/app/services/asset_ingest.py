import copy
import time

from app.models import Asset
from app.services.canvas_delivery import public_project_envelope
from app.services.media import asset_view
from app.services.projects import iso_timestamp, project_envelope, record_project_version


def register_asset(session, project, media):
    asset = Asset(owner_id=project.owner_id, project_id=project.id, generation_id=None, name=media.name,
                  relpath=media.relpath, poster_relpath=media.poster_relpath, mime_type=media.mime_type,
                  bytes=media.bytes, sha256=media.sha256, duration_ms=media.duration_ms,
                  width=media.width, height=media.height, metadata_json=media.metadata_json)
    session.add(asset)
    session.flush()
    snapshot = copy.deepcopy(project.data_json)
    asset_type = "text" if media.mime_type.startswith("text/") else media.mime_type.split("/", 1)[0]
    now = time.time()
    snapshot["assets"].append({
        "id": asset.id, "type": asset_type, "name": asset.name,
        "path": f"/api/v1/assets/{asset.id}/content", "relativePath": f"assets/{asset.id}",
        "importedAt": iso_timestamp(now),
        "metadata": {**media.metadata_json, "duration": (asset.duration_ms or 0) / 1000, "width": asset.width, "height": asset.height,
                     "poster": f"/api/v1/assets/{asset.id}/poster" if asset.poster_relpath else None},
    })
    snapshot["updatedAt"] = iso_timestamp(now)
    project.data_json = snapshot
    project.revision += 1
    project.updated_at = now
    record_project_version(session, project)
    return asset


def uploaded_asset_view(session, project, asset):
    return {"asset": asset_view(asset), **public_project_envelope(session, project.owner_id, project_envelope(session, project))}
