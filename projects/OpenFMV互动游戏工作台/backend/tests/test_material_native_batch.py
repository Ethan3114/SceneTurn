import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent_models import AgentCall, AgentRun, AgentStep
from app.core.errors import AppError
from app.agent_schemas import AgentRunView, RunRetryUnknown, RunLimitExtension
from app.services.agent import _apply_native_call, _native_messages, _safe_native_rejection, _unknown_retry_authorized, extend_run_limit, resume_run, retry_unknown_run, run_view
from app.services.agent_provider import MATERIAL_NATIVE_AGENT_PROTOCOL, decode_platform_message, native_batch_ids
from app.core.config import Settings
from app.models import Budget, BudgetReservation, Project, TextProviderSettings, User
from app.material_models import MaterialConversation, MaterialMessage
from app.services.materials import view
from app.services.context import SourceResolver, retrieve_context
from app.services.projects import payload_digest
from app.tools.project_tools import _read_artifact, execute_tool, require_canvas


DATABASE = Path(__file__).resolve().parents[2] / "data/openfmv.sqlite3"
CALL_ID = "f5cfa651-93a7-456d-b758-01cbe8d1d1ad"
UNKNOWN_SNAPSHOT = DATABASE.parent.parent / ".intermediate/material-recovery/eafe14be-35fd-4bf6-97c3-613133121dcd/unknown-state.sqlite3"


@pytest.fixture
def unknown_database(writable_snapshot, request):
    snapshot = UNKNOWN_SNAPSHOT.with_name(request.param) if hasattr(request, "param") else UNKNOWN_SNAPSHOT
    database = writable_snapshot(snapshot)
    engine = create_engine(f"sqlite:///{database}")
    request.addfinalizer(engine.dispose)
    return engine, database.parent


def test_real_provider_batch_is_rejected_with_genuine_call_ids():
    engine = create_engine(f"sqlite:///{DATABASE.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        call = session.get(AgentCall, CALL_ID)
        assert call.remote_request_id and call.usage_json["settled"]
        message = json.loads(call.raw_output)
        identifiers = native_batch_ids(message, MATERIAL_NATIVE_AGENT_PROTOCOL)
        assert identifiers == [item["id"] for item in message["tool_calls"]] and len(identifiers) == 2
        with pytest.raises(AppError) as invalid:
            decode_platform_message(message, False, call.role, MATERIAL_NATIVE_AGENT_PROTOCOL, "R5")
        assert invalid.value.code == "AGENT_NATIVE_BATCH_NOT_EXECUTED"
        assert _safe_native_rejection(invalid.value, None)
        assert native_batch_ids(message, "native-tools-v3-strict-ref") == []
    engine.dispose()


def test_actual_rejected_batch_history_returns_each_real_id_once():
    engine = create_engine(f"sqlite:///{UNKNOWN_SNAPSHOT.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        call = session.get(AgentCall, CALL_ID)
        assert call.status == "tool_rejected" and call.usage_json["toolError"]["businessExecuted"] is False
        run = session.get(AgentRun, call.run_id)
        step = session.get(AgentStep, call.step_id)
        context = json.loads(call.input_json["messages"][1]["content"])["context"]
        messages = _native_messages(run, step, step.role, context, [call], [])
        replies = [message for message in messages if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in replies] == call.usage_json["rejectedBatchIds"]
        assert all(json.loads(message["content"])["error"]["businessExecuted"] is False for message in replies)
        assert [message for message in messages if message["role"] == "assistant"] == [json.loads(call.raw_output)]
    engine.dispose()


def test_real_plain_source_is_read_as_text_with_its_actual_hash():
    engine = create_engine(f"sqlite:///{UNKNOWN_SNAPSHOT.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        call = session.get(AgentCall, CALL_ID)
        run = session.get(AgentRun, call.run_id)
        project = session.get(Project, run.project_id)
        canvas = require_canvas(session, run, project)
        target = next(node for node in canvas.data_json["nodes"] if node["id"] == "e78e7504-6d50-42ae-baf0-cacd987755c9")
        result = _read_artifact(session, run, project, canvas, target["id"], Settings(data_dir=DATABASE.parent))
        assert result["kind"] == "plain_text" and result["artifact"] is None and result["professionalAvailable"] is False
        assert result["text"] == target["metadata"]["content"] and result["patchablePaths"] == []
        assert result["sourceKind"] == "canvas_node"
    engine.dispose()


def test_real_argument_rejection_is_reported_without_replaying_invalid_arguments():
    engine = create_engine(f"sqlite:///{UNKNOWN_SNAPSHOT.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        call = session.scalar(select(AgentCall).where(AgentCall.run_id == "eafe14be-35fd-4bf6-97c3-613133121dcd", AgentCall.role == "character_designer", AgentCall.status == "tool_rejected").order_by(AgentCall.created_at.desc()))
        assert call.usage_json["toolError"]["code"] == "AGENT_NATIVE_ARGUMENTS_INVALID"
        run = session.get(AgentRun, call.run_id)
        step = session.get(AgentStep, call.step_id)
        context = json.loads(call.input_json["messages"][1]["content"])["context"]
        messages = _native_messages(run, step, step.role, context, [call], [])
        assert not any(message["role"] == "assistant" for message in messages)
        report = json.loads(messages[-1]["content"])
        assert report["origin"] == "runtime_parameter_validation" and report["requestId"] == call.remote_request_id
        assert report["error"] == call.usage_json["toolError"]
        assert '当前岗位write_artifact完整参数Schema' in messages[0]["content"]
    engine.dispose()


def test_actual_unknown_submission_requires_confirmation_and_keeps_original_reservation(unknown_database):
    engine, directory = unknown_database
    with Session(engine) as session:
        run = session.get(AgentRun, "eafe14be-35fd-4bf6-97c3-613133121dcd")
        call = session.get(AgentCall, "cbd46923-d04b-47c9-8cf1-4dbd40e54b1d")
        user = session.get(User, session.get(TextProviderSettings, "platform").admin_owner_id)
        settings = Settings(data_dir=directory, text_admin_email=user.email, worker_enabled=False)
        before_budget = session.get(Budget, "global").text_reserved_micros
        reservation = session.get(BudgetReservation, call.id)
        before_reservation = reservation.reserved_micros
        before_limit = run.limits_json["maxCostMicros"]
        assert run_view(session, run)["retryableUnknownCallId"] == call.id
        with pytest.raises(AppError) as forbidden:
            resume_run(session, run.project_id, run.id, run.owner_id, str(uuid.uuid4()), run.project_revision)
        assert forbidden.value.code == "AGENT_SUBMISSION_UNKNOWN"
        session.rollback()
        body = RunRetryUnknown(operation_id=str(uuid.uuid4()), expected_revision=run.project_revision, call_id=call.id, confirmed=True, max_steps=40)
        retry_unknown_run(session, run.project_id, run.id, user, body, settings)
        first_limits = run.limits_json
        retry_unknown_run(session, run.project_id, run.id, user, body, settings)
        assert run.status == "queued" and run.limits_json == first_limits and len(first_limits["unknownRetryConfirmations"]) == 1
        assert first_limits["maxCostMicros"] == before_limit and first_limits["maxSteps"] == 40
        assert _unknown_retry_authorized(session, run, call)
        session.expire_all()
        assert session.get(AgentCall, call.id).status == "submission_unknown"
        assert session.get(BudgetReservation, call.id).reserved_micros == before_reservation > 0
        assert session.get(Budget, "global").text_reserved_micros == before_budget
        assert session.get(BudgetReservation, call.id).status == "reserved"
        assert run.limits_json["materialContext"]["scope"] == "current_round"
        assert run.limits_json["materialContext"]["readableNodeIds"] == []
        message = session.scalar(select(MaterialMessage).where(MaterialMessage.run_id == run.id))
        conversation = view(session, session.get(MaterialConversation, message.conversation_id))
        assert {node["id"] for node in conversation["nodes"]} == set(run.created_ids_json)
        assert len(conversation["nodes"]) == 2
        project = session.get(Project, run.project_id)
        canvas = require_canvas(session, run, project)
        forest_id = "e78e7504-6d50-42ae-baf0-cacd987755c9"
        assert forest_id in {node["id"] for node in canvas.data_json["nodes"]}
        with pytest.raises(AppError) as excluded:
            _read_artifact(session, run, project, canvas, forest_id, settings)
        assert excluded.value.code == "AGENT_TARGET_NOT_ALLOWED"
        context = retrieve_context(session, project.id, user.id, run.scene_id, target_ids=[], material_only=True)
        assert context["canvasTexts"] == [] and context["memories"] == []
        session.commit()
        result = execute_tool(session, run, "scene_designer", "read_project", {}, str(uuid.uuid4()), run.project_revision, settings, execution_origin="runtime")
        assert {node["id"] for node in result["result"]["canvas"]["nodes"]} == set(run.created_ids_json)
        assert all(asset["mimeType"] != "video/mp4" for asset in result["result"]["assets"])


def test_unknown_retry_rejects_another_actual_call_and_stale_revision(unknown_database):
    engine, directory = unknown_database
    with Session(engine) as session:
        run = session.get(AgentRun, "eafe14be-35fd-4bf6-97c3-613133121dcd")
        user = session.get(User, run.owner_id)
        settings = Settings(data_dir=directory, text_admin_email=user.email, worker_enabled=False)
        for identifier, revision, expected in [(CALL_ID, run.project_revision, "AGENT_UNKNOWN_RETRY_UNAVAILABLE"),
                ("cbd46923-d04b-47c9-8cf1-4dbd40e54b1d", run.project_revision - 1, "REVISION_CONFLICT")]:
            body = RunRetryUnknown(operation_id=str(uuid.uuid4()), expected_revision=revision, call_id=identifier, confirmed=True, max_steps=40)
            with pytest.raises(AppError) as invalid:
                retry_unknown_run(session, run.project_id, run.id, user, body, settings)
            assert invalid.value.code == expected
            session.rollback()
        assert run.status == "submission_unknown" and "unknownRetryConfirmations" not in run.limits_json


@pytest.mark.parametrize("unknown_database", ["approval-state.sqlite3"], indirect=True)
def test_confirmed_retry_reads_current_actual_version_and_preserves_original_request(unknown_database):
    engine, directory = unknown_database
    with Session(engine) as session:
        run = session.get(AgentRun, "eafe14be-35fd-4bf6-97c3-613133121dcd")
        project = session.get(Project, run.project_id)
        call = session.get(AgentCall, "cbd46923-d04b-47c9-8cf1-4dbd40e54b1d")
        user = session.get(User, run.owner_id)
        assert project.revision > call.input_json["projectRevision"]
        old_input = call.input_json
        body = RunRetryUnknown(operation_id=str(uuid.uuid4()), expected_revision=project.revision, call_id=call.id, confirmed=True, max_steps=40)
        retry_unknown_run(session, run.project_id, run.id, user, body, Settings(data_dir=directory, text_admin_email=user.email, worker_enabled=False))
        assert run.project_revision == project.revision and call.input_json == old_input
        assert call.status == "submission_unknown"
        step = session.get(AgentStep, call.step_id)
        previous = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id, AgentCall.step_id == step.id)).all()
        messages = _native_messages(run, step, step.role, {}, previous, [])
        assert not any(message["role"] in {"assistant", "tool"} for message in messages)
        assert _unknown_retry_authorized(session, run, call)


@pytest.mark.parametrize("unknown_database", ["approval-state.sqlite3"], indirect=True)
def test_actual_layout_changes_keep_content_sources_valid_and_preserve_digest_checks(unknown_database):
    engine, directory = unknown_database
    with Session(engine) as session:
        run = session.get(AgentRun, "eafe14be-35fd-4bf6-97c3-613133121dcd")
        project = session.get(Project, run.project_id)
        canvas = require_canvas(session, run, project)
        character = next(node for node in canvas.data_json["nodes"] if node["id"] == "2cf5326d-a3ca-4471-9d6c-ceb003603870")
        binding = next(item for item in character["metadata"]["artifactSources"] if item["kind"] == "canvas_node" and item["id"] == "ca8c7cb9-9ea9-45ac-b3e5-9237d42debce")
        reference = {key: binding[key] for key in ("kind", "id", "revision", "sceneId")}
        resolver = SourceResolver(session, project)
        original, current = resolver.resolve(reference), resolver.resolve(reference, current=True)
        assert original["position"] != current["position"] and payload_digest(original) == binding["digest"]
        assert payload_digest(original) != payload_digest(current)
        assert resolver.source_status(binding) == (True, None)
        assert resolver.source_status({**binding, "digest": "0" * 64}) == (False, "source_changed")
        result = _read_artifact(session, run, project, canvas, character["id"], Settings(data_dir=directory))
        assert result["sourcesValidated"] is True and result["artifact"] == character["metadata"]["creativeArtifact"]


@pytest.mark.parametrize("unknown_database", ["layout-conflict-state.sqlite3"], indirect=True)
def test_actual_read_response_can_finish_after_layout_validation_without_another_model_request(unknown_database):
    engine, directory = unknown_database
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        run = session.get(AgentRun, "eafe14be-35fd-4bf6-97c3-613133121dcd")
        call = session.get(AgentCall, "ec6a72d3-6027-4b22-a188-4e4c7510dcd5")
        assert run.status == "conflict" and call.status == "failed"
        original_request = (call.remote_request_id, call.raw_output, call.total_tokens, call.cost_micros)
        call_count = len(session.scalars(select(AgentCall).where(AgentCall.run_id == run.id)).all())
        resume_run(session, run.project_id, run.id, run.owner_id, str(uuid.uuid4()), run.project_revision)
        assert run.status == "queued" and call.status == "ready" and call.usage_json["resolvedReadFailure"]["code"] == "AGENT_ARTIFACT_SOURCE_CHANGED"
        run_id, call_id = run.id, call.id
    _apply_native_call(Settings(data_dir=directory, worker_enabled=False), factory, run_id, call_id)
    with factory() as session:
        call = session.get(AgentCall, call_id)
        assert call.usage_json["toolExecutionDone"] is True
        assert (call.remote_request_id, call.raw_output, call.total_tokens, call.cost_micros) == original_request
        assert len(session.scalars(select(AgentCall).where(AgentCall.run_id == run_id)).all()) == call_count


@pytest.mark.parametrize("unknown_database", ["scope-rejection-state.sqlite3"], indirect=True)
def test_actual_out_of_scope_read_is_rejected_and_recovers_within_the_same_authorization(unknown_database):
    engine, _ = unknown_database
    with Session(engine) as session:
        run = session.get(AgentRun, "eafe14be-35fd-4bf6-97c3-613133121dcd")
        call = session.get(AgentCall, "eabd9c96-a119-4d01-88f3-59f6e2e4506a")
        assert run_view(session, run)["canResume"] is True
        raw, request_id = call.raw_output, call.remote_request_id
        resume_run(session, run.project_id, run.id, run.owner_id, str(uuid.uuid4()), run.project_revision)
        assert run.status == "queued" and call.status == "tool_rejected"
        assert call.raw_output == raw and call.remote_request_id == request_id
        assert call.usage_json["toolError"]["businessExecuted"] is False
        assert run.limits_json["materialContext"]["readableNodeIds"] == []
        step = session.get(AgentStep, call.step_id)
        messages = _native_messages(run, step, step.role, {}, [call], [])
        assert '旧计划中超出该范围的历史对象及衔接要求已经取消' in messages[0]["content"]
        assert messages[-1]["tool_call_id"] == call.usage_json["toolCallId"]
        assert json.loads(messages[-1]["content"])["error"]["code"] == "AGENT_TARGET_NOT_ALLOWED"
        error = AppError(403, "AGENT_TARGET_NOT_ALLOWED", "该资料不在本轮范围")
        assert _safe_native_rejection(error, "retrieve_context")
        assert not _safe_native_rejection(error, "write_artifact")


@pytest.mark.parametrize("unknown_database", ["request-limit-state.sqlite3"], indirect=True)
def test_confirmed_extension_of_actual_task_keeps_cost_limit_reservations_and_previous_calls(unknown_database):
    engine, directory = unknown_database
    with Session(engine) as session:
        run = session.get(AgentRun, "eafe14be-35fd-4bf6-97c3-613133121dcd")
        user = session.get(User, run.owner_id)
        assert run.status == "failed" and run.error_code == "AGENT_STEP_LIMIT"
        assert len(session.scalars(select(AgentCall).where(AgentCall.run_id == run.id)).all()) == 40
        old_cost_limit, old_reserve = run.limits_json["maxCostMicros"], session.get(Budget, "global").text_reserved_micros
        body = RunLimitExtension(operation_id=str(uuid.uuid4()), expected_revision=run.project_revision, confirmed=True, max_steps=60)
        settings = Settings(data_dir=directory, text_admin_email=user.email, worker_enabled=False)
        extend_run_limit(session, run.project_id, run.id, user, body, settings)
        extend_run_limit(session, run.project_id, run.id, user, body, settings)
        assert run.status == "queued" and run.limits_json["maxSteps"] == 60
        assert len(run.limits_json["requestLimitExtensions"]) == 1
        assert run.limits_json["maxCostMicros"] == old_cost_limit == 10000000
        assert session.get(Budget, "global").text_reserved_micros == old_reserve
        assert len(session.scalars(select(AgentCall).where(AgentCall.run_id == run.id)).all()) == 40
        assert AgentRunView.model_validate(run_view(session, run)).limits.max_steps == 60
        session.commit()
        with pytest.raises(AppError) as duplicate:
            extend_run_limit(session, run.project_id, run.id, user, body.model_copy(update={"operation_id": str(uuid.uuid4())}), settings)
        assert duplicate.value.code == "AGENT_LIMIT_EXTENSION_UNAVAILABLE"
