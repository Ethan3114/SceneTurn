import logging
from hmac import compare_digest
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.services.cloudbase_database import gateway_enabled
from app.services.cloudbase_text import CloudBaseTextRepository
from app.services.deployment_queue import (
    DISPATCH_BATCH_SIZE,
    DISPATCH_WAKE_HEADER,
    DISPATCH_WAKE_MAX_AGE_SECONDS,
    DISPATCH_WAKE_PATH,
    DISPATCH_WAKE_TIME_HEADER,
    DispatchMarker,
    epoch_milliseconds,
    wake_signature,
)
from app.services.transfer_rpc import TransferRpc


router = APIRouter(include_in_schema=False)
logger = logging.getLogger("openfmv")


class WakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    markers: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(min_length=1, max_length=DISPATCH_BATCH_SIZE)


def start_accepted_transfer(request: Request, tasks: BackgroundTasks, transfer) -> None:
    worker = request.app.state.hosted_transfer_worker
    if worker is not None and transfer.status == "queued":
        tasks.add_task(worker.run_now, transfer.id)


async def wake_gateway(settings: Settings, markers: tuple[DispatchMarker, ...]) -> None:
    transfer = TransferRpc(settings)
    text = CloudBaseTextRepository(settings)
    for marker in markers:
        try:
            if marker.kind == "transfer":
                await transfer.run_transfer(marker)
            else:
                await text.run(marker)
        except AppError:
            logger.warning("后台任务由定时检查核对运行状态")


@router.post(DISPATCH_WAKE_PATH, status_code=202)
async def wake(body: WakeRequest, request: Request, tasks: BackgroundTasks):
    worker = request.app.state.hosted_transfer_worker
    settings = get_settings()
    token = settings.scheduler_token
    if worker is None or token is None:
        raise AppError(404, "SCHEDULER_UNAVAILABLE", "任务唤醒服务不可用")
    supplied = request.headers.get(DISPATCH_WAKE_HEADER, "")
    timestamp = request.headers.get(DISPATCH_WAKE_TIME_HEADER, "")
    if (len(timestamp) != 10 or not timestamp.isascii() or not timestamp.isdecimal()
            or abs(epoch_milliseconds() // 1000 - int(timestamp)) > DISPATCH_WAKE_MAX_AGE_SECONDS
            or not compare_digest(supplied.encode(), wake_signature(token.get_secret_value(), timestamp, await request.body()).encode())):
        raise AppError(403, "SCHEDULER_FORBIDDEN", "任务唤醒未授权")
    try:
        markers = tuple(DispatchMarker.from_key(item) for item in dict.fromkeys(body.markers))
    except ValueError:
        raise AppError(422, "SCHEDULER_MARKER_INVALID", "任务调度记录格式无效") from None
    accepted = {"transfer", "text"} if gateway_enabled(settings) else {"transfer"}
    if any(marker.kind not in accepted for marker in markers):
        raise AppError(422, "SCHEDULER_KIND_UNAVAILABLE", "当前调度服务仅受理传输任务")
    if gateway_enabled(settings):
        tasks.add_task(wake_gateway, settings, markers)
    else:
        tasks.add_task(worker.wake, markers)
    return {"status": "accepted", "count": len(markers)}
