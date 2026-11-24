import json
import os
import re
from hmac import compare_digest
from http.client import HTTPSConnection
from urllib.parse import urlsplit

from app.services.deployment_queue import (
    DISPATCH_WAKE_HEADER,
    DISPATCH_WAKE_PATH,
    DISPATCH_WAKE_TIME_HEADER,
    DeploymentQueue,
    DispatchMarker,
    epoch_milliseconds,
    wake_signature,
)

_RUN_HOST = re.compile(r"sceneturn-api(?:-[a-z0-9-]+)?\.[a-z0-9-]+\.(?:run|app)\.tcloudbase\.com")
_TRIGGER_NAME = "sceneturn-due-jobs"


def wake_backend(markers: tuple[DispatchMarker, ...]) -> None:
    origin = urlsplit(os.environ["SCENETURN_PRIVATE_API_ORIGIN"])
    if (origin.scheme != "https" or not origin.hostname or not _RUN_HOST.fullmatch(origin.hostname) or origin.port not in {None, 443}
            or origin.username or origin.password
            or origin.path not in {"", "/"} or origin.query or origin.fragment):
        raise ValueError("定时函数需要 SceneTurn 云托管后端的 HTTPS 地址")
    token = os.environ["SCENETURN_SCHEDULER_TOKEN"]
    if len(token) < 32:
        raise ValueError("任务唤醒密钥长度不足")
    connection = HTTPSConnection(origin.hostname, origin.port, timeout=12)
    body = json.dumps({"markers": [marker.key for marker in markers]}).encode()
    timestamp = str(epoch_milliseconds() // 1000)
    try:
        connection.request("POST", DISPATCH_WAKE_PATH, body=body, headers={
            "Content-Type": "application/json",
            DISPATCH_WAKE_HEADER: wake_signature(token, timestamp, body),
            DISPATCH_WAKE_TIME_HEADER: timestamp,
        })
        response = connection.getresponse()
        if response.status != 202:
            raise RuntimeError(f"工作台拒绝了任务唤醒，HTTP {response.status}")
        response.read(4096)
    finally:
        connection.close()


def main_handler(event, context):
    token = os.environ.get("SCENETURN_TIMER_EVENT_TOKEN", "")
    message = event.get("Message", "") if isinstance(event, dict) else ""
    if (not isinstance(event, dict) or event.get("Type") != "Timer" or event.get("TriggerName") != _TRIGGER_NAME
            or len(token) < 32 or not isinstance(message, str) or not compare_digest(message.encode(), token.encode())):
        raise ValueError("任务调度事件未授权")
    queue = DeploymentQueue(
        bucket=os.environ["SCENETURN_QUEUE_COS_BUCKET"],
        region=os.environ["TENCENTCLOUD_REGION"],
        secret_id=os.environ["TENCENTCLOUD_SECRETID"],
        secret_key=os.environ["TENCENTCLOUD_SECRETKEY"],
        token=os.environ["TENCENTCLOUD_SESSIONTOKEN"],
    )
    markers = queue.due(epoch_milliseconds())
    if markers:
        wake_backend(markers)
    return {"due": len(markers)}
