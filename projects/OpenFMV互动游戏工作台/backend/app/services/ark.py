import base64
import re

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.errors import AppError
from app.services.providers import ARK_BASE_URL, SEEDREAM_PIXEL_RANGE, profile
from app.services.wan import WanFailure, WanOutput, WanUsage


SEEDANCE_25_PUBLIC_TOKEN_RATES = {
    "seedance-2-5": {"480P": 70_000_000, "720P": 70_000_000, "1080P": 77_000_000},
    "seedance-2-5-flash": {"480P": 46_000_000, "720P": 46_000_000},
}
SEEDANCE_25_FRAME_SIZES = {
    "480P": (854 * 480, 480 * 854, 640 * 640, 752 * 560, 560 * 752, 992 * 432),
    "720P": (1280 * 720, 720 * 1280, 960 * 960, 1112 * 834, 834 * 1112, 1470 * 630),
    "1080P": (1920 * 1080, 1080 * 1920, 1440 * 1440, 1664 * 1248, 1248 * 1664, 2206 * 946),
}


def seedance_billing_limit(selection, resolution, duration, ratio, reserved_cents):
    catalog = SEEDANCE_25_PUBLIC_TOKEN_RATES.get(selection["profileId"])
    if catalog is None:
        return None
    rate = selection["price"]["outputMicrosPerMillion"]
    if not rate:
        raise AppError(409, "MODEL_PRICE_NOT_CONFIGURED", "须核实火山方舟的实际 Token 单价")
    prices = max(rate, catalog[resolution])
    max_tokens = reserved_cents * 10_000_000_000 // prices
    sizes = SEEDANCE_25_FRAME_SIZES[resolution]
    pixels = max(sizes) if ratio == "adaptive" else sizes[("16:9", "9:16", "1:1", "4:3", "3:4", "21:9").index(ratio)]
    # 用30帧每秒覆盖公开像素规格，实际仍使用供应商回执中的 Token 用量核对。
    estimate = (pixels * duration * 30 + 1023) // 1024
    if max_tokens < estimate:
        raise AppError(409, "MODEL_VIDEO_PRICE_UPPER_BOUND_TOO_LOW", "已核实的每秒额度低于当前画幅和清晰度所需的 Token 预留，请核对模型价表")
    return {"cents": reserved_cents, "maxCompletionTokens": max_tokens, "microsPerMillion": rate}


class ArkVideoUsage(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")
    completion_tokens: int = Field(ge=0)
    total_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def correct_total(self):
        if self.total_tokens is not None and self.total_tokens < self.completion_tokens:
            raise ValueError("方舟视频总Token小于输出Token")
        return self


class ArkTask(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    status: str | None = None
    model: str | None = None
    content: dict | None = None
    error: dict | None = None
    duration: int | None = None
    resolution: str | None = None
    ratio: str | None = None
    framespersecond: int | None = None
    usage: ArkVideoUsage | None = None


class ArkVideoResult(BaseModel):
    output: WanOutput
    request_id: str | None = None
    usage: WanUsage | None = None
    provider_response: dict


def video_request(frozen):
    selected = profile(frozen["modelProfile"]["profileId"], "video")
    capabilities = selected["capabilities"]
    if frozen["model"] != selected["model"] or frozen["resolution"] not in capabilities["resolutions"] or not capabilities["minDuration"] <= frozen["duration"] <= capabilities["maxDuration"]:
        raise AppError(422, "ARK_VIDEO_PARAMETERS_UNSUPPORTED", "视频模型不支持所选清晰度或时长")
    media = frozen.get("media", [])
    if media and [item["type"] for item in media] not in (["first_frame"], ["first_frame", "last_frame"]):
        raise AppError(422, "ARK_BOUNDARIES_INVALID", "请使用固定首帧或固定首尾帧")
    if len(media) == 2 and not capabilities["lastFrame"]:
        raise AppError(422, "MODEL_LAST_FRAME_UNSUPPORTED", "所选模型不支持首尾帧视频，请选择支持尾帧的模型")
    content = [{"type": "text", "text": frozen["prompt"]}]
    content.extend({"type": "image_url", "image_url": {"url": item["url"]}, "role": item["type"]} for item in media)
    body = {"model": selected["model"], "content": content, "duration": frozen["duration"], "resolution": frozen["resolution"].lower(),
        "ratio": "adaptive" if media and capabilities.get("fixedFrameRatioMode") == "adaptive" else frozen.get("ratio", "16:9"), "watermark": False}
    if capabilities["audio"]:
        body["generate_audio"] = bool(frozen.get("audio", True))
    return body


def video_result(payload, request_id=None):
    task = ArkTask.model_validate(payload)
    statuses = {None: "PENDING", "queued": "PENDING", "running": "RUNNING", "succeeded": "SUCCEEDED", "failed": "FAILED", "cancelled": "CANCELED", "expired": "FAILED"}
    if task.status not in statuses:
        raise ValueError("方舟视频返回未知状态")
    usage = None
    if task.status == "succeeded":
        if task.duration is None or task.resolution not in {"480p", "720p", "1080p"} or task.ratio is None or task.framespersecond is None or task.usage is None or task.model is None or task.usage.completion_tokens <= 0:
            raise ValueError("方舟视频结果缺少实际规格或用量")
        usage = WanUsage(video_count=1, duration=task.duration, input_video_duration=0, output_video_duration=task.duration,
            fps=task.framespersecond, SR=int(task.resolution[:-1]), ratio=task.ratio)
    return ArkVideoResult(output=WanOutput(task_id=task.id, task_status=statuses[task.status],
        video_url=(task.content or {}).get("video_url"), code=(task.error or {}).get("code")), request_id=request_id, usage=usage, provider_response=payload)


class ArkVideoClient:
    def __init__(self, settings, api_key, model):
        if not api_key.strip():
            raise AppError(503, "ARK_NOT_CONFIGURED", "请先保存方舟API Key")
        self.settings, self.api_key, self.model = settings, api_key, model

    async def submit(self, frozen_input):
        body = video_request(frozen_input)
        if body["model"] != self.model:
            raise AppError(409, "MODEL_CONFIGURATION_CHANGED", "视频请求与原任务固定模型不一致")
        try:
            async with httpx.AsyncClient(timeout=self.settings.wan_submit_timeout, trust_env=False, follow_redirects=False) as client:
                response = await client.post(ARK_BASE_URL + "/contents/generations/tasks", headers={"Authorization": f"Bearer {self.api_key}"}, json=body)
        except httpx.TransportError as error:
            raise WanFailure("ARK_SUBMISSION_UNKNOWN", "方舟视频提交结果待核实，原预留继续保留", True) from error
        if response.status_code >= 300:
            raise WanFailure("ARK_SUBMISSION_REJECTED" if 400 <= response.status_code < 500 and response.status_code != 408 else "ARK_SUBMISSION_UNKNOWN", "方舟未接受视频参数或未确认提交结果", not (400 <= response.status_code < 500 and response.status_code != 408))
        try:
            result = video_result(response.json(), response.headers.get("x-request-id") or response.headers.get("x-tt-logid"))
            if result.provider_response.get("model") not in {None, self.model}:
                raise ValueError("供应商实际模型与原请求不一致")
            return result
        except (ValueError, ValidationError) as error:
            raise WanFailure("ARK_SUBMISSION_UNKNOWN", "方舟提交回执未通过校验，原请求保持待核实", True) from error

    async def query(self, task_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task_id):
            raise AppError(422, "ARK_TASK_ID_INVALID", "方舟任务标识无效")
        try:
            async with httpx.AsyncClient(timeout=self.settings.request_timeout, trust_env=False, follow_redirects=False) as client:
                response = await client.get(ARK_BASE_URL + "/contents/generations/tasks/" + task_id, headers={"Authorization": f"Bearer {self.api_key}"})
            if response.status_code >= 300:
                raise WanFailure("ARK_QUERY_INTERRUPTED", "方舟暂时无法查询原任务")
            result = video_result(response.json(), response.headers.get("x-request-id") or response.headers.get("x-tt-logid"))
            if result.provider_response.get("model") not in {None, self.model}:
                raise ValueError("供应商实际模型与原请求不一致")
            if result.output.task_id != task_id:
                raise WanFailure("ARK_TASK_MISMATCH", "方舟返回的任务与原任务不一致")
            return result
        except (httpx.TransportError, ValueError, ValidationError) as error:
            raise WanFailure("ARK_QUERY_INTERRUPTED", "方舟查询或实际结果未通过校验") from error


def seedream_image_size(width, height):
    if min(width, height) < 512 or max(width, height) > 4096 or not SEEDREAM_PIXEL_RANGE[0] <= width * height <= SEEDREAM_PIXEL_RANGE[1]:
        raise AppError(422, "ARK_IMAGE_PARAMETERS_UNSUPPORTED", "Seedream 5.5 图片总像素须在921600至4624220之间")
    return f"{width}x{height}"


def seedream_cost_micros(request, reference_count):
    output_cost = 130_000 if request["width"] * request["height"] <= 2_610_000 else 230_000
    return max(output_cost, request["modelProfile"]["price"]["microsPerImage"]) + 50_000 * reference_count


def image_request(request, references):
    selected = profile(request["modelProfile"]["profileId"], "image")
    width, height = request["width"], request["height"]
    if selected["id"] != "seedream-5-5" or len(references) > 5:
        raise AppError(422, "ARK_IMAGE_PARAMETERS_UNSUPPORTED", "Seedream图片尺寸或参考数量不受支持")
    body = {"model": selected["model"], "prompt": request["prompt"], "size": seedream_image_size(width, height), "response_format": "b64_json", "output_format": "png", "watermark": False, "stream": False}
    if references:
        body["image"] = [f"data:{mime};base64,{base64.b64encode(raw).decode()}" for mime, raw in references]
    return body


async def submit_image(api_key, request, references):
    body = image_request(request, references)
    async with httpx.AsyncClient(timeout=180, trust_env=False, follow_redirects=False) as client:
        return await client.post(ARK_BASE_URL + "/images/generations", headers={"Authorization": f"Bearer {api_key}"}, json=body)
