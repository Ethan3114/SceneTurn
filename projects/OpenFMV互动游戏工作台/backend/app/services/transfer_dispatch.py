import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import AppError
from app.services.deployment_queue import DeploymentQueue, DeploymentQueueError, DispatchMarker
from app.services.deployment_schedule import acknowledge_obsolete_dispatch, register_dispatch
from app.transfer_models import TransferJob


logger = logging.getLogger("openfmv")


def transfer_queue(settings: Settings) -> DeploymentQueue | None:
    if settings.transfer_queue_bucket is None:
        return None
    token = settings.transfer_queue_session_token
    return DeploymentQueue(bucket=settings.transfer_queue_bucket, region=settings.transfer_queue_region,
        secret_id=settings.transfer_queue_secret_id.get_secret_value(), secret_key=settings.transfer_queue_secret_key.get_secret_value(),
        token=token.get_secret_value() if token is not None else None)


def acknowledge_transfer_marker(factory: sessionmaker, queue: DeploymentQueue, marker: DispatchMarker) -> None:
    try:
        acknowledge_obsolete_dispatch(factory, queue, marker)
    except (DeploymentQueueError, SQLAlchemyError):
        logger.warning("传输任务旧调度记录将由下一次定时检查清理")


def commit_transfer(session: Session, value: TransferJob, settings: Settings) -> None:
    queue = transfer_queue(settings)
    try:
        change = register_dispatch(session, queue, value) if queue is not None else None
    except DeploymentQueueError:
        session.rollback()
        raise AppError(503, "TRANSFER_QUEUE_UNAVAILABLE", "传输任务暂未安排，请重试") from None
    factory = sessionmaker(bind=session.get_bind()) if change is not None and change.superseded is not None else None
    session.commit()
    if factory is not None:
        acknowledge_transfer_marker(factory, queue, change.superseded)
