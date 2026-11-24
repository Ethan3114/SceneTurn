from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.api.transfers import game_file, game_response
from app.core.config import get_settings
from app.core.errors import AppError
from app.models import Asset, User
from app.publication_models import Publication
from app.schemas import WireModel
from app.services.cloudbase_database import gateway_enabled
from app.services.cloudbase_publications import CloudBasePublications, PublicationRecord
from app.services.media import resolve_asset_path
from app.services.object_storage import private_storage
from app.services.project_transfers import require_transfer
from app.services.projects import iso_timestamp, lock_write
from app.services.transfer_archives import checked_member
from app.transfer_models import TransferJob


router = APIRouter(tags=["publications"])
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
PLAYER_POLICY = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; "
                 "style-src 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; "
                 "frame-ancestors 'self'; form-action 'none'; base-uri 'none'")


class PublicationInput(WireModel):
    published: bool
    title: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=2000)
    poster_asset_id: str | None = Field(default=None, alias="posterAssetId", max_length=36)
    trailer_asset_id: str | None = Field(default=None, alias="trailerAssetId", max_length=36)


def view(row: Publication | PublicationRecord, *, owner: bool = False) -> dict:
    base = f"/api/v1/publications/{row.slug}"
    result = {"slug": row.slug, "title": row.title, "description": row.description, "createdAt": iso_timestamp(row.created_at),
              "posterUrl": f"{base}/media/poster" if row.poster_asset_id else None,
              "trailerUrl": f"{base}/media/trailer" if row.trailer_asset_id else None,
              "playUrl": f"{base}/play/index.html" if row.published else None}
    if owner:
        result.update(published=row.published, posterAssetId=row.poster_asset_id, trailerAssetId=row.trailer_asset_id)
    return result


def public_game(session: Session, slug: str) -> tuple[Publication, TransferJob]:
    value = session.execute(select(Publication, TransferJob).join(TransferJob, TransferJob.id == Publication.transfer_id)
        .where(Publication.slug == slug, Publication.published.is_(True), TransferJob.status == "ready", TransferJob.kind == "game")).first()
    if value is None:
        raise AppError(404, "PUBLICATION_NOT_FOUND", "作品暂不可访问")
    return value[0], value[1]


def publication_asset(session: Session, transfer: TransferJob, asset_id: str, kind: Literal["image", "video"]) -> Asset:
    asset = session.get(Asset, asset_id)
    if (asset is None or asset.owner_id != transfer.owner_id or asset.project_id != transfer.project_id
            or not asset.mime_type.startswith(f"{kind}/") or asset.mime_type == "image/svg+xml"):
        raise AppError(404, "PUBLICATION_MEDIA_NOT_FOUND", "作品媒体不存在或不属于该项目")
    return asset


@router.get("/publications")
def list_publications(session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        rows = CloudBasePublications(settings).list()
    else:
        rows = session.scalars(select(Publication).join(TransferJob, TransferJob.id == Publication.transfer_id)
            .where(Publication.published.is_(True), TransferJob.status == "ready", TransferJob.kind == "game")
            .order_by(Publication.created_at.desc(), Publication.slug).limit(100))
    return JSONResponse({"publications": [view(row) for row in rows]}, headers=NO_STORE)


@router.get("/publications/{slug}")
def read_publication(slug: str, session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        row = CloudBasePublications(settings).read(slug)
    else:
        row, _ = public_game(session, slug)
    return JSONResponse({"publication": view(row)}, headers=NO_STORE)


@router.get("/publications/{slug}/play/{name:path}")
def play_publication(slug: str, name: str, request: Request, session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        name = checked_member(name)
        transfer = CloudBasePublications(settings).game(slug, name).as_transfer()
    else:
        _, transfer = public_game(session, slug)
        name = checked_member(name)
    headers = {**NO_STORE, "Content-Security-Policy": PLAYER_POLICY} if name == "index.html" else NO_STORE
    return game_response(transfer, name, settings, request, headers=headers)


@router.get("/publications/{slug}/media/{role}")
def public_media(slug: str, role: Literal["poster", "trailer"], request: Request, session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        asset = CloudBasePublications(settings).media(slug, role)
    else:
        row, transfer = public_game(session, slug)
        asset_id = row.poster_asset_id if role == "poster" else row.trailer_asset_id
        if asset_id is None:
            raise AppError(404, "PUBLICATION_MEDIA_NOT_FOUND", "作品媒体尚未配置")
        asset = publication_asset(session, transfer, asset_id, "image" if role == "poster" else "video")
    storage = private_storage(settings)
    if storage is not None:
        return storage.response(asset.relpath, media_type=asset.mime_type, headers=NO_STORE,
                                range_header=request.headers.get("range"))
    return FileResponse(resolve_asset_path(settings, asset.relpath), media_type=asset.mime_type, headers=NO_STORE)


@router.get("/transfers/{identifier}/publication")
def own_publication(identifier: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        row = CloudBasePublications(settings).owner(user.id, identifier)
    else:
        require_transfer(session, identifier, user.id)
        row = session.scalar(select(Publication).where(Publication.transfer_id == identifier))
    return {"publication": view(row, owner=True) if row else None}


@router.put("/transfers/{identifier}/publication")
def save_publication(identifier: str, body: PublicationInput, user: User = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        repository = CloudBasePublications(settings)
        title = body.title.strip()
        verified = None
        if body.published:
            verified = repository.prepare(user.id, identifier, title)
            game_file(verified.as_transfer(), "index.html", settings)
        row = repository.save(user.id, identifier, published=body.published, title=title,
            description=body.description.strip(), poster_asset_id=body.poster_asset_id,
            trailer_asset_id=body.trailer_asset_id, verified=verified)
        return {"publication": view(row, owner=True) if row else None}
    lock_write(session)
    transfer = require_transfer(session, identifier, user.id)
    row = session.scalar(select(Publication).where(Publication.transfer_id == identifier))
    if not body.published:
        if row:
            row.published = False
        session.commit()
        return {"publication": view(row, owner=True) if row else None}
    title = body.title.strip()
    if not title:
        raise AppError(422, "PUBLICATION_TITLE_REQUIRED", "请填写作品名称")
    game_file(transfer, "index.html", settings)
    if body.poster_asset_id:
        publication_asset(session, transfer, body.poster_asset_id, "image")
    if body.trailer_asset_id:
        publication_asset(session, transfer, body.trailer_asset_id, "video")
    if row is None:
        row = Publication(transfer_id=identifier)
        session.add(row)
    row.title, row.description, row.published = title, body.description.strip(), True
    row.poster_asset_id, row.trailer_asset_id = body.poster_asset_id, body.trailer_asset_id
    session.commit()
    return {"publication": view(row, owner=True)}
