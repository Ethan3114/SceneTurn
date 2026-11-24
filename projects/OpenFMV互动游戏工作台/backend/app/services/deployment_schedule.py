import math
from dataclasses import dataclass
from enum import Enum
from uuid import UUID, uuid5

from sqlalchemy import BigInteger, CheckConstraint, String
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from app.agent_models import AgentRun
from app.db import lock_write
from app.image_models import ImageJob
from app.models import Base, Generation, TextGeneration
from app.services.deployment_queue import DispatchMarker, JOB_KINDS, QueueWriter, epoch_milliseconds
from app.services.sensetime import IMAGE_RECEIPT_TIMEOUT_SECONDS
from app.transfer_models import TransferJob


SCHEDULE_NAMESPACE = UUID("fa8b73bc-b9ba-413d-a9e2-cc5af2a4981c")
JOB_TYPES = {AgentRun: "agent", ImageJob: "image", TextGeneration: "text", TransferJob: "transfer", Generation: "video"}
LEASED_STATES = {
    AgentRun: {"running", "stopping"},
    TextGeneration: {"running", "submitting"},
    TransferJob: {"processing"},
}
Job = AgentRun | ImageJob | TextGeneration | TransferJob | Generation


class DeploymentDispatch(Base):
    __tablename__ = "deployment_dispatch"
    __table_args__ = (CheckConstraint("due_ms >= 0 AND due_ms <= 9999999999999", name="ck_deployment_dispatch_due"),)

    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[str] = mapped_column(String(36))
    due_ms: Mapped[int | None] = mapped_column(BigInteger)

    @property
    def marker(self) -> DispatchMarker | None:
        if self.due_ms is None:
            return None
        return DispatchMarker(self.due_ms, self.kind, UUID(self.job_id), UUID(self.revision))


@dataclass(frozen=True)
class DispatchChange:
    current: DispatchMarker | None
    superseded: DispatchMarker | None


class WakeDecision(str, Enum):
    DUE = "due"
    EARLY = "early"
    STALE = "stale"


def _at_or_after(now_ms: int, *timestamps: float | None) -> int:
    return max(now_ms, *(math.ceil((timestamp or 0) * 1000) for timestamp in timestamps))


def _image_due(job: ImageJob, now_ms: int, dependencies_ready: bool | None, slot_at_ms: int | None) -> int | None:
    if job.request_json.get("modelProfile", {}).get("providerId") == "codex-local":
        return None
    if job.status == "queued":
        if dependencies_ready is False or (dependencies_ready is None and job.dependency_ids):
            return None
        return max(now_ms, slot_at_ms or 0)
    if job.status == "response_saved":
        return now_ms
    if job.status in {"claimed", "submitting"} or (job.status == "submission_unknown" and job.account_id is not None):
        return _at_or_after(now_ms, (job.submitted_at or job.updated_at) + IMAGE_RECEIPT_TIMEOUT_SECONDS)
    return None


def dispatch_due_ms(job: Job, now_ms: int, *, image_dependencies_ready: bool | None = None,
                    image_slot_at_ms: int | None = None) -> int | None:
    if isinstance(job, ImageJob):
        return _image_due(job, now_ms, image_dependencies_ready, image_slot_at_ms)
    if job.status == "queued" and not (isinstance(job, AgentRun) and job.executor != "platform"):
        return now_ms
    if isinstance(job, Generation):
        if job.status == "submitting":
            return _at_or_after(now_ms, job.lease_expires_at)
        if job.status in {"processing", "downloading"} or (job.status == "submission_unknown" and job.remote_task_id):
            return _at_or_after(now_ms, job.lease_expires_at, job.next_poll_at)
        return None
    if job.status in LEASED_STATES[type(job)]:
        return _at_or_after(now_ms, job.lease_expires_at)
    return None


def _plan_dispatch(session: Session, *, kind: str, job_id: UUID, due_ms: int | None) -> DispatchChange | None:
    if kind not in JOB_KINDS:
        raise ValueError("不支持此类任务的续跑调度")
    row = session.get(DeploymentDispatch, (kind, str(job_id)))
    if (row is None and due_ms is None) or (row is not None and row.due_ms == due_ms):
        return None
    superseded = row.marker if row is not None else None
    revision = uuid5(SCHEDULE_NAMESPACE, f"{kind}:{job_id}:{row.revision if row is not None else 'first'}:{due_ms}")
    current = DispatchMarker(due_ms, kind, job_id, revision) if due_ms is not None else None
    if row is None:
        session.add(DeploymentDispatch(kind=kind, job_id=str(job_id), revision=str(revision), due_ms=due_ms))
    else:
        row.revision, row.due_ms = str(revision), due_ms
    session.flush()
    return DispatchChange(current, superseded)


def register_dispatch(session: Session, queue: QueueWriter, job: Job, *,
                      image_dependencies_ready: bool | None = None, image_slot_at_ms: int | None = None) -> DispatchChange | None:
    # 调用方需先取得业务写锁，并在 COS 发布成功后一起提交状态和调度版本。
    due_ms = dispatch_due_ms(job, epoch_milliseconds(), image_dependencies_ready=image_dependencies_ready,
                             image_slot_at_ms=image_slot_at_ms)
    change = _plan_dispatch(session, kind=JOB_TYPES[type(job)], job_id=UUID(job.id), due_ms=due_ms)
    if change is not None and change.current is not None:
        queue.publish(change.current)
    return change


def wake_decision(session: Session, marker: DispatchMarker, now_ms: int) -> WakeDecision:
    # 调用方持同一业务写锁核对当前版本，再使用任务自己的领取及租约逻辑。
    row = session.get(DeploymentDispatch, (marker.kind, str(marker.job_id)), populate_existing=True)
    if row is None or row.marker != marker:
        return WakeDecision.STALE
    return WakeDecision.DUE if marker.due_ms <= now_ms else WakeDecision.EARLY


def retire_dispatch(session: Session, marker: DispatchMarker) -> None:
    _plan_dispatch(session, kind=marker.kind, job_id=marker.job_id, due_ms=None)


def acknowledge_obsolete_dispatch(factory: sessionmaker, queue: QueueWriter, marker: DispatchMarker) -> bool:
    # 删除也受同一把锁保护，避免误删其他事务刚重新发布的记录。
    with factory.begin() as session:
        lock_write(session)
        if wake_decision(session, marker, epoch_milliseconds()) is not WakeDecision.STALE:
            return False
        queue.remove(marker)
        return True
