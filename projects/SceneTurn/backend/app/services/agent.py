import asyncio
import copy
import json
import time
import uuid

from pydantic import ValidationError
from jsonpointer import JsonPointer, JsonPointerException
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.agent_models import AgentCall, AgentRun, AgentStep
from app.agent_schemas import AgentDecision, MaterialPlanningDecision, PlanningDecision, ReportedCodexUsage, RunCreate, RunRetryUnknown, RunLimitExtension, StepDecision
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.security import decode_secret
from app.creative_artifacts import ARTIFACT_CONTRACT_VERSION, SUPPORTED_ARTIFACT_CONTRACT_VERSIONS, artifact_model
from app.models import Budget, BudgetReservation, Canvas, Generation, Project, TextProviderSettings, User, new_id
from app.services.agent_provider import DEFAULT_AGENT_PROTOCOL, JSON_AGENT_PROTOCOL, MATERIAL_JSON_AGENT_PROTOCOL, MATERIAL_NATIVE_AGENT_PROTOCOL, THINKING_JSON_PROTOCOLS, SUPPORTED_JSON_PROTOCOLS, SUPPORTED_NATIVE_PROTOCOLS, SUPPORTED_PLATFORM_PROTOCOLS, agent_business_tools, agent_request_body, agent_response_schema, agent_tools, complete_agent, decode_platform_message, decode_structured_scene_closure, is_json_protocol, json_syntax_details, provider_endpoint
from app.services.context import retrieve_context, source_scope
from app.services.connections import require_image_connection
from app.services.projects import get_operation, iso_timestamp, lock_write, payload_digest, require_project, require_revision, require_scene, save_operation
from app.services.prompts.agent import PROMPT_VERSION, agent_prompt, codex_prompt, json_agent_prompt, material_json_agent_prompt, material_planning_prompt, material_scene_artifact_prompt, native_agent_prompt
from app.services.text_generation import _reserve, require_text_admin
from app.services.text_provider import MODEL, TextProviderFailure, complete_text, cost_micros, current_price, model_aliases, peak_price
from app.services.agent_provider import MATERIAL_STRUCTURED_RECOVERY_ROLES, native_batch_ids
from app.services.prompts.agent import material_structured_artifact_prompt
from app.services.providers import connection as model_connection, freeze_selection, frozen_key, profile as model_profile, project_defaults
from app.services.qwen import MODEL as QWEN_MODEL, complete_qwen, endpoint as qwen_endpoint, request_body as qwen_request_body
from app.tools.project_tools import LEGACY_TOOL_CATALOG_VERSION, TOOL_CATALOG_VERSION, WORKSPACE_TOOL_CATALOG_VERSION, MATERIAL_TOOL_CATALOG_VERSION, MATERIAL_ROLES, PROJECT_ROLE_TOOLS, SCENE_ROLES, READ_TOOLS, ROLE_TOOLS, _read_artifact, check_codex_connection, execute_tool, require_canvas, role_catalog, tool_catalog, validate_artifacts
from app.tools.project_tools import MATERIAL_SOP_TOOL_CATALOG_VERSION, MATERIAL_CATALOG_VERSIONS


TERMINAL = {"completed", "failed", "stopped", "submission_unknown"}


def _json_repair_info(call: AgentCall) -> dict | None:
    if call.input_json.get("protocol") in SUPPORTED_PLATFORM_PROTOCOLS or call.status != "failed" or call.provider != "deepseek" or call.input_json.get("jsonRepairAttempt", 0) != 0 or not call.usage_json.get("settled") or call.cost_micros is None or not call.remote_request_id:
        return None
    schema = PlanningDecision if call.input_json["planning"] else StepDecision
    try:
        schema.model_validate_json(call.raw_output)
    except ValidationError as error:
        errors = error.errors()
    else:
        return None
    if not errors or any(item["type"] != "extra_forbidden" for item in errors):
        return None
    try:
        original = json.loads(call.raw_output)
    except ValueError:
        return None
    corrected = copy.deepcopy(original)
    for item in errors:
        location = item["loc"]
        container = corrected
        for part in location[:-1]:
            container = container[part]
        if not isinstance(container, dict) or not isinstance(location[-1], str):
            return None
        del container[location[-1]]
    expected = schema.model_validate(corrected).model_dump(by_alias=True)
    return {"repairOf": call.id, "jsonRepairAttempt": 1,
            "repairProjectRevision": call.input_json["projectRevision"], "repairExpectedSha256": payload_digest(expected),
            "extraFieldPaths": [list(item["loc"]) for item in errors]}


def _checked_json_repair(session: Session, run: AgentRun, call: AgentCall, current_revision: int) -> dict | None:
    info = _json_repair_info(call)
    if info is None:
        return None
    reservation = session.get(BudgetReservation, call.id)
    if reservation is None or reservation.status != "settled" or reservation.reserved_micros != 0 or reservation.cost_micros != call.cost_micros:
        return None
    if call.run_id != run.id or current_revision != info["repairProjectRevision"] or run.project_revision != info["repairProjectRevision"]:
        raise AppError(409, "AGENT_JSON_REPAIR_STALE", "原模型格式修复绑定的项目版本已有变化，旧候选不会用于当前版本")
    return info


def require_run(session: Session, project_id: str, run_id: str, owner_id: str) -> AgentRun:
    require_project(session, project_id, owner_id)
    run = session.scalar(select(AgentRun).where(AgentRun.id == run_id, AgentRun.project_id == project_id, AgentRun.owner_id == owner_id))
    if run is None:
        raise AppError(404, "AGENT_RUN_NOT_FOUND", "角色任务不存在或无权访问")
    return run


def _redact_private_value(value, secret: str):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, list):
        return [_redact_private_value(item, secret) for item in value]
    if isinstance(value, dict):
        return {_redact_private_value(key, secret): _redact_private_value(item, secret) for key, item in value.items()}
    return value


def _record_provider_failure(call: AgentCall, error: TextProviderFailure, api_key: str) -> None:
    details = {}
    cause, causes = error.__cause__, []
    while cause is not None and len(causes) < 4:
        causes.append(type(cause).__name__)
        cause = cause.__cause__
    if causes:
        details["failureCauses"] = causes
    if error.http_status is not None:
        details["httpStatus"] = error.http_status
    if error.provider_error is not None:
        details["providerError"] = _redact_private_value(error.provider_error, api_key)
    if details:
        call.usage_json = {**call.usage_json, **details}


def _public_call_usage(usage: dict) -> dict:
    return {key: value for key, value in usage.items() if key not in {"httpStatus", "providerError", "reasoning_content", "failureCauses"}}


def _public_run_result(value: dict | None) -> dict | None:
    if not isinstance(value, dict):
        return value
    candidate = value.get("candidate")
    if not isinstance(candidate, dict) or not isinstance(candidate.get("providerMessage"), dict):
        return value
    message = {key: item for key, item in candidate["providerMessage"].items() if key in {"role", "content", "tool_calls"}}
    return {**value, "candidate": {**candidate, "providerMessage": message}}


def _unknown_retry_authorized(session: Session, run: AgentRun, call: AgentCall) -> bool:
    confirmation = next((item for item in run.limits_json.get("unknownRetryConfirmations", []) if item["callId"] == call.id), None)
    if confirmation is None:
        return False
    payload = confirmation.get("payload") or {"runId": run.id, "operationId": confirmation["operationId"], "expectedRevision": confirmation["projectRevision"],
        "callId": call.id, "confirmed": True, "maxSteps": confirmation["maxSteps"]}
    return get_operation(session, run.owner_id, run.project_id, confirmation["operationId"], "agent.retry_unknown", payload) is not None


def _retryable_unknown_call(session: Session, run: AgentRun, revision: int) -> AgentCall | None:
    if run.status != "submission_unknown" or run.error_code not in {"TEXT_SUBMISSION_UNKNOWN", "AGENT_SUBMISSION_UNKNOWN"} or run.executor != "platform" or run.limits_json.get("toolCatalogVersion") not in MATERIAL_CATALOG_VERSIONS:
        return None
    call = session.scalar(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at.desc()).limit(1))
    if call is None or call.status != "submission_unknown" or call.raw_output is not None or call.response_json is not None or call.total_tokens is not None or call.input_json["projectRevision"] > revision:
        return None
    reservation = session.get(BudgetReservation, call.id)
    if reservation is None or reservation.status != "reserved" or reservation.reserved_micros <= 0 or _unknown_retry_authorized(session, run, call):
        return None
    return call


def _recoverable_scope_read(session: Session, run: AgentRun, revision: int) -> AgentCall | None:
    if run.status != "failed" or run.error_code != "AGENT_TARGET_NOT_ALLOWED" or run.project_revision != revision or run.limits_json.get("materialContext", {}).get("scope") != "current_round":
        return None
    call = session.scalar(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at.desc()).limit(1))
    if call is None or call.status != "failed" or (call.response_json or {}).get("tool") not in {"read_artifact", "retrieve_context"} or call.usage_json.get("toolFailure", {}).get("code") != "AGENT_TARGET_NOT_ALLOWED" or not call.usage_json.get("settled") or call.usage_json.get("toolExecutionDone"):
        return None
    return call


def run_view(session: Session, run: AgentRun, settings: Settings | None = None) -> dict:
    planned_steps = session.execute(select(AgentRun.plan_json, AgentStep).outerjoin(
        AgentStep, AgentStep.run_id == AgentRun.id).where(AgentRun.id == run.id).order_by(AgentStep.index)).all()
    plan = planned_steps[0][0]
    steps = [step for _, step in planned_steps if step is not None]
    calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
    reservations = session.scalars(select(BudgetReservation).where(BudgetReservation.id.in_([call.id for call in calls]))).all() if calls else []
    project = require_project(session, run.project_id, run.owner_id)
    retryable = _retryable_unknown_call(session, run, project.revision)
    can_resume = (_recoverable_native_batch(session, run, project.revision) is not None
        or _recoverable_plain_read(session, run, project) is not None
        or _recoverable_native_format(session, run, project.revision) is not None
        or _recoverable_scope_read(session, run, project.revision) is not None
        or _recoverable_material_revision(session, run, project.revision, settings) is not None
        or _recoverable_image_prompt_selection(session, run, project.revision) is not None
        or _recoverable_material_json_format(session, run, project.revision) is not None
        or _recoverable_structured_scene_closure(session, run, project.revision, calls) is not None)
    return render_run_view(run, plan, steps, calls, reservations, project.revision, can_resume=can_resume,
        can_extend_limit=_material_limit_extension_available(session, run, project.revision, calls),
        retryable_unknown_call_id=retryable.id if retryable is not None else None)


def render_run_view(run, plan, steps, calls, reservations, revision, *, can_resume=False, can_extend_limit=False,
                    retryable_unknown_call_id=None) -> dict:
    unknown = (run.executor == "codex" and not calls) or any(call.total_tokens is None for call in calls)
    cost_unknown = run.executor == "codex" or any(call.cost_micros is None for call in calls)
    return {
        "id": run.id, "projectId": run.project_id, "sceneId": run.scene_id, "branchId": run.branch_id,
        "controllerNodeId": run.controller_node_id, "role": run.role, "executor": run.executor, "goal": run.goal,
        "status": run.status, "projectRevision": run.project_revision, "initialRevision": run.initial_revision,
        "currentRevision": revision, "targetIds": run.target_ids_json, "createdNodeIds": run.created_ids_json,
        "limits": {key: run.limits_json[key] for key in ("maxSteps", "maxTokens", "maxCostMicros")},
        "deliveryMode": run.limits_json.get("deliveryMode"), "artifactContractVersion": run.limits_json.get("artifactContractVersion"),
        "workspace": run.limits_json.get("workspace"),
        "toolCatalogVersion": run.limits_json.get("toolCatalogVersion", LEGACY_TOOL_CATALOG_VERSION),
        "plan": plan, "result": _public_run_result(run.result_json),
        "stopRequested": bool(run.stop_requested), "threadId": run.thread_id, "turnId": run.turn_id,
        "error": {"code": run.error_code, "message": run.error_message} if run.error_code else None,
        "canResume": can_resume, "canExtendLimit": can_extend_limit, "retryableUnknownCallId": retryable_unknown_call_id,
        "steps": [{"id": step.id, "index": step.index, "role": step.role, "title": step.title,
                   "instruction": step.instruction, "kind": plan[step.index].get("kind", "produce"),
                   "dependsOn": plan[step.index].get("dependsOn", []), "status": step.status, "result": step.result_json,
                   "toolResults": step.tool_results_json} for step in steps],
        "calls": [{"id": call.id, "stepId": call.step_id, "role": call.role, "provider": call.provider,
                   "model": call.model, "status": call.status, "remoteRequestId": call.remote_request_id,
                   "response": call.response_json, "inputTokens": call.input_tokens, "outputTokens": call.output_tokens,
                   "totalTokens": call.total_tokens, "costMicros": call.cost_micros, "usage": _public_call_usage(call.usage_json),
                   "createdAt": iso_timestamp(call.created_at), "finishedAt": iso_timestamp(call.finished_at) if call.finished_at else None} for call in calls],
        "usage": {"inputTokens": None if unknown else sum(call.input_tokens or 0 for call in calls),
                  "outputTokens": None if unknown else sum(call.output_tokens or 0 for call in calls),
                  "totalTokens": None if unknown else sum(call.total_tokens or 0 for call in calls),
                  "costMicros": None if cost_unknown else sum(call.cost_micros or 0 for call in calls),
                  "knownCostMicros": sum(call.cost_micros or 0 for call in calls) if run.executor == "platform" else None,
                  "reservedCostMicros": sum(reservation.reserved_micros for reservation in reservations),
                  "billingPolicyConfigured": False, "costSource": "provider_estimate" if run.executor == "platform" else "personal_account_unknown"},
        "createdAt": iso_timestamp(run.created_at), "updatedAt": iso_timestamp(run.updated_at),
        "startedAt": iso_timestamp(run.started_at) if run.started_at else None,
        "finishedAt": iso_timestamp(run.finished_at) if run.finished_at else None,
    }


def _new_run_limits(body: RunCreate) -> dict:
    limits = {**body.limits.model_dump(by_alias=True), "deliveryMode": body.delivery_mode, "artifactContractVersion": ARTIFACT_CONTRACT_VERSION,
              "toolCatalogVersion": TOOL_CATALOG_VERSION}
    if body.workspace:
        limits["workspace"] = body.workspace
    if body.workspace == "project":
        limits["toolCatalogVersion"] = WORKSPACE_TOOL_CATALOG_VERSION
    if body.workspace == "materials":
        from app.services.materials import MATERIAL_INSTRUCTIONS
        limits["toolCatalogVersion"] = MATERIAL_SOP_TOOL_CATALOG_VERSION
        limits["materialContext"] = {"requireHumanReview": True, "scope": "current_round", "instructions": MATERIAL_INSTRUCTIONS}
    if body.executor == "platform":
        limits.update(providerProtocol=MATERIAL_JSON_AGENT_PROTOCOL if body.workspace == "materials" else DEFAULT_AGENT_PROTOCOL, model=MODEL)
    return limits


def create_run(session: Session, project_id: str, user: User, body: RunCreate, settings: Settings, *, commit: bool = True) -> AgentRun:
    if commit:
        lock_write(session)
    project = require_project(session, project_id, user.id)
    payload = body.model_dump(by_alias=True)
    if body.model_profile_id is None:
        payload.pop("modelProfileId")
    if body.model_selection is None:
        payload.pop("modelSelection")
    replay = get_operation(session, user.id, project_id, body.operation_id, "agent.create", payload)
    if replay is not None:
        return require_run(session, project_id, replay["runId"], user.id)
    require_revision(project, body.expected_revision)
    if body.delivery_mode == "text_edit" and "write_script" not in ROLE_TOOLS[body.role]:
        raise AppError(422, "AGENT_DELIVERY_ROLE_UNSUPPORTED", "当前岗位没有正文局部修订工具，请选择对应交付类型")
    scene = require_scene(project, body.scene_id)
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project_id, Canvas.scene_id == body.scene_id, Canvas.owner_id == user.id))
    if canvas is None:
        raise AppError(404, "CANVAS_NOT_FOUND", "当前剧情画布不存在")
    nodes = {node["id"]: node for node in canvas.data_json["nodes"]}
    clips = {clip["id"] for track in scene["data"].get("timeline", {}).get("tracks", []) for clip in track["clips"]}
    if body.workspace == "project":
        if body.role not in PROJECT_ROLE_TOOLS or body.delivery_mode != "tool_action" or body.branch_id:
            raise AppError(422, "AGENT_WORKSPACE_ROLE_INVALID", "总画布任务需要对应岗位、实际工具操作和明确场景范围")
        scene_ids = {node["id"] for node in project.data_json["graphData"]["nodes"] if node["type"] == "scene"}
        if not body.target_ids or any(target not in scene_ids for target in body.target_ids):
            raise AppError(422, "AGENT_SCENE_SCOPE_REQUIRED", "请选择本次允许修改的真实场景")
    elif body.workspace == "materials" and (body.role not in MATERIAL_ROLES or body.delivery_mode != "professional" or body.executor != "platform"):
        raise AppError(422, "MATERIAL_ROLE_INVALID", "素材创作需要使用素材岗位与平台专业生成")
    elif body.workspace == "scene" and body.role not in SCENE_ROLES:
        raise AppError(422, "AGENT_WORKSPACE_ROLE_INVALID", "当前岗位通过故事总画布执行")
    elif any(target not in nodes and target not in clips for target in body.target_ids):
        raise AppError(422, "AGENT_TARGET_NOT_FOUND", "授权目标必须属于当前场景")
    if any(nodes[target]["type"] == "agent" for target in body.target_ids if target in nodes):
        raise AppError(403, "AGENT_CONTROL_NODE_PROTECTED", "Agent控制节点不能作为角色写入目标")
    if body.delivery_mode == "text_edit" and not any(nodes[target]["type"] == "text" for target in body.target_ids if target in nodes):
        raise AppError(422, "AGENT_TEXT_EDIT_TARGET_REQUIRED", "正文局部修订需要至少授权一个已有文字节点")
    if body.controller_node_id is not None and (body.controller_node_id not in nodes or nodes[body.controller_node_id]["type"] != "agent"):
        raise AppError(422, "AGENT_CONTROLLER_INVALID", "任务控制节点必须是当前画布中的Agent节点")
    if body.controller_node_id and body.workspace and nodes[body.controller_node_id].get("metadata", {}).get("agentWorkspace", "scene") != body.workspace:
        raise AppError(422, "AGENT_CONTROLLER_WORKSPACE_INVALID", "控制节点与任务所属画布不一致")
    source_scope(session, project_id, user.id, body.scene_id, body.branch_id)
    selected_models = {**project_defaults(project), **(body.model_selection.model_dump(exclude_none=True) if body.model_selection else {})}
    if body.model_profile_id is not None:
        selected_models["text"] = body.model_profile_id
    for kind, identifier in selected_models.items():
        model_profile(identifier, kind)
    if body.workspace == "materials":
        if selected_models["image"] == "codex-image":
            require_image_connection(session, user.id, project.id)
        if not model_profile(selected_models["video"], "video")["capabilities"]["lastFrame"]:
            raise AppError(422, "MODEL_LAST_FRAME_UNSUPPORTED", "请选择支持固定首尾帧的素材视频模型")
    text_selection = None
    if body.executor == "platform":
        require_text_admin(user, settings)
        text_selection = freeze_selection(session, user.id, project, "text", selected_models["text"])
        provider = session.get(TextProviderSettings, "platform") if text_selection["providerId"] == "deepseek" else model_connection(session, user.id, "aliyun-qwen")
        if provider is None or not provider.api_key_encrypted:
            raise AppError(503, "TEXT_NOT_CONFIGURED", "平台管理员尚未配置DeepSeek API Key")
    if body.limits.max_tokens > settings.text_max_output_tokens or body.limits.max_cost_micros > settings.max_generation_cents * 10000:
        raise AppError(422, "AGENT_LIMIT_EXCEEDED", "任务限制超过平台允许范围")
    limits = {**_new_run_limits(body), "modelSelection": selected_models}
    if text_selection and text_selection["providerId"] == "aliyun-qwen":
        limits.update(model=text_selection["model"], textProviderSnapshot=text_selection)
    run = AgentRun(owner_id=user.id, project_id=project_id, scene_id=body.scene_id, branch_id=body.branch_id,
                   controller_node_id=body.controller_node_id, operation_id=body.operation_id, role=body.role,
                   executor=body.executor, goal=body.goal, target_ids_json=body.target_ids, created_ids_json=[],
                   limits_json=limits, initial_revision=project.revision,
                   project_revision=project.revision, plan_json=[], status="queued" if body.executor == "platform" else "waiting_connection")
    session.add(run)
    session.flush()
    save_operation(session, user.id, project_id, body.operation_id, "agent.create", payload, {"runId": run.id})
    if commit:
        session.commit()
    return run


def stop_run(session: Session, project_id: str, run_id: str, owner_id: str, operation_id: str) -> AgentRun:
    lock_write(session)
    run = require_run(session, project_id, run_id, owner_id)
    payload = {"runId": run_id}
    replay = get_operation(session, owner_id, project_id, operation_id, "agent.stop", payload)
    if replay is not None:
        return run
    run.stop_requested, run.updated_at = 1, time.time()
    if run.limits_json.get("materialContext", {}).get("requireHumanReview"):
        from app.image_models import ImageAccount, ImageJob
        from app.services.codex_images import stop_material_images

        stop_material_images(session, run)
        for job in session.scalars(select(ImageJob).where(ImageJob.run_id == run.id, ImageJob.status.in_(["queued", "awaiting_confirmation", "claimed"]))):
            if job.status == "claimed":
                account = session.get(ImageAccount, job.account_id)
                if account.active_job_id != job.id:
                    raise AppError(409, "IMAGE_ACCOUNT_CONFLICT", "图片账号占用状态已经变化")
                account.active_job_id = None
            job.status, job.updated_at = "cancelled", time.time()
    if run.status not in {"completed", "failed", "submission_unknown"}:
        run.status = "stopping" if run.status == "running" else "stopped"
        _mark_step_state(session, run, run.status)
        if run.status == "stopped":
            run.finished_at = time.time()
            for step in session.scalars(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.status != "completed")):
                step.status = "stopped"
    save_operation(session, owner_id, project_id, operation_id, "agent.stop", payload, {"runId": run_id})
    session.commit()
    return run


def resume_run(session: Session, project_id: str, run_id: str, owner_id: str, operation_id: str, expected_revision: int,
               settings: Settings | None = None, *, commit: bool = True) -> AgentRun:
    if commit:
        lock_write(session)
    run = require_run(session, project_id, run_id, owner_id)
    payload = {"runId": run_id, "expectedRevision": expected_revision}
    replay = get_operation(session, owner_id, project_id, operation_id, "agent.resume", payload)
    if replay is not None:
        return run
    project = require_project(session, project_id, owner_id)
    require_revision(project, expected_revision)
    if run.status == "submission_unknown":
        raise AppError(409, "AGENT_SUBMISSION_UNKNOWN", "原模型请求结果待核实，保留费用预留且不会再次提交")
    if ((run.result_json or {}).get("materialReview", {}).get("status") in {"pending", "generating", "review_images"}
            or (run.result_json or {}).get("materialClarification", {}).get("status") == "pending"):
        raise AppError(409, "MATERIAL_CONFIRMATION_REQUIRED", "请先在素材对话中回答待确认事项")
    if run.executor == "platform" and any(call.status == "submitting" or not _unknown_retry_authorized(session, run, call) for call in session.scalars(select(AgentCall).where(AgentCall.run_id == run.id, AgentCall.status.in_(["submitting", "submission_unknown"])))):
        raise AppError(409, "AGENT_SUBMISSION_UNKNOWN", "原模型请求仍待核实，当前任务不能提交新的模型调用")
    if run.status in {"running", "stopping"}:
        raise AppError(409, "AGENT_RUN_ACTIVE", "当前回合尚未终止，请等待真实状态")
    batch_recovery = _recoverable_native_batch(session, run, project.revision)
    plain_recovery = _recoverable_plain_read(session, run, project)
    format_recovery = _recoverable_native_format(session, run, project.revision)
    scope_recovery = _recoverable_scope_read(session, run, project.revision)
    material_recovery = _recoverable_material_revision(session, run, project.revision, settings)
    image_selection_recovery = _recoverable_image_prompt_selection(session, run, project.revision)
    material_json_recovery = _recoverable_material_json_format(session, run, project.revision)
    structured_scene_recovery = None
    if run.error_code == "AGENT_NATIVE_CORRECTION_LIMIT" and run.limits_json.get("providerProtocol") == MATERIAL_JSON_AGENT_PROTOCOL:
        ordered_calls = list(session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)))
        structured_scene_recovery = _recoverable_structured_scene_closure(session, run, project.revision, ordered_calls)
    if batch_recovery is not None:
        _reject_native_call(session, run, batch_recovery, AppError(422, "AGENT_NATIVE_BATCH_NOT_EXECUTED", "本轮多个操作均未执行，请每次只调用一个函数并等待结果"))
    elif plain_recovery is not None:
        previous_error = plain_recovery.usage_json["toolError"]
        plain_recovery.usage_json = {**{key: value for key, value in plain_recovery.usage_json.items() if key != "toolError"}, "resolvedReadError": previous_error}
        plain_recovery.status = "ready"
        run.result_json = {key: value for key, value in (run.result_json or {}).items() if key != "nativeCorrection"}
    elif format_recovery is not None:
        result = {key: value for key, value in (run.result_json or {}).items() if key not in {"nativeCorrection", "toolError"}}
        if run.limits_json.get("toolCatalogVersion") == MATERIAL_SOP_TOOL_CATALOG_VERSION:
            run.limits_json = {**run.limits_json, "providerProtocol": MATERIAL_JSON_AGENT_PROTOCOL}
            run.result_json = {**result, "materialProtocolUpgrade": {"sourceProtocol": MATERIAL_NATIVE_AGENT_PROTOCOL,
                "targetProtocol": MATERIAL_JSON_AGENT_PROTOCOL, "rejectedCallId": format_recovery.id, "operationId": operation_id}}
        else:
            run.result_json = {**result, "manualNativeRepairOf": format_recovery.id, "manualRepairOperationId": operation_id}
    elif structured_scene_recovery is not None:
        usage = structured_scene_recovery.usage_json
        structured_scene_recovery.usage_json = {**{key: value for key, value in usage.items() if key != "toolError"},
            "resolvedStructuredSceneSyntax": {**usage["toolError"], "operationId": operation_id}}
        structured_scene_recovery.status = "response_received"
        run.result_json = {key: value for key, value in (run.result_json or {}).items() if key not in {"nativeCorrection", "toolError"}}
    elif material_json_recovery is not None:
        _record_material_json_format_retry(run, material_json_recovery, operation_id)
    elif scope_recovery is not None:
        previous = scope_recovery.usage_json["toolFailure"]
        scope_recovery.usage_json = {**{key: value for key, value in scope_recovery.usage_json.items() if key != "toolFailure"}, "resolvedScopeFailure": previous}
        _reject_native_call(session, run, scope_recovery, AppError(403, "AGENT_TARGET_NOT_ALLOWED", "该资料未被本轮明确引用，请使用本轮已保存产物继续完成岗位交付"))
    elif material_recovery is not None:
        revision = _material_revision_request(run, material_recovery.step_id)
        context = run.limits_json["materialContext"]
        run.limits_json = {**run.limits_json, "materialContext": {**context, "revisionRequest": {
            key: revision[key] for key in ("reviewId", "stepId", "nodeId", "artifactHash", "operationId", "requestedAt", "answers", "requestedChanges")}}}
        session.get(AgentStep, material_recovery.step_id).result_json = None
        if run.limits_json.get("providerProtocol") == MATERIAL_JSON_AGENT_PROTOCOL:
            run.result_json = {**run.result_json, "materialRevisionResume": {"reviewId": revision["reviewId"],
                "revisionOperationId": revision["operationId"], "operationId": operation_id, "rejectedCallId": material_recovery.id}}
        elif material_recovery.usage_json.get("toolError", {}).get("code") == "AGENT_ARTIFACT_PATCH_INVALID":
            run.result_json = {**run.result_json, "nativeCorrection": {"callId": material_recovery.id, "tool": "patch_artifact"},
                "materialRevisionRecovery": {"callId": material_recovery.id, "operationId": operation_id,
                    "reason": "semantic_correction_labels", "businessReplayed": False}}
        else:
            _reject_native_call(session, run, material_recovery, _material_revision_error())
    elif image_selection_recovery is not None:
        previous = image_selection_recovery.usage_json["toolFailure"]
        image_selection_recovery.usage_json = {key: value for key, value in image_selection_recovery.usage_json.items() if key != "toolFailure"}
        image_selection_recovery.usage_json = {**image_selection_recovery.usage_json, "resolvedImagePromptSelection": previous}
        _reject_native_call(session, run, image_selection_recovery, AppError(422, "AGENT_IMAGE_PROMPT_SELECTION_INVALID",
            "保留原方案中用户选定的图片。对其余图片使用RFC6902的remove操作和原条目的下标路径，不需要重述任何图片内容。仅提交所需的remove操作。"))
    elif run.status == "conflict" and run.error_code == "AGENT_ARTIFACT_SOURCE_CHANGED" and run.limits_json.get("toolCatalogVersion") in MATERIAL_CATALOG_VERSIONS:
        last_call = session.scalar(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at.desc()).limit(1))
        if last_call is not None and last_call.status == "failed" and last_call.input_json["projectRevision"] == project.revision and (last_call.response_json or {}).get("tool") == "read_artifact" and last_call.usage_json.get("settled") and not last_call.usage_json.get("toolExecutionDone"):
            _read_artifact(session, run, project, require_canvas(session, run, project), last_call.response_json["arguments"]["targetId"], get_settings())
            last_call.usage_json = {**{key: value for key, value in last_call.usage_json.items() if key != "toolFailure"}, "resolvedReadFailure": last_call.usage_json.get("toolFailure")}
            last_call.status = "ready"
    elif run.status in {"failed", "completed"}:
        last_call = session.scalar(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at.desc()).limit(1)) if run.executor == "platform" and run.error_code == "AGENT_RESULT_INVALID" else None
        repair = _checked_json_repair(session, run, last_call, project.revision) if last_call is not None else None
        if repair is None:
            raise AppError(409, "AGENT_RUN_TERMINAL", "该任务已经结束，请保留原结果并发起新任务")
        run.result_json = {**(run.result_json or {}), "jsonRepairOf": last_call.id}
    pending_repair = (run.result_json or {}).get("jsonRepairOf")
    if run.executor == "platform" and pending_repair:
        pending_call = session.get(AgentCall, pending_repair)
        if pending_call is None or _checked_json_repair(session, run, pending_call, project.revision) is None:
            raise AppError(409, "AGENT_JSON_REPAIR_UNAVAILABLE", "原模型调用不满足一次格式修复条件")
    if run.executor == "codex" and run.status in {"stopped", "conflict"}:
        run.turn_id = None
        run.lease_owner, run.lease_expires_at = None, None
        run.result_json = {**(run.result_json or {}), "codexResumeOperationId": operation_id}
    if run.executor == "platform" and run.status == "conflict" and run.project_revision != project.revision:
        run.result_json = {key: value for key, value in (run.result_json or {}).items() if key != "nativeCorrection"}
    if run.status == "waiting_confirmation":
        steps = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id)).all()
        generation_ids = [item["result"]["generation"]["id"] for step in steps for item in step.tool_results_json if item.get("tool") == "submit_generation"]
        generations = session.scalars(select(Generation).where(Generation.id.in_(generation_ids))).all() if generation_ids else []
        if any(generation.status == "awaiting_confirmation" for generation in generations):
            raise AppError(409, "CONFIRMATION_REQUIRED", "媒体报价尚未由本人确认，任务继续等待")
        if run.executor == "codex" and run.turn_id:
            completed_turn = session.scalar(select(AgentCall.id).where(AgentCall.run_id == run.id, AgentCall.provider == "personal-codex", AgentCall.remote_request_id == run.turn_id, AgentCall.finished_at.is_not(None)))
            if completed_turn is None:
                raise AppError(409, "AGENT_RUN_ACTIVE", "当前本机回合尚未结束，报价确认已保留，请等待回合结束后恢复")
            run.turn_id = None
            run.lease_owner, run.lease_expires_at = None, None
            run.result_json = {**(run.result_json or {}), "codexResumeOperationId": operation_id}
    run.stop_requested, run.project_revision = 0, expected_revision
    run.status, run.error_code, run.error_message = ("queued" if run.executor == "platform" else "waiting_connection"), None, None
    run.finished_at, run.updated_at = None, time.time()
    for step in session.scalars(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.status.in_(["running", "conflict", "stopped", "waiting_confirmation", "failed"]))):
        step.status = "queued"
    save_operation(session, owner_id, project_id, operation_id, "agent.resume", payload, {"runId": run_id})
    if commit:
        session.commit()
    return run


def retry_unknown_run(session: Session, project_id: str, run_id: str, user: User, body: RunRetryUnknown, settings: Settings) -> AgentRun:
    lock_write(session)
    run = require_run(session, project_id, run_id, user.id)
    require_text_admin(user, settings)
    payload = {"runId": run_id, **body.model_dump(by_alias=True)}
    if get_operation(session, user.id, project_id, body.operation_id, "agent.retry_unknown", payload) is not None:
        return run
    project = require_project(session, project_id, user.id)
    require_revision(project, body.expected_revision)
    call = _retryable_unknown_call(session, run, project.revision)
    if call is None or call.id != body.call_id:
        raise AppError(409, "AGENT_UNKNOWN_RETRY_UNAVAILABLE", "当前请求不符合本人确认后的重试条件，请核对最新任务状态")
    count = session.scalar(select(func.count()).select_from(AgentCall).where(AgentCall.run_id == run.id))
    if body.max_steps < run.limits_json["maxSteps"] or count >= body.max_steps:
        raise AppError(422, "AGENT_STEP_LIMIT", "确认的请求上限必须保留已有授权且允许下一次请求")
    confirmation = {"callId": call.id, "operationId": body.operation_id, "projectRevision": project.revision,
        "originalProjectRevision": call.input_json["projectRevision"], "previousMaxSteps": run.limits_json["maxSteps"], "maxSteps": body.max_steps, "payload": payload}
    run.limits_json = {**run.limits_json, "maxSteps": body.max_steps,
        "unknownRetryConfirmations": [*run.limits_json.get("unknownRetryConfirmations", []), confirmation]}
    if body.current_round_only:
        from app.services.materials import MATERIAL_INSTRUCTIONS

        context = run.limits_json.get("materialContext", {})
        run.limits_json = {**run.limits_json, "materialContext": {**context, "scope": "current_round",
            "readableNodeIds": list(run.target_ids_json), "history": [], "historyFromRevision": project.revision, "instructions": MATERIAL_INSTRUCTIONS}}
    run.status, run.error_code, run.error_message = "queued", None, None
    run.stop_requested, run.finished_at, run.updated_at, run.project_revision = 0, None, time.time(), project.revision
    run.lease_owner, run.lease_expires_at = None, None
    for step in session.scalars(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.status == "submission_unknown")):
        step.status = "queued"
    save_operation(session, user.id, project_id, body.operation_id, "agent.retry_unknown", payload, {"runId": run.id})
    session.commit()
    return run


def extend_run_limit(session: Session, project_id: str, run_id: str, user: User, body: RunLimitExtension, settings: Settings) -> AgentRun:
    lock_write(session)
    run = require_run(session, project_id, run_id, user.id)
    require_text_admin(user, settings)
    payload = {"runId": run_id, **body.model_dump(by_alias=True)}
    if get_operation(session, user.id, project_id, body.operation_id, "agent.extend_limit", payload) is not None:
        return run
    project = require_project(session, project_id, user.id)
    require_revision(project, body.expected_revision)
    if run.project_revision != project.revision:
        raise AppError(409, "REVISION_CONFLICT", "项目已有编辑，请核对当前产物后继续")
    calls = list(session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)))
    if not _material_limit_extension_available(session, run, project.revision, calls):
        raise AppError(409, "AGENT_LIMIT_EXTENSION_UNAVAILABLE", "当前任务没有可增加请求次数的暂停记录")
    if body.max_steps <= max(len(calls), run.limits_json["maxSteps"]):
        raise AppError(422, "AGENT_LIMIT_EXTENSION_INVALID", "确认的请求上限必须高于已执行次数及原上限")
    if run.error_code == "AGENT_NATIVE_CORRECTION_LIMIT":
        _record_material_json_format_retry(run, calls[-1], body.operation_id, with_extension=True)
    extension = {"operationId": body.operation_id, "previousMaxSteps": run.limits_json["maxSteps"], "maxSteps": body.max_steps, "payload": payload}
    run.limits_json = {**run.limits_json, "maxSteps": body.max_steps,
        "requestLimitExtensions": [*run.limits_json.get("requestLimitExtensions", []), extension]}
    run.status, run.error_code, run.error_message = "queued", None, None
    run.stop_requested, run.finished_at, run.updated_at = 0, None, time.time()
    run.lease_owner, run.lease_expires_at = None, None
    _mark_step_state(session, run, "queued")
    save_operation(session, user.id, project_id, body.operation_id, "agent.extend_limit", payload, {"runId": run.id})
    session.commit()
    return run


def _settle_call(session: Session, call: AgentCall, actual: int, usage: dict) -> None:
    reservation = session.get(BudgetReservation, call.id)
    if reservation is None or reservation.status == "settled":
        raise AppError(409, "AGENT_BUDGET_INCONSISTENT", "模型调用费用预留状态无效")
    if actual < 0 or actual > reservation.reserved_micros:
        raise AppError(409, "AGENT_COST_OUT_OF_BOUNDS", "供应商用量超出预留上界，保留原预留等待核实")
    changed = session.execute(update(Budget).where(Budget.id == "global", Budget.text_reserved_micros >= reservation.reserved_micros).values(text_reserved_micros=Budget.text_reserved_micros - reservation.reserved_micros, text_spent_micros=Budget.text_spent_micros + actual))
    if changed.rowcount != 1:
        raise AppError(409, "AGENT_BUDGET_INCONSISTENT", "模型调用费用与主预算不一致")
    reservation.cost_micros, reservation.reserved_micros, reservation.status = actual, 0, "settled"
    reservation.updated_at = time.time()
    reservation.details_json = {**reservation.details_json, "usage": usage, "remoteRequestId": call.remote_request_id}
    call.cost_micros, call.usage_json = actual, {**usage, "settled": True, "costEstimate": True}


def _next_step(session: Session, run: AgentRun) -> AgentStep | None:
    return session.scalar(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.status != "completed").order_by(AgentStep.index).limit(1))


def _mark_step_state(session: Session, run: AgentRun, status: str) -> None:
    step = _next_step(session, run)
    if step is not None:
        step.status, step.updated_at = status, time.time()


def require_step_delivery(session: Session, run: AgentRun, step: AgentStep, project: Project, settings: Settings) -> dict | None:
    if run.limits_json.get("materialContext", {}).get("requireHumanReview") and run.plan_json[step.index].get("kind", "produce") != "verify":
        review = (run.result_json or {}).get("materialReview", {})
        if review.get("stepId") == step.id and review.get("status") == "revision_requested":
            raise _material_revision_error()
        if review.get("stepId") != step.id or review.get("status") not in ({"approved", "not_required"} if step.role == "media_producer" else {"approved"}):
            raise AppError(409, "MATERIAL_CONFIRMATION_REQUIRED", "当前岗位成果需要用户确认后才能交给后续岗位")
        if step.role == "media_producer":
            from app.services.material_review import require_video_delivery
            require_video_delivery(session, run, step, review)
    receipts = step.tool_results_json
    if run.limits_json.get("conversationId"):
        writes = [receipt for receipt in receipts if receipt["tool"] not in READ_TOOLS and receipt["result"].get("persisted")]
        return {"readOnly": not writes, "writtenOperationIds": [receipt["operationId"] for receipt in writes], "verifiedRevision": project.revision}
    if not receipts:
        raise AppError(422, "AGENT_NO_TOOL_RESULTS", "本步骤尚未执行真实项目工具，无法登记完成")
    kind = run.plan_json[step.index].get("kind", "produce")
    validator = "validate_story" if run.limits_json.get("workspace") == "project" else "validate_artifacts"
    verification = next((receipt["result"] for receipt in reversed(receipts) if receipt["tool"] == validator and receipt["result"].get("valid") is True and receipt["result"].get("verifiedRevision") == project.revision), None)
    if kind == "verify":
        if verification is None:
            raise AppError(422, "AGENT_VERIFICATION_MISSING", "验证步骤尚未取得当前版本的真实产物核对证据")
        return verification
    mode = run.limits_json.get("deliveryMode")
    if run.limits_json.get("workspace") == "project":
        required_tools = {"editor": {"edit_timeline"}, "interaction_designer": {"edit_interaction"}, "writer": {"apply_story_ops", "prepare_scene_media"}, "director": {"apply_story_ops", "edit_timeline", "edit_interaction", "prepare_scene_media"}}
        if not any(receipt["tool"] in required_tools[step.role] and receipt["result"].get("persisted") for receipt in receipts):
            raise AppError(422, "AGENT_PROJECT_EDIT_MISSING", "该岗位尚未保存实际场景、时间线或互动修改")
        return verification
    if mode == "professional":
        delivered = [receipt for receipt in receipts if receipt["tool"] in {"write_artifact", "patch_artifact"} and receipt["result"].get("artifact", {}).get("role") == step.role]
        if not delivered or run.limits_json.get("artifactContractVersion") not in SUPPORTED_ARTIFACT_CONTRACT_VERSIONS:
            raise AppError(422, "AGENT_PROFESSIONAL_ARTIFACT_MISSING", "当前岗位尚未按本任务专业结构保存完整产物")
        return validate_artifacts(session, run, step, project, require_canvas(session, run, project), settings)
    if mode == "text_edit":
        if not any(receipt["tool"] == "write_script" and receipt["arguments"].get("targetId") in run.target_ids_json for receipt in receipts):
            raise AppError(422, "AGENT_TEXT_EDIT_MISSING", "本次局部修订尚未保存授权已有正文")
    elif mode == "tool_action":
        if not any(receipt["tool"] not in READ_TOOLS or receipt["tool"] in {"validate_scene", "validate_artifacts"} for receipt in receipts):
            raise AppError(422, "AGENT_TOOL_ACTION_MISSING", "本次工具操作尚未执行实际写入或处理")
    elif step.role != "director" and not any(receipt["tool"] not in READ_TOOLS for receipt in receipts):
        raise AppError(422, "AGENT_DELIVERABLE_MISSING", "当前创作步骤尚未保存实际产物，候选及工具结果继续保留")
    return verification


def _context_targets(session: Session, run: AgentRun) -> list[str]:
    canvas = session.scalar(select(Canvas).where(Canvas.owner_id == run.owner_id, Canvas.project_id == run.project_id, Canvas.scene_id == run.scene_id))
    identifiers = {node["id"] for node in canvas.data_json["nodes"] if node["type"] != "agent"} if canvas else set()
    targets = [*run.target_ids_json, *run.created_ids_json] if run.limits_json.get("materialContext", {}).get("scope") == "current_round" else run.target_ids_json
    return list(dict.fromkeys(target for target in targets if target in identifiers))


def _tool_observation(receipt: dict) -> dict:
    result = receipt["result"]
    observation = {"operationId": receipt["operationId"], "tool": receipt["tool"], "revision": receipt["revision"], "status": "committed"}
    if "persisted" in result:
        observation.update(persisted=result["persisted"], operations=result.get("operations", []),
                           savedTexts=[{key: value for key, value in item.items() if key != "text"} for item in result.get("savedTexts", [])])
        if receipt["tool"] in {"write_artifact", "patch_artifact"}:
            observation.update(artifactContractVersion=result["artifactContractVersion"], artifactSha256=result["artifactSha256"],
                               sourceNodeId=result["sourceNodeId"], sourceRevision=result["sourceRevision"], promptCount=len(result["promptItems"]))
            if receipt["tool"] == "patch_artifact":
                observation.update(baseArtifactSha256=result["baseArtifactSha256"], changedPaths=result["changedPaths"])
    elif receipt["tool"] == "retrieve_context":
        observation.update(context=result)
    elif receipt["tool"] == "read_project":
        observation.update(project=result.get("project"), sceneId=result.get("scene", {}).get("id"),
            nodes=[{"id": node["id"], "title": node.get("title"), "type": node["type"],
                    "artifactRole": node["metadata"]["creativeArtifact"].get("role") if isinstance(node.get("metadata", {}).get("creativeArtifact"), dict) else None,
                    "artifactCurrent": node.get("artifactCurrent"), "assetId": node.get("metadata", {}).get("storageKey"),
                    "generationMode": node.get("metadata", {}).get("generationMode"), "dramaTask": node.get("metadata", {}).get("dramaTask")}
                   for node in result.get("canvas", {}).get("nodes", [])],
            connections=result.get("canvas", {}).get("connections", []), assets=result.get("assets", []),
            storyTopology=result.get("storyTopology"), capabilities=result.get("capabilities"))
    elif "generation" in result:
        generation = result["generation"]
        observation.update(generation={key: generation.get(key) for key in ("id", "status", "assetId", "remoteTaskId")}, confirmationRequired=result.get("confirmationRequired", False))
    else:
        observation["result"] = result
    return observation


def _role_messages(run: AgentRun, step: AgentStep | None, role: str, context: dict, previous_calls: list[AgentCall], completed_steps: list[AgentStep]) -> list[dict]:
    observations = [_tool_observation(receipt) for receipt in step.tool_results_json] if step else []
    mode = run.limits_json.get("deliveryMode", "legacy")
    instruction = {"goal": run.goal, "deliveryMode": mode, "artifactContractVersion": run.limits_json.get("artifactContractVersion"), "step": {"role": step.role, "title": step.title, "instruction": step.instruction,
                   "kind": run.plan_json[step.index].get("kind", "produce"), "dependsOn": run.plan_json[step.index].get("dependsOn", [])} if step else None,
                   "authorizedTargetIds": run.target_ids_json, "createdNodeIds": run.created_ids_json,
                   "projectRevision": run.project_revision, "context": context, "completedOperations": observations,
                   "remainingModelRequests": run.limits_json["maxSteps"] - len(previous_calls),
                   "previousSteps": [{"role": item.role, "result": item.result_json} for item in completed_steps]}
    messages = [{"role": "system", "content": agent_prompt(role, tool_catalog(role, run.limits_json.get("toolCatalogVersion", LEGACY_TOOL_CATALOG_VERSION)), not run.plan_json, mode)},
                {"role": "user", "content": json.dumps(instruction, ensure_ascii=False)}]
    if step is not None:
        receipts = {receipt["operationId"]: receipt for receipt in step.tool_results_json}
        for call in [item for item in previous_calls if item.step_id == step.id and item.response_json][-4:]:
            messages.append({"role": "assistant", "content": json.dumps(call.response_json, ensure_ascii=False)})
            receipt = receipts.get(f"agent-{call.id}")
            if receipt is not None:
                result = receipt["result"] if receipt["tool"] == "write_script" else _tool_observation(receipt)
                messages.append({"role": "user", "content": json.dumps({"toolResult": {"tool": receipt["tool"], "operationId": receipt["operationId"], "result": result},
                    "nextAction": "按实际成功回执继续尚未完成的步骤；已完成要求时返回action=complete。"}, ensure_ascii=False)})
            else:
                messages.append({"role": "user", "content": json.dumps({"toolExecution": "not_executed", "reason": "这条候选没有已保存的成功工具回执，不能视为已执行；依据最新版本决定下一步。"}, ensure_ascii=False)})
    return messages


def _repair_messages(messages: list[dict], call: AgentCall, info: dict) -> list[dict]:
    return [*messages, {"role": "assistant", "content": call.raw_output},
            {"role": "user", "content": json.dumps({"jsonRepair": {
                "originalCallId": call.id, "extraFieldPaths": info["extraFieldPaths"],
                "instruction": "本轮只修复上一条JSON格式：只删除列出的额外字段，其他所有合法字段和值必须逐字保持，尤其action、tool、arguments、plan和summary。禁止重新规划、修改正文、新增工具动作或再次执行已成功工具。只返回符合本阶段Schema的JSON对象。"}}, ensure_ascii=False)}]


def _material_revision_error() -> AppError:
    return AppError(422, "MATERIAL_REVISION_REQUIRED",
        "用户已要求修订当前岗位成果。请先read_artifact读取materialRevision.nodeId的完整当前结构，再根据materialRevision.answers和requestedChanges用patch_artifact保存实际修订，或用write_artifact创建完整的新候选。旧写入和validate_artifacts结果不能完成本次修订；新成果保存后必须等待用户再次确认。")


def _material_revision_request(run: AgentRun, step_id: str | None) -> dict | None:
    context = run.limits_json.get("materialContext", {})
    if not step_id or not context.get("requireHumanReview"):
        return None
    review = (run.result_json or {}).get("materialReview", {})
    request = context.get("revisionRequest")
    if not request or request.get("stepId") != step_id:
        if review.get("stepId") != step_id or review.get("status") != "revision_requested":
            return None
        request = {"reviewId": review["id"], "stepId": step_id, "nodeId": review["nodeId"], "artifactHash": review["artifactHash"],
            "operationId": review["operationId"], "requestedAt": review["answeredAt"], "answers": review["answers"],
            "requestedChanges": review["answerPayload"]["requestedChanges"]}
    return {**request, "currentReviewStatus": review.get("status") if review.get("stepId") == step_id else None,
        "currentReviewId": review.get("id") if review.get("stepId") == step_id else None,
        "currentArtifactHash": review.get("artifactHash") if review.get("stepId") == step_id else None,
        "requiresArtifactWrite": review.get("stepId") == step_id and review.get("status") == "revision_requested"}


def _call_in_material_revision(call: AgentCall, revision: dict | None) -> bool:
    if revision is None:
        return True
    operation = call.input_json.get("materialRevisionOperationId")
    return operation == revision["operationId"] if operation else call.created_at >= revision["requestedAt"]


def _recoverable_image_prompt_selection(session: Session, run: AgentRun, revision: int) -> AgentCall | None:
    if (run.status != "failed" or run.error_code != "AGENT_ARTIFACT_PATCH_FORBIDDEN" or run.project_revision != revision
            or run.limits_json.get("providerProtocol") != MATERIAL_JSON_AGENT_PROTOCOL or run.stop_requested):
        return None
    review = (run.result_json or {}).get("materialReview", {})
    step_id = review.get("stepId")
    request = _material_revision_request(run, step_id)
    if request is None or not request["requiresArtifactWrite"] or review.get("role") not in {"character_designer", "scene_designer"}:
        return None
    calls = list(session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)))
    if not calls or len(calls) >= run.limits_json["maxSteps"] or any("resolvedImagePromptSelection" in item.usage_json
            and _call_in_material_revision(item, request) for item in calls):
        return None
    call = calls[-1]
    decision = call.response_json or {}
    if (call.status != "failed" or call.step_id != step_id or call.role != review["role"] or not _call_in_material_revision(call, request)
            or call.input_json.get("protocol") != MATERIAL_JSON_AGENT_PROTOCOL or call.input_json.get("projectRevision") != revision
            or decision.get("tool") != "patch_artifact" or not call.remote_request_id or not call.usage_json.get("settled")
            or call.cost_micros is None or call.usage_json.get("toolExecutionDone")
            or call.usage_json.get("toolFailure", {}).get("code") != "AGENT_ARTIFACT_PATCH_FORBIDDEN"
            or _parameter_correction_count(run, step_id, calls) >= 2):
        return None
    arguments = decision["arguments"]
    if arguments["targetId"] != request["nodeId"] or arguments["baseArtifactSha256"] != request["artifactHash"] or arguments["expectedRevision"] != revision:
        return None
    step = session.get(AgentStep, step_id)
    if step is None or any(receipt["operationId"] == f"agent-{call.id}" for receipt in step.tool_results_json):
        return None
    selected_paths = {patch["path"] for patch in arguments["patches"] if patch["op"] == "replace" and isinstance(patch["value"], list)}
    has_read = any(receipt["tool"] == "read_artifact" and receipt["revision"] == revision
        and receipt["result"].get("baseArtifactSha256") == request["artifactHash"]
        and receipt["result"].get("targetId") == request["nodeId"] and receipt["result"].get("imagePromptSubsetReviewId") == request["reviewId"]
        and any(path.get("imagePromptSubset") is True and path["path"] in selected_paths for path in receipt["result"].get("patchablePaths", []))
        for receipt in step.tool_results_json)
    reservation = session.get(BudgetReservation, call.id)
    if not has_read or reservation is None or reservation.status != "settled" or reservation.reserved_micros != 0 or reservation.cost_micros != call.cost_micros:
        return None
    return call


def _recoverable_material_json_format(session: Session, run: AgentRun, revision: int) -> AgentCall | None:
    if (run.status != "failed" or run.error_code != "AGENT_NATIVE_CORRECTION_LIMIT" or run.project_revision != revision
            or run.limits_json.get("providerProtocol") != MATERIAL_JSON_AGENT_PROTOCOL or run.stop_requested):
        return None
    calls = list(session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)))
    if not calls or len(calls) >= run.limits_json["maxSteps"]:
        return None
    call = calls[-1]
    retry = (run.result_json or {}).get("materialJsonFormatRetries", {}).get(call.step_id)
    if retry:
        if call.role in MATERIAL_STRUCTURED_RECOVERY_ROLES:
            if retry.get("materialArtifactOnly") or call.input_json.get("materialArtifactOnly"):
                return None
        elif call.role != "scene_designer" or retry.get("sceneArtifactOnly") or call.input_json.get("sceneArtifactOnly"):
            return None
    return _settled_material_json_rejection(session, run, revision, calls)


def _settled_material_json_rejection(session: Session, run: AgentRun, revision: int, calls: list[AgentCall]) -> AgentCall | None:
    if not calls or run.limits_json.get("providerProtocol") != MATERIAL_JSON_AGENT_PROTOCOL or run.stop_requested:
        return None
    call = calls[-1]
    error = call.usage_json.get("toolError", {})
    if (call.step_id is None
            or call.status != "tool_rejected" or call.response_json is not None or not call.raw_output
            or call.input_json.get("protocol") != MATERIAL_JSON_AGENT_PROTOCOL or call.input_json.get("projectRevision") != revision
            or not call.remote_request_id or call.cost_micros is None or call.usage_json.get("toolExecutionDone")
            or not call.usage_json.get("settled") or not _is_material_json_syntax_error(error)):
        return None
    step = session.get(AgentStep, call.step_id)
    reservation = session.get(BudgetReservation, call.id)
    if (step is None or step.run_id != run.id or step.role != call.role or step.status != "failed"
            or any(receipt["operationId"] == f"agent-{call.id}" for receipt in step.tool_results_json)
            or reservation is None or reservation.status != "settled" or reservation.reserved_micros != 0
            or reservation.cost_micros != call.cost_micros):
        return None
    return call


def _material_limit_extension_available(session: Session, run: AgentRun, revision: int, calls: list[AgentCall]) -> bool:
    if (run.executor != "platform" or run.limits_json.get("workspace") != "materials" or run.status != "failed"
            or run.project_revision != revision or max(len(calls), run.limits_json["maxSteps"]) >= 60
            or any(call.status in {"submitting", "tool_executing", "tool_outcome_unknown"}
                or call.status == "submission_unknown" and not _unknown_retry_authorized(session, run, call) for call in calls)):
        return False
    return run.error_code == "AGENT_STEP_LIMIT" or (run.error_code == "AGENT_NATIVE_CORRECTION_LIMIT"
        and _settled_material_json_rejection(session, run, revision, calls) is not None)


def _record_material_json_format_retry(run: AgentRun, call: AgentCall, operation_id: str, *, with_extension: bool = False) -> None:
    result = {key: value for key, value in (run.result_json or {}).items() if key not in {"nativeCorrection", "toolError"}}
    retries = result.get("materialJsonFormatRetries", {})
    run.result_json = {**result, "materialJsonFormatRetries": {**retries, call.step_id: {
        "rejectedCallId": call.id, "operationId": operation_id, "sceneArtifactOnly": call.role == "scene_designer",
        "materialArtifactOnly": call.role in MATERIAL_STRUCTURED_RECOVERY_ROLES,
        "withExtension": with_extension}}}


def _is_structured_scene_call(call: AgentCall) -> bool:
    request = call.input_json
    return (request.get("protocol") == MATERIAL_JSON_AGENT_PROTOCOL and request.get("sceneArtifactOnly") is True
        and request.get("toolCatalogVersion") == MATERIAL_SOP_TOOL_CATALOG_VERSION and call.role == "scene_designer"
        and request.get("providerEndpoint") == provider_endpoint(MATERIAL_JSON_AGENT_PROTOCOL, structured_response=True)
        and request.get("requestBody", {}).get("text", {}).get("format", {}).get("type") == "json_schema"
        and call.usage_json.get("providerFinishReason") == "stop")


def _recoverable_structured_scene_closure(session: Session, run: AgentRun, revision: int, calls: list[AgentCall]) -> AgentCall | None:
    if run.status != "failed" or run.error_code != "AGENT_NATIVE_CORRECTION_LIMIT" or run.project_revision != revision:
        return None
    call = _settled_material_json_rejection(session, run, revision, calls)
    if call is None or not _is_structured_scene_call(call) or call.usage_json.get("resolvedStructuredSceneSyntax"):
        return None
    if any(item.status in {"submitting", "tool_executing", "tool_outcome_unknown"}
            or item.status == "submission_unknown" and not _unknown_retry_authorized(session, run, item) for item in calls):
        return None
    return call if decode_structured_scene_closure(json.loads(call.raw_output)) is not None else None


def _is_material_json_syntax_error(error: dict) -> bool:
    return (error.get("code") == "AGENT_JSON_ARGUMENTS_INVALID" and error.get("businessExecuted") is False
            and any(item.get("type") == "json_invalid" for item in error.get("details", [])))


def _material_workflow_rejection(run: AgentRun, call: AgentCall, revision: dict | None) -> bool:
    protocol = run.limits_json.get("providerProtocol")
    tool_call_id = call.usage_json.get("toolCallId")
    eligible = (revision is not None and protocol in {MATERIAL_NATIVE_AGENT_PROTOCOL, MATERIAL_JSON_AGENT_PROTOCOL}
        and call.run_id == run.id and call.step_id == revision["stepId"]
        and call.role == (run.result_json or {}).get("materialReview", {}).get("role")
        and call.input_json.get("protocol") == protocol and _call_in_material_revision(call, revision)
        and call.status == "tool_rejected" and call.response_json is not None and call.response_json.get("action") == "complete"
        and call.usage_json.get("toolError", {}).get("code") == "MATERIAL_REVISION_REQUIRED"
        and call.usage_json["toolError"].get("businessExecuted") is False and not call.usage_json.get("toolExecutionDone")
        and call.usage_json.get("settled") and call.cost_micros is not None and call.total_tokens is not None
        and call.remote_request_id and call.raw_output
        and (bool(tool_call_id) if protocol == MATERIAL_NATIVE_AGENT_PROTOCOL else tool_call_id is None)
        and call.input_json.get("toolCatalogVersion") == run.limits_json.get("toolCatalogVersion"))
    if not eligible:
        return False
    try:
        decoded, identifier = decode_platform_message(json.loads(call.raw_output), False, call.role,
            protocol, run.limits_json["toolCatalogVersion"])
    except (AppError, ValidationError, json.JSONDecodeError):
        return False
    return decoded == call.response_json and identifier == tool_call_id


def _parameter_correction_count(run: AgentRun, step_id: str | None, calls: list[AgentCall]) -> int:
    revision = _material_revision_request(run, step_id)
    protocol = run.limits_json.get("providerProtocol")
    retry = (run.result_json or {}).get("materialJsonFormatRetries", {}).get(step_id, {})
    by_id = {call.id: call for call in calls if call.step_id == step_id and
             (protocol != MATERIAL_JSON_AGENT_PROTOCOL or call.input_json.get("protocol") == protocol)
             and (not retry.get("withExtension") or call.input_json.get("limitExtensionOperationId") == retry["operationId"])}
    return sum(1 for call in by_id.values() if call.input_json.get("correctionOf") and _call_in_material_revision(call, revision)
        and not (call.input_json["correctionOf"] in by_id and _material_workflow_rejection(run, by_id[call.input_json["correctionOf"]], revision)))


def _material_patch_feedback(run: AgentRun, step: AgentStep | None, calls: list[AgentCall]) -> dict | None:
    pending = (run.result_json or {}).get("nativeCorrection", {})
    revision = _material_revision_request(run, step.id if step else None)
    call = next((item for item in calls if item.id == pending.get("callId")), None)
    if (step is None or revision is None or call is None or call.step_id != step.id
            or call.usage_json.get("toolError", {}).get("code") != "AGENT_ARTIFACT_PATCH_INVALID"
            or (call.response_json or {}).get("tool") != "patch_artifact" or not _call_in_material_revision(call, revision)):
        return None
    arguments = call.response_json["arguments"]
    baseline = next((receipt["result"] for receipt in reversed(step.tool_results_json) if receipt["tool"] == "read_artifact"
        and receipt["result"].get("targetId") == arguments["targetId"]
        and receipt["result"].get("revision") == arguments["expectedRevision"]
        and receipt["result"].get("baseArtifactSha256") == arguments["baseArtifactSha256"]), None)
    if baseline is None:
        return None
    allowed = {item["path"]: item for item in baseline["patchablePaths"]}
    invalid, arrays = [], set()
    for patch in arguments["patches"]:
        if patch["op"] == "replace" and patch["path"] not in allowed:
            invalid.append(patch["path"])
            try:
                parent = JsonPointer.from_parts(JsonPointer(patch["path"]).parts[:-1]).path
            except JsonPointerException:
                continue
            if isinstance(allowed.get(parent, {}).get("value"), list):
                arrays.add(parent)
    return {"callId": call.id, "invalidPaths": invalid, "replaceWholeListPaths": sorted(arrays),
        "instruction": "不存在的路径不能replace。只使用实际read_artifact返回的patchablePaths；扩展标量数组时，replace已允许的完整数组路径并提供保留原值及新增项的完整数组。"}


def _recoverable_material_revision(session: Session, run: AgentRun, revision: int, settings: Settings | None = None) -> AgentCall | None:
    protocol = run.limits_json.get("providerProtocol")
    json_resume = (protocol == MATERIAL_JSON_AGENT_PROTOCOL and run.error_code == "MATERIAL_REVISION_CORRECTION_LIMIT"
                   and run.limits_json.get("toolCatalogVersion") == MATERIAL_SOP_TOOL_CATALOG_VERSION)
    native_resume = (protocol == MATERIAL_NATIVE_AGENT_PROTOCOL and run.error_code in {"MATERIAL_CONFIRMATION_REQUIRED", "AGENT_NATIVE_CORRECTION_LIMIT"})
    if (run.status != "failed" or not (json_resume or native_resume) or run.stop_requested
            or run.project_revision != revision or run.executor != "platform"
            or run.limits_json.get("toolCatalogVersion") not in MATERIAL_CATALOG_VERSIONS):
        return None
    step = _next_step(session, run)
    request = _material_revision_request(run, step.id if step else None)
    review = (run.result_json or {}).get("materialReview", {})
    if (step is None or step.role not in MATERIAL_ROLES or request is None or not request["requiresArtifactWrite"]
            or any(request.get(key) != review.get(key) for key in ("nodeId", "artifactHash"))
            or request["reviewId"] != review.get("id") or request["operationId"] != review.get("operationId")
            or request["requestedAt"] != review.get("answeredAt")):
        return None
    if json_resume and (run.result_json or {}).get("materialRevisionResume", {}).get("revisionOperationId") == request["operationId"]:
        return None
    calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
    if not calls or len(calls) >= run.limits_json["maxSteps"] or _parameter_correction_count(run, step.id, calls) >= 2:
        return None
    reservations = {item.id: item for item in session.scalars(select(BudgetReservation).where(BudgetReservation.id.in_([call.id for call in calls])))}
    for item in calls:
        reservation = reservations.get(item.id)
        if (item.status in {"submitting", "submission_unknown", "response_received", "tool_executing", "tool_outcome_unknown"}
                or item.total_tokens is None or item.cost_micros is None or not item.usage_json.get("settled")
                or reservation is None or reservation.status != "settled" or reservation.reserved_micros != 0
                or reservation.cost_micros != item.cost_micros):
            return None
    if sum(item.cost_micros for item in calls) >= run.limits_json["maxCostMicros"]:
        return None
    call = calls[-1]
    native_workflow_resume = run.error_code == "MATERIAL_CONFIRMATION_REQUIRED"
    tool_call_id = call.usage_json.get("toolCallId")
    if (call.step_id != step.id or call.role != step.role or call.status != ("failed" if native_workflow_resume else "tool_rejected") or not call.raw_output
            or not call.remote_request_id or (tool_call_id is not None if json_resume else not tool_call_id) or call.usage_json.get("toolExecutionDone")
            or call.usage_json.get("controlResult") is not None
            or call.input_json.get("planning") is not False or call.input_json.get("protocol") != protocol
            or call.input_json.get("toolCatalogVersion") != run.limits_json["toolCatalogVersion"]
            or call.input_json.get("projectRevision") != revision or not _call_in_material_revision(call, request)
            or any(item["operationId"] == f"agent-{call.id}" for item in step.tool_results_json)):
        return None
    if json_resume:
        rejections = [item for item in calls if _material_workflow_rejection(run, item, request)]
        if len(rejections) != 2 or rejections[-1].id != call.id:
            return None
    elif native_workflow_resume:
        if (call.usage_json.get("toolFailure", {}).get("code") != "MATERIAL_CONFIRMATION_REQUIRED"
                or any(_material_workflow_rejection(run, item, request) for item in calls)):
            return None
    else:
        parent = next((item for item in calls if item.id == call.input_json.get("correctionOf")), None)
        if (call.usage_json.get("toolError", {}).get("code") != "AGENT_ARTIFACT_PATCH_INVALID"
                or call.usage_json["toolError"].get("businessExecuted") is not False
                or parent is None or not _material_workflow_rejection(run, parent, request)
                or _parameter_correction_count(run, step.id, calls) != 0
                or sum(1 for item in calls if item.step_id == step.id and item.input_json.get("correctionOf") and _call_in_material_revision(item, request)) < 2
                or sum(1 for item in calls if _material_workflow_rejection(run, item, request)) >= 2
                or (run.result_json or {}).get("nativeCorrection", {}).get("callId") != call.id):
            return None
        successful_read = next((item for item in reversed(calls[:-1]) if item.step_id == step.id
            and item.input_json.get("correctionOf") == parent.id and item.status == "ready" and item.usage_json.get("toolExecutionDone")
            and _call_in_material_revision(item, request) and item.input_json.get("projectRevision") == revision
            and item.finished_at is not None and item.finished_at <= call.created_at
            and (item.response_json or {}).get("tool") == "read_artifact" and item.response_json["arguments"]["targetId"] == request["nodeId"]
            and any(receipt["operationId"] == f"agent-{item.id}" and receipt["tool"] == "read_artifact"
                and receipt["result"].get("baseArtifactSha256") == request["artifactHash"] for receipt in step.tool_results_json)), None)
        if successful_read is None:
            return None
    from app.image_models import ImageJob
    from app.media_models import MediaSequence
    from app.services.generation import ACTIVE_STATUSES
    from app.services.material_references import frozen_reference_assets
    if session.scalar(select(ImageJob.id).where(ImageJob.run_id == run.id, ImageJob.status.in_([
            "queued", "claimed", "submitting", "submission_unknown", "response_saved"])).limit(1)) is not None:
        return None
    generation_ids = [receipt["result"]["generation"]["id"]
        for item in session.scalars(select(AgentStep).where(AgentStep.run_id == run.id))
        for receipt in item.tool_results_json if receipt.get("tool") == "submit_generation"]
    sequence_ids = {item.id for item in session.scalars(select(MediaSequence).where(MediaSequence.project_id == run.project_id,
        MediaSequence.owner_id == run.owner_id, MediaSequence.scene_id == run.scene_id)) if item.input_json.get("runId") == run.id}
    active_generations = session.scalars(select(Generation).where(Generation.project_id == run.project_id,
        Generation.owner_id == run.owner_id, Generation.scene_id == run.scene_id,
        Generation.status.in_([*ACTIVE_STATUSES, "queued"])))
    if any(item.id in generation_ids or item.input_json.get("boundaryFrames", {}).get("sequenceId") in sequence_ids for item in active_generations):
        return None
    try:
        _validate_frozen_model(run, call)
        decision, identifier = decode_platform_message(json.loads(call.raw_output), False, step.role,
            protocol, run.limits_json["toolCatalogVersion"])
        workflow_resume = json_resume or native_workflow_resume
        if (decision != call.response_json or identifier != tool_call_id
                or workflow_resume and decision.get("action") != "complete"
                or not workflow_resume and decision.get("tool") != "patch_artifact"):
            return None
        project = require_project(session, run.project_id, run.owner_id)
        actual_settings = settings or get_settings()
        baseline = _read_artifact(session, run, project, require_canvas(session, run, project), request["nodeId"], actual_settings)
        if baseline.get("artifactSha256") != request["artifactHash"] or baseline["artifact"]["role"] != step.role:
            return None
        if not workflow_resume and (decision["arguments"]["targetId"] != request["nodeId"]
                or decision["arguments"]["expectedRevision"] != revision
                or decision["arguments"]["baseArtifactSha256"] != baseline["artifactSha256"]):
            return None
        frozen_reference_assets(session, run, actual_settings)
    except (AppError, ValidationError, json.JSONDecodeError, OSError):
        return None
    return call


def _protocol_instruction(run: AgentRun, step: AgentStep | None, context: dict, calls: list[AgentCall], completed_steps: list[AgentStep]) -> dict:
    revision = _material_revision_request(run, step.id if step else None)
    current_operations = {f"agent-{call.id}" for call in calls if step and call.step_id == step.id and _call_in_material_revision(call, revision)}
    verifications = [receipt for receipt in step.tool_results_json if receipt["tool"] in {"validate_artifacts", "validate_story"}] if step else []
    if revision is not None:
        verifications = [receipt for receipt in verifications if not revision["requiresArtifactWrite"]
            and receipt["operationId"] in current_operations and any(item.get("kind") == "creative_artifact"
                and item.get("id") == revision["nodeId"] and item.get("sha256") == revision["currentArtifactHash"]
                for item in receipt["result"].get("artifacts", []))]
    return {"goal": run.goal, "deliveryMode": run.limits_json.get("deliveryMode", "legacy"),
        "materialConversation": run.limits_json.get("materialContext"),
        "materialRevision": revision,
        "materialPatchCorrection": _material_patch_feedback(run, step, calls),
        "artifactContractVersion": run.limits_json.get("artifactContractVersion"), "projectRevision": run.project_revision,
        "authorizedTargetIds": run.target_ids_json, "createdNodeIds": run.created_ids_json, "context": context,
        "step": {"role": step.role, "title": step.title, "instruction": step.instruction,
                 "kind": run.plan_json[step.index].get("kind", "produce"), "dependsOn": run.plan_json[step.index].get("dependsOn", [])} if step else None,
        "previousSteps": [{"role": item.role, "result": item.result_json} for item in completed_steps],
        "verificationResults": [receipt["result"] for receipt in verifications],
        "remainingModelRequests": run.limits_json["maxSteps"] - len(calls)}


def _run_model(run: AgentRun) -> str:
    return run.limits_json.get("model", MODEL) if run.limits_json.get("providerProtocol") in SUPPORTED_JSON_PROTOCOLS else MODEL


def _validate_frozen_model(run: AgentRun, call: AgentCall) -> None:
    body = call.input_json.get("requestBody", {})
    requested = call.input_json.get("model", body.get("model", MODEL))
    if requested != _run_model(run) or body and body.get("model") != requested:
        raise AppError(409, "AGENT_MODEL_CONFLICT", "本次模型与任务冻结请求不一致，原始记录已保留")
    aliases = {QWEN_MODEL} if (run.limits_json.get("textProviderSnapshot") or {}).get("providerId") == "aliyun-qwen" and requested == QWEN_MODEL else model_aliases(requested)
    if call.model not in aliases:
        raise AppError(409, "AGENT_MODEL_CONFLICT", "供应商实际模型与冻结请求不一致，原始记录已保留")


def _native_messages(run: AgentRun, step: AgentStep | None, role: str, context: dict, calls: list[AgentCall], completed_steps: list[AgentStep]) -> list[dict]:
    instruction = _protocol_instruction(run, step, context, calls, completed_steps)
    revision = instruction["materialRevision"]
    verification = step is not None and run.plan_json[step.index].get("kind", "produce") == "verify"
    version = run.limits_json.get("toolCatalogVersion")
    if verification:
        system = "你负责当前步骤的只读核查。核对verificationResults中执行器已取得的真实结果，valid=true且没有内容问题时，调用complete_step提交简短结论。需要进一步核实时仅调用只读函数。当前步骤保持产物内容不变。"
    elif version == MATERIAL_SOP_TOOL_CATALOG_VERSION and step is None:
        system = material_planning_prompt()
    else:
        system = native_agent_prompt(role, not run.plan_json, run.limits_json.get("deliveryMode", "legacy"),
                                     version if version == MATERIAL_SOP_TOOL_CATALOG_VERSION else "R1")
    if version in MATERIAL_CATALOG_VERSIONS:
        system += "\n" + run.limits_json.get("materialContext", {}).get("instructions", "")
        if version != MATERIAL_SOP_TOOL_CATALOG_VERSION and run.limits_json.get("materialContext", {}).get("scope") == "current_round":
            system += "本轮范围以materialNodes、authorizedTargetIds和createdNodeIds为准。旧计划中超出该范围的历史对象及衔接要求已经取消，直接省略；只依据当前用户目标和本轮真实产物完成岗位交付，不读取或引用其他历史对象。"
        system += "普通文字节点通过read_project或retrieve_context读取正文；只有materialNodes中professionalRole有值的节点才可用read_artifact读取专业结构。每次只调用一个函数并等待真实结果。"
        if not verification and step is not None:
            if version == MATERIAL_SOP_TOOL_CATALOG_VERSION:
                system += (
                    "本步骤的真实输入列在context.adoptedHandoff。按节点标识用read_artifact读取；保存时在sources中以kind=artifact和实际nodeId引用已交接的产物。"
                    "用户直接陈述的内容使用kind=user_requirement和referenceId:null。项目、场景、资产或文档没有已读真实标识就不要列为来源。只需要列出实际使用的来源。"
                    '新建岗位成果的参数根对象固定为{"targetId":null,"title":null,"artifact":{…}}。标题只填写artifact.title，全用标准双引号字符串，根对象的title保持JSON字面量null。'
                )
            system += "修订专业产物时，先read_artifact读取完整当前结构、revision、baseArtifactSha256及patchablePaths，再以patch_artifact逐字段replace并保留未涉及字段。读取上游产物不授予修改它的权限。"
            system += "patch只能使用实际返回的patchablePaths。新增标量数组元素时，replace已允许的完整数组路径并提供完整数组；不存在的数组下标不能replace。"
            write_schema = next(item["function"]["parameters"] for item in agent_tools(role, False,
                protocol=run.limits_json.get("providerProtocol", MATERIAL_NATIVE_AGENT_PROTOCOL),
                catalog_version=run.limits_json["toolCatalogVersion"]) if item["function"]["name"] == "write_artifact")
            system += "write_artifact参数必须包含artifact、title及targetId；新建产物时targetId填写null，使用产物名称时title填写null；完整专业对象置于artifact字段中。\n当前岗位write_artifact完整参数Schema：" + json.dumps(write_schema, ensure_ascii=False)
            if revision and revision["requiresArtifactWrite"]:
                system += "当前materialRevision明确要求修订已有成果。先读取materialRevision.nodeId，逐项执行最新answers和requestedChanges并保存新成果。当前步骤的旧成功写入与旧验证仅属于修订前记录，不能作为本次完成证据。保存后等待新的成果确认；当前不得调用complete_step跳过修订。"
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps(instruction, ensure_ascii=False)}]
    steps = [*completed_steps, *([step] if step is not None else [])]
    receipts = {receipt["operationId"]: receipt for item in steps for receipt in item.tool_results_json}
    for call in calls:
        if not _call_in_material_revision(call, revision):
            continue
        if run.limits_json.get("providerProtocol") == MATERIAL_NATIVE_AGENT_PROTOCOL and call.step_id != (step.id if step else None):
            continue
        if run.limits_json.get("materialContext", {}).get("scope") == "current_round" and call.input_json.get("projectRevision", 0) < run.limits_json["materialContext"].get("historyFromRevision", run.initial_revision):
            continue
        if call.input_json.get("protocol") not in SUPPORTED_NATIVE_PROTOCOLS or not call.raw_output:
            continue
        identifier = call.usage_json.get("toolCallId")
        rejected_batch = call.usage_json.get("rejectedBatchIds")
        if rejected_batch:
            message = json.loads(call.raw_output)
            if rejected_batch != native_batch_ids(message, call.input_json["protocol"]) or not call.usage_json.get("toolError") or call.usage_json["toolError"].get("businessExecuted") is not False:
                raise AppError(409, "AGENT_NATIVE_BATCH_HISTORY_INVALID", "批量调用记录缺少真实拒绝依据")
            messages.append(message)
            for call_id in rejected_batch:
                messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({"error": call.usage_json["toolError"]}, ensure_ascii=False)})
            continue
        if not identifier:
            continue
        if call.input_json["protocol"] == MATERIAL_NATIVE_AGENT_PROTOCOL and call.usage_json.get("toolError", {}).get("code") == "AGENT_NATIVE_ARGUMENTS_INVALID":
            error = call.usage_json["toolError"]
            feedback = {"origin": "runtime_parameter_validation", "callId": call.id, "requestId": call.remote_request_id,
                        "error": error, "instruction": "该操作未执行。请按当前函数完整参数Schema重新生成，保留正确的artifact参数层级。"}
            if version == MATERIAL_SOP_TOOL_CATALOG_VERSION and call.usage_json.get("nativeFunction") == "write_artifact":
                if error["details"] and not any(detail["type"] == "json_invalid" for detail in error["details"]):
                    raw = json.loads(call.raw_output)
                    feedback["rejectedArguments"] = raw["tool_calls"][0]["function"]["arguments"]
                feedback["instruction"] = (
                    "此写入未执行。按已列出的确切字段错误修正，参考最近一份语法有效的rejectedArguments保留合格内容。"
                    "无真实标识的额外引用请去掉，保留用户确认事项和已采用上游来源。新成果根字段title与targetId必须是null，所有正文只写在artifact内部。"
                )
            messages.append({"role": "system", "content": json.dumps(feedback, ensure_ascii=False)})
            continue
        receipt = receipts.get(f"agent-{call.id}")
        if call.usage_json.get("toolError") is not None:
            outcome = {"error": call.usage_json["toolError"]}
        elif call.usage_json.get("toolFailure") is not None:
            outcome = {"error": {**call.usage_json["toolFailure"], "automaticCorrectionAllowed": False}}
        elif call.usage_json.get("controlResult") is not None:
            outcome = call.usage_json["controlResult"]
        elif receipt is not None:
            outcome = _tool_observation(receipt)
        else:
            raise AppError(409, "AGENT_NATIVE_TOOL_RESULT_MISSING", "原生工具调用尚无实际回执，不能发起下一次模型请求")
        messages.append(json.loads(call.raw_output))
        messages.append({"role": "tool", "tool_call_id": identifier, "content": json.dumps(outcome, ensure_ascii=False)})
    return messages


def _json_messages(run: AgentRun, step: AgentStep | None, role: str, context: dict, calls: list[AgentCall], completed_steps: list[AgentStep], *, scene_artifact_only: bool = False, material_artifact_only: bool = False) -> list[dict]:
    planning = not run.plan_json
    protocol = run.limits_json.get("providerProtocol", JSON_AGENT_PROTOCOL)
    schema = agent_response_schema(planning, protocol=protocol)
    catalog_version = run.limits_json.get("toolCatalogVersion", LEGACY_TOOL_CATALOG_VERSION)
    business = agent_business_tools(role, planning, catalog_version=catalog_version)
    verification = step is not None and run.plan_json[step.index].get("kind", "produce") == "verify"
    if verification:
        business = [tool for tool in business if tool["function"]["name"] in READ_TOOLS | {"complete_step"}]
        system = ("你负责当前步骤的只读核查。执行器已经调用真实验证工具，核查证据包含在context和工具回执中。"
            "核对dependsOn指定的真实产物及当前指令；已有valid=true的当次验证且没有内容问题时，输出一个json对象："
            '{"action":"complete","summary":"依据实际核验结果填写简短结论"}。'
            "需要进一步读取时，仅调用下列只读工具。保持产物内容不变。当前步骤不创作专业产物。"
            "只输出一个标准json对象。\n响应Schema：" + json.dumps(schema, ensure_ascii=False)
            + "\n只读工具Schema：" + json.dumps(business, ensure_ascii=False))
    elif scene_artifact_only:
        system = material_scene_artifact_prompt(artifact_model(role).model_json_schema(by_alias=True))
    elif material_artifact_only:
        system = material_structured_artifact_prompt(role, artifact_model(role, reusable_frames=True).model_json_schema(by_alias=True))
    elif protocol == MATERIAL_JSON_AGENT_PROTOCOL:
        system = material_json_agent_prompt(role, planning, schema, business)
    else:
        system = json_agent_prompt(role, business, planning, run.limits_json.get("deliveryMode", "legacy"), schema,
            catalog_version=TOOL_CATALOG_VERSION if catalog_version in MATERIAL_CATALOG_VERSIONS else catalog_version)
    if catalog_version in MATERIAL_CATALOG_VERSIONS:
        system += "\n素材工作范围：" + run.limits_json.get("materialContext", {}).get("instructions", "")
    messages = [{"role": "system", "content": system},
        {"role": "user", "content": json.dumps(_protocol_instruction(run, step, context, calls, completed_steps), ensure_ascii=False)}]
    steps = [*completed_steps, *([step] if step is not None else [])]
    receipts = {receipt["operationId"]: receipt for item in steps for receipt in item.tool_results_json}
    if protocol == MATERIAL_JSON_AGENT_PROTOCOL and step is not None and (run.result_json or {}).get("materialProtocolUpgrade"):
        accepted_reads = [receipts[f"agent-{call.id}"] for call in calls
                          if call.step_id == step.id and call.input_json.get("protocol") == MATERIAL_NATIVE_AGENT_PROTOCOL
                          and f"agent-{call.id}" in receipts and receipts[f"agent-{call.id}"]["tool"] == "read_artifact"]
        if accepted_reads:
            messages.append({"role": "user", "content": json.dumps({"alreadyRead": [_tool_observation(item) for item in accepted_reads]}, ensure_ascii=False)})
    format_retry = (run.result_json or {}).get("materialJsonFormatRetries", {}).get(step.id) if step is not None else None
    latest_json_correction = None
    for call in calls:
        if call.input_json.get("protocol") != protocol or not call.raw_output:
            continue
        if protocol == MATERIAL_JSON_AGENT_PROTOCOL and (call.step_id != (step.id if step else None)
                or not _call_in_material_revision(call, _material_revision_request(run, step.id if step else None))):
            continue
        if format_retry and _is_material_json_syntax_error(call.usage_json.get("toolError", {})):
            continue
        original = json.loads(call.raw_output)
        if not isinstance(original.get("content"), str) or original.get("tool_calls"):
            raise AppError(409, "AGENT_JSON_HISTORY_INVALID", "JSON协议历史缺少真实文本回复，不能发起后续模型请求")
        latest_json_correction = None
        receipt = receipts.get(f"agent-{call.id}")
        if call.usage_json.get("toolError") is not None:
            outcome = {"error": call.usage_json["toolError"]}
            if any(item.get("type") == "json_invalid" for item in call.usage_json["toolError"].get("details", [])):
                try:
                    json.loads(original["content"])
                except json.JSONDecodeError as error:
                    latest_json_correction = {"origin": "standard_json_diagnostics", "sourceCallId": call.id,
                        "location": json_syntax_details(error),
                        "instruction": "根据原始回复的lineno、colno、pos和excerptErrorOffset定位语法错误，同时依据本岗位完整Schema检查当前完整对象的字段归属、对象与数组的闭合和分隔，输出完整合法JSON对象。禁止原样重复同一非法回复或只复述错误；保持本岗位全部专业字段和内容要求，不重复执行已有成功操作。"}
                    outcome["jsonCorrection"] = latest_json_correction
        elif call.usage_json.get("toolFailure") is not None:
            outcome = {"error": {**call.usage_json["toolFailure"], "automaticCorrectionAllowed": False}}
        elif call.usage_json.get("controlResult") is not None:
            outcome = call.usage_json["controlResult"]
        elif receipt is not None:
            outcome = _tool_observation(receipt)
        else:
            raise AppError(409, "AGENT_JSON_RESULT_MISSING", "原JSON请求尚无实际处理结果，不能发起下一次模型请求")
        decision = call.response_json or {}
        messages.append({"role": "assistant", "content": original["content"]})
        result = {"callId": call.id, "tool": decision.get("tool") or decision.get("action"), "result": outcome}
        if receipt is not None:
            result["operationId"] = receipt["operationId"]
        messages.append({"role": "user", "content": json.dumps({"toolResult": result}, ensure_ascii=False)})
    if catalog_version in {TOOL_CATALOG_VERSION, WORKSPACE_TOOL_CATALOG_VERSION}:
        messages.append({"role": "user", "content": json.dumps({"taskReminder": {
            "origin": "original_user_goal", "goal": run.goal,
            "stepInstruction": step.instruction if step is not None else None,
            "authorizedTargetIds": run.target_ids_json}}, ensure_ascii=False)})
    if latest_json_correction is not None:
        messages.append({"role": "user", "content": json.dumps({"jsonCorrectionReminder": latest_json_correction}, ensure_ascii=False)})
    if format_retry:
        messages.append({"role": "user", "content": "之前的回复格式无效，没有保存。请按照当前岗位结构从已读取的真实输入重新输出一个完整JSON对象。先核对各层数组和对象的闭合，只输出本次所需操作。"})
    return messages


def _native_correction_info(session: Session, run: AgentRun, step_id: str | None, calls: list[AgentCall]) -> dict:
    manual = (run.result_json or {}).get("manualNativeRepairOf")
    if manual and not any(call.input_json.get("manualRepairOf") == manual for call in calls):
        original = session.get(AgentCall, manual)
        if original is None or original.run_id != run.id or original.step_id != step_id or original.status != "tool_rejected" or not original.usage_json.get("settled"):
            raise AppError(409, "AGENT_MANUAL_REPAIR_INVALID", "本次重试缺少原始拒绝记录")
        return {"manualRepairOf": manual, "manualRepairOperationId": run.result_json["manualRepairOperationId"]}
    pending = (run.result_json or {}).get("nativeCorrection")
    if pending is None:
        return {}
    original = session.get(AgentCall, pending["callId"])
    if original is None or original.run_id != run.id or original.step_id != step_id or original.status != "tool_rejected" or not original.usage_json.get("settled") or not original.remote_request_id:
        raise AppError(409, "AGENT_NATIVE_CORRECTION_INVALID", "工具修正缺少已结算的真实拒绝记录")
    revision = _material_revision_request(run, step_id)
    if _material_workflow_rejection(run, original, revision):
        return {}
    attempts = _parameter_correction_count(run, step_id, calls)
    if attempts >= 2:
        raise AppError(422, "AGENT_NATIVE_CORRECTION_LIMIT", "本步骤已达到两次模型参数修正上限，原始回复和费用继续保留")
    return {"correctionOf": original.id, "correctionAttempt": attempts + 1}


def _native_tool_error(error: AppError) -> dict:
    cause = error.__cause__
    details = cause.errors(include_input=False, include_context=False, include_url=False) if isinstance(cause, ValidationError) else []
    if isinstance(cause, json.JSONDecodeError):
        details = [json_syntax_details(cause)]
    return {"code": error.code, "message": error.message, "details": details, "businessExecuted": False}


def _recoverable_native_format(session: Session, run: AgentRun, revision: int) -> AgentCall | None:
    if (run.status != "failed" or run.error_code != "AGENT_NATIVE_CORRECTION_LIMIT" or run.project_revision != revision
            or run.limits_json.get("providerProtocol") != MATERIAL_NATIVE_AGENT_PROTOCOL):
        return None
    upgrade = run.limits_json.get("toolCatalogVersion") == MATERIAL_SOP_TOOL_CATALOG_VERSION
    if (run.result_json or {}).get("materialProtocolUpgrade" if upgrade else "manualNativeRepairOf"):
        return None
    call = session.scalar(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at.desc()).limit(1))
    if call is None or call.status != "tool_rejected" or call.usage_json.get("toolError", {}).get("code") != "AGENT_NATIVE_ARGUMENTS_INVALID" or not call.usage_json.get("toolCallId") or not call.remote_request_id or not call.usage_json.get("settled") or call.usage_json.get("toolExecutionDone"):
        return None
    if upgrade:
        reservation = session.get(BudgetReservation, call.id)
        if (call.input_json.get("protocol") != MATERIAL_NATIVE_AGENT_PROTOCOL or call.input_json.get("projectRevision") != revision
                or call.usage_json["toolError"].get("businessExecuted") is not False or call.cost_micros is None
                or reservation is None or reservation.status != "settled" or reservation.reserved_micros != 0
                or reservation.cost_micros != call.cost_micros):
            return None
    count = session.scalar(select(func.count()).select_from(AgentCall).where(AgentCall.run_id == run.id))
    if count >= run.limits_json["maxSteps"]:
        return None
    return call


def _recoverable_plain_read(session: Session, run: AgentRun, project: Project) -> AgentCall | None:
    if run.status != "failed" or run.error_code != "AGENT_NATIVE_CORRECTION_LIMIT" or run.project_revision != project.revision or run.limits_json.get("toolCatalogVersion") not in MATERIAL_CATALOG_VERSIONS:
        return None
    call = session.scalar(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at.desc()).limit(1))
    if call is None or call.status != "tool_rejected" or not call.response_json or call.response_json.get("tool") != "read_artifact" or call.usage_json.get("toolError", {}).get("code") != "AGENT_ARTIFACT_NOT_AVAILABLE":
        return None
    if not call.usage_json.get("settled") or not call.remote_request_id or not call.usage_json.get("toolCallId") or call.usage_json.get("toolExecutionDone"):
        return None
    target = call.response_json["arguments"]["targetId"]
    node = next((node for node in require_canvas(session, run, project).data_json["nodes"] if node["id"] == target), None)
    if node is None or node["type"] != "text" or node.get("metadata", {}).get("creativeArtifact") or node.get("metadata", {}).get("artifactDraft"):
        return None
    return call


def _recoverable_native_batch(session: Session, run: AgentRun, revision: int) -> AgentCall | None:
    if run.status != "failed" or run.executor != "platform" or run.error_code != "AGENT_NATIVE_CALL_INVALID" or run.project_revision != revision:
        return None
    call = session.scalar(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at.desc()).limit(1))
    if call is None or call.status != "failed" or call.response_json is not None or not call.raw_output or call.input_json.get("protocol") != MATERIAL_NATIVE_AGENT_PROTOCOL:
        return None
    if not native_batch_ids(json.loads(call.raw_output), call.input_json["protocol"]) or call.usage_json.get("nativeFinishReason") != "tool_calls" or not call.usage_json.get("settled") or not call.remote_request_id or call.usage_json.get("toolExecutionDone"):
        return None
    reservation = session.get(BudgetReservation, call.id)
    if reservation is None or reservation.status != "settled" or reservation.reserved_micros != 0 or reservation.cost_micros != call.cost_micros:
        return None
    return call


def _reject_native_call(session: Session, run: AgentRun, call: AgentCall, error: AppError) -> None:
    reservation = session.get(BudgetReservation, call.id)
    if (call.input_json.get("protocol") not in SUPPORTED_PLATFORM_PROTOCOLS or not call.remote_request_id or not call.usage_json.get("settled") or call.cost_micros is None or reservation is None
            or reservation.status != "settled" or reservation.reserved_micros != 0 or reservation.cost_micros != call.cost_micros):
        raise AppError(409, "AGENT_NATIVE_REJECTION_UNSETTLED", "原请求尚未结算，不能自动修正或再次提交")
    batch = native_batch_ids(json.loads(call.raw_output), call.input_json["protocol"]) if error.code == "AGENT_NATIVE_BATCH_NOT_EXECUTED" else []
    if batch:
        call.usage_json = {**call.usage_json, "rejectedBatchIds": batch}
    if call.input_json.get("protocol") in SUPPORTED_NATIVE_PROTOCOLS and not call.usage_json.get("toolCallId") and not batch:
        raise AppError(422, "AGENT_NATIVE_TOOL_ID_MISSING", "原供应商回复缺少可关联的真实函数调用标识")
    errors = _native_tool_error(error)
    call.status, call.usage_json = "tool_rejected", {**call.usage_json, "toolError": errors}
    attempts = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id, AgentCall.step_id == call.step_id)).all()
    revision = _material_revision_request(run, call.step_id)
    workflow = _material_workflow_rejection(run, call, revision)
    count = sum(1 for item in attempts if _material_workflow_rejection(run, item, revision)) if workflow else _parameter_correction_count(run, call.step_id, attempts)
    result = {**(run.result_json or {}), "candidate": call.response_json, "toolError": errors}
    if workflow:
        pending = result.get("nativeCorrection")
        previous = session.get(AgentCall, pending["callId"]) if pending else None
        if previous is not None and _material_workflow_rejection(run, previous, revision):
            result.pop("nativeCorrection")
        result["materialWorkflowCorrection"] = {"reviewId": revision["reviewId"], "callId": call.id, "rejections": count, "maxRejections": 2}
    else:
        result["nativeCorrection"] = {"callId": call.id, "tool": (call.response_json or {}).get("tool") or call.usage_json.get("nativeFunction")}
    run.result_json = result
    if run.stop_requested:
        run.status, run.error_code, run.error_message = "stopped", "RUN_STOPPED", "任务已停止，拒绝记录与实际费用继续保留"
    elif count >= 2:
        run.status, run.error_code, run.error_message = "failed", ("MATERIAL_REVISION_CORRECTION_LIMIT" if workflow else "AGENT_NATIVE_CORRECTION_LIMIT"), ("本次修订已达到两次提前提交完成的纠正上限" if workflow else "本步骤已达到两次模型参数修正上限")
    else:
        run.status, run.error_code, run.error_message = "queued", "AGENT_NATIVE_TOOL_REJECTED", "参数已拒绝且没有执行业务操作，保留真实回复与费用并允许有界修正"
    run.updated_at, run.lease_expires_at = time.time(), None
    _mark_step_state(session, run, "queued" if run.status == "queued" else run.status)


def _safe_native_rejection(error: AppError, tool_name: str | None) -> bool:
    if error.code == "MATERIAL_REVISION_REQUIRED":
        return error.status == 422 and tool_name == "complete_step"
    if error.code == "AGENT_IMAGE_PROMPT_SELECTION_INVALID":
        return error.status == 422 and tool_name == "patch_artifact"
    if error.status == 403 and error.code == "AGENT_TARGET_NOT_ALLOWED" and tool_name in {"read_artifact", "retrieve_context"}:
        return True
    if error.status == 422 and error.code == "AGENT_NATIVE_BATCH_NOT_EXECUTED":
        return True
    if error.status != 422 or error.code.endswith("_FORBIDDEN") or error.code in {"AGENT_NATIVE_CALL_INVALID", "AGENT_NATIVE_ROLE_INVALID"}:
        return False
    if tool_name in {"split_image", "join_videos", "adopt_asset", "submit_generation"}:
        return error.code in {"AGENT_TOOL_ARGUMENTS_INVALID", "AGENT_NATIVE_ARGUMENTS_INVALID"} and isinstance(error.__cause__, ValidationError)
    if isinstance(error.__cause__, (ValidationError, json.JSONDecodeError)):
        return True
    return tool_name in {"submit_plan", "complete_step", "write_artifact", "patch_artifact", "read_artifact", "write_script", "apply_canvas_ops", "write_memory", "retrieve_context"}


def _native_not_executed(call: AgentCall, code: str, message: str) -> None:
    call.status = "not_executed"
    call.usage_json = {**call.usage_json, "toolError": {"code": code, "message": message, "details": [], "businessExecuted": False, "retryable": False}}


def _complete_platform_step(session, run, step, summary, validation):
    step.status, step.result_json = "completed", {"summary": summary, "verifiedToolCount": len(step.tool_results_json), "artifactValidation": validation}
    session.flush()
    run.status = "completed" if _next_step(session, run) is None else "queued"
    run.result_json = {**(run.result_json or {}), "summary": summary, "lastStepId": step.id, "actualToolResults": step.tool_results_json}
    if run.status == "completed":
        run.finished_at = time.time()


def _finish_native_action(session: Session, run: AgentRun, call: AgentCall, decision: AgentDecision, project: Project, settings: Settings) -> None:
    step = session.get(AgentStep, call.step_id) if call.step_id else None
    if call.input_json["planning"]:
        if isinstance(decision, MaterialPlanningDecision):
            run.result_json = {**(run.result_json or {}), "directorBrief": decision.director_brief.model_dump(by_alias=True)}
            if decision.questions:
                from app.services.material_review import pause_for_clarification
                review_id = pause_for_clarification(run, [question.model_dump(by_alias=True) for question in decision.questions])
                call.usage_json = {**call.usage_json, "controlResult": {"accepted": True, "status": "waiting_confirmation", "reviewId": review_id}}
                return
        if decision.action != "plan" or not decision.plan:
            raise AppError(422, "AGENT_PLAN_INVALID", "角色计划缺少必要步骤")
        if run.role != "director" and any(item.role != run.role for item in decision.plan):
            raise AppError(403, "AGENT_PLAN_ROLE_NOT_ALLOWED", "计划包含当前岗位没有授权的角色")
        if run.limits_json.get("workspace") == "materials" and any(item.role not in MATERIAL_ROLES for item in decision.plan):
            raise AppError(403, "AGENT_PLAN_ROLE_NOT_ALLOWED", "素材创作计划仅允许素材岗位")
        if run.limits_json.get("workspace") == "project" and any(item.role not in PROJECT_ROLE_TOOLS for item in decision.plan):
            raise AppError(403, "AGENT_PLAN_ROLE_NOT_ALLOWED", "总画布计划仅安排总监、编剧、剪辑和互动岗位；素材制作通过场景素材画布执行")
        for index, item in enumerate(decision.plan):
            if len(set(item.depends_on)) != len(item.depends_on) or any(type(dependency) is not int or dependency < 0 or dependency >= index for dependency in item.depends_on):
                raise AppError(422, "AGENT_PLAN_DEPENDENCY_INVALID", "计划依赖必须唯一且引用前面的步骤")
            if item.kind == "verify" and (not item.depends_on or any(decision.plan[dependency].kind != "produce" for dependency in item.depends_on)):
                raise AppError(422, "AGENT_PLAN_DEPENDENCY_INVALID", "验证步骤必须依赖前面的产物步骤")
        plan = [item.model_dump(by_alias=True) for item in decision.plan]
        if isinstance(decision, MaterialPlanningDecision):
            from app.services.material_handoffs import material_plan
            plan = material_plan(session, run, require_canvas(session, run, project), plan)
        run.plan_json = plan
        for index, item in enumerate(plan):
            session.add(AgentStep(run_id=run.id, index=index, role=item["role"], title=item["title"], instruction=item["instruction"], status="queued", tool_results_json=[]))
        call.usage_json = {**call.usage_json, "controlResult": {"accepted": True, "plan": run.plan_json}}
        run.status = "queued"
    elif decision.action == "complete" and step is not None:
        validation = require_step_delivery(session, run, step, project, settings)
        _complete_platform_step(session, run, step, decision.summary, validation)
        call.usage_json = {**call.usage_json, "controlResult": {"completed": True, "stepId": step.id, "summary": decision.summary, "artifactValidation": validation}}
    else:
        raise AppError(422, "AGENT_DECISION_INVALID", "当前函数调用不符合规划或执行阶段")


def _apply_native_call(settings: Settings, factory: sessionmaker, run_id: str, call_id: str) -> None:
    tool_name = None
    try:
        with factory() as session:
            lock_write(session)
            run, call = session.get(AgentRun, run_id), session.get(AgentCall, call_id)
            if (call.input_json.get("protocol") not in SUPPORTED_PLATFORM_PROTOCOLS or call.input_json.get("protocol") != run.limits_json.get("providerProtocol")
                    or call.status != "ready" or not call.remote_request_id or not call.usage_json.get("settled")
                    or call.input_json.get("protocol") in SUPPORTED_NATIVE_PROTOCOLS and not call.usage_json.get("toolCallId")):
                raise AppError(409, "AGENT_NATIVE_CALL_NOT_READY", "项目操作需要已结算且通过校验的实际供应商回复")
            if call.input_json.get("protocol") in SUPPORTED_JSON_PROTOCOLS and call.usage_json.get("toolCallId") is not None:
                raise AppError(409, "AGENT_JSON_ID_INVALID", "JSON协议不允许使用原生函数调用标识")
            _validate_frozen_model(run, call)
            if call.input_json.get("toolCatalogVersion", LEGACY_TOOL_CATALOG_VERSION) != run.limits_json.get("toolCatalogVersion", LEGACY_TOOL_CATALOG_VERSION):
                raise AppError(409, "AGENT_TOOL_CATALOG_CONFLICT", "工具目录与任务冻结版本不一致，原始记录已保留")
            if call.usage_json.get("toolExecutionDone"):
                session.commit()
                return
            schema = MaterialPlanningDecision if call.input_json["planning"] and run.limits_json.get("toolCatalogVersion") == MATERIAL_SOP_TOOL_CATALOG_VERSION else AgentDecision
            decision = schema.model_validate(call.response_json)
            tool_name = "submit_plan" if call.input_json["planning"] else "complete_step" if decision.action == "complete" else decision.tool
            if tool_name in {"split_image", "join_videos", "adopt_asset", "submit_generation"}:
                call.status = "tool_executing"
                session.commit()
                lock_write(session)
                session.refresh(run)
                session.refresh(call)
            project = require_project(session, run.project_id, run.owner_id)
            step = session.get(AgentStep, call.step_id) if call.step_id else None
            existing = next((item for item in step.tool_results_json if item["operationId"] == f"agent-{call.id}"), None) if step else None
            if run.stop_requested:
                if existing is None:
                    _native_not_executed(call, "RUN_STOPPED", "本人已停止任务，当前函数没有执行")
                else:
                    call.usage_json = {**call.usage_json, "toolExecutionDone": True}
                run.status, run.finished_at = "stopped", time.time()
                _mark_step_state(session, run, "stopped")
                run.result_json = {**(run.result_json or {}), "candidate": call.response_json, "automaticWriteStopped": True}
                session.commit()
                return
            if project.revision != run.project_revision or existing is None and project.revision != call.input_json["projectRevision"]:
                if existing is None:
                    _native_not_executed(call, "REVISION_CONFLICT", "项目已有编辑，旧版本函数没有执行")
                else:
                    call.usage_json = {**call.usage_json, "toolExecutionDone": True}
                run.status, run.error_code, run.error_message = "conflict", "REVISION_CONFLICT", "函数调用期间项目已有编辑，候选已保存，请读取最新版本后恢复"
                _mark_step_state(session, run, "conflict")
                run.result_json = {**(run.result_json or {}), "candidate": call.response_json}
                session.commit()
                return
            if decision.action == "tool" and step is not None:
                output = {"result": existing["result"], "revision": existing["revision"]} if existing else execute_tool(session, run, step.role, decision.tool, decision.arguments, f"agent-{call.id}", run.project_revision, settings, step, manage_transaction=False)
                run.result_json = {**(run.result_json or {}), "lastTool": decision.tool, **output}
                if run.status == "running":
                    run.status = "queued"
            else:
                _finish_native_action(session, run, call, decision, project, settings)
            pending = (run.result_json or {}).get("nativeCorrection")
            previous = session.get(AgentCall, pending["callId"]) if pending else None
            if pending and (previous is not None and _material_workflow_rejection(run, previous, _material_revision_request(run, call.step_id))
                    or pending.get("tool") == decision.tool or tool_name in {"submit_plan", "complete_step"} or project.revision != call.input_json["projectRevision"]):
                run.result_json = {key: value for key, value in run.result_json.items() if key not in {"nativeCorrection", "toolError"}}
                run.error_code, run.error_message = None, None
            workflow = (run.result_json or {}).get("materialWorkflowCorrection")
            revision = _material_revision_request(run, call.step_id)
            if (workflow and revision and workflow["reviewId"] == revision["reviewId"]
                    and (run.result_json or {}).get("toolError", {}).get("code") == "MATERIAL_REVISION_REQUIRED"):
                run.result_json = {key: value for key, value in run.result_json.items() if key != "toolError"}
                run.error_code, run.error_message = None, None
            call.status, call.usage_json = "ready", {**call.usage_json, "toolExecutionDone": True}
            run.updated_at, run.lease_expires_at = time.time(), None
            session.commit()
    except AppError as error:
        if not _safe_native_rejection(error, tool_name):
            with factory() as session:
                lock_write(session)
                run, call = session.get(AgentRun, run_id), session.get(AgentCall, call_id)
                call.status, call.usage_json = "failed", {**call.usage_json, "toolFailure": {"code": error.code, "message": error.message}}
                run.status = "conflict" if error.code in {"REVISION_CONFLICT", "TARGET_CHANGED", "AGENT_ARTIFACT_CHANGED", "AGENT_ARTIFACT_SOURCE_CHANGED", "AGENT_ARTIFACT_NOT_READY", "AGENT_DEPENDENCY_NOT_READY"} else "failed"
                run.error_code, run.error_message, run.updated_at, run.lease_expires_at = error.code, error.message, time.time(), None
                run.result_json = {**(run.result_json or {}), "candidate": call.response_json}
                _mark_step_state(session, run, run.status)
                session.commit()
            return
        with factory() as session:
            lock_write(session)
            run, call = session.get(AgentRun, run_id), session.get(AgentCall, call_id)
            project = require_project(session, run.project_id, run.owner_id)
            if project.revision != run.project_revision or project.revision != call.input_json["projectRevision"]:
                raise AppError(409, "REVISION_CONFLICT", "拒绝参数后项目已有编辑，不能自动修正旧版本")
            _reject_native_call(session, run, call, error)
            session.commit()


def _decode_native_saved_call(settings: Settings, factory: sessionmaker, run_id: str, call_id: str) -> None:
    with factory() as session:
        call = session.get(AgentCall, call_id)
        message = json.loads(call.raw_output)
        planning, role = call.input_json["planning"], call.role
        protocol = call.input_json["protocol"]
        json_protocol = is_json_protocol(protocol)
        finish_reason = call.usage_json.get("providerFinishReason", call.usage_json.get("nativeFinishReason"))
        closed_scene = decode_structured_scene_closure(message) if _is_structured_scene_call(call) else None
    try:
        if finish_reason != ("stop" if json_protocol else "tool_calls"):
            raise AppError(422, "AGENT_RESULT_INCOMPLETE", "模型回复未完整结束，实际回复和费用已保存")
        if closed_scene is None:
            decision, identifier = decode_platform_message(message, planning, role, protocol=protocol,
                catalog_version=call.input_json.get("toolCatalogVersion", LEGACY_TOOL_CATALOG_VERSION),
                scene_artifact_only=bool(call.input_json.get("sceneArtifactOnly")),
                material_artifact_only=bool(call.input_json.get("materialArtifactOnly")))
        else:
            decision, identifier = closed_scene[0], None
    except AppError as error:
        with factory() as session:
            lock_write(session)
            run, call = session.get(AgentRun, run_id), session.get(AgentCall, call_id)
            function = "json_decision" if json_protocol else call.usage_json.get("nativeFunction")
            project = require_project(session, run.project_id, run.owner_id)
            if project.revision != run.project_revision or project.revision != call.input_json["projectRevision"]:
                run.status, run.error_code, run.error_message = "conflict", "REVISION_CONFLICT", "模型返回后项目已有编辑，原始候选已保存"
                _native_not_executed(call, "REVISION_CONFLICT", "项目已有编辑，项目操作没有执行")
                _mark_step_state(session, run, "conflict")
            elif (isinstance(message.get("content"), str) and not message.get("tool_calls") if json_protocol else bool(call.usage_json.get("toolCallId") or native_batch_ids(message, protocol))) and _safe_native_rejection(error, function) and error.code != "AGENT_RESULT_INCOMPLETE":
                _reject_native_call(session, run, call, error)
            else:
                call.status = "failed"
                run.status, run.error_code, run.error_message = "failed", error.code, error.message
                _mark_step_state(session, run, "failed")
            run.result_json = {**(run.result_json or {}), "candidate": {"providerMessage": message}}
            session.commit()
        return
    with factory() as session:
        lock_write(session)
        call = session.get(AgentCall, call_id)
        call.response_json, call.status = decision, "ready"
        if closed_scene is not None:
            call.usage_json = {**call.usage_json, "structuredSceneClosure": {"appended": closed_scene[1], "scope": "terminal_delimiters"}}
        if identifier is not None:
            call.usage_json = {**call.usage_json, "toolCallId": identifier}
        session.commit()
    _apply_native_call(settings, factory, run_id, call_id)


async def _execute_native_platform(settings: Settings, factory: sessionmaker, run_id: str, worker_id: str) -> None:
    with factory() as session:
        lock_write(session)
        run = session.get(AgentRun, run_id)
        if run is None or run.executor != "platform" or run.status != "queued":
            session.rollback()
            return
        if run.stop_requested:
            run.status, run.finished_at = "stopped", time.time()
            _mark_step_state(session, run, "stopped")
            session.commit()
            return
        require_text_admin(session.get(User, run.owner_id), settings)
        project = require_project(session, run.project_id, run.owner_id)
        if project.revision != run.project_revision:
            run.status, run.error_code, run.error_message = "conflict", "REVISION_CONFLICT", "项目已有人工编辑，请读取最新版本后恢复"
            _mark_step_state(session, run, "conflict")
            session.commit()
            return
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
        unconfirmed = next((call for call in calls if call.status == "submission_unknown" and not _unknown_retry_authorized(session, run, call)), None)
        if unconfirmed is not None:
            run.status, run.error_code, run.error_message = "submission_unknown", "AGENT_SUBMISSION_UNKNOWN", "原请求结果待核实，请本人确认后再尝试；原费用预留继续保留"
            _mark_step_state(session, run, "submission_unknown")
            session.commit()
            return
        interrupted_tool = next((call for call in reversed(calls) if call.status == "tool_executing"), None)
        if interrupted_tool is not None:
            interrupted_tool.status = "tool_outcome_unknown"
            run.status, run.error_code, run.error_message = "submission_unknown", "AGENT_TOOL_OUTCOME_UNKNOWN", "服务中断时工具正在处理，实际结果需要核实，系统不会重复执行"
            run.result_json = {**(run.result_json or {}), "candidate": interrupted_tool.response_json}
            _mark_step_state(session, run, "submission_unknown")
            session.commit()
            return
        pending = next((call for call in reversed(calls) if call.status in {"response_received", "ready"} and not call.usage_json.get("toolExecutionDone")), None)
        if pending is not None:
            pending_id, status = pending.id, pending.status
            session.commit()
            if status == "response_received":
                _decode_native_saved_call(settings, factory, run_id, pending_id)
            else:
                _apply_native_call(settings, factory, run_id, pending_id)
            return
        step = _next_step(session, run)
        planning = not run.plan_json
        if not planning and step is None:
            run.status, run.finished_at = "completed", time.time()
            session.commit()
            return
        role = run.role if planning else step.role
        protocol = run.limits_json["providerProtocol"]
        catalog_version = run.limits_json.get("toolCatalogVersion", LEGACY_TOOL_CATALOG_VERSION)
        model = _run_model(run)
        text_snapshot = run.limits_json.get("textProviderSnapshot")
        qwen = bool(text_snapshot and text_snapshot["providerId"] == "aliyun-qwen" and model == QWEN_MODEL)
        if model != MODEL and not qwen:
            raise AppError(403, "MODEL_NOT_AUTHORIZED", "当前仅授权deepseek-flash发起新请求；该任务的原模型、回复和费用记录继续保留")
        review = (run.result_json or {}).get("materialReview") or {}
        if (catalog_version == MATERIAL_SOP_TOOL_CATALOG_VERSION and step is not None and step.role != "media_producer"
                and review.get("stepId") == step.id and review.get("status") == "approved"):
            validation = require_step_delivery(session, run, step, project, settings)
            _complete_platform_step(session, run, step, "此岗位的成果已采用。", validation)
            run.lease_owner, run.lease_expires_at, run.updated_at = None, None, time.time()
            session.commit()
            return
        if step is not None and run.plan_json[step.index].get("kind", "produce") == "verify":
            execute_tool(session, run, step.role, "validate_story" if run.limits_json.get("workspace") == "project" else "validate_artifacts", {}, f"agent-verify-{step.id}-{project.revision}", project.revision, settings, step,
                         manage_transaction=False, execution_origin="runtime")
            if run.limits_json.get("toolCatalogVersion") in MATERIAL_CATALOG_VERSIONS:
                validation = require_step_delivery(session, run, step, project, settings)
                summary = "已核对当前保存产物的专业结构、来源、内容哈希与版本。"
                _complete_platform_step(session, run, step, summary, validation)
                run.lease_owner, run.lease_expires_at, run.updated_at = None, None, time.time()
                session.commit()
                return
        if len(calls) >= run.limits_json["maxSteps"]:
            raise AppError(409, "AGENT_STEP_LIMIT", "任务已达到确认的模型请求上限")
        provider = model_connection(session, run.owner_id, "aliyun-qwen") if qwen else session.get(TextProviderSettings, "platform")
        if provider is None or not provider.api_key_encrypted:
            raise AppError(503, "TEXT_NOT_CONFIGURED", "平台文字配置尚未完成")
        correction = _native_correction_info(session, run, step.id if step else None, calls)
        completed = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.status == "completed").order_by(AgentStep.index)).all()
        current_round = run.limits_json.get("materialContext", {}).get("scope") == "current_round"
        context = retrieve_context(session, run.project_id, run.owner_id, run.scene_id, run.branch_id, _context_targets(session, run), 16000, material_only=current_round)
        if catalog_version in MATERIAL_CATALOG_VERSIONS:
            from app.services.materials import material_followups
            followups = material_followups(session, run)
            if followups:
                context["conversationFollowups"] = followups
            canvas = require_canvas(session, run, project)
            context["materialNodes"] = [{"id": node["id"], "type": node["type"], "title": node.get("title"),
                "professionalRole": (node.get("metadata", {}).get("creativeArtifact") or {}).get("role")} for node in canvas.data_json["nodes"] if node["type"] != "agent" and (not current_round or node["id"] in {*run.target_ids_json, *run.created_ids_json})]
            if catalog_version == MATERIAL_SOP_TOOL_CATALOG_VERSION:
                from app.services.material_handoffs import adopted_material_inputs, prepare_material_handoff, read_material_handoff
                context["adoptedHandoff"] = (prepare_material_handoff(session, run, step, canvas) if step is not None
                    else adopted_material_inputs(session, run, canvas, run.target_ids_json))
                if protocol == MATERIAL_JSON_AGENT_PROTOCOL and step is not None:
                    context["adoptedHandoffArtifacts"] = read_material_handoff(session, run, step, project,
                        context["adoptedHandoff"], settings)
        direct_artifact_allowed = (protocol == MATERIAL_JSON_AGENT_PROTOCOL and step is not None
            and bool(context.get("adoptedHandoffArtifacts"))
            and run.plan_json[step.index].get("kind", "produce") == "produce" and _material_revision_request(run, step.id) is None
            and not any(receipt["tool"] in {"write_artifact", "patch_artifact"} for receipt in step.tool_results_json))
        scene_artifact_only = direct_artifact_allowed and role == "scene_designer"
        format_retry = (run.result_json or {}).get("materialJsonFormatRetries", {}).get(step.id, {}) if step is not None else {}
        material_artifact_only = (direct_artifact_allowed and role in MATERIAL_STRUCTURED_RECOVERY_ROLES
            and format_retry.get("materialArtifactOnly") is True)
        messages = (_json_messages(run, step, role, context, calls, completed, scene_artifact_only=scene_artifact_only,
            material_artifact_only=material_artifact_only)
            if is_json_protocol(protocol) else _native_messages(run, step, role, context, calls, completed))
        if qwen and not is_json_protocol(protocol):
            raise AppError(422, "QWEN_PROTOCOL_UNSUPPORTED", "千问角色任务需要结构化JSON协议")
        tools = [] if qwen else agent_tools(role, planning, protocol=protocol, model=model, catalog_version=catalog_version)
        business_tools = agent_business_tools(role, planning, catalog_version=catalog_version)
        maximum = run.limits_json["maxTokens"]
        structured_schema = (artifact_model(role, reusable_frames=True).model_json_schema(by_alias=True)
            if scene_artifact_only or material_artifact_only else None)
        request_body = qwen_request_body(messages, maximum) if qwen else agent_request_body(messages, maximum, tools, planning, settings, protocol=protocol, model=model,
            catalog_version=catalog_version, structured_response_schema=structured_schema)
        input_upper = len(json.dumps(request_body, ensure_ascii=False, separators=(",", ":")).encode()) + 2048
        call_price = text_snapshot["price"] if qwen else current_price(time.time(), model=model)
        upper = cost_micros(0, input_upper, maximum, call_price if qwen else peak_price(model=model))
        reserved = session.scalar(select(func.coalesce(func.sum(BudgetReservation.reserved_micros), 0)).where(BudgetReservation.id.in_([call.id for call in calls])))
        if sum(call.cost_micros or 0 for call in calls) + reserved + upper > run.limits_json["maxCostMicros"]:
            raise AppError(402, "AGENT_COST_LIMIT", "本次请求将超过已确认费用限制")
        _reserve(session, upper, settings)
        call = AgentCall(id=new_id(), run_id=run.id, step_id=step.id if step else None, role=role, provider="aliyun-qwen" if qwen else "deepseek", model=model,
            status="submitting", input_json={"protocol": protocol, "toolCatalogVersion": catalog_version,
                "providerEndpoint": qwen_endpoint(text_snapshot["workspaceId"], text_snapshot["region"]) if qwen else provider_endpoint(protocol, structured_response=structured_schema is not None), "businessTools": business_tools,
                "messages": messages, "tools": tools, "planning": planning, "model": model,
                **({"responseSchema": structured_schema} if structured_schema is not None
                    else {"responseSchema": agent_response_schema(planning, protocol=protocol)} if is_json_protocol(protocol) else {}),
                **({"sceneArtifactOnly": True} if scene_artifact_only else {}),
                **({"materialArtifactOnly": True} if material_artifact_only else {}),
                "requestBody": request_body,
                **({"requestTimeoutSeconds": settings.text_timeout} if protocol in THINKING_JSON_PROTOCOLS else {}),
                "maxTokens": maximum, "inputTokenUpperBound": input_upper, "price": call_price,
                **({"textProviderSnapshot": text_snapshot} if qwen else {}),
                "projectRevision": project.revision, "sceneId": run.scene_id, "providerRevision": provider.updated_at,
                "promptVersion": PROMPT_VERSION,
                **({"materialRevisionOperationId": _material_revision_request(run, step.id)["operationId"]}
                    if step and _material_revision_request(run, step.id) else {}),
                **({"limitExtensionOperationId": run.limits_json["requestLimitExtensions"][-1]["operationId"]} if run.limits_json.get("requestLimitExtensions") else {}),
                "acknowledgedUnknownCalls": [{"callId": item["callId"], "operationId": item["operationId"]} for item in run.limits_json.get("unknownRetryConfirmations", [])], **correction})
        session.add(call)
        session.add(BudgetReservation(id=call.id, purpose="agent-model", reserved_micros=upper, cost_micros=0, status="reserved",
            details_json={"runId": run.id, "projectId": run.project_id, "callId": call.id}))
        run.status, run.lease_owner, run.lease_expires_at = "running", worker_id, time.time() + settings.text_timeout + 60
        run.started_at, run.updated_at = run.started_at or time.time(), time.time()
        if step is not None:
            step.status = "running"
        key, call_id = frozen_key(session, run.owner_id, text_snapshot, settings) if qwen else decode_secret(provider.api_key_encrypted, settings), call.id
        session.commit()
    try:
        response = await complete_qwen(key, text_snapshot["workspaceId"], text_snapshot["region"], messages, maximum, settings) if qwen else await complete_agent(key, messages, maximum, tools, planning, settings, protocol=protocol, model=model,
            catalog_version=catalog_version, structured_response_schema=structured_schema)
    except TextProviderFailure as error:
        with factory() as session:
            lock_write(session)
            run, call = session.get(AgentRun, run_id), session.get(AgentCall, call_id)
            if run.lease_owner != worker_id or call.status != "submitting":
                raise AppError(409, "AGENT_LEASE_LOST", "角色任务执行权已经变化")
            if not error.unknown:
                _settle_call(session, call, 0, {})
            _record_provider_failure(call, error, key)
            call.status, call.finished_at = "submission_unknown" if error.unknown else "failed", time.time()
            run.status, run.error_code, run.error_message = call.status, error.code, error.message
            run.updated_at, run.lease_expires_at = time.time(), None
            _mark_step_state(session, run, run.status)
            session.commit()
        return
    with factory() as session:
        lock_write(session)
        run, call = session.get(AgentRun, run_id), session.get(AgentCall, call_id)
        if run.lease_owner != worker_id or call.status != "submitting":
            raise AppError(409, "AGENT_LEASE_LOST", "角色任务执行权已经变化")
        message = response.choices[0].message.model_dump(mode="json", exclude_unset=True)
        call.raw_output = json.dumps(message, ensure_ascii=False)
        call.remote_request_id, call.model, call.finished_at = response.id, response.model, time.time()
        call.input_tokens, call.output_tokens, call.total_tokens = response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens
        actual = cost_micros(response.usage.prompt_cache_hit_tokens, response.usage.prompt_cache_miss_tokens, response.usage.completion_tokens, call.input_json["price"])
        reservation = session.get(BudgetReservation, call.id)
        if call.input_tokens > call.input_json["inputTokenUpperBound"] or actual > reservation.reserved_micros:
            call.status, call.usage_json = "submission_unknown", response.usage.model_dump()
            run.status, run.error_code, run.error_message = "submission_unknown", "AGENT_USAGE_OUT_OF_BOUNDS", "供应商用量超过预留上界，请核实原请求，系统不会自动重发"
            _mark_step_state(session, run, "submission_unknown")
            session.commit()
            return
        _settle_call(session, call, actual, {**response.usage.model_dump(), "price": call.input_json["price"],
            **({"providerFinishReason": response.choices[0].finish_reason} if is_json_protocol(protocol) else {"nativeFinishReason": response.choices[0].finish_reason})})
        tool_calls = message.get("tool_calls") or []
        single = tool_calls[0] if len(tool_calls) == 1 and isinstance(tool_calls[0], dict) else None
        if protocol in SUPPORTED_NATIVE_PROTOCOLS and single and isinstance(single.get("id"), str) and single["id"]:
            function = single.get("function")
            call.usage_json = {**call.usage_json, "toolCallId": single["id"], "nativeFunction": function.get("name") if isinstance(function, dict) else None}
        call.status = "response_received"
        session.commit()
    _decode_native_saved_call(settings, factory, run_id, call_id)


async def _execute_platform(settings: Settings, factory: sessionmaker, run_id: str, worker_id: str) -> None:
    with factory() as session:
        run = session.get(AgentRun, run_id)
        protocol = run.limits_json.get("providerProtocol") if run is not None else None
        if protocol is not None and protocol not in SUPPORTED_PLATFORM_PROTOCOLS:
            raise AppError(422, "AGENT_PROTOCOL_UNSUPPORTED", "当前任务冻结的请求协议尚未支持，请保留原记录")
        native = protocol in SUPPORTED_PLATFORM_PROTOCOLS
    if native:
        await _execute_native_platform(settings, factory, run_id, worker_id)
    else:
        await _execute_legacy_platform(settings, factory, run_id, worker_id)


async def _execute_legacy_platform(settings: Settings, factory: sessionmaker, run_id: str, worker_id: str) -> None:
    with factory() as session:
        lock_write(session)
        run = session.get(AgentRun, run_id)
        if run is None or run.status != "queued" or run.executor != "platform":
            session.rollback()
            return
        if run.stop_requested:
            run.status, run.finished_at = "stopped", time.time()
            _mark_step_state(session, run, "stopped")
            session.commit()
            return
        owner = session.get(User, run.owner_id)
        require_text_admin(owner, settings)
        project = require_project(session, run.project_id, run.owner_id)
        if project.revision != run.project_revision:
            run.status, run.error_code, run.error_message = "conflict", "REVISION_CONFLICT", "项目已有人工编辑，请读取最新版本后恢复任务"
            _mark_step_state(session, run, "conflict")
            session.commit()
            return
        provider = session.get(TextProviderSettings, "platform")
        if provider is None or not provider.api_key_encrypted:
            raise AppError(503, "TEXT_NOT_CONFIGURED", "平台文字配置尚未完成")
        step = _next_step(session, run)
        planning = not run.plan_json
        if not planning and step is None:
            run.status, run.finished_at = "completed", time.time()
            session.commit()
            return
        role = run.role if planning else step.role
        if step is not None and run.plan_json[step.index].get("kind", "produce") == "verify":
            operation_id = f"agent-verify-{step.id}-{project.revision}"
            execute_tool(session, run, step.role, "validate_story" if run.limits_json.get("workspace") == "project" else "validate_artifacts", {}, operation_id, project.revision, settings, step,
                         manage_transaction=False, execution_origin="runtime")
        context = retrieve_context(session, run.project_id, run.owner_id, run.scene_id, run.branch_id, _context_targets(session, run), 16000)
        previous_calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id).order_by(AgentCall.created_at)).all()
        if len(previous_calls) >= run.limits_json["maxSteps"]:
            raise AppError(409, "AGENT_STEP_LIMIT", "任务已达到确认的步骤上限，已停止新增模型调用")
        completed_steps = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.status == "completed").order_by(AgentStep.index)).all()
        messages = _role_messages(run, step, role, context, previous_calls, completed_steps)
        repair = None
        repair_id = (run.result_json or {}).get("jsonRepairOf")
        if repair_id:
            original_call = session.get(AgentCall, repair_id)
            repair = _checked_json_repair(session, run, original_call, project.revision) if original_call else None
            if repair is None:
                raise AppError(409, "AGENT_JSON_REPAIR_UNAVAILABLE", "原模型调用不满足一次格式修复条件")
            messages = _repair_messages(messages, original_call, repair)
        input_upper = sum(len(message["content"].encode()) for message in messages) + 1024
        maximum = run.limits_json["maxTokens"]
        upper = cost_micros(0, input_upper, maximum, {"cacheHitMicrosPerMillion": 40000, "cacheMissMicrosPerMillion": 2000000, "outputMicrosPerMillion": 8000000})
        spent = sum(call.cost_micros or 0 for call in previous_calls)
        if spent + upper > run.limits_json["maxCostMicros"]:
            raise AppError(402, "AGENT_COST_LIMIT", "本次模型请求将超过任务已确认的费用限制")
        _reserve(session, upper, settings)
        call = AgentCall(id=new_id(), run_id=run.id, step_id=step.id if step else None, role=role, provider="deepseek", model=MODEL,
                         status="submitting", input_json={"messages": messages, "maxTokens": maximum, "inputTokenUpperBound": input_upper,
                         "price": current_price(time.time()), "projectRevision": project.revision, "sceneId": run.scene_id,
                         "providerRevision": provider.updated_at, "promptVersion": PROMPT_VERSION, "planning": planning,
                         **({key: value for key, value in repair.items() if key != "extraFieldPaths"} if repair else {})})
        session.add(call)
        session.add(BudgetReservation(id=call.id, purpose="agent-model", reserved_micros=upper, cost_micros=0, status="reserved", details_json={"runId": run.id, "projectId": run.project_id, "callId": call.id}))
        run.status, run.lease_owner, run.lease_expires_at = "running", worker_id, time.time() + settings.text_timeout + 60
        run.started_at, run.updated_at = run.started_at or time.time(), time.time()
        if step is not None:
            step.status = "running"
        key, call_id = decode_secret(provider.api_key_encrypted, settings), call.id
        session.commit()
    try:
        response = await complete_text(key, messages, maximum, settings)
    except TextProviderFailure as error:
        with factory() as session:
            lock_write(session)
            run, call = session.get(AgentRun, run_id), session.get(AgentCall, call_id)
            if run.lease_owner != worker_id or call.status != "submitting":
                raise AppError(409, "AGENT_LEASE_LOST", "角色任务执行权已变化")
            if not error.unknown:
                _settle_call(session, call, 0, {})
            _record_provider_failure(call, error, key)
            call.status = "submission_unknown" if error.unknown else "failed"
            call.finished_at = time.time()
            run.status, run.error_code, run.error_message = call.status, error.code, error.message
            _mark_step_state(session, run, run.status)
            run.finished_at, run.updated_at, run.lease_expires_at = time.time(), time.time(), None
            session.commit()
        return
    with factory() as session:
        lock_write(session)
        run, call = session.get(AgentRun, run_id), session.get(AgentCall, call_id)
        if run.lease_owner != worker_id or call.status != "submitting":
            raise AppError(409, "AGENT_LEASE_LOST", "角色任务执行权已变化")
        actual = cost_micros(response.usage.prompt_cache_hit_tokens, response.usage.prompt_cache_miss_tokens, response.usage.completion_tokens, call.input_json["price"])
        call.remote_request_id, call.model = response.id, response.model
        call.raw_output = response.choices[0].message.content or ""
        call.input_tokens, call.output_tokens, call.total_tokens = response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens
        call.finished_at = time.time()
        reservation = session.get(BudgetReservation, call.id)
        if call.input_tokens > call.input_json["inputTokenUpperBound"] or actual > reservation.reserved_micros:
            call.status, call.usage_json = "submission_unknown", response.usage.model_dump()
            run.status, run.error_code, run.error_message = "submission_unknown", "AGENT_USAGE_OUT_OF_BOUNDS", "供应商上报用量超出预留上界，请核实原请求，系统不会自动重发"
            _mark_step_state(session, run, "submission_unknown")
            session.commit()
            return
        _settle_call(session, call, actual, {**response.usage.model_dump(), "price": call.input_json["price"]})
        if response.choices[0].finish_reason != "stop" or response.choices[0].message.tool_calls:
            call.status = "failed"
            run.status, run.error_code, run.error_message = "failed", "AGENT_RESULT_INCOMPLETE", "模型输出不完整，实际Token及费用已保存"
            _mark_step_state(session, run, "failed")
            session.commit()
            return
        try:
            decision_schema = PlanningDecision if call.input_json["planning"] else StepDecision
            decision = decision_schema.model_validate_json(call.raw_output)
        except ValidationError:
            call.status = "failed"
            session.commit()
            lock_write(session)
            session.refresh(run)
            if run.stop_requested:
                run.status, run.finished_at, run.lease_expires_at = "stopped", time.time(), None
                _mark_step_state(session, run, "stopped")
                session.commit()
                return
            repair = _checked_json_repair(session, run, call, require_project(session, run.project_id, run.owner_id).revision)
            if repair is not None and not run.stop_requested:
                run.result_json = {**(run.result_json or {}), "jsonRepairOf": call.id}
                run.status, run.error_code, run.error_message = "queued", "AGENT_JSON_REPAIRING", "已保存真实调用和费用，正在执行最多一次JSON格式修复，不重复执行历史工具"
                run.lease_expires_at = None
                _mark_step_state(session, run, "queued")
            else:
                run.status, run.error_code, run.error_message = "failed", "AGENT_RESULT_INVALID", "模型结果未通过工具结构校验，原始输出和实际费用已保存"
                _mark_step_state(session, run, "failed")
            session.commit()
            return
        call.response_json, call.status = decision.model_dump(by_alias=True), "ready"
        if call.input_json.get("jsonRepairAttempt", 0):
            if payload_digest(call.response_json) != call.input_json["repairExpectedSha256"]:
                call.status = "failed"
                run.status, run.error_code, run.error_message = "failed", "AGENT_JSON_REPAIR_CHANGED", "格式修复改变了原有业务内容，候选及真实费用保留，本次不会执行工具"
                _mark_step_state(session, run, "failed")
                session.commit()
                return
            run.result_json = {key: value for key, value in (run.result_json or {}).items() if key != "jsonRepairOf"}
            run.error_code, run.error_message = None, None
        run.updated_at = time.time()
        session.commit()
        lock_write(session)
        session.refresh(run)
        if run.stop_requested:
            run.status, run.finished_at = "stopped", time.time()
            _mark_step_state(session, run, "stopped")
            run.result_json = {"candidate": call.response_json, "automaticWriteStopped": True}
            session.commit()
            return
        project = require_project(session, run.project_id, run.owner_id)
        if project.revision != call.input_json["projectRevision"] or project.revision != run.project_revision:
            run.status, run.error_code, run.error_message = "conflict", "REVISION_CONFLICT", "模型运行期间已有人工编辑，候选已保存，请读取最新版本后恢复"
            _mark_step_state(session, run, "conflict")
            run.result_json = {"candidate": call.response_json}
            session.commit()
            return
        step = session.get(AgentStep, call.step_id) if call.step_id else None
        if planning:
            if decision.action != "plan" or not decision.plan or any(item.role != run.role for item in decision.plan if run.role != "director"):
                raise AppError(422, "AGENT_PLAN_INVALID", "角色计划未符合授权职责或缺少步骤")
            if run.limits_json.get("workspace") == "materials" and any(item.role not in MATERIAL_ROLES for item in decision.plan):
                raise AppError(403, "AGENT_PLAN_ROLE_NOT_ALLOWED", "素材创作计划仅允许素材岗位")
            for index, item in enumerate(decision.plan):
                if len(set(item.depends_on)) != len(item.depends_on) or any(type(dependency) is not int or dependency < 0 or dependency >= index for dependency in item.depends_on):
                    raise AppError(422, "AGENT_PLAN_DEPENDENCY_INVALID", "计划依赖必须唯一且引用前面的步骤")
                if item.kind == "verify" and (not item.depends_on or any(decision.plan[dependency].kind != "produce" for dependency in item.depends_on)):
                    raise AppError(422, "AGENT_PLAN_DEPENDENCY_INVALID", "验证步骤必须明确依赖前面的产物步骤")
            run.plan_json = [item.model_dump(by_alias=True) for item in decision.plan]
            for index, item in enumerate(decision.plan):
                session.add(AgentStep(run_id=run.id, index=index, role=item.role, title=item.title, instruction=item.instruction, status="queued", tool_results_json=[]))
            run.status = "queued"
        elif decision.action == "complete":
            validation = require_step_delivery(session, run, step, project, settings)
            step.status, step.result_json = "completed", {"summary": decision.summary, "verifiedToolCount": len(step.tool_results_json), "artifactValidation": validation}
            session.flush()
            run.status = "completed" if _next_step(session, run) is None else "queued"
            run.result_json = {"summary": decision.summary, "lastStepId": step.id, "actualToolResults": step.tool_results_json}
            if run.status == "completed":
                run.finished_at = time.time()
        elif decision.action == "tool" and step is not None and decision.tool:
            session.commit()
            output = execute_tool(session, run, step.role, decision.tool, decision.arguments, f"agent-{call.id}", run.project_revision, settings, step)
            if run.status == "running":
                run.status = "queued"
            run.result_json = {"lastTool": decision.tool, **output}
        else:
            raise AppError(422, "AGENT_DECISION_INVALID", "当前角色返回的步骤类型无效")
        session.commit()


async def agent_worker_loop(settings: Settings, factory: sessionmaker, stop_event: asyncio.Event) -> None:
    worker_id = str(uuid.uuid4())
    while not stop_event.is_set():
        with factory() as session:
            lock_write(session)
            expired = session.scalars(select(AgentRun).where(AgentRun.status.in_(["running", "stopping"]), or_(AgentRun.lease_expires_at.is_(None), AgentRun.lease_expires_at < time.time()))).all()
            for run in expired:
                pending = session.scalars(select(AgentCall).where(AgentCall.run_id == run.id, AgentCall.status == "submitting")).all()
                for call in pending:
                    call.status = "submission_unknown"
                if run.executor == "codex":
                    run.status = "stopped" if run.stop_requested else "waiting_connection"
                elif pending:
                    run.status, run.error_code, run.error_message = "submission_unknown", "AGENT_SUBMISSION_UNKNOWN", "服务重启后原模型请求结果待核实，保留预留且不会自动重发"
                elif run.stop_requested:
                    run.status = "stopped"
                else:
                    run.status = "queued"
                _mark_step_state(session, run, run.status)
                run.updated_at = time.time()
            identifier = session.scalar(select(AgentRun.id).where(AgentRun.executor == "platform", AgentRun.status == "queued").order_by(AgentRun.created_at).limit(1))
            session.commit()
        if identifier is not None:
            try:
                await _execute_platform(settings, factory, identifier, worker_id)
            except AppError as error:
                with factory() as session:
                    lock_write(session)
                    run = session.get(AgentRun, identifier)
                    if run is not None and run.status not in TERMINAL:
                        run.status = "conflict" if error.code in {"REVISION_CONFLICT", "TARGET_CHANGED", "AGENT_ARTIFACT_CHANGED", "AGENT_ARTIFACT_NOT_READY", "AGENT_DEPENDENCY_NOT_READY", "AGENT_JSON_REPAIR_STALE"} else "failed"
                        run.error_code, run.error_message, run.updated_at = error.code, error.message, time.time()
                        _mark_step_state(session, run, run.status)
                        session.commit()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.3)
        except TimeoutError:
            pass


def claim_codex_run(session: Session, owner_id: str, project_id: str, connection_id: str) -> dict | None:
    lock_write(session)
    check_codex_connection(session, owner_id, project_id, connection_id)
    require_project(session, project_id, owner_id)
    run = session.scalar(select(AgentRun).where(AgentRun.owner_id == owner_id, AgentRun.project_id == project_id, AgentRun.executor == "codex", AgentRun.stop_requested == 0,
        or_(AgentRun.status.in_(["queued", "waiting_connection"]), (AgentRun.status == "running") & (AgentRun.lease_owner == connection_id))).order_by(AgentRun.created_at).limit(1))
    if run is None:
        session.commit()
        return None
    project = require_project(session, project_id, owner_id)
    if project.revision != run.project_revision:
        run.status, run.error_code, run.error_message = "conflict", "REVISION_CONFLICT", "连接恢复时项目已发生人工编辑，请读取最新版本后恢复"
        _mark_step_state(session, run, "conflict")
        session.commit()
        return None
    run.status, run.lease_owner, run.lease_expires_at = "running", connection_id, time.time() + 90
    run.started_at, run.updated_at = run.started_at or time.time(), time.time()
    if not run.plan_json:
        run.plan_json = [{"role": run.role, "title": "本人Codex项目任务", "instruction": run.goal}]
        session.add(AgentStep(run_id=run.id, index=0, role=run.role, title="本人Codex项目任务", instruction=run.goal, status="running", tool_results_json=[]))
    else:
        _mark_step_state(session, run, "running")
    session.commit()
    context = retrieve_context(session, project_id, owner_id, run.scene_id, run.branch_id, _context_targets(session, run), 16000)
    mode = run.limits_json.get("deliveryMode", "legacy")
    catalog_version = run.limits_json.get("toolCatalogVersion", LEGACY_TOOL_CATALOG_VERSION)
    instructions = codex_prompt(run.role, mode, catalog_version=catalog_version) + "\n" + json.dumps({"goal": run.goal, "deliveryMode": mode, "artifactContractVersion": run.limits_json.get("artifactContractVersion"), "toolCatalogVersion": catalog_version, "context": context, "authorizedTargetIds": run.target_ids_json, "createdNodeIds": run.created_ids_json, "projectRevision": run.project_revision}, ensure_ascii=False)
    if run.limits_json.get("conversationId"):
        from app.conversation_models import ConversationMessage
        messages = session.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == run.limits_json["conversationId"]).order_by(ConversationMessage.sequence.desc()).limit(20)).all()
        instructions += "\n" + json.dumps({"editingContext": run.limits_json.get("editingContext"), "conversationHistory": [{"role": item.role, "content": item.content, "runId": item.run_id, "status": item.context_json.get("status")} for item in reversed(messages)],
                                           "currentRunId": run.id, "conversationId": run.limits_json["conversationId"]}, ensure_ascii=False)
    return {"run": run_view(session, run), "instructions": instructions, "tools": tool_catalog(run.role, catalog_version), "skills": run.limits_json.get("skills", []), "usageBaseline": run.limits_json.get("codexThreadUsageBaseline")}


def execute_codex_tool(session: Session, owner_id: str, run_id: str, tool_name: str, arguments: dict, operation_id: str, expected_revision: int, connection_id: str | None = None) -> dict:
    run = session.get(AgentRun, run_id)
    if run is None or run.owner_id != owner_id or run.executor != "codex":
        raise AppError(404, "AGENT_RUN_NOT_FOUND", "本机角色任务不存在")
    step = _next_step(session, run)
    if step is None:
        raise AppError(409, "AGENT_STEP_MISSING", "本机任务没有可执行步骤")
    session.commit()
    result = execute_tool(session, run, step.role, tool_name, arguments, operation_id, expected_revision, get_settings(), step, connection_id=connection_id)
    run.lease_expires_at = time.time() + 90
    session.commit()
    return result


def update_codex_run(session: Session, owner_id: str, run_id: str, connection_id: str, event_type: str, thread_id: str | None = None, turn_id: str | None = None, usage: dict | None = None, error_code: str | None = None) -> dict:
    lock_write(session)
    run = session.get(AgentRun, run_id)
    if run is None or run.owner_id != owner_id or run.executor != "codex" or run.lease_owner != connection_id:
        raise AppError(403, "AGENT_CONNECTION_MISMATCH", "连接没有本次任务执行权")
    check_codex_connection(session, owner_id, run.project_id, connection_id)
    if thread_id is not None and run.thread_id is not None and thread_id != run.thread_id:
        raise AppError(409, "CODEX_THREAD_MISMATCH", "本机事件与任务保存的线程不一致")
    if turn_id is not None and run.turn_id is not None and turn_id != run.turn_id:
        raise AppError(409, "CODEX_TURN_MISMATCH", "本机事件与任务保存的回合不一致")
    if thread_id is not None:
        run.thread_id = thread_id
    if turn_id is not None:
        run.turn_id = turn_id
    if event_type == "disconnected" and run.status not in TERMINAL:
        run.status = "stopped" if run.stop_requested else "waiting_connection"
        _mark_step_state(session, run, run.status)
    elif event_type == "interrupted" and run.status not in TERMINAL:
        run.status = "stopped"
        run.stop_requested = 1
        step = _next_step(session, run)
        if step is not None:
            step.status = "stopped"
    elif event_type == "failed" and run.status not in TERMINAL:
        run.status, run.error_code, run.error_message = "failed", error_code or "CODEX_TURN_FAILED", "本人Codex回合未能完成，已执行结果继续保留"
        _mark_step_state(session, run, "failed")
    if usage is not None:
        try:
            validated_usage = ReportedCodexUsage.model_validate(usage).model_dump(exclude_unset=True)
        except ValidationError as error:
            raise AppError(422, "CODEX_USAGE_INVALID", "本机上报用量没有通过结构校验") from error
        run.result_json = {**(run.result_json or {}), "reportedCodexUsage": validated_usage}
    run.updated_at, run.lease_expires_at = time.time(), time.time() + 90
    session.commit()
    return run_view(session, run)


def finish_codex_run(session: Session, owner_id: str, run_id: str, result: dict, thread_id: str | None, turn_id: str | None, usage: dict | None, connection_id: str) -> dict:
    lock_write(session)
    run = session.get(AgentRun, run_id)
    if run is None or run.owner_id != owner_id or run.executor != "codex" or run.lease_owner != connection_id:
        raise AppError(403, "AGENT_CONNECTION_MISMATCH", "连接没有本次任务执行权")
    check_codex_connection(session, owner_id, run.project_id, connection_id)
    if run.thread_id is not None and thread_id != run.thread_id:
        raise AppError(409, "CODEX_THREAD_MISMATCH", "完成结果与任务保存的线程不一致")
    if run.turn_id is not None and turn_id != run.turn_id:
        raise AppError(409, "CODEX_TURN_MISMATCH", "完成结果与任务保存的回合不一致")
    if usage is not None:
        try:
            usage = ReportedCodexUsage.model_validate(usage).model_dump(exclude_unset=True)
        except ValidationError as error:
            raise AppError(422, "CODEX_USAGE_INVALID", "本机上报用量没有通过结构校验") from error
    project = require_project(session, run.project_id, owner_id)
    if run.status not in {"completed", "stopped", "failed", "conflict", "submission_unknown"} and project.revision != run.project_revision:
        run.status, run.error_code, run.error_message = "conflict", "REVISION_CONFLICT", "本机回合完成前项目已有人工编辑，真实回复和用量已保存为候选，请读取最新版本后恢复"
        _mark_step_state(session, run, "conflict")
    candidate = {"finalText": str(result.get("finalText", ""))[:20000], "threadId": thread_id, "turnId": turn_id,
                 "projectRevision": run.project_revision, "currentRevision": project.revision}
    existing_call = session.scalar(select(AgentCall).where(AgentCall.run_id == run.id, AgentCall.provider == "personal-codex", AgentCall.remote_request_id == turn_id)) if turn_id else None
    if existing_call is not None:
        if existing_call.response_json != {"finalText": str(result.get("finalText", ""))[:20000]}:
            raise AppError(409, "CODEX_RESULT_CONFLICT", "同一Codex回合已经登记其他结果")
        if run.status in {"conflict", "submission_unknown"} and ((run.result_json or {}).get("candidate") != candidate or not (run.result_json or {}).get("automaticWriteStopped")):
            run.result_json = {**(run.result_json or {}), "candidate": candidate, "automaticWriteStopped": True}
            run.finished_at, run.updated_at = None, time.time()
            current_step = _next_step(session, run)
            if current_step is not None and current_step.status != run.status:
                _mark_step_state(session, run, run.status)
        session.commit()
        return run_view(session, run)
    if run.status == "completed":
        raise AppError(409, "AGENT_RUN_TERMINAL", "该任务已经完成，不能登记新的回合")
    step = _next_step(session, run)
    actual_tools = step.tool_results_json if step else []
    desired = result.get("status", "completed")
    delivery_validation = None
    if run.status in {"waiting_confirmation", "stopped", "failed", "conflict", "submission_unknown"}:
        pass
    elif run.stop_requested or desired == "interrupted":
        run.status = "stopped"
    elif desired == "failed":
        run.status, run.error_code, run.error_message = "failed", "CODEX_TURN_FAILED", "本人Codex回合未能完成，实际工具结果继续保留"
    elif not actual_tools and not run.limits_json.get("conversationId"):
        run.status, run.error_code, run.error_message = "failed", "AGENT_NO_TOOL_RESULTS", "本回合没有实际项目工具结果，回复已保存为候选"
    else:
        try:
            delivery_validation = require_step_delivery(session, run, step, project, get_settings())
        except AppError as error:
            run.status, run.error_code, run.error_message = "failed", error.code, error.message
        else:
            run.status = "completed"
    run.thread_id, run.turn_id = thread_id or run.thread_id, turn_id or run.turn_id
    run.result_json = {**(run.result_json or {}), "finalText": str(result.get("finalText", ""))[:20000], "actualToolResults": actual_tools, "personalAccountCost": None, "usage": usage}
    if delivery_validation is not None:
        run.result_json["artifactValidation"] = delivery_validation
    if run.status in {"conflict", "submission_unknown", "failed"}:
        run.result_json.update(candidate=candidate, automaticWriteStopped=True)
    run.finished_at, run.updated_at, run.lease_expires_at = (None if run.status in {"waiting_confirmation", "conflict", "submission_unknown"} else time.time()), time.time(), None
    if step is not None:
        step.status, step.result_json = ("completed" if run.status == "completed" else run.status), run.result_json
    counts = usage or {}
    session.add(AgentCall(run_id=run.id, step_id=step.id if step else None, role=run.role, provider="personal-codex", model=str(counts.get("model") or "unknown")[:100], status=run.status,
                          input_json={"threadId": run.thread_id, "turnId": run.turn_id}, response_json={"finalText": str(result.get("finalText", ""))[:20000]}, remote_request_id=run.turn_id,
                          input_tokens=counts.get("inputTokens"), output_tokens=counts.get("outputTokens"), total_tokens=counts.get("totalTokens"), cost_micros=None, usage_json=counts, finished_at=time.time()))
    if run.limits_json.get("conversationId"):
        from app.services.conversations import complete_conversation_turn
        complete_conversation_turn(session, run)
    session.commit()
    return run_view(session, run)
