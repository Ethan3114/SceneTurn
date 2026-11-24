import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from app.core.config import Settings
from app.core.errors import AppError


_CHUNK_SIZE = 1024 * 1024
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")


class _SignedUpload(BaseModel):
    token: str = Field(min_length=1, max_length=8192)


def _object_path(key: str) -> str:
    if len(key) > 1024 or not all(_SEGMENT.fullmatch(part) for part in key.split("/")):
        raise AppError(500, "STORAGE_OBJECT_KEY_INVALID", "私有存储对象标识无效")
    return quote(key, safe="/")


def _check_response(response: httpx.Response, allowed: set[int]) -> None:
    if response.status_code in allowed:
        return
    if response.status_code == 404:
        raise AppError(404, "STORAGE_OBJECT_MISSING", "私有存储对象不存在")
    if response.status_code == 409:
        raise AppError(409, "STORAGE_OBJECT_EXISTS", "私有存储对象已存在")
    if response.status_code == 413:
        raise AppError(413, "STORAGE_OBJECT_TOO_LARGE", "私有存储对象超过上传限制")
    raise AppError(502, "STORAGE_REQUEST_FAILED", "私有存储服务请求失败")


def _check_content(response: httpx.Response) -> None:
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise AppError(502, "STORAGE_CONTENT_INVALID", "私有存储返回了不支持的内容编码")


@dataclass(frozen=True)
class CloudBaseStorage:
    env_id: str
    bucket: str
    api_key: SecretStr
    timeout: float

    @property
    def _origin(self) -> str:
        return f"https://{self.env_id}.api.tcloudbasegateway.com"

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._origin,
            headers={"Authorization": f"Bearer {self.api_key.get_secret_value()}", "Accept-Encoding": "identity"},
            timeout=self.timeout,
            trust_env=False,
        )

    def _path(self, key: str, *, sign: bool = False) -> str:
        prefix = "object/upload/sign" if sign else "object"
        return f"/v1/storages/{prefix}/{self.bucket}/{_object_path(key)}"

    def put(self, key: str, source: Path, content_type: str) -> None:
        try:
            with self._client() as client, source.open("rb") as stream:
                headers = {"Content-Type": content_type, "Content-Length": str(os.fstat(stream.fileno()).st_size), "x-upsert": "false"}
                response = client.post(self._path(key), headers=headers, content=iter(lambda: stream.read(_CHUNK_SIZE), b""))
                _check_response(response, {200, 201})
        except httpx.HTTPError:
            raise AppError(502, "STORAGE_REQUEST_FAILED", "无法连接私有存储服务") from None

    def signed_upload(self, key: str, *, expires_in: int = 600) -> dict[str, str]:
        if not 1 <= expires_in <= 3600:
            raise AppError(500, "STORAGE_SIGNATURE_INVALID", "私有存储上传授权期限无效")
        path = self._path(key, sign=True)
        try:
            with self._client() as client:
                response = client.post(path, params={"expiresIn": expires_in}, headers={"x-upsert": "false"})
                _check_response(response, {200})
                signed = _SignedUpload.model_validate_json(response.content)
        except httpx.HTTPError:
            raise AppError(502, "STORAGE_REQUEST_FAILED", "无法连接私有存储服务") from None
        except ValidationError:
            raise AppError(502, "STORAGE_SIGNATURE_INVALID", "私有存储返回的上传授权无效") from None
        url = f"{self._origin}{path}?{urlencode({'token': signed.token})}"
        return {"fullSignedURL": url, "token": signed.token, "path": key,
                "bucket": self.bucket, "environmentId": self.env_id}

    def copy(self, source_key: str, destination_key: str, *, content_type: str) -> None:
        _object_path(source_key)
        _object_path(destination_key)
        payload = {"bucketId": self.bucket, "sourceKey": source_key, "destinationKey": destination_key,
                   "copyMetadata": False, "metadata": {"contentType": content_type, "mimetype": content_type}}
        try:
            with self._client() as client:
                response = client.post("/v1/storages/object/copy", json=payload, headers={"x-upsert": "false"})
                _check_response(response, {200, 201})
        except httpx.HTTPError:
            raise AppError(502, "STORAGE_REQUEST_FAILED", "无法连接私有存储服务") from None

    def object_size(self, key: str) -> int:
        try:
            with self._client() as client:
                response = client.head(self._path(key))
                _check_response(response, {200})
                length = response.headers.get("content-length", "")
                if not length.isascii() or not length.isdecimal():
                    raise AppError(502, "STORAGE_CONTENT_INVALID", "私有存储未返回有效的对象大小")
                return int(length)
        except httpx.HTTPError:
            raise AppError(502, "STORAGE_REQUEST_FAILED", "无法连接私有存储服务") from None

    def materialize(self, key: str, destination: Path, *, max_bytes: int, sha256: str | None = None) -> Path:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            with self._client() as client, client.stream("GET", self._path(key)) as response:
                _check_response(response, {200})
                _check_content(response)
                length = response.headers.get("content-length")
                expected = int(length) if length and length.isdecimal() else None
                if expected is not None and expected > max_bytes:
                    raise AppError(413, "STORAGE_OBJECT_TOO_LARGE", "私有存储对象超过允许的读取大小")
                return self._write_download(response, destination, max_bytes, expected, sha256)
        except httpx.HTTPError:
            raise AppError(502, "STORAGE_REQUEST_FAILED", "无法连接私有存储服务") from None

    @staticmethod
    def _write_download(response: httpx.Response, destination: Path, max_bytes: int, expected: int | None, sha256: str | None) -> Path:
        stream = tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".download-", suffix=".part", delete=False)
        part = Path(stream.name)
        size = 0
        digest = hashlib.sha256() if sha256 is not None else None
        try:
            with stream:
                for chunk in response.iter_raw(_CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_bytes:
                        raise AppError(413, "STORAGE_OBJECT_TOO_LARGE", "私有存储对象超过允许的读取大小")
                    if digest is not None:
                        digest.update(chunk)
                    stream.write(chunk)
                if (expected is not None and size != expected) or (digest is not None and digest.hexdigest() != sha256):
                    raise AppError(502, "STORAGE_CONTENT_INVALID", "私有存储对象字节数或摘要不一致")
                stream.flush()
                os.fsync(stream.fileno())
            part.replace(destination)
            return destination
        finally:
            part.unlink(missing_ok=True)

    def delete(self, key: str) -> None:
        try:
            with self._client() as client:
                _check_response(client.delete(self._path(key)), {200, 204, 404})
        except httpx.HTTPError:
            raise AppError(502, "STORAGE_REQUEST_FAILED", "无法连接私有存储服务") from None

    def response(self, key: str, *, media_type: str, headers: dict[str, str], range_header: str | None = None,
                 filename: str | None = None, disposition: Literal["inline", "attachment"] = "inline") -> Response:
        request_headers = {"Range": range_header} if range_header else {}
        client = self._client()
        response = None
        streaming = False

        def close() -> None:
            try:
                if response is not None:
                    response.close()
            finally:
                client.close()

        def content():
            try:
                yield from response.iter_raw(64 * 1024)
            except httpx.HTTPError:
                raise RuntimeError("私有存储媒体流中断") from None
            finally:
                close()

        try:
            response = client.send(client.build_request("GET", self._path(key), headers=request_headers), stream=True)
            _check_response(response, {200, 206, 416})
            _check_content(response)
            outgoing = {**headers, "Accept-Ranges": "bytes"}
            if "content-range" in response.headers:
                outgoing["Content-Range"] = response.headers["content-range"]
            if response.status_code == 416:
                return Response(status_code=416, headers=outgoing)
            if "content-length" in response.headers:
                outgoing["Content-Length"] = response.headers["content-length"]
            if filename is not None:
                outgoing["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}"
            if response.status_code == 206 and response.headers.get("content-type", "").lower().startswith("multipart/byteranges;"):
                media_type = response.headers["content-type"]
            result = StreamingResponse(content(), status_code=response.status_code, media_type=media_type, headers=outgoing,
                                       background=BackgroundTask(close))
            streaming = True
            return result
        except httpx.HTTPError:
            raise AppError(502, "STORAGE_REQUEST_FAILED", "无法连接私有存储服务") from None
        finally:
            if not streaming:
                close()


def private_storage(settings: Settings) -> CloudBaseStorage | None:
    if settings.cloudbase_env_id is None:
        return None
    if settings.cloudbase_storage_bucket is None or settings.cloudbase_api_key is None:
        raise AppError(500, "STORAGE_CONFIGURATION_INVALID", "私有存储配置不完整")
    if not re.fullmatch(r"sceneturn-[a-z0-9-]+", settings.cloudbase_storage_bucket):
        raise AppError(500, "STORAGE_CONFIGURATION_INVALID", "必须使用 SceneTurn 专用私有存储桶")
    return CloudBaseStorage(settings.cloudbase_env_id, settings.cloudbase_storage_bucket, settings.cloudbase_api_key,
                            settings.download_timeout)
