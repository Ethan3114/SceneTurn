import asyncio
import codecs
import hashlib
import ipaddress
import math
import os
import subprocess
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import BinaryIO

import filetype
import httpx
from charset_normalizer import from_bytes
from fastapi import UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.core.errors import AppError


SUPPORTED_MIME = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/x-wav": ".wav",
    "audio/wav": ".wav",
}
TEXT_EXTENSIONS = {".txt": "text/plain", ".md": "text/markdown"}
TEXT_LIMIT_BYTES = 10 * 1024 * 1024
TEXT_ENCODINGS = ("ascii", "utf_8", "utf_16", "gb18030", "gbk", "gb2312")
DECLARED_TEXT_ENCODINGS = {"ascii", "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "gb18030", "gbk", "gb2312"}


class ProbeStream(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    codec_type: str
    codec_name: str
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    duration: float | None = Field(default=None, ge=0)


class ProbeFormat(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    format_name: str
    duration: float | None = Field(default=None, ge=0)


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    streams: list[ProbeStream]
    format: ProbeFormat


class DnsAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    record_type: int = Field(alias="type")
    data: str


class DnsResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: int = Field(alias="Status")
    truncated: bool = Field(default=False, alias="TC")
    answers: list[DnsAnswer] = Field(default_factory=list, alias="Answer")


@dataclass(frozen=True)
class StoredMedia:
    name: str
    relpath: str
    poster_relpath: str
    mime_type: str
    bytes: int
    sha256: str
    duration_ms: int
    width: int
    height: int
    metadata_json: dict = field(default_factory=dict)


def resolve_asset_path(settings: Settings, relpath: str) -> Path:
    root = settings.data_dir.resolve()
    path = (root / relpath).resolve()
    if not path.is_relative_to(root / "media") or not path.is_file():
        raise AppError(404, "ASSET_FILE_NOT_FOUND", "素材文件不存在")
    return path


def asset_view(asset) -> dict:
    return {
        "id": asset.id,
        "projectId": asset.project_id,
        "generationId": asset.generation_id,
        "name": asset.name,
        "mimeType": asset.mime_type,
        "bytes": asset.bytes,
        "sha256": asset.sha256,
        "durationMs": asset.duration_ms,
        "width": asset.width,
        "height": asset.height,
        "url": f"/api/v1/assets/{asset.id}/content",
        "posterUrl": f"/api/v1/assets/{asset.id}/poster" if asset.poster_relpath else None,
        "createdAt": datetime.fromtimestamp(asset.created_at, timezone.utc).isoformat(),
    }


def _staging_file(settings: Settings) -> Path:
    directory = settings.tmp_dir / "media"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="media-", suffix=".part", dir=directory, delete=False) as stream:
            return Path(stream.name)
    except OSError as exc:
        raise AppError(507, "MEDIA_STORAGE_FAILED", "素材暂存目录不可写或磁盘空间不足") from exc


def _filename(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 200 or any(char in name for char in ("/", "\\", "\x00")):
        raise AppError(422, "MEDIA_FILENAME_INVALID", "请使用不含路径的有效素材文件名")
    return name


async def save_upload(upload: UploadFile, settings: Settings) -> StoredMedia:
    name = _filename(upload.filename or "uploaded-media")
    suffix = Path(name).suffix.lower()
    text_mime = TEXT_EXTENSIONS.get(suffix)
    declared_charset = _text_header(upload.content_type, suffix) if text_mime else None
    maximum_bytes = min(settings.max_upload_bytes, TEXT_LIMIT_BYTES) if text_mime else settings.max_upload_bytes
    path = _staging_file(settings)
    size = 0
    try:
        with path.open("wb") as stream:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > maximum_bytes:
                    raise AppError(413, "MEDIA_TOO_LARGE", "文本素材超过10MiB或当前上传限制" if text_mime else "素材超过允许的上传大小")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if not size and not text_mime:
            raise AppError(422, "MEDIA_EMPTY", "素材文件为空")
        if text_mime:
            return await asyncio.to_thread(_store_text, path, settings, name, suffix, text_mime, declared_charset)
        return await asyncio.to_thread(_validate_and_store, path, settings, name, False)
    except OSError as exc:
        raise AppError(507, "MEDIA_STORAGE_FAILED", "素材保存失败，请检查磁盘空间与目录权限") from exc
    finally:
        path.unlink(missing_ok=True)


def _text_header(content_type: str | None, suffix: str) -> str | None:
    if content_type is None:
        return None
    header = Message()
    header["content-type"] = content_type
    mime_type = header.get_content_type()
    accepted = {"text/plain", "application/octet-stream"}
    if suffix == ".md":
        accepted.update({"text/markdown", "text/x-markdown"})
    if mime_type not in accepted:
        raise AppError(422, "TEXT_MIME_INVALID", "文本素材的文件扩展名与MIME类型不一致")
    charset = header.get_content_charset()
    if charset is None:
        return None
    try:
        encoding = codecs.lookup(charset).name
    except LookupError as exc:
        raise AppError(422, "TEXT_ENCODING_UNSUPPORTED", "文本声明了无法识别的字符编码") from exc
    if encoding not in DECLARED_TEXT_ENCODINGS:
        raise AppError(422, "TEXT_ENCODING_UNSUPPORTED", "文本编码需要使用UTF8、UTF16或GB18030兼容编码")
    return encoding


def _store_text(path: Path, settings: Settings, name: str, suffix: str, mime_type: str, declared_charset: str | None) -> StoredMedia:
    if filetype.guess(str(path)) is not None:
        raise AppError(422, "TEXT_CONTENT_INVALID", "文本文件包含其他格式的媒体或二进制内容")
    raw = path.read_bytes()
    candidates = from_bytes(raw, cp_isolation=[declared_charset] if declared_charset else list(TEXT_ENCODINGS), enable_fallback=False)
    match = candidates.best()
    if match is None:
        raise AppError(422, "TEXT_ENCODING_INVALID", "文本编码无法可靠识别，请保存为UTF8、UTF16或GB18030后上传")
    encoding = codecs.lookup(match.encoding).name
    try:
        content = raw.decode(encoding, errors="strict")
    except UnicodeDecodeError as exc:
        raise AppError(422, "TEXT_ENCODING_INVALID", "文本包含与其编码不符的字节，无法完整读取") from exc
    if content.startswith("\ufeff"):
        content = content.removeprefix("\ufeff")
    if any(unicodedata.category(character) == "Cc" and character not in "\n\r\t\f" for character in content):
        raise AppError(422, "TEXT_CONTENT_INVALID", "文本包含无法显示的二进制控制字符")
    directory = settings.data_dir / "media"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{uuid.uuid4().hex}{suffix}"
    digest = hashlib.sha256(raw).hexdigest()
    os.replace(path, destination)
    return StoredMedia(
        name=name,
        relpath=destination.relative_to(settings.data_dir).as_posix(),
        poster_relpath="",
        mime_type=mime_type,
        bytes=len(raw),
        sha256=digest,
        duration_ms=0,
        width=0,
        height=0,
        metadata_json={"content": content, "encoding": encoding},
    )


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if not address.is_global or address.is_multicast or address.is_reserved or address.is_unspecified or address.is_loopback or address.is_link_local:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.is_site_local:
        return False
    return True


async def _public_address(hostname: str) -> str:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
            responses = await asyncio.gather(_dns_query(client, hostname, "A"), _dns_query(client, hostname, "AAAA"))
        try:
            resolved = {ipaddress.ip_address(answer.data) for response in responses for answer in response.answers if answer.record_type in {1, 28}}
        except ValueError as exc:
            raise AppError(502, "MEDIA_DNS_INVALID", "公开域名解析返回了无效地址") from exc
    else:
        resolved = {literal}
    if not resolved or any(not _is_public_address(address) for address in resolved):
        raise AppError(422, "MEDIA_URL_UNSAFE", "生成视频的下载地址必须指向公开网络")
    return str(sorted(resolved, key=lambda address: (address.version, int(address)))[0])


async def _dns_query(client: httpx.AsyncClient, hostname: str, record_type: str) -> DnsResponse:
    try:
        response = await client.get(
            "https://223.5.5.5/resolve",
            params={"name": hostname, "type": record_type},
            headers={"Host": "dns.alidns.com", "Accept": "application/dns-json"},
            extensions={"sni_hostname": "dns.alidns.com"},
        )
    except httpx.TransportError as exc:
        raise AppError(502, "MEDIA_DNS_FAILED", "公开域名解析连接中断，可以继续下载原结果") from exc
    if response.status_code != 200:
        raise AppError(502, "MEDIA_DNS_FAILED", "公开域名解析服务未成功响应")
    try:
        result = DnsResponse.model_validate_json(response.content)
    except ValidationError as exc:
        raise AppError(502, "MEDIA_DNS_INVALID", "公开域名解析响应未通过结构检查") from exc
    if result.status != 0 or result.truncated:
        raise AppError(502, "MEDIA_DNS_FAILED", "生成视频的公开下载域名无法完整解析")
    return result


async def download_video(url: str, settings: Settings) -> StoredMedia:
    try:
        source = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise AppError(422, "MEDIA_URL_INVALID", "生成视频的下载地址无效") from exc
    if source.scheme != "https" or not source.host or source.port not in (None, 443) or source.username or source.password:
        raise AppError(422, "MEDIA_URL_UNSAFE", "生成视频必须使用无凭据的HTTPS公开下载地址")
    address = await _public_address(source.host)
    pinned = source.copy_with(host=address, fragment=None)
    path = _staging_file(settings)
    size = 0
    deadline = time.monotonic() + settings.download_timeout
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout, connect=20), trust_env=False, follow_redirects=False) as client:
            async with client.stream(
                "GET",
                pinned,
                headers={"Host": source.host, "Accept": "video/mp4", "Accept-Encoding": "identity"},
                extensions={"sni_hostname": source.host},
            ) as response:
                if response.status_code != 200:
                    raise AppError(502, "MEDIA_DOWNLOAD_FAILED", "生成视频下载未成功，原任务与结果已保留")
                content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                if content_type not in {"video/mp4", "application/octet-stream"}:
                    raise AppError(422, "MEDIA_MIME_INVALID", "供应商返回的下载内容不是MP4视频")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        expected_bytes = int(content_length)
                    except ValueError as exc:
                        raise AppError(502, "MEDIA_LENGTH_INVALID", "生成视频下载响应的文件大小无效") from exc
                    if expected_bytes < 0:
                        raise AppError(502, "MEDIA_LENGTH_INVALID", "生成视频下载响应的文件大小无效")
                    if expected_bytes > settings.max_download_bytes:
                        raise AppError(413, "MEDIA_TOO_LARGE", "生成视频超过允许的下载大小")
                with path.open("wb") as stream:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > settings.max_download_bytes:
                            raise AppError(413, "MEDIA_TOO_LARGE", "生成视频超过允许的下载大小")
                        if time.monotonic() > deadline:
                            raise AppError(504, "MEDIA_DOWNLOAD_TIMEOUT", "生成视频下载超时，可以继续获取原结果")
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
        if not size:
            raise AppError(422, "MEDIA_EMPTY", "供应商返回的视频文件为空")
        return await asyncio.to_thread(_validate_and_store, path, settings, "Wan生成视频.mp4", True)
    except httpx.TransportError as exc:
        raise AppError(502, "MEDIA_DOWNLOAD_INTERRUPTED", "生成视频下载连接中断，可以继续获取原结果") from exc
    except OSError as exc:
        raise AppError(507, "MEDIA_STORAGE_FAILED", "生成视频保存失败，请检查磁盘空间与目录权限") from exc
    finally:
        path.unlink(missing_ok=True)


def _run_media(command: list[str], stdin: BinaryIO | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180, check=False)
    except FileNotFoundError as exc:
        raise AppError(503, "MEDIA_TOOL_UNAVAILABLE", "媒体检查工具不可用，请检查FFmpeg与ffprobe安装") from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError(422, "MEDIA_VALIDATION_TIMEOUT", "素材解码检查超时，文件未登记为可用") from exc
    if result.returncode != 0:
        raise AppError(422, "MEDIA_DECODE_FAILED", "素材无法完整解码，文件未登记为可用")
    return result


def _validate_and_store(path: Path, settings: Settings, name: str, generated_video: bool) -> StoredMedia:
    try:
        return _inspect_and_store(path, settings, name, generated_video)
    except OSError as exc:
        raise AppError(507, "MEDIA_STORAGE_FAILED", "媒体文件保存失败，请检查磁盘空间与目录权限") from exc


def _inspect_and_store(path: Path, settings: Settings, name: str, generated_video: bool) -> StoredMedia:
    detected = filetype.guess(str(path))
    if detected is None or detected.mime not in SUPPORTED_MIME:
        raise AppError(422, "MEDIA_TYPE_UNSUPPORTED", "素材真实格式不在支持范围内")
    mime_type = detected.mime
    if generated_video and mime_type != "video/mp4":
        raise AppError(422, "MEDIA_MIME_INVALID", "生成视频的真实格式必须为MP4")
    probe_response = _run_media([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-show_format", "-of", "json", str(path),
    ])
    try:
        probe = ProbeResult.model_validate_json(probe_response.stdout)
    except ValidationError as exc:
        raise AppError(422, "MEDIA_METADATA_INVALID", "素材的媒体信息未通过结构检查") from exc
    visual = next((item for item in probe.streams if item.codec_type == "video"), None)
    audible = next((item for item in probe.streams if item.codec_type == "audio"), None)
    if mime_type.startswith(("video/", "image/")) and (visual is None or visual.width <= 0 or visual.height <= 0):
        raise AppError(422, "MEDIA_DIMENSIONS_INVALID", "素材没有有效的视频或图像尺寸")
    if mime_type.startswith("audio/") and audible is None:
        raise AppError(422, "MEDIA_AUDIO_INVALID", "素材没有有效音轨")
    if generated_video and (visual is None or visual.codec_name != "h264"):
        raise AppError(422, "MEDIA_CODEC_INVALID", "生成视频必须包含H.264视频编码")
    duration = probe.format.duration
    if duration is None and visual is not None:
        duration = visual.duration
    if mime_type.startswith(("video/", "audio/")) and (duration is None or not math.isfinite(duration) or duration <= 0):
        raise AppError(422, "MEDIA_DURATION_INVALID", "素材没有有效时长")
    _run_media([
        "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", str(path),
        "-map", "0:v?", "-map", "0:a?", "-f", "null", "-",
    ])
    directory = settings.data_dir / "media"
    directory.mkdir(parents=True, exist_ok=True)
    media_id = uuid.uuid4().hex
    target = directory / f"{media_id}{SUPPORTED_MIME[mime_type]}"
    poster_source = path.with_suffix(".jpg")
    poster_target = directory / f"{media_id}-poster.jpg"
    poster_relpath = ""
    try:
        if visual is not None:
            _run_media([
                "ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", str(path),
                "-frames:v", "1", "-q:v", "2", "-y", str(poster_source),
            ])
            if not poster_source.is_file() or poster_source.stat().st_size == 0:
                raise AppError(422, "MEDIA_POSTER_FAILED", "素材封面未成功生成")
            os.replace(poster_source, poster_target)
            poster_relpath = poster_target.relative_to(settings.data_dir).as_posix()
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        size = path.stat().st_size
        os.replace(path, target)
    finally:
        poster_source.unlink(missing_ok=True)
    return StoredMedia(
        name=name,
        relpath=target.relative_to(settings.data_dir).as_posix(),
        poster_relpath=poster_relpath,
        mime_type=mime_type,
        bytes=size,
        sha256=digest,
        duration_ms=round((duration or 0) * 1000),
        width=visual.width if visual else 0,
        height=visual.height if visual else 0,
    )
