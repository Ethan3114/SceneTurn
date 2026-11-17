import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from smoke import ComposedInput, GenerationBinding, validate_canvas_binding
from smoke_registry import EvidenceStore, write_private_json


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.models import Asset, Budget, BudgetReservation, Canvas, Generation, Operation, Project, ProjectVersion, TextGeneration, TextUsageLedger, User
from app.services.generation import _compose_prompt, _node_metadata, _resource_input, digest
from app.services.prompts.text import PROMPT_VERSION
from app.services.text_generation import _format_result
from app.services.text_provider import MODEL, RESULTS, Usage, cost_micros, current_price, task_instructions


# 这些哈希对应已保存的真实旧请求及其完整输出契约。
LEGACY_TEXT_PROMPT_CONTRACTS = {
    "openfmv-text-2026-11-13": {
        "text": {
            "systemSha256": "abca4ae7ee496113282f29ad0a0fbad070e2496972ae6142377532afcaf268b6",
            "resultSchemaSha256": "b60285c1e23c54024e7cfbd5f61532bd3256eeccb0d2bf4bf33376be2bbe1083",
            "priceVersion": "deepseek-flash-2026-11-13",
        },
    },
}


class ServiceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    baseUrl: str
    pid: int = Field(gt=0)
    processStarted: str = Field(min_length=1)
    healthVersion: str
    dataDirectory: str


class RestartSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schemaVersion: Literal[1]
    capturedAt: datetime
    dataDirectory: str
    generationId: str
    remoteRequestId: str
    service: ServiceIdentity
    fingerprints: dict[str, str]


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def verify_text_prompt_contract(task: str, frozen: dict) -> None:
    if frozen["maxAttempts"] != 1:
        raise ValueError("真实文本请求缺少已固定的调用次数上限")
    version = frozen["promptVersion"]
    if version == PROMPT_VERSION:
        instruction = task_instructions(task)
    else:
        contract = LEGACY_TEXT_PROMPT_CONTRACTS.get(version, {}).get(task)
        if contract is None:
            raise ValueError("真实文本请求的提示词版本或任务没有已知历史契约")
        messages = frozen["messages"]
        if not isinstance(messages, list) or len(messages) != 2 or not isinstance(messages[0], dict) or not isinstance(messages[0].get("content"), str):
            raise ValueError("真实文本任务缺少完整的冻结模型输入")
        instruction = messages[0]["content"]
        if hashlib.sha256(instruction.encode()).hexdigest() != contract["systemSha256"]:
            raise ValueError("真实旧文本请求的系统提示词与已知历史契约不一致")
        if fingerprint(RESULTS[task].model_json_schema()) != contract["resultSchemaSha256"] or frozen["price"]["version"] != contract["priceVersion"]:
            raise ValueError("真实旧文本请求的结果结构或价表版本与已知历史契约不一致")
    if frozen["messages"] != [{"role": "system", "content": instruction}, {"role": "user", "content": frozen["prompt"]}]:
        raise ValueError("真实文本任务的最终模型输入与冻结输入不一致")


def private_permissions(directory: Path) -> dict:
    result = {}
    paths = {"dataDirectory": directory, "database": directory / "openfmv.sqlite3", "secretDirectory": directory / "secrets", "secretMaster": directory / "secrets" / "master.key"}
    for suffix in ("-wal", "-shm"):
        path = directory / ("openfmv.sqlite3" + suffix)
        if path.exists():
            paths["database" + suffix] = path
    for name, path in paths.items():
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(directory) or resolved.stat().st_uid != os.getuid() or stat.S_IMODE(resolved.stat().st_mode) & 0o077:
            raise ValueError("真实任务数据或认证存储的文件权限不符合本人私有范围")
        result[name] = f"{stat.S_IMODE(resolved.stat().st_mode):04o}"
    return result


def historical_ready_videos(session: Session, job: TextGeneration) -> dict[str, dict]:
    rows = session.execute(select(
        Generation.id, Generation.source_node_id, Generation.input_json["outputNodeId"].as_string().label("output_node_id"),
        Generation.status, Generation.finished_at, Asset.id.label("asset_id"), Asset.owner_id, Asset.project_id, Asset.generation_id, Asset.mime_type,
    ).outerjoin(Asset, Asset.id == Generation.asset_id).where(Generation.owner_id == job.owner_id, Generation.project_id == job.project_id, Generation.scene_id == job.scene_id, Generation.created_at <= job.created_at).order_by(Generation.created_at.desc(), Generation.id.desc())).all()
    resources, seen = {}, set()
    for row in rows:
        node_id = row.output_node_id or row.source_node_id
        if node_id in seen:
            continue
        seen.add(node_id)
        if row.status == "ready" and row.finished_at is not None and row.finished_at <= job.created_at and row.asset_id and row.owner_id == job.owner_id and row.project_id == job.project_id and row.generation_id == row.id and row.mime_type.startswith("video/"):
            resources[node_id] = {"assetId": row.asset_id, "generationId": row.id}
    return resources


def recompose_history(session: Session, job: TextGeneration, canvas: dict) -> None:
    nodes, connections = canvas["nodes"], canvas["connections"]
    by_id = {node["id"]: node for node in nodes}
    source, output = by_id[job.source_node_id], by_id[job.output_node_id]
    if source["type"] == "config" and _node_metadata(source).get("generationMode") not in {None, "text"}:
        raise ValueError("冻结历史中的来源节点没有选择文本生成")
    ready_videos = historical_ready_videos(session, job)

    def connected(target_id):
        inputs, links = [], []
        for link in connections:
            if link["toNodeId"] != target_id:
                continue
            node = by_id[link["fromNodeId"]]
            if node["type"] == "group":
                children = [resource for child in nodes if _node_metadata(child).get("groupId") == node["id"] and (resource := _resource_input(child, ready_videos)) is not None]
                resource = {"nodeId": node["id"], "type": "group", "title": node.get("title", ""), "children": children} if children else None
            else:
                resource = _resource_input(node, ready_videos)
            if resource is not None:
                inputs.append(resource)
                links.append(link)
        return inputs, links

    config_link = next((link for link in connections if link["fromNodeId"] == source["id"] and by_id[link["toNodeId"]]["type"] == "config"), None)
    config, inputs, input_links = None, [], []
    if config_link is not None:
        config = by_id[config_link["toNodeId"]]
        config_inputs, config_links = connected(config["id"])
        for resource, link in zip(config_inputs, config_links, strict=True):
            if resource["nodeId"] != source["id"]:
                inputs.append(resource)
                input_links.append(link)
    if not inputs:
        inputs, input_links = connected(source["id"])
        config, config_link = None, None
    prompt, selected = _compose_prompt(source, inputs, job.input_json["sourcePrompt"])
    if prompt != job.input_json["prompt"] or selected != job.input_json["inputs"]:
        raise ValueError("真实历史连线重组输入与实际提交输入不一致")
    expected = digest({"node": source, "inputs": inputs, "connections": input_links, "config": config, "configConnection": config_link, "outputNode": output})
    if expected != job.source_digest:
        raise ValueError("真实历史来源摘要与文本任务冻结摘要不一致")


def task_record(job: TextGeneration) -> dict:
    keys = ("id", "owner_id", "project_id", "scene_id", "source_node_id", "output_node_id", "operation_id", "task", "project_revision", "source_digest", "input_json", "result_json", "output_text", "partial_text", "status", "provider", "model", "remote_request_id", "input_tokens", "output_tokens", "total_tokens", "reserved_cost_micros", "cost_micros", "usage_json", "applied_revision", "error_code", "error_message", "stop_requested", "started_at", "finished_at", "created_at", "updated_at")
    return {key: getattr(job, key) for key in keys}


def verify_text_data(data_directory: Path, task_id: str) -> tuple[dict, dict[str, str]]:
    directory = (ROOT / data_directory).resolve(strict=True)
    if not directory.is_relative_to(ROOT):
        raise ValueError("只读验收的数据目录必须位于本产品目录中")
    modes = private_permissions(directory)
    database = (directory / "openfmv.sqlite3").resolve(strict=True)
    engine = create_engine(f"sqlite:///{database.as_uri()}?mode=ro&uri=true")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA query_only = ON")
        connection.exec_driver_sql("BEGIN")
        with Session(bind=connection, autoflush=False) as session:
            job = session.get(TextGeneration, task_id)
            if job is None or job.status != "ready" or job.provider != "deepseek" or job.model != MODEL or not job.remote_request_id or job.applied_revision is None or job.started_at is None or job.finished_at is None or job.finished_at < job.started_at:
                raise ValueError("指定真实文本任务尚未完成生成、结构校验和固定目标写入")
            project = session.get(Project, job.project_id)
            if project is None or project.owner_id != job.owner_id or session.scalar(select(User.id).where(User.id == job.owner_id)) is None:
                raise ValueError("真实文本任务、账号和项目归属不一致")
            ComposedInput.model_validate(job.input_json)
            binding = GenerationBinding.model_validate({"projectId": job.project_id, "sceneId": job.scene_id, "sourceNodeId": job.source_node_id, "outputNodeId": job.output_node_id, "projectRevision": job.project_revision, "input": job.input_json})
            request = job.input_json["request"]
            for key, value in {"sceneId": job.scene_id, "sourceNodeId": job.source_node_id, "outputNodeId": job.output_node_id, "task": job.task, "expectedRevision": job.project_revision, "operationId": job.operation_id, "prompt": job.input_json["sourcePrompt"]}.items():
                if request[key] != value:
                    raise ValueError("真实文本请求与持久任务绑定不一致")
            verify_text_prompt_contract(job.task, job.input_json)
            operation = session.scalar(select(Operation).where(Operation.owner_id == job.owner_id, Operation.project_id == job.project_id, Operation.operation_id == job.operation_id))
            if operation is None or operation.kind != "text.generate" or operation.payload_hash != digest(request) or operation.response_json != {"textGenerationId": job.id}:
                raise ValueError("真实文本任务缺少对应的幂等操作记录")
            history = session.scalar(select(ProjectVersion).where(ProjectVersion.project_id == job.project_id, ProjectVersion.owner_id == job.owner_id, ProjectVersion.revision == job.project_revision))
            applied = session.scalar(select(ProjectVersion).where(ProjectVersion.project_id == job.project_id, ProjectVersion.owner_id == job.owner_id, ProjectVersion.revision == job.applied_revision))
            if history is None or applied is None or history.data_json["id"] != job.project_id or applied.data_json["id"] != job.project_id:
                raise ValueError("真实文本任务缺少冻结输入或目标写入的项目历史")
            validate_canvas_binding(history.canvases_json, binding, output_type="text")
            frozen_canvas = next(canvas for canvas in history.canvases_json if canvas["sceneId"] == job.scene_id)
            recompose_history(session, job, frozen_canvas)
            try:
                parsed = RESULTS[job.task].model_validate(job.result_json).model_dump()
                raw = RESULTS[job.task].model_validate_json(job.partial_text).model_dump()
            except ValueError:
                raise ValueError("真实供应商原始输出或保存结果没有通过结构校验") from None
            if raw != parsed or parsed != job.result_json or _format_result(job.task, parsed) != job.output_text:
                raise ValueError("真实供应商输出、结构化结果与显示正文不一致")
            written_canvas = next(canvas for canvas in applied.canvases_json if canvas["sceneId"] == job.scene_id)
            target = next(node for node in written_canvas["nodes"] if node["id"] == job.output_node_id)
            metadata = target.get("metadata", {})
            if target["type"] != "text" or metadata.get("content") != job.output_text or metadata.get("result") != job.result_json or metadata.get("textGenerationId") != job.id:
                raise ValueError("真实文本未写入所绑定的独立文字结果节点")
            usage = Usage.model_validate(job.usage_json)
            if not job.usage_json.get("settled") or job.reserved_cost_micros != 0 or (job.input_tokens, job.output_tokens, job.total_tokens) != (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens):
                raise ValueError("真实文本Token账目没有完整结算")
            if usage.prompt_tokens > job.input_json["inputTokenUpperBound"] or usage.completion_tokens > job.input_json["maxTokens"]:
                raise ValueError("真实文本用量超出请求冻结上界")
            price = job.input_json["price"]
            if price != current_price(job.started_at) or job.usage_json.get("price") != price:
                raise ValueError("真实文本用量的冻结价表与实际提交时段不一致")
            calculated = cost_micros(usage.prompt_cache_hit_tokens, usage.prompt_cache_miss_tokens, usage.completion_tokens, price)
            ledger = session.scalars(select(TextUsageLedger).where(TextUsageLedger.generation_id == job.id)).all()
            if len(ledger) != 1:
                raise ValueError("真实文本任务缺少唯一Token费用账本")
            ledger = ledger[0]
            if (ledger.owner_id, ledger.project_id, ledger.remote_request_id, ledger.provider, ledger.input_tokens, ledger.output_tokens, ledger.total_tokens, ledger.cache_hit_tokens, ledger.cache_miss_tokens, ledger.cost_micros, ledger.price_json) != (job.owner_id, job.project_id, job.remote_request_id, job.provider, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, usage.prompt_cache_hit_tokens, usage.prompt_cache_miss_tokens, calculated, price) or job.cost_micros != calculated or ledger.model not in {MODEL, "DeepSeek-V4.1-Flash-1017"}:
                raise ValueError("真实文本任务、供应商Token与费用账本不一致")
            budget = session.get(Budget, "global")
            ledger_total = session.scalar(select(func.coalesce(func.sum(TextUsageLedger.cost_micros), 0)))
            verification_total = session.scalar(select(func.coalesce(func.sum(BudgetReservation.cost_micros), 0)))
            reserved_jobs = session.scalar(select(func.coalesce(func.sum(TextGeneration.reserved_cost_micros), 0)))
            reserved_verification = session.scalar(select(func.coalesce(func.sum(BudgetReservation.reserved_micros), 0)))
            if budget is None or budget.text_spent_micros != ledger_total + verification_total or budget.text_reserved_micros != reserved_jobs + reserved_verification:
                raise ValueError("主文本预算与真实Token账本及独立验证费用不一致")
            canvas = session.scalar(select(Canvas).where(Canvas.project_id == job.project_id, Canvas.scene_id == job.scene_id, Canvas.owner_id == job.owner_id))
            current_target = next((node for node in canvas.data_json["nodes"] if node["id"] == job.output_node_id), None) if canvas is not None else None
            ledger_data = {key: getattr(ledger, key) for key in ("id", "generation_id", "owner_id", "project_id", "remote_request_id", "provider", "model", "input_tokens", "output_tokens", "total_tokens", "cache_hit_tokens", "cache_miss_tokens", "cost_micros", "price_json", "created_at")}
            fingerprints = {"task": fingerprint(task_record(job)), "ledger": fingerprint(ledger_data), "frozenHistory": fingerprint({"project": history.data_json, "canvases": history.canvases_json}), "appliedHistory": fingerprint({"project": applied.data_json, "canvases": applied.canvases_json}), "outputText": hashlib.sha256(job.output_text.encode()).hexdigest(), "result": fingerprint(job.result_json), "input": fingerprint(job.input_json), "currentTarget": fingerprint(current_target)}
            started = datetime.fromtimestamp(job.started_at, timezone.utc)
            finished = datetime.fromtimestamp(job.finished_at, timezone.utc)
            record = {"kind": "text", "provider": job.provider, "generationId": job.id, "remoteRequestId": job.remote_request_id, "projectId": job.project_id, "sceneId": job.scene_id, "sourceNodeId": job.source_node_id, "outputNodeId": job.output_node_id, "projectRevision": job.project_revision, "appliedRevision": job.applied_revision, "task": job.task, "model": ledger.model, "requestedModel": job.model, "usage": usage.model_dump(), "costMicros": job.cost_micros, "price": price, "retryCount": 0, "startedAt": started.isoformat(), "finishedAt": finished.isoformat(), "elapsedSeconds": (finished - started).total_seconds(), "fingerprints": {key: value for key, value in fingerprints.items() if key != "currentTarget"}, "targetWritten": True, "filePermissions": modes}
    engine.dispose()
    return record, fingerprints


def service_identity(base_url: str, data_directory: Path) -> ServiceIdentity:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port is None or parsed.username or parsed.password:
        raise ValueError("实际重启证明仅接受本机固定HTTP预览服务")
    response = httpx.get(base_url.rstrip("/") + "/health", timeout=5, follow_redirects=False, trust_env=False)
    response.raise_for_status()
    health = response.json()
    if health.get("status") != "ok":
        raise ValueError("本机预览服务尚未健康")
    listener = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{parsed.port}", "-sTCP:LISTEN"], check=True, capture_output=True, text=True)
    pid = int(listener.stdout.strip())
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], check=True, capture_output=True, text=True).stdout
    if "app.main:app" not in command or str(parsed.port) not in command:
        raise ValueError("健康端口未归属本产品后端进程")
    directory = (ROOT / data_directory).resolve(strict=True)
    if not directory.is_relative_to(ROOT):
        raise ValueError("进程验证的数据目录必须位于本产品范围内")
    opened = subprocess.run(["lsof", "-nP", "-t", "-a", "-p", str(pid), str(directory / "openfmv.sqlite3")], check=True, capture_output=True, text=True)
    if int(opened.stdout.strip()) != pid:
        raise ValueError("健康后端进程没有打开本次验收的真实数据库")
    started = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], check=True, capture_output=True, text=True).stdout.strip()
    return ServiceIdentity(baseUrl=base_url.rstrip("/"), pid=pid, processStarted=started, healthVersion=health["version"], dataDirectory=str(directory))


def snapshot_path(path: Path, task_id: str) -> Path:
    selected = path or ROOT / ".intermediate" / "smoke-restart" / f"{task_id}.before.json"
    selected = (ROOT / selected).resolve()
    if not selected.is_relative_to(ROOT / ".intermediate"):
        raise ValueError("实际重启基线必须保存在本产品.intermediate目录中")
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="只读复核已有真实DeepSeek任务；实际重启后首次登记，多次复核保持记录不变")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8310/api/v1")
    parser.add_argument("--snapshot-file", type=Path)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--capture-before-restart", action="store_true")
    actions.add_argument("--verify-after-restart", action="store_true")
    parser.add_argument("--register", action="store_true")
    arguments = parser.parse_args()
    if arguments.register and not arguments.verify_after_restart:
        raise ValueError("正式首次登记必须先完成实际重启前后验证")
    record, fingerprints = verify_text_data(arguments.data_dir, arguments.task_id)
    summary = {"generationId": record["generationId"], "remoteRequestId": record["remoteRequestId"], "model": record["model"], "startedAt": record["startedAt"], "finishedAt": record["finishedAt"], "inputTokens": record["usage"]["prompt_tokens"], "outputTokens": record["usage"]["completion_tokens"], "totalTokens": record["usage"]["total_tokens"], "costMicros": record["costMicros"], "readonlyVerified": True}
    if arguments.capture_before_restart or arguments.verify_after_restart:
        path = snapshot_path(arguments.snapshot_file, arguments.task_id)
        identity = service_identity(arguments.base_url, arguments.data_dir)
        data_directory = str((ROOT / arguments.data_dir).resolve())
        if arguments.capture_before_restart:
            snapshot = RestartSnapshot(schemaVersion=1, capturedAt=datetime.now(timezone.utc), dataDirectory=data_directory, generationId=record["generationId"], remoteRequestId=record["remoteRequestId"], service=identity, fingerprints=fingerprints)
            if path.exists():
                previous = RestartSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
                if previous.model_dump(exclude={"capturedAt"}) != snapshot.model_dump(exclude={"capturedAt"}):
                    raise ValueError("已有实际重启基线对应其他状态，请使用新的中间文件路径")
            else:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                write_private_json(path, snapshot.model_dump(mode="json"))
            summary.update(snapshotFile=str(path.relative_to(ROOT)), servicePid=identity.pid, beforeRestartCaptured=True)
        else:
            if not path.is_file() or path.stat().st_mode & 0o077:
                raise ValueError("实际重启基线不存在或权限超过0600")
            previous = RestartSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
            if previous.dataDirectory != data_directory or previous.generationId != record["generationId"] or previous.remoteRequestId != record["remoteRequestId"] or previous.fingerprints != fingerprints or previous.service.baseUrl != identity.baseUrl:
                raise ValueError("实际重启前后的任务、账本、输入历史或目标正文不一致")
            if previous.service.pid == identity.pid or previous.service.processStarted == identity.processStarted:
                raise ValueError("尚无后端实际停止并重新启动的进程证据")
            record.update(verifiedAt=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"), restartVerified=True, restartVerification={"capturedAt": previous.capturedAt.isoformat(), "verifiedAt": datetime.now(timezone.utc).isoformat(), "beforeService": previous.service.model_dump(), "afterService": identity.model_dump(), "snapshotFile": str(path.relative_to(ROOT)), "currentTargetFingerprint": fingerprints["currentTarget"]})
            summary.update(restartVerified=True, beforePid=previous.service.pid, afterPid=identity.pid)
            if arguments.register:
                summary["firstRegistered"] = EvidenceStore(ROOT).register(record)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
