import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.core.errors import AppError


MODEL = "wan3.0-video"
REGION = "cn-beijing"
PRICE_VERSION = "wan3.0-video-cn-beijing-2026-11-12-list"
PRICES = {"480P": 30, "720P": 60, "1080P": 120}
MAX_REFERENCE_IMAGES = 10
VIDEO_INPUT_MODES = ("reference", "edit", "extend")
UPLOAD_POLICY_URL = "https://dashscope.aliyuncs.com/api/v1/uploads"
UPLOAD_BUCKET = re.compile(r"dashscope-file-[a-z0-9-]+\.oss-cn-beijing\.aliyuncs\.com")
TaskStatus = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"]


class WanUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    video_count: int = Field(ge=1, le=1)
    duration: float = Field(gt=0)
    input_video_duration: float = Field(default=0, ge=0)
    output_video_duration: float = Field(gt=0)
    fps: int = Field(gt=0)
    SR: int = Field(gt=0)
    ratio: str


class WanOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    task_status: TaskStatus
    video_url: str | None = None
    code: str | None = None


class WanResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    output: WanOutput
    request_id: str = Field(min_length=1, max_length=200)
    usage: WanUsage | None = None


class WanUploadPolicy(BaseModel):
    upload_host: str
    upload_dir: str = Field(pattern=r"^dashscope-instant(?:/[A-Za-z0-9_-]+)+$")
    max_file_size_mb: int = Field(ge=1)
    oss_access_key_id: str = Field(min_length=1)
    signature: str = Field(min_length=1)
    policy: str = Field(min_length=1)
    x_oss_object_acl: Literal["private"]
    x_oss_forbid_overwrite: Literal["true"]


class WanUploadGrant(BaseModel):
    data: WanUploadPolicy


@dataclass
class WanFailure(Exception):
    code: str
    message: str
    submission_unknown: bool = False


def endpoint(workspace_id: str, region: str = REGION) -> str:
    if region != REGION:
        raise AppError(422, "WAN_REGION_UNSUPPORTED", "本阶段只支持北京地域，请检查地区设置")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", workspace_id):
        raise AppError(422, "WAN_WORKSPACE_INVALID", "业务空间ID必须是有效的官方域名标识")
    return f"https://{workspace_id}.{REGION}.maas.aliyuncs.com"


def prices_view() -> dict:
    return {
        "currency": "CNY",
        "unit": "second",
        "version": PRICE_VERSION,
        "centsPerSecond": PRICES,
    }


def video_request(frozen_input: dict) -> dict:
    if frozen_input["model"] != MODEL:
        raise AppError(422, "WAN_MODEL_UNSUPPORTED", "本阶段只支持wan3.0-video")
    mode = frozen_input.get("videoInputMode")
    media = frozen_input.get("media", [])
    kinds = [item["type"] for item in media]
    has_video = "reference_video" in kinds
    if mode is None:
        valid_media = kinds in ([], ["first_frame"], ["first_frame", "last_frame"])
    else:
        valid_media = (mode in VIDEO_INPUT_MODES and bool(kinds) and (mode == "reference" or has_video)
            and set(kinds) <= {"reference_image", "reference_video"} and kinds.count("reference_video") <= 1
            and kinds.count("reference_image") <= MAX_REFERENCE_IMAGES)
    if not valid_media:
        raise AppError(422, "WAN_MEDIA_INVALID", "请检查固定帧或参考素材的组合")
    instructions = {"edit": "根据下列要求编辑视频1。", "extend": "按下列要求延长视频1。"}
    if mode == "reference":
        reference_subject = "视频1" if has_video else "参考图片"
        if has_video and "reference_image" in kinds:
            reference_subject = "视频1及参考图片"
        instructions["reference"] = f"以{reference_subject}为参考生成画面。"
    payload = {"model": MODEL, "input": {"prompt": instructions.get(mode, "") + frozen_input["prompt"]},
        "parameters": {"resolution": frozen_input["resolution"], "duration": frozen_input["duration"],
            "ratio": frozen_input.get("ratio", "16:9"), "audio": bool(frozen_input.get("audio", True)),
            "prompt_extend": bool(frozen_input.get("promptExtend", False)), "watermark": False}}
    if media:
        payload["input"]["media"] = media
    return payload


class WanClient:
    def __init__(self, settings: Settings, workspace_id: str, api_key: str, region: str = REGION):
        if not api_key:
            raise AppError(503, "WAN_NOT_CONFIGURED", "请先保存阿里云业务空间和API Key")
        self.base_url = endpoint(workspace_id, region)
        self.api_key = api_key
        self.timeout = settings.request_timeout
        self.submit_timeout = httpx.Timeout(self.timeout, write=settings.wan_submit_timeout, read=settings.wan_submit_timeout)

    async def upload_reference_video(self, path: Path) -> str:
        try:
            async with httpx.AsyncClient(timeout=self.submit_timeout, trust_env=False, follow_redirects=False) as client:
                reply = await client.get(UPLOAD_POLICY_URL, params={"action": "getPolicy", "model": MODEL},
                    headers={"Authorization": f"Bearer {self.api_key}"})
                if reply.status_code != 200:
                    raise WanFailure("WAN_REFERENCE_UPLOAD_DENIED", "百炼未授权临时上传，请检查业务空间和模型权限")
                grant = WanUploadGrant.model_validate_json(reply.content).data
                host = httpx.URL(grant.upload_host)
                if host.scheme != "https" or not UPLOAD_BUCKET.fullmatch(host.host) or host.port not in (None, 443) or host.path != "/" or host.userinfo or host.query or host.fragment:
                    raise WanFailure("WAN_REFERENCE_UPLOAD_INVALID", "百炼临时存储地址未通过校验")
                if path.stat().st_size > grant.max_file_size_mb * 1024 * 1024:
                    raise WanFailure("WAN_REFERENCE_UPLOAD_TOO_LARGE", "原视频超过百炼当前允许的临时上传大小")
                name = f"{uuid.uuid4().hex}.mp4"
                key = f"{grant.upload_dir}/{name}"
                form = {
                    "OSSAccessKeyId": grant.oss_access_key_id,
                    "Signature": grant.signature,
                    "policy": grant.policy,
                    "x-oss-object-acl": grant.x_oss_object_acl,
                    "x-oss-forbid-overwrite": grant.x_oss_forbid_overwrite,
                    "key": key,
                    "success_action_status": "200",
                }
                with path.open("rb") as stream:
                    uploaded = await client.post(host, data=form, files={"file": (name, stream, "video/mp4")})
                if uploaded.status_code != 200:
                    raise WanFailure("WAN_REFERENCE_UPLOAD_DENIED", "百炼临时存储没有接收原视频，请核实上传权限")
                return f"oss://{key}"
        except (ValueError, httpx.InvalidURL) as failure:
            raise WanFailure("WAN_REFERENCE_UPLOAD_INVALID", "百炼临时上传凭证未通过校验") from failure
        except (httpx.TransportError, OSError) as failure:
            raise WanFailure("WAN_REFERENCE_UPLOAD_INTERRUPTED", "原视频临时上传中断，请重新准备生成") from failure

    async def submit(self, frozen_input: dict) -> WanResponse:
        payload = video_request(frozen_input)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        if any(item["url"].startswith("oss://dashscope-instant/") for item in payload["input"].get("media", [])):
            headers["X-DashScope-OssResourceResolve"] = "enable"
        async with httpx.AsyncClient(timeout=self.submit_timeout, trust_env=False, follow_redirects=False) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/v1/services/aigc/video-generation/video-synthesis",
                    headers=headers,
                    json=payload,
                )
            except httpx.TransportError as exc:
                raise WanFailure("WAN_SUBMISSION_UNKNOWN", f"提交连接中断（{type(exc).__name__}），原任务状态待核实，费用预留继续保留", True) from exc
        if 400 <= response.status_code < 500 and response.status_code != 408:
            raise WanFailure("WAN_SUBMISSION_REJECTED", "阿里云拒绝了生成请求，请检查Key、模型权限和输入参数")
        if response.status_code >= 300:
            raise WanFailure("WAN_SUBMISSION_UNKNOWN", "供应商未返回明确的提交结果，费用预留继续保留", True)
        try:
            return WanResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise WanFailure("WAN_SUBMISSION_UNKNOWN", "供应商提交响应未通过结构检查，原任务状态待核实", True) from exc

    async def query(self, task_id: str) -> WanResponse:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task_id):
            raise AppError(422, "WAN_TASK_ID_INVALID", "供应商任务标识无效")
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/api/v1/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
            except httpx.TransportError as exc:
                raise WanFailure("WAN_QUERY_INTERRUPTED", "供应商任务查询中断，可以继续查询同一任务") from exc
        if response.status_code >= 300:
            raise WanFailure("WAN_QUERY_INTERRUPTED", "供应商暂时无法查询原任务，请检查配置后继续查询")
        try:
            result = WanResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise WanFailure("WAN_RESPONSE_INVALID", "供应商查询响应未通过结构检查，已保留原任务") from exc
        if result.output.task_id != task_id:
            raise WanFailure("WAN_TASK_MISMATCH", "供应商响应的任务标识与原任务不一致")
        return result
