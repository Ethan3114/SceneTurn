import hmac
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from qcloud_cos import CosClientError, CosConfig, CosS3Client, CosServiceError


DISPATCH_PREFIX = "sceneturn/scheduler/v1/"
DISPATCH_BATCH_SIZE = 32
DISPATCH_WAKE_PATH = "/internal/worker/wake"
DISPATCH_WAKE_HEADER = "X-SceneTurn-Scheduler-Signature"
DISPATCH_WAKE_TIME_HEADER = "X-SceneTurn-Scheduler-Time"
DISPATCH_WAKE_MAX_AGE_SECONDS = 120
JOB_KINDS = {"agent", "image", "text", "transfer", "video"}
_BUCKET = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*-[1-9][0-9]{4,14}")
_KEY = re.compile(
    DISPATCH_PREFIX + r"([0-9]{13})/([a-z]+)/([0-9a-f-]{36})/([0-9a-f-]{36})"
)

# COS SDK 的错误响应可能包含请求签名与临时凭据。
logging.getLogger("qcloud_cos").setLevel(logging.CRITICAL + 1)


class DeploymentQueueError(RuntimeError):
    pass


@contextmanager
def _cos_request(action: str):
    try:
        yield
    except CosServiceError as error:
        raise DeploymentQueueError(f"COS 无法{action}任务调度记录，HTTP {error.get_status_code()}") from None
    except CosClientError:
        raise DeploymentQueueError(f"连接 COS 失败，无法{action}任务调度记录") from None


def epoch_milliseconds() -> int:
    return time.time_ns() // 1_000_000


def wake_signature(token: str, timestamp: str, body: bytes) -> str:
    return hmac.digest(token.encode(), timestamp.encode() + b"." + body, "sha256").hex()


@dataclass(frozen=True)
class DispatchMarker:
    due_ms: int
    kind: str
    job_id: UUID
    revision: UUID

    def __post_init__(self) -> None:
        if type(self.due_ms) is not int or not 0 <= self.due_ms <= 9_999_999_999_999 or self.kind not in JOB_KINDS:
            raise ValueError("任务调度记录的类型或截止时间无效")
        if not isinstance(self.job_id, UUID) or not isinstance(self.revision, UUID):
            raise ValueError("任务和调度版本必须使用 UUID")

    @property
    def key(self) -> str:
        return f"{DISPATCH_PREFIX}{self.due_ms:013}/{self.kind}/{self.job_id}/{self.revision}"

    @classmethod
    def from_key(cls, key: str) -> "DispatchMarker":
        match = _KEY.fullmatch(key)
        if match is None:
            raise ValueError("任务调度记录的对象路径无效")
        due_ms, kind, job_id, revision = match.groups()
        marker = cls(int(due_ms), kind, UUID(job_id), UUID(revision))
        if marker.key != key:
            raise ValueError("任务调度记录的标识未使用标准格式")
        return marker


class DeploymentQueue:
    def __init__(self, *, bucket: str, region: str, secret_id: str, secret_key: str, token: str | None = None):
        if not _BUCKET.fullmatch(bucket):
            raise ValueError("任务调度需要包含账号 APPID 的私有 COS 存储桶名称")
        config = CosConfig(Region=region, SecretId=secret_id, SecretKey=secret_key, Token=token, Scheme="https", Timeout=4)
        self._bucket = bucket
        self._client = CosS3Client(config, retry=1)

    def publish(self, marker: DispatchMarker) -> None:
        with _cos_request("保存"):
            self._client.put_object(Bucket=self._bucket, Key=marker.key, Body=b"", ContentType="application/octet-stream")

    def remove(self, marker: DispatchMarker) -> None:
        with _cos_request("删除"):
            self._client.delete_object(Bucket=self._bucket, Key=marker.key)

    def due(self, now_ms: int) -> tuple[DispatchMarker, ...]:
        with _cos_request("列出"):
            result = self._client.list_objects(Bucket=self._bucket, Prefix=DISPATCH_PREFIX, MaxKeys=DISPATCH_BATCH_SIZE)
        due = []
        # 固定宽度时间戳让 COS 的字典序与续跑时间一致。
        for entry in result.get("Contents", []):
            marker = DispatchMarker.from_key(entry["Key"])
            if marker.due_ms > now_ms:
                break
            due.append(marker)
        return tuple(due)
