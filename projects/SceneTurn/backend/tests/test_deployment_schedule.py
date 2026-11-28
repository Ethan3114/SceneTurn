import asyncio
import uuid
import zipfile
from contextlib import nullcontext

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db import lock_write
from app.services.deployment_queue import DISPATCH_WAKE_PATH, epoch_milliseconds
from app.services.deployment_schedule import DeploymentDispatch, WakeDecision, _plan_dispatch, dispatch_due_ms, wake_decision
from app.services.transfer_worker import HostedTransferWorker
from app.transfer_models import TransferJob
from test_api import create_project
from test_postgresql_persistence import postgres_runtime


@pytest.mark.parametrize("database", ("sqlite", "postgresql"))
def test_dispatch_revision_survives_commit_and_keeps_old_wake_stale(request, database):
    if database == "postgresql":
        _, engine, start = request.getfixturevalue("postgres_runtime")
        service_context = start("schedule")
    else:
        service = request.getfixturevalue("live_service")
        engine = create_engine(f"sqlite:///{service.data_dir / 'sceneturn.sqlite3'}")
        service_context = nullcontext(service)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    with service_context as service, service.client() as client:
        account = {"email": f"schedule-{uuid.uuid4().hex}@example.com", "password": uuid.uuid4().hex}
        assert client.post("/auth/register", json=account).status_code == 201
        envelope = create_project(client)
        response = client.post(f"/projects/{envelope['project']['id']}/exports", json={
            "kind": "backup", "operationId": str(uuid.uuid4()), "expectedRevision": envelope["revision"],
        })
        assert response.status_code == 201 and response.json()["transfer"]["status"] == "queued"
        identifier = uuid.UUID(response.json()["transfer"]["id"])
        local_data = service.data_dir

    now = epoch_milliseconds()
    key = ("transfer", str(identifier))
    with factory() as session:
        lock_write(session)
        job = session.get(TransferJob, str(identifier))
        assert dispatch_due_ms(job, now) == now
        rolled_back = _plan_dispatch(session, kind="transfer", job_id=identifier, due_ms=now)
        session.rollback()
    with factory.begin() as session:
        lock_write(session)
        assert session.get(DeploymentDispatch, key) is None
        initial = _plan_dispatch(session, kind="transfer", job_id=identifier, due_ms=now)
        assert initial == rolled_back and initial.superseded is None
        assert _plan_dispatch(session, kind="transfer", job_id=identifier, due_ms=now) is None

    with factory.begin() as session:
        lock_write(session)
        assert wake_decision(session, initial.current, now - 1) is WakeDecision.EARLY
        assert wake_decision(session, initial.current, now) is WakeDecision.DUE
        job = session.get(TransferJob, str(identifier))
        job.status = "processing"
        job.lease_expires_at = (now + 30_000) / 1000
        due = dispatch_due_ms(job, now)
        assert now + 30_000 <= due <= now + 30_001
        revised = _plan_dispatch(session, kind="transfer", job_id=identifier, due_ms=due)
        assert revised.superseded == initial.current
    with factory.begin() as session:
        lock_write(session)
        assert wake_decision(session, initial.current, due) is WakeDecision.STALE
        assert wake_decision(session, revised.current, due - 1) is WakeDecision.EARLY
        job = session.get(TransferJob, str(identifier))
        job.status = "awaiting_confirmation"
        assert dispatch_due_ms(job, due) is None
        retired = _plan_dispatch(session, kind="transfer", job_id=identifier, due_ms=None)
        assert retired.current is None and retired.superseded == revised.current
        assert wake_decision(session, revised.current, due) is WakeDecision.STALE
    with factory.begin() as session:
        lock_write(session)
        job = session.get(TransferJob, str(identifier))
        job.status = "queued"
        resubmitted = _plan_dispatch(session, kind="transfer", job_id=identifier, due_ms=dispatch_due_ms(job, due))
        assert resubmitted.current.revision != revised.current.revision
        assert wake_decision(session, revised.current, due) is WakeDecision.STALE
        assert wake_decision(session, resubmitted.current, due) is WakeDecision.DUE

    with factory.begin() as session:
        lock_write(session)
        executable = _plan_dispatch(session, kind="transfer", job_id=identifier, due_ms=epoch_milliseconds())
    worker = HostedTransferWorker(Settings(_env_file=None, data_dir=local_data, tmp_dir=local_data.parent / "media"), factory)

    async def resume_twice():
        await worker.wake((resubmitted.current, executable.current))
        await worker.wake((executable.current,))

    asyncio.run(resume_twice())
    with factory() as session:
        job = session.get(TransferJob, str(identifier))
        assert job.status == "ready" and job.lease_owner is None
        archive = local_data / job.private_json["archive"]
        with zipfile.ZipFile(archive) as backup:
            assert backup.testzip() is None
        assert list(archive.parent.glob("*.zip")) == [archive]
    engine.dispose()


def test_unconfigured_signed_wake_endpoint_does_not_enable_queue(live_service):
    with live_service.client() as client:
        response = client.post(f"{live_service.base_url.removesuffix('/api/v1')}{DISPATCH_WAKE_PATH}", json={"markers": ["not-a-marker"]})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "SCHEDULER_UNAVAILABLE"
