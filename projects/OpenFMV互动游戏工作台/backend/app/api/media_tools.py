import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from functools import lru_cache
from typing import Literal
from pathlib import Path
from threading import BoundedSemaphore

from fastapi import APIRouter, Depends, File, Path as ApiPath, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.deps import current_user, get_db
from app.api.local_audio import router as local_audio_router
from app.core.config import get_settings
from app.core.errors import AppError
from app.models import Asset, User
from app.schemas import ProjectEnvelope, WireModel
from app.services.canvas_delivery import public_project_envelope
from app.services.canvas_media_edit import MediaEditRequest, render_edit
from app.services.durable_files import persist_file
from app.services.image_split import split_image
from app.services.media import resolve_asset_path
from app.services.object_storage import private_storage
from app.services.projects import require_project


router = APIRouter(tags=["media-tools"])
router.include_router(local_audio_router)
_render_slot = BoundedSemaphore(1)
_DIRECTOR_MODEL_LIMIT = 16 * 1024 * 1024


@lru_cache(maxsize=1)
def system_voices() -> dict[str, str]:
    if not shutil.which("say"):
        return {}
    result = subprocess.run(["say", "-v", "?"], check=True, capture_output=True, text=True, timeout=15)
    voices = {}
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"(.+?)\s+((?:zh|en)_[A-Z]{2})\s+#.*", line)
        if match:
            voices[match[1]] = match[2]
    return voices


class NarrationRequest(WireModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str = Field(min_length=1, max_length=100)
    expression: Literal["自然", "平静", "活泼", "严肃"] = "自然"
    speed: float = Field(default=1, ge=0.5, le=2, allow_inf_nan=False)
    pitch: float = Field(default=0, ge=-12, le=12, allow_inf_nan=False)
    volume: float = Field(default=1, ge=0, le=2, allow_inf_nan=False)


@router.get("/media-tools/narrators")
def narrators(user: User = Depends(current_user)):
    return {"voices": [{"name": name, "locale": locale} for name, locale in system_voices().items()]}


@router.post("/projects/{project_id}/media-tools/narrate")
def narrate(project_id: str, body: NarrationRequest, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_project(session, project_id, user.id)
    if body.voice not in system_voices():
        raise AppError(422, "NARRATOR_UNAVAILABLE", "所选系统音色在此服务上不可用。")
    if not body.text.strip() or "[[" in body.text or "]]" in body.text:
        raise AppError(422, "NARRATION_TEXT_INVALID", "请输入需要朗读的纯文本。")
    root = get_settings().tmp_dir
    root.mkdir(parents=True, exist_ok=True)
    working = tempfile.TemporaryDirectory(prefix="narration-", dir=root)
    directory = Path(working.name)
    try:
        textfile = directory / "narration.txt"
        textfile.write_text(body.text, encoding="utf-8")
        base_speed = {"自然": 178, "平静": 150, "活泼": 205, "严肃": 160}[body.expression]
        subprocess.run(["say", "-v", body.voice, "-r", str(round(base_speed * body.speed)), "-f", str(textfile), "-o", str(directory / "speech.aiff")], check=True, timeout=90, capture_output=True)
        ratio = 2 ** (body.pitch / 12)
        output = directory / "narration.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(directory / "speech.aiff"), "-af", f"aresample=24000,asetrate={round(24000 * ratio)},aresample=24000,atempo={1 / ratio},volume={body.volume}", "-c:a", "pcm_s16le", str(output)], check=True, capture_output=True, timeout=60)
        if output.stat().st_size < 160:
            raise AppError(422, "NARRATION_NO_SAMPLES", "系统语音没有输出音频。")
        return FileResponse(output, media_type="audio/wav", filename="narration.wav", background=BackgroundTask(working.cleanup), headers={"Cache-Control": "no-store"})
    except (subprocess.SubprocessError, OSError) as exc:
        working.cleanup()
        raise AppError(422, "NARRATION_FAILED", "系统语音暂时无法输出所选音色。") from exc
    except BaseException:
        working.cleanup()
        raise


class ModelVersion(BaseModel):
    version: str = Field(pattern=r"^2\.0$")


class ModelResource(BaseModel):
    uri: str | None = Field(default=None, pattern=r"^data:")


class ModelDocument(BaseModel):
    asset: ModelVersion
    scenes: list[dict] = Field(min_length=1, max_length=64)
    nodes: list[dict] = Field(min_length=1, max_length=20000)
    buffers: list[ModelResource] = Field(max_length=1024)
    images: list[ModelResource] = Field(default_factory=list, max_length=1024)


@router.post("/projects/{project_id}/media-tools/director-models", status_code=201)
async def store_director_model(project_id: str, file: UploadFile = File(), user: User = Depends(current_user), session: Session = Depends(get_db)):
    project = require_project(session, project_id, user.id)
    data = await file.read(_DIRECTOR_MODEL_LIMIT + 1)
    if len(data) > _DIRECTOR_MODEL_LIMIT:
        raise AppError(413, "DIRECTOR_MODEL_TOO_LARGE", "导演台模型须在 16 MiB 以内。")
    try:
        ModelDocument.model_validate_json(data)
    except ValidationError as exc:
        raise AppError(422, "DIRECTOR_MODEL_INVALID", "需要包含内嵌资源的 glTF 2.0 模型。") from exc
    key = hashlib.sha256(data).hexdigest()
    settings = get_settings()
    directory = settings.data_dir.resolve() / "media" / "director-models" / project.id
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (key + ".gltf")
    if not destination.is_file():
        with tempfile.NamedTemporaryFile(dir=directory, prefix="upload-", delete=False) as part:
            try:
                part.write(data)
                part.flush()
                os.fsync(part.fileno())
                part.close()
                os.replace(part.name, destination)
            finally:
                Path(part.name).unlink(missing_ok=True)
    storage = private_storage(settings)
    if storage is not None:
        try:
            await asyncio.to_thread(persist_file, settings, destination, "model/gltf+json")
        except AppError as error:
            if error.code != "STORAGE_OBJECT_EXISTS":
                raise
            relative = destination.relative_to(settings.data_dir.resolve()).as_posix()
            await asyncio.to_thread(storage.materialize, relative, destination, max_bytes=len(data), sha256=key)
    return {"id": key}


@router.get("/projects/{project_id}/media-tools/director-models/{model_id}")
def director_model(project_id: str, request: Request, model_id: str = ApiPath(pattern="^[a-f0-9]{64}$"), user: User = Depends(current_user), session: Session = Depends(get_db)):
    project = require_project(session, project_id, user.id)
    relative = f"media/director-models/{project.id}/{model_id}.gltf"
    settings = get_settings()
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    storage = private_storage(settings)
    if storage is not None:
        return storage.response(relative, media_type="model/gltf+json", headers=headers,
                                range_header=request.headers.get("range"))
    return FileResponse(resolve_asset_path(settings, relative), media_type="model/gltf+json", headers=headers)


@router.post("/projects/{project_id}/media-tools/render")
def render(project_id: str, body: MediaEditRequest, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_project(session, project_id, user.id)
    settings = get_settings()
    identifiers = {clip.asset_id for clip in [*body.videos, *body.audio]}
    assets = session.query(Asset).filter(Asset.owner_id == user.id, Asset.project_id == project_id, Asset.id.in_(identifiers)).all()
    if len(assets) != len(identifiers):
        raise AppError(404, "ASSET_NOT_FOUND", "来源素材不在当前项目中。")
    paths = {asset.id: resolve_asset_path(settings, asset.relpath) for asset in assets}
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    if not _render_slot.acquire(blocking=False):
        raise AppError(409, "MEDIA_EDIT_BUSY", "正在导出其他素材，请稍后重试。")
    try:
        working = tempfile.TemporaryDirectory(prefix="canvas-edit-", dir=settings.tmp_dir)
        try:
            output = render_edit(body, paths, Path(working.name), settings.max_upload_bytes)
            media_type = {"mp4": "video/mp4", "webm": "video/webm", "wav": "audio/wav"}[body.container]
            return FileResponse(output, media_type=media_type, filename=output.name, background=BackgroundTask(working.cleanup), headers={"Cache-Control": "no-store"})
        except BaseException:
            working.cleanup()
            raise
    finally:
        _render_slot.release()


class ImageSplitRequest(WireModel):
    scene_id: str = Field(min_length=1, max_length=100)
    source_node_id: str = Field(min_length=1, max_length=100)
    rows: int = Field(ge=1, le=12, strict=True)
    columns: int = Field(ge=1, le=12, strict=True)
    horizontal_lines: list[float] | None = Field(default=None, max_length=11)
    vertical_lines: list[float] | None = Field(default=None, max_length=11)
    expected_revision: int = Field(ge=1, strict=True)
    operation_id: str = Field(min_length=1, max_length=100)


class ImageSplitPiece(WireModel):
    node_id: str
    asset_id: str
    row: int
    column: int


class ImageSplitResult(ProjectEnvelope):
    pieces: list[ImageSplitPiece]


@router.post("/projects/{project_id}/media-tools/split", response_model=ImageSplitResult, response_model_exclude_unset=True, status_code=201)
def split(project_id: str, body: ImageSplitRequest, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return public_project_envelope(session, user.id, split_image(session, project_id, user.id, body.model_dump(by_alias=True), get_settings()))
