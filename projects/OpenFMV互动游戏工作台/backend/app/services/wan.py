import re
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.core.errors import AppError


MODEL = "wan3.0-video"
REGION = "cn-beijing"
PRICE_VERSION = "wan3.0-video-cn-beijing-2026-11-12-list"
PRICES = {"480P": 30, "720P": 60, "1080P": 120}
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


class WanClient:
    def __init__(self, settings: Settings, workspace_id: str, api_key: str, region: str = REGION):
        if not api_key:
            raise AppError(503, "WAN_NOT_CONFIGURED", "请先保存阿里云业务空间和API Key")
        self.base_url = endpoint(workspace_id, region)
        self.api_key = api_key
        self.timeout = settings.request_timeout
        self.submit_timeout = httpx.Timeout(self.timeout, write=settings.wan_submit_timeout, read=settings.wan_submit_timeout)

    async def submit(self, frozen_input: dict) -> WanResponse:
        if frozen_input["model"] != MODEL:
            raise AppError(422, "WAN_MODEL_UNSUPPORTED", "本阶段只支持wan3.0-video")
        payload = {
            "model": frozen_input["model"],
            "input": {"prompt": frozen_input["prompt"]},
            "parameters": {
                "resolution": frozen_input["resolution"],
                "duration": frozen_input["duration"],
                "ratio": "16:9",
                "audio": True,
                "prompt_extend": False,
                "watermark": False,
            },
        }
        if frozen_input.get("media"):
            media = frozen_input["media"]
            if [item["type"] for item in media] != ["first_frame", "last_frame"]:
                raise AppError(422, "WAN_BOUNDARIES_INVALID", "首尾帧视频必须同时提供已确认的首帧和尾帧")
            payload["input"]["media"] = media
        async with httpx.AsyncClient(timeout=self.submit_timeout, trust_env=False, follow_redirects=False) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/v1/services/aigc/video-generation/video-synthesis",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "X-DashScope-Async": "enable",
                    },
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
