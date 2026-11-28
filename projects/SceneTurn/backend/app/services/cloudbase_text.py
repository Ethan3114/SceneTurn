import asyncio
import logging
import time
from math import ceil
from uuid import UUID, uuid4

from cryptography.fernet import InvalidToken

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import decode_secret, encode_secret
from app.models import TextGeneration, User
from app.services.cloudbase_database import call_cloudbase_rpc
from app.services.deployment_queue import DeploymentQueueError, DispatchMarker, epoch_milliseconds
from app.services.generation import _canvas_composition_snapshot, digest
from app.services.prompts.text import PROMPT_VERSION
from app.services.text_generation import parse_text_completion, require_text_admin, text_instruction, text_quality_issues, text_view
from app.services.text_provider import MODEL, TextProviderFailure, complete_text, current_price, price_catalog
from app.services.transfer_dispatch import transfer_queue


logger = logging.getLogger("sceneturn")
_ACCEPT_WINDOW_MS = 45_000
_RECOVERY_BUFFER_MS = 45_000


def _identifier(value: str, *, project: bool = False) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        if project:
            raise AppError(404, "PROJECT_NOT_FOUND", "项目不存在") from None
        raise AppError(404, "TEXT_GENERATION_NOT_FOUND", "文本任务不存在") from None


def _job_view(row: dict) -> dict:
    return text_view(TextGeneration(**row))


def _compose(context: dict, payload: dict) -> tuple[dict, str, dict]:
    return _canvas_composition_snapshot(context["canvas"], payload["sourceNodeId"], payload["prompt"], payload["outputNodeId"],
        ready_videos=context["videoResources"], historical_sources=context["historicalSources"], generation_mode="text",
        output_type="text", text_image_input=payload["task"] == "translate")


class CloudBaseTextRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.queue = transfer_queue(settings)

    def _call(self, name: str, **parameters):
        return call_cloudbase_rpc(self.settings, f"sceneturn_text_{name}", parameters)

    def _prepare(self, owner_id: str, project_id: str, scene_id: str) -> dict:
        return self._call("prepare", p_actor_id=owner_id, p_project_id=project_id, p_scene_id=scene_id)

    def _publish(self, job_id: UUID, delay_ms: int) -> DispatchMarker:
        if self.queue is None:
            raise AppError(503, "TEXT_WORKER_UNAVAILABLE", "文本生成的后台调度尚未连接")
        marker = DispatchMarker(epoch_milliseconds() + delay_ms, "text", job_id, uuid4())
        try:
            self.queue.publish(marker)
        except DeploymentQueueError:
            raise AppError(503, "TEXT_WORKER_UNAVAILABLE", "文本生成暂未安排，请重试") from None
        return marker

    def _remove(self, marker: DispatchMarker) -> None:
        if self.queue is not None:
            try:
                self.queue.remove(marker)
            except DeploymentQueueError:
                logger.warning("文本任务旧调度记录将由下一次定时检查清理")

    def _remove_obsolete(self, result: dict) -> None:
        if obsolete := result.get("obsolete"):
            self._remove(DispatchMarker(obsolete["dueMs"], "text", UUID(obsolete["jobId"]), UUID(obsolete["revision"])))

    def provider_settings(self, owner_id: str, key: str | None = None) -> dict:
        stored = self._call("settings", p_actor_id=owner_id, p_admin_email=self.settings.text_admin_email,
            p_encrypted_key=encode_secret(key, self.settings) if key else None)
        return {"configured": stored["configured"], "provider": "deepseek", "model": MODEL, "prices": price_catalog(),
            "billingPolicyConfigured": False, "canManage": stored["canManage"]}

    def accept(self, user: User, project_id: str, payload: dict) -> tuple[dict, DispatchMarker | None]:
        require_text_admin(user, self.settings)
        project_id = _identifier(project_id, project=True)
        if payload.get("modelProfileId") is None:
            payload = {key: value for key, value in payload.items() if key != "modelProfileId"}
        payload_hash = digest(payload)
        replayed = self._call("replay", p_owner_id=user.id, p_project_id=project_id,
            p_operation_id=payload["operationId"], p_payload_hash=payload_hash)
        if replayed is not None:
            return _job_view(replayed), None
        context = self._prepare(user.id, project_id, payload["sceneId"])
        if context["revision"] != payload["expectedRevision"]:
            raise AppError(409, "PROJECT_REVISION_CONFLICT", "项目已有新编辑，请保存并重新提交文本任务")
        if (payload.get("modelProfileId") or context["modelDefault"] or MODEL) != MODEL:
            raise AppError(422, "TEXT_MODEL_UNSUPPORTED", "当前线上文本任务支持 DeepSeek Flash，请选择此模型")
        maximum = payload.get("maxTokens") or self.settings.text_max_output_tokens
        if maximum > self.settings.text_max_output_tokens:
            raise AppError(422, "TEXT_OUTPUT_LIMIT", "本次输出Token上限超过平台配置")
        source, source_digest, composition = _compose(context, payload)
        instruction = text_instruction(payload["task"], source)
        message = payload["prompt"] if payload["task"] == "translate" else composition["prompt"]
        messages = [{"role": "system", "content": instruction}, {"role": "user", "content": message}]
        frozen = {**composition, "request": payload, "messages": messages, "maxTokens": maximum, "promptVersion": PROMPT_VERSION}
        marker = self._publish(uuid4(), _ACCEPT_WINDOW_MS)
        try:
            result = self._call("accept", p_actor_id=user.id, p_project_id=project_id, p_job_id=str(marker.job_id),
                p_scene_id=payload["sceneId"], p_source_id=payload["sourceNodeId"], p_output_id=payload["outputNodeId"],
                p_operation_id=payload["operationId"], p_task=payload["task"], p_expected_revision=payload["expectedRevision"],
                p_payload_hash=payload_hash, p_source_digest=source_digest, p_provider_revision=context["providerRevision"],
                p_frozen_input=frozen, p_budget_limit_cents=self.settings.budget_cents, p_request_limit_cents=self.settings.max_generation_cents,
                p_dispatch_revision=str(marker.revision), p_due_ms=marker.due_ms)
        except AppError as error:
            if error.status in {402, 404, 409, 422} or error.code == "TEXT_NOT_CONFIGURED":
                self._remove(marker)
            raise
        if result["replayed"]:
            self._remove(marker)
            return _job_view(result["job"]), None
        return _job_view(result["job"]), marker

    def list(self, owner_id: str, project_id: str) -> list[dict]:
        return [_job_view(row) for row in self._call("list", p_owner_id=owner_id, p_project_id=_identifier(project_id, project=True))]

    def read(self, owner_id: str, job_id: str) -> dict:
        return _job_view(self._call("read", p_owner_id=owner_id, p_job_id=_identifier(job_id)))

    def stop(self, owner_id: str, job_id: str) -> dict:
        result = self._call("stop", p_owner_id=owner_id, p_job_id=_identifier(job_id))
        self._remove_obsolete(result)
        return _job_view(result["job"])

    def resume(self, owner_id: str, job_id: str) -> dict:
        result = self.read(owner_id, job_id)
        if result["status"] not in {"ready", "conflict", "queued", "running", "submitting"}:
            raise AppError(409, "TEXT_NOT_RESUMABLE", "该同步文本请求无法继续查询，请保留原任务核实费用")
        return result

    def apply(self, owner_id: str, job_id: str, expected_revision: int, operation_id: str) -> dict:
        job_id = _identifier(job_id)
        return _job_view(self._call("apply", p_owner_id=owner_id, p_job_id=job_id, p_expected_revision=expected_revision,
            p_operation_id=operation_id, p_payload_hash=digest({"textGenerationId": job_id, "expectedRevision": expected_revision})))

    def usage(self, owner_id: str, project_id: str) -> dict:
        return self._call("usage", p_owner_id=owner_id, p_project_id=_identifier(project_id, project=True))

    def _claim(self, marker: DispatchMarker, worker_id: str, *, immediate: bool) -> dict | None:
        lease_seconds = ceil(self.settings.text_timeout) + 60
        recovery = self._publish(marker.job_id, lease_seconds * 1000 + _RECOVERY_BUFFER_MS)
        result = self._call("claim", p_job_id=str(marker.job_id), p_worker_id=worker_id, p_marker_revision=str(marker.revision),
            p_marker_due_ms=marker.due_ms, p_immediate=immediate, p_recovery_revision=str(recovery.revision),
            p_recovery_due_ms=recovery.due_ms, p_lease_seconds=lease_seconds, p_price_band=current_price(time.time())["band"],
            p_admin_email=self.settings.text_admin_email)
        if result["state"] == "claimed":
            self._remove(marker)
            return result
        self._remove(recovery)
        if result["state"] in {"interrupted", "submission_unknown"} or (result["state"] == "stale" and marker.due_ms <= epoch_milliseconds()):
            self._remove(marker)
        return None

    def _finish(self, job_id: str, worker_id: str, outcome: str, *, error: tuple[str, str] | None = None,
                receipt: str | None = None, model: str | None = None, usage: dict | None = None,
                partial_text: str | None = None, result: dict | None = None, output: str | None = None) -> None:
        response = self._call("finish", p_job_id=job_id, p_worker_id=worker_id, p_outcome=outcome,
            p_remote_request_id=receipt, p_remote_model=model, p_usage=usage, p_partial_text=partial_text,
            p_result=result, p_output_text=output, p_error_code=error[0] if error else None, p_error_message=error[1] if error else None)
        self._remove_obsolete(response)

    def _unchanged(self, job: dict) -> bool:
        data = job["input_json"]
        context = self._prepare(job["owner_id"], job["project_id"], job["scene_id"])
        if context["revision"] != job["project_revision"] or context["providerRevision"] != data["providerRevision"]:
            return False
        _, source_digest, _ = _compose(context, data["request"])
        return source_digest == job["source_digest"]

    async def run(self, marker: DispatchMarker, *, immediate: bool = False) -> None:
        try:
            await self._run(marker, immediate=immediate)
        except AppError:
            logger.warning("文本任务由定时检查核对运行状态")

    async def _run(self, marker: DispatchMarker, *, immediate: bool) -> None:
        if marker.kind != "text" or (not immediate and marker.due_ms > epoch_milliseconds()):
            return
        worker_id = str(uuid4())
        claim = await asyncio.to_thread(self._claim, marker, worker_id, immediate=immediate)
        if claim is None:
            return
        job = claim["job"]
        try:
            unchanged = await asyncio.to_thread(self._unchanged, job)
        except AppError as error:
            await asyncio.to_thread(self._finish, job["id"], worker_id, "failed", error=(error.code, error.message))
            return
        if not unchanged:
            await asyncio.to_thread(self._finish, job["id"], worker_id, "failed",
                error=("TEXT_INPUT_CHANGED", "提交前来源或引用已变化，请重新提交当前输入"))
            return
        try:
            key = decode_secret(claim["apiKeyEncrypted"], self.settings)
        except InvalidToken:
            await asyncio.to_thread(self._finish, job["id"], worker_id, "failed",
                error=("TEXT_NOT_CONFIGURED", "文字供应商配置无法读取，请重新保存配置"))
            return
        inputs = job["input_json"]
        timeout = min(self.settings.text_timeout, job["lease_expires_at"] - time.time() - 15)
        if timeout <= 0:
            await asyncio.to_thread(self._finish, job["id"], worker_id, "failed",
                error=("TEXT_EXECUTION_WINDOW_EXPIRED", "文本任务执行时间已到，请重新提交"))
            return
        try:
            async with asyncio.timeout(timeout):
                response = await complete_text(key, inputs["messages"], inputs["maxTokens"], self.settings)
        except TextProviderFailure as error:
            await asyncio.to_thread(self._finish, job["id"], worker_id, "unknown" if error.unknown else "failed", error=(error.code, error.message))
            return
        except TimeoutError:
            await asyncio.to_thread(self._finish, job["id"], worker_id, "unknown",
                error=("TEXT_SUBMISSION_UNKNOWN", "文本请求结果待核实，费用预留继续保留"))
            return
        result, output, error = parse_text_completion(job["task"], inputs["sourcePrompt"], response)
        usage = response.usage.model_dump()
        if result is not None and (issues := text_quality_issues(job["task"], result)):
            usage["qualityIssues"] = issues
        await asyncio.to_thread(self._finish, job["id"], worker_id, "received", error=error, receipt=response.id, model=response.model,
            usage=usage, partial_text=(response.choices[0].message.content or "")[:200000], result=result, output=output)
