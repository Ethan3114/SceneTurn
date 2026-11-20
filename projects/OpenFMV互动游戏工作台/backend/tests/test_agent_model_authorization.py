import asyncio
import copy
import hashlib
import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent_models import AgentCall, AgentRun, AgentStep
from app.core.config import Settings
from app.core.errors import AppError
from app.models import User
from app.services.agent import _execute_platform, resume_run
from app.services.text_provider import PRO_MODEL
from conftest import PRODUCT_ROOT


def archived_pro_recovery(writable_snapshot, request, pending_status=None):
    source = PRODUCT_ROOT / ".intermediate/agent-smoke/438bb5cc-7709-40f9-a295-187a39fbfd6f/data/openfmv.sqlite3"
    assert source.is_file(), "需要已发生的真实Pro任务记录"
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = writable_snapshot(source)
    data_dir = destination.parent
    engine = create_engine(f"sqlite:///{destination}")
    request.addfinalizer(engine.dispose)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.role == "editor"))
        assert run.status == "completed" and run.limits_json["model"] == PRO_MODEL
        final = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at.desc())).first()
        assert final.response_json["action"] == "complete" and final.remote_request_id and final.usage_json["settled"]
        original_limits = copy.deepcopy(run.limits_json)
        owner_email = session.get(User, run.owner_id).email
        step = session.get(AgentStep, final.step_id)
        # 仅在隔离副本恢复真实任务的中断控制状态，供应商原文与账目保持。
        run.status, run.stop_requested = "stopped", 1
        step.status = "stopped"
        if pending_status is not None:
            final.status = pending_status
            final.usage_json = {key: value for key, value in final.usage_json.items() if key != "toolExecutionDone"}
        run_id, project_id, owner_id, revision = run.id, run.project_id, run.owner_id, run.project_revision
        session.commit()
    with factory() as session:
        resumed = resume_run(session, project_id, run_id, owner_id, str(uuid.uuid4()), revision)
        assert resumed.status == "queued" and resumed.limits_json == original_limits
    settings = Settings(data_dir=data_dir, text_admin_email=owner_email)
    return source, original_hash, destination, engine, factory, settings, run_id, original_limits


def immutable_records(database):
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        return {
            "calls": connection.execute("select id,run_id,step_id,role,provider,model,input_json,response_json,raw_output,remote_request_id,input_tokens,output_tokens,total_tokens,cost_micros,created_at,finished_at from agent_calls order by id").fetchall(),
            "budget": connection.execute("select * from budgets order by id").fetchall(),
            "reservations": connection.execute("select * from budget_reservations order by id").fetchall(),
            "projects": connection.execute("select * from projects order by id").fetchall(),
            "versions": connection.execute("select * from project_versions order by id").fetchall(),
        }


def test_resumed_real_pro_task_is_denied_before_new_reservation_or_supplier_request(writable_snapshot, request):
    source, source_hash, database, engine, factory, settings, run_id, frozen = archived_pro_recovery(writable_snapshot, request)
    before = immutable_records(database)
    with pytest.raises(AppError) as failure:
        asyncio.run(_execute_platform(settings, factory, run_id, "authorization-check"))
    assert failure.value.status == 403 and failure.value.code == "MODEL_NOT_AUTHORIZED"
    assert immutable_records(database) == before
    with factory() as session:
        assert session.get(AgentRun, run_id).limits_json == frozen
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash


@pytest.mark.parametrize("pending_status", ["response_received", "ready"])
def test_saved_real_pro_reply_can_finish_recovery_without_new_paid_request(writable_snapshot, request, pending_status):
    source, source_hash, database, engine, factory, settings, run_id, frozen = archived_pro_recovery(writable_snapshot, request, pending_status)
    before = immutable_records(database)
    asyncio.run(_execute_platform(settings, factory, run_id, "saved-reply-check"))
    assert immutable_records(database) == before
    with factory() as session:
        run = session.get(AgentRun, run_id)
        assert run.status == "completed" and run.limits_json == frozen
        final = session.scalars(select(AgentCall).where(AgentCall.run_id == run_id).order_by(AgentCall.created_at.desc())).first()
        assert final.status == "ready" and final.usage_json["toolExecutionDone"] is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
