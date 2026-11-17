import asyncio
import copy
import time
import uuid

from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import decode_secret
from app.models import Budget, Canvas, Operation, Project, TextGeneration, TextProviderSettings, TextUsageLedger, User
from app.services.generation import _canvas_composition, digest
from app.services.projects import iso_timestamp, lock_write, record_project_version, require_project
from app.services.prompts.text import PROMPT_VERSION
from app.services.text_provider import MODEL, RESULTS, TITLES, Completion, TextProviderFailure, complete_text, cost_micros, current_price, task_instructions


def is_text_admin(user: User, settings: Settings) -> bool:
    return bool(settings.text_admin_email) and user.email.casefold() == settings.text_admin_email.strip().casefold()


def require_text_admin(user: User, settings: Settings) -> None:
    if not is_text_admin(user, settings):
        raise AppError(402, "TEXT_BILLING_NOT_CONFIGURED", "平台Token销售计费策略尚未配置，当前仅开放管理员开发验证")


def text_view(job: TextGeneration) -> dict:
    usage = job.usage_json or {}
    return {
        "id": job.id, "projectId": job.project_id, "sceneId": job.scene_id, "sourceNodeId": job.source_node_id,
        "outputNodeId": job.output_node_id, "task": job.task, "projectRevision": job.project_revision,
        "status": job.status, "input": job.input_json, "result": job.result_json, "outputText": job.output_text,
        "partialText": job.partial_text, "provider": job.provider, "model": job.model,
        "remoteRequestId": job.remote_request_id, "inputTokens": job.input_tokens, "outputTokens": job.output_tokens,
        "totalTokens": job.total_tokens, "cacheHitTokens": usage.get("prompt_cache_hit_tokens", 0),
        "cacheMissTokens": usage.get("prompt_cache_miss_tokens", 0), "usage": usage,
        "costMicros": job.cost_micros if usage.get("settled") else None, "reservedCostMicros": job.reserved_cost_micros,
        "billingPolicyConfigured": False, "qualityIssues": usage.get("qualityIssues", []),
        "appliedRevision": job.applied_revision, "stopRequested": bool(job.stop_requested),
        "error": {"code": job.error_code, "message": job.error_message} if job.error_code else None,
        "createdAt": iso_timestamp(job.created_at), "updatedAt": iso_timestamp(job.updated_at),
        "startedAt": iso_timestamp(job.started_at) if job.started_at else None,
        "finishedAt": iso_timestamp(job.finished_at) if job.finished_at else None,
    }


def require_text_job(session: Session, identifier: str, owner_id: str) -> TextGeneration:
    job = session.get(TextGeneration, identifier)
    if job is None or job.owner_id != owner_id:
        raise AppError(404, "TEXT_GENERATION_NOT_FOUND", "文本任务不存在")
    require_project(session, job.project_id, owner_id)
    return job


def _reserve(session: Session, amount: int, settings: Settings) -> None:
    result = session.execute(update(Budget).where(
        Budget.id == "global",
        (Budget.spent_cents + Budget.reserved_cents) * 10000 + Budget.text_spent_micros + Budget.text_reserved_micros + amount <= Budget.limit_cents * 10000,
        (Budget.spent_cents + Budget.reserved_cents) * 10000 + Budget.text_spent_micros + Budget.text_reserved_micros + amount <= settings.budget_cents * 10000,
    ).values(text_reserved_micros=Budget.text_reserved_micros + amount))
    if result.rowcount != 1:
        raise AppError(402, "BUDGET_EXCEEDED", "本次文本请求将超过累计验证预算，已停止新增收费请求")


def _settle(session: Session, job: TextGeneration, actual: int, usage: dict | None = None) -> None:
    if job.usage_json.get("settled"):
        return
    if actual < 0 or actual > job.reserved_cost_micros:
        raise AppError(409, "TEXT_COST_OUT_OF_BOUNDS", "供应商费用超出已保存的预留上界，原预留继续保留并等待核实")
    changed = session.execute(update(Budget).where(Budget.id == "global", Budget.text_reserved_micros >= job.reserved_cost_micros).values(
        text_reserved_micros=Budget.text_reserved_micros - job.reserved_cost_micros,
        text_spent_micros=Budget.text_spent_micros + actual,
    ))
    if changed.rowcount != 1:
        raise AppError(409, "TEXT_BUDGET_INCONSISTENT", "文本费用预留与账目不一致，已终止结算")
    job.reserved_cost_micros = 0
    job.cost_micros = actual
    job.usage_json = {**(usage or {}), "settled": True}


def create_text_job(session: Session, project_id: str, user: User, payload: dict, settings: Settings) -> TextGeneration:
    require_text_admin(user, settings)
    lock_write(session)
    project = require_project(session, project_id, user.id)
    operation = session.scalar(select(Operation).where(Operation.owner_id == user.id, Operation.project_id == project_id, Operation.operation_id == payload["operationId"]))
    if operation is not None:
        if operation.kind != "text.generate" or operation.payload_hash != digest(payload):
            raise AppError(409, "OPERATION_CONFLICT", "该操作标识已用于不同操作")
        return require_text_job(session, operation.response_json["textGenerationId"], user.id)
    if project.revision != payload["expectedRevision"]:
        raise AppError(409, "PROJECT_REVISION_CONFLICT", "项目已有新编辑，请保存并重新提交文本任务")
    provider = session.get(TextProviderSettings, "platform")
    if provider is None or not provider.api_key_encrypted:
        raise AppError(503, "TEXT_NOT_CONFIGURED", "平台管理员尚未配置DeepSeek API Key")
    _, source_digest, composition = _canvas_composition(session, project, payload["sceneId"], payload["sourceNodeId"], payload["prompt"], payload["outputNodeId"], generation_mode="text", output_type="text")
    maximum = payload.get("maxTokens") or settings.text_max_output_tokens
    if maximum > settings.text_max_output_tokens:
        raise AppError(422, "TEXT_OUTPUT_LIMIT", "本次输出Token上限超过平台配置")
    messages = [{"role": "system", "content": task_instructions(payload["task"])}, {"role": "user", "content": composition["prompt"]}]
    input_upper_bound = sum(len(item["content"].encode("utf-8")) for item in messages) + 1024
    upper_price = {"cacheHitMicrosPerMillion": 40000, "cacheMissMicrosPerMillion": 2000000, "outputMicrosPerMillion": 8000000}
    reserved = cost_micros(0, input_upper_bound, maximum, upper_price)
    if reserved > settings.max_generation_cents * 10000:
        raise AppError(422, "TEXT_COST_LIMIT", "本次文本费用上界超过单次最高费用")
    _reserve(session, reserved, settings)
    job = TextGeneration(owner_id=user.id, project_id=project_id, scene_id=payload["sceneId"], source_node_id=payload["sourceNodeId"], output_node_id=payload["outputNodeId"], operation_id=payload["operationId"], task=payload["task"], project_revision=project.revision, source_digest=source_digest, input_json={**composition, "request": payload, "messages": messages, "maxTokens": maximum, "maxAttempts": 1, "promptVersion": PROMPT_VERSION, "inputTokenUpperBound": input_upper_bound, "providerRevision": provider.updated_at, "price": current_price(time.time())}, provider="deepseek", model=MODEL, reserved_cost_micros=reserved, status="queued", usage_json={}, events_json=[])
    session.add(job)
    session.flush()
    session.add(Operation(owner_id=user.id, project_id=project_id, operation_id=payload["operationId"], kind="text.generate", payload_hash=digest(payload), response_json={"textGenerationId": job.id}))
    session.commit()
    return job


def _apply_output(session: Session, job: TextGeneration, project: Project) -> None:
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.scene_id == job.scene_id, Canvas.owner_id == job.owner_id))
    if canvas is None:
        raise AppError(409, "TEXT_OUTPUT_MISSING", "原剧情画布已移除，文本结果继续保存在任务记录中")
    data = copy.deepcopy(canvas.data_json)
    target = next((node for node in data["nodes"] if node["id"] == job.output_node_id and node.get("type") == "text"), None)
    if target is None or target["id"] == job.source_node_id:
        raise AppError(409, "TEXT_OUTPUT_MISSING", "原文字结果节点已移除或类型已变化，文本结果继续保留")
    target["metadata"] = {**target.get("metadata", {}), "content": job.output_text, "status": "success", "pendingResult": False, "textGenerationId": job.id, "result": job.result_json}
    canvas.data_json = data
    canvas.updated_at = time.time()
    project.revision += 1
    project.updated_at = time.time()
    job.applied_revision = project.revision
    job.status = "ready"
    job.error_code = None
    job.error_message = None
    session.flush()
    record_project_version(session, project)


def apply_text_job(session: Session, identifier: str, owner_id: str, expected_revision: int, operation_id: str) -> TextGeneration:
    lock_write(session)
    job = require_text_job(session, identifier, owner_id)
    payload = {"textGenerationId": identifier, "expectedRevision": expected_revision}
    operation = session.scalar(select(Operation).where(Operation.owner_id == owner_id, Operation.project_id == job.project_id, Operation.operation_id == operation_id))
    if operation is not None:
        if operation.kind != "text.apply" or operation.payload_hash != digest(payload):
            raise AppError(409, "OPERATION_CONFLICT", "该操作标识已用于不同操作")
        return job
    if job.result_json is None or job.output_text is None:
        raise AppError(409, "TEXT_NOT_READY", "当前任务没有可采用的有效文本结果")
    if job.applied_revision is None:
        project = require_project(session, job.project_id, owner_id)
        if project.revision != expected_revision:
            raise AppError(409, "PROJECT_REVISION_CONFLICT", "项目已有新编辑，请重新读取后采用文本")
        _apply_output(session, job, project)
    session.add(Operation(owner_id=owner_id, project_id=job.project_id, operation_id=operation_id, kind="text.apply", payload_hash=digest(payload), response_json={"textGenerationId": job.id}))
    session.commit()
    return job


def stop_text_job(session: Session, identifier: str, owner_id: str) -> TextGeneration:
    lock_write(session)
    job = require_text_job(session, identifier, owner_id)
    if job.status == "queued":
        _settle(session, job, 0)
        job.status = "interrupted"
        job.finished_at = time.time()
    if job.status in {"queued", "submitting", "running", "interrupted"}:
        job.stop_requested = 1
    job.updated_at = time.time()
    session.commit()
    return job


def resume_text_job(session: Session, identifier: str, owner_id: str) -> TextGeneration:
    job = require_text_job(session, identifier, owner_id)
    if job.status in {"ready", "conflict", "queued", "running", "submitting"}:
        return job
    raise AppError(409, "TEXT_NOT_RESUMABLE", "该同步文本请求无法继续查询；请保留原任务核实费用，再以新任务明确重新生成")


def _format_result(task: str, parsed: dict) -> str:
    if task == "plot":
        return "分镜情节\n" + "\n".join(f"镜头 {shot['no']}｜{shot['text']}" for shot in parsed["shots"])
    if task == "shot":
        return f"单镜提示词\n镜头 {parsed['no']}｜{parsed['text']}"
    return f"{TITLES[task]}\n{parsed['text']}" if task in TITLES else parsed["text"]


def _save_completion(session: Session, job: TextGeneration, response: Completion) -> None:
    usage = response.usage.model_dump()
    actual = cost_micros(response.usage.prompt_cache_hit_tokens, response.usage.prompt_cache_miss_tokens, response.usage.completion_tokens, job.input_json["price"])
    job.remote_request_id = response.id
    job.input_tokens, job.output_tokens, job.total_tokens = response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens
    job.partial_text = response.choices[0].message.content or ""
    _settle(session, job, actual, {**usage, "price": job.input_json["price"], "costEstimate": True})
    session.add(TextUsageLedger(generation_id=job.id, owner_id=job.owner_id, project_id=job.project_id, remote_request_id=response.id, provider=job.provider, model=response.model, input_tokens=job.input_tokens, output_tokens=job.output_tokens, total_tokens=job.total_tokens, cache_hit_tokens=response.usage.prompt_cache_hit_tokens, cache_miss_tokens=response.usage.prompt_cache_miss_tokens, cost_micros=actual, price_json=job.input_json["price"]))
    choice = response.choices[0]
    if choice.finish_reason != "stop" or choice.message.tool_calls:
        job.status, job.error_code, job.error_message = "failed", "TEXT_RESULT_INCOMPLETE", "供应商没有返回完整的纯文本结果，真实Token和费用已记录"
        return
    try:
        result = RESULTS[job.task].model_validate_json(job.partial_text).model_dump()
    except ValidationError:
        job.status, job.error_code, job.error_message = "failed", "TEXT_RESULT_INVALID", "真实文本结果未通过结构校验，原始输出及Token费用继续保留"
        return
    job.result_json, job.output_text = result, _format_result(job.task, result)
    if job.task == "plot" and (len(result["shots"]) != 3 or [shot["no"] for shot in result["shots"]] != [1, 2, 3]):
        job.usage_json = {**job.usage_json, "qualityIssues": [f"本次返回{len(result['shots'])}个镜头，请核对数量和编号是否符合本次要求；完整分镜均已保留"]}
    project = require_project(session, job.project_id, job.owner_id)
    if job.stop_requested:
        job.status, job.error_code, job.error_message = "interrupted", "TEXT_STOPPED", "任务已停止自动采用，已提交的真实输出和费用继续保留"
    elif project.revision != job.project_revision:
        job.status, job.error_code, job.error_message = "conflict", "PROJECT_REVISION_CONFLICT", "生成期间项目已有人工编辑，文本结果已保存，请核对后采用"
    else:
        try:
            _apply_output(session, job, project)
        except AppError as error:
            job.status, job.error_code, job.error_message = "conflict", error.code, error.message


async def _run_text(settings: Settings, factory: sessionmaker, worker_id: str, identifier: str) -> None:
    with factory() as session:
        lock_write(session)
        job = session.get(TextGeneration, identifier)
        if job is None or job.status != "queued":
            session.rollback()
            return
        owner = session.get(User, job.owner_id)
        if owner is None or not is_text_admin(owner, settings):
            _settle(session, job, 0)
            job.status, job.error_code, job.error_message = "interrupted", "TEXT_BILLING_NOT_CONFIGURED", "当前账号已不具备平台文本验证权限，未提交的费用预留已释放"
            job.finished_at, job.updated_at = time.time(), time.time()
            session.commit()
            return
        project = require_project(session, job.project_id, job.owner_id)
        provider = session.get(TextProviderSettings, "platform")
        if provider is None or provider.updated_at != job.input_json["providerRevision"] or project.revision != job.project_revision:
            _settle(session, job, 0)
            job.status, job.error_code, job.error_message = "interrupted", "TEXT_INPUT_CHANGED", "提交前项目或平台模型配置已变化，请重新提交当前输入"
            job.finished_at = time.time()
            session.commit()
            return
        _, source_digest, _ = _canvas_composition(session, project, job.scene_id, job.source_node_id, job.input_json["sourcePrompt"], job.output_node_id, generation_mode="text", output_type="text")
        if source_digest != job.source_digest:
            _settle(session, job, 0)
            job.status, job.error_code, job.error_message = "interrupted", "TEXT_INPUT_CHANGED", "提交前来源或引用已变化，请重新提交当前输入"
            session.commit()
            return
        job.status, job.lease_owner, job.lease_expires_at = "submitting", worker_id, time.time() + settings.text_timeout + 60
        job.started_at, job.updated_at = time.time(), time.time()
        job.input_json = {**job.input_json, "price": current_price(time.time()), "submittedAt": iso_timestamp(time.time())}
        key = decode_secret(provider.api_key_encrypted, settings)
        messages, maximum = job.input_json["messages"], job.input_json["maxTokens"]
        session.commit()
    try:
        response = await complete_text(key, messages, maximum, settings)
    except TextProviderFailure as error:
        with factory() as session:
            lock_write(session)
            job = session.get(TextGeneration, identifier)
            if job.lease_owner != worker_id:
                raise AppError(409, "TEXT_LEASE_LOST", "文本任务执行权已变化")
            if not error.unknown:
                _settle(session, job, 0)
            job.status = "submission_unknown" if error.unknown else "failed"
            job.error_code, job.error_message = error.code, error.message
            job.finished_at, job.updated_at, job.lease_expires_at = time.time(), time.time(), None
            session.commit()
        return
    with factory() as session:
        lock_write(session)
        job = session.get(TextGeneration, identifier)
        if job.lease_owner != worker_id or job.status != "submitting":
            raise AppError(409, "TEXT_LEASE_LOST", "文本任务执行权已变化")
        reported_cost = cost_micros(response.usage.prompt_cache_hit_tokens, response.usage.prompt_cache_miss_tokens, response.usage.completion_tokens, job.input_json["price"])
        if response.usage.prompt_tokens > job.input_json["inputTokenUpperBound"] or reported_cost > job.reserved_cost_micros:
            job.remote_request_id = response.id
            job.input_tokens, job.output_tokens, job.total_tokens = response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens
            job.partial_text = response.choices[0].message.content or ""
            job.usage_json = {**response.usage.model_dump(), "reportedCostMicros": reported_cost, "settled": False, "price": job.input_json["price"]}
            job.status, job.error_code, job.error_message = "submission_unknown", "TEXT_USAGE_OUT_OF_BOUNDS", "供应商上报用量超出请求预留上界，真实上报值已保存，请核实原请求；系统不会自动重发"
        else:
            _save_completion(session, job, response)
        job.finished_at, job.updated_at, job.lease_expires_at = time.time(), time.time(), None
        session.commit()


async def text_worker_loop(settings: Settings, factory: sessionmaker, stop_event: asyncio.Event) -> None:
    worker_id = str(uuid.uuid4())
    while not stop_event.is_set():
        with factory() as session:
            lock_write(session)
            expired = session.scalars(select(TextGeneration).where(TextGeneration.status.in_(["submitting", "running"]), or_(TextGeneration.lease_expires_at.is_(None), TextGeneration.lease_expires_at < time.time()))).all()
            for job in expired:
                job.status, job.error_code, job.error_message = "submission_unknown", "TEXT_SUBMISSION_UNKNOWN", "服务重启后原文本请求结果待核实，预留继续保留，系统不会自动重发"
                job.updated_at = time.time()
            identifier = session.scalar(select(TextGeneration.id).where(TextGeneration.status == "queued").order_by(TextGeneration.created_at).limit(1))
            session.commit()
        if identifier is not None:
            try:
                await _run_text(settings, factory, worker_id, identifier)
            except AppError as error:
                with factory() as session:
                    lock_write(session)
                    job = session.get(TextGeneration, identifier)
                    if job is not None and job.status in {"queued", "submitting"}:
                        if job.status == "queued":
                            _settle(session, job, 0)
                            job.status = "failed"
                        else:
                            job.status = "submission_unknown"
                        job.error_code, job.error_message = error.code, error.message
                        job.updated_at, job.finished_at = time.time(), time.time()
                        session.commit()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.5)
        except TimeoutError:
            pass
