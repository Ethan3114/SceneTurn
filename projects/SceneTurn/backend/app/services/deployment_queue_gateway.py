from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.services.deployment_queue import DeploymentQueueError, DispatchMarker, epoch_milliseconds

if TYPE_CHECKING:
    from pydantic import SecretStr


QueueAction = Literal["publish", "remove"]
QUEUE_EVENT_TYPE = "SceneTurnQueue"
_QUEUE_FUNCTION = "sceneturn-task-dispatch"
_SIGNATURE_DOMAIN = b"sceneturn-queue-write-v1"
_MAX_EVENT_AGE_SECONDS = 60
_ENV_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,79}")
_API_KEY = re.compile(r"[!-~]+")
_ISSUED_AT = re.compile(r"[0-9]{10}")
_SIGNATURE = re.compile(r"[0-9a-f]{64}")


def _sign(token: str, action: QueueAction, key: str, issued_at: str) -> str:
    signed = b"\0".join((_SIGNATURE_DOMAIN, action.encode(), key.encode(), issued_at.encode()))
    return hmac.digest(token.encode(), signed, "sha256").hex()


def verified_queue_event(event: dict, token: str) -> tuple[QueueAction, DispatchMarker]:
    action, key = event.get("action"), event.get("key")
    issued_at, supplied = event.get("issuedAt"), event.get("signature")
    if (event.get("Type") != QUEUE_EVENT_TYPE or not 32 <= len(token) <= 256
            or action not in ("publish", "remove") or not isinstance(key, str) or len(key) > 160
            or not isinstance(issued_at, str) or not _ISSUED_AT.fullmatch(issued_at)
            or not isinstance(supplied, str) or not _SIGNATURE.fullmatch(supplied)
            or abs(epoch_milliseconds() // 1000 - int(issued_at)) > _MAX_EVENT_AGE_SECONDS
            or not hmac.compare_digest(supplied, _sign(token, action, key, issued_at))):
        raise ValueError("任务队列写入事件未授权")
    return action, DispatchMarker.from_key(key)


def _check_acknowledgement(content: bytes, action: QueueAction, key: str) -> None:
    try:
        value = json.loads(content)
        if isinstance(value, dict) and set(value) <= {"result", "requestId", "timestamp"} and "result" in value:
            value = value["result"]
            if isinstance(value, str):
                value = json.loads(value)
    except ValueError:
        raise DeploymentQueueError("任务队列函数回执无法读取") from None
    if value != {"action": action, "key": key}:
        raise DeploymentQueueError("任务队列函数回执与本次操作不一致")


@dataclass(frozen=True)
class CloudBaseDeploymentQueue:
    env_id: str
    api_key: SecretStr
    write_token: SecretStr
    timeout: float = 10.0

    def __post_init__(self) -> None:
        if not _ENV_ID.fullmatch(self.env_id) or self.env_id.endswith("-"):
            raise DeploymentQueueError("任务队列函数的环境标识无效")
        if not _API_KEY.fullmatch(self.api_key.get_secret_value()) or not 32 <= len(self.write_token.get_secret_value()) <= 256:
            raise DeploymentQueueError("任务队列函数的访问凭据无效")

    def publish(self, marker: DispatchMarker) -> None:
        self._invoke("publish", marker)

    def remove(self, marker: DispatchMarker) -> None:
        self._invoke("remove", marker)

    def _invoke(self, action: QueueAction, marker: DispatchMarker) -> None:
        # 云函数仅使用同一模块的签名校验，无须安装后端 HTTP 客户端依赖。
        import httpx

        endpoint = f"https://{self.env_id}.api.tcloudbasegateway.com/v1/functions/{_QUEUE_FUNCTION}"
        key = marker.key
        issued_at = str(epoch_milliseconds() // 1000)
        event = {
            "Type": QUEUE_EVENT_TYPE,
            "action": action,
            "key": key,
            "issuedAt": issued_at,
            "signature": _sign(self.write_token.get_secret_value(), action, key, issued_at),
        }
        headers = {"Authorization": f"Bearer {self.api_key.get_secret_value()}", "Accept": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False, follow_redirects=False) as client:
                response = client.post(endpoint, headers=headers, json=event)
        except httpx.TimeoutException:
            raise DeploymentQueueError("任务队列函数响应超时，请核实操作结果") from None
        except httpx.HTTPError:
            raise DeploymentQueueError("无法连接任务队列函数") from None
        if response.status_code in (401, 403):
            raise DeploymentQueueError("任务队列函数访问未授权")
        if response.status_code == 404:
            raise DeploymentQueueError("任务队列函数不可用")
        if response.status_code != 200:
            raise DeploymentQueueError(f"任务队列函数调用失败，HTTP {response.status_code}")
        _check_acknowledgement(response.content, action, key)
