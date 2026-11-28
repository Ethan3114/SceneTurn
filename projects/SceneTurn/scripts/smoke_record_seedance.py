import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from smoke import PRODUCT_ROOT, validate_adopted_clip, validate_media
from smoke_record_text import RestartSnapshot, fingerprint, service_identity, snapshot_path
from smoke_registry import EvidenceStore, write_private_json


sys.path.insert(0, str(PRODUCT_ROOT / "backend"))

from app.core.config import sqlite_database_path
from app.models import Asset, Budget, Generation, Operation, Project, ProjectVersion, User
from app.services.ark import ArkVideoUsage, video_result
from app.services.generation import digest


def verified_receipt(task: Generation) -> dict:
    frozen = task.input_json
    selection = frozen.get("modelProfile", {})
    provider = frozen.get("provider")
    model = frozen.get("model")
    if provider != "volcengine-ark" or selection.get("providerId") != provider or selection.get("model") != model:
        raise ValueError("指定任务缺少一致的火山方舟模型配置")
    if not isinstance(model, str) or not model.startswith("doubao-seedance-"):
        raise ValueError("指定任务没有使用 Seedance")
    if task.status != "ready" or not all((task.remote_task_id, task.remote_request_id, task.asset_id)):
        raise ValueError("指定任务尚无已完成的供应商回执和项目视频")
    if task.started_at is None or task.finished_at is None or task.finished_at < task.started_at:
        raise ValueError("真实任务时间不完整")
    response = task.usage_json.get("providerResponse")
    if not isinstance(response, dict):
        raise ValueError("缺少实际方舟回执")
    result = video_result(response, task.remote_request_id)
    if result.output.task_status != "SUCCEEDED" or result.output.task_id != task.remote_task_id or response["model"] != model or not result.output.video_url:
        raise ValueError("保存的供应商回执与原任务不一致")
    usage = ArkVideoUsage.model_validate(response["usage"])
    tokens = usage.completion_tokens
    if result.usage is None or tokens != task.usage_json.get("completionTokens") or any(
        task.usage_json.get(key) != value for key, value in result.usage.model_dump().items()
    ):
        raise ValueError("已保存的方舟用量与原始供应商回执不一致")
    price = selection.get("price", {})
    rate = price.get("outputMicrosPerMillion")
    if type(rate) is not int or rate <= 0 or price.get("verified") is not True or price.get("version") != frozen.get("priceVersion"):
        raise ValueError("冻结的火山方舟价表缺少已核对的 Token 单价")
    calculated = (tokens * rate + 9_999_999_999) // 10_000_000_000
    limit = frozen.get("billingLimit")
    if (task.usage_json.get("settled") is not True or task.reserved_cents != 0 or calculated != task.actual_cents
            or not 0 < calculated <= task.quoted_cents <= frozen["maxAllowedCents"]
            or limit and (limit.get("cents") != task.quoted_cents or tokens > limit["maxCompletionTokens"])):
        raise ValueError("已结算金额或输出 Token 超出冻结的报价")
    return usage.model_dump(exclude_none=True, include={"completion_tokens", "total_tokens"})


def read_generation(data_directory: Path, task_id: str) -> dict:
    directory = (PRODUCT_ROOT / data_directory).resolve(strict=True)
    database = sqlite_database_path(directory).resolve(strict=True)
    if not directory.is_relative_to(PRODUCT_ROOT) or not database.is_relative_to(directory):
        raise ValueError("只读验证的数据文件必须位于本产品范围内")
    engine = create_engine(f"sqlite:///file:{database}?mode=ro&uri=true")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA query_only = ON")
        with Session(bind=connection, autoflush=False) as session:
            task = session.get(Generation, task_id)
            if task is None:
                raise ValueError("指定数据目录没有找到这项视频任务")
            usage = verified_receipt(task)
            frozen = task.input_json
            expected_hash = digest({
                "input": frozen, "projectId": task.project_id, "sceneId": task.scene_id,
                "sourceNodeId": task.source_node_id, "clipId": task.clip_id, "projectRevision": task.project_revision,
                "sourceDigest": task.source_digest, "quotedCents": task.quoted_cents,
            })
            quote = session.scalar(select(Operation).where(Operation.owner_id == task.owner_id, Operation.project_id == task.project_id,
                Operation.operation_id == task.operation_id, Operation.kind == "generation.quote"))
            if task.input_hash != expected_hash or quote is None or quote.payload_hash != digest(frozen["request"]) or quote.response_json.get("generationId") != task.id:
                raise ValueError("供应商任务与已保存的报价不一致")
            history = session.scalar(select(ProjectVersion).where(ProjectVersion.project_id == task.project_id,
                ProjectVersion.owner_id == task.owner_id, ProjectVersion.revision == task.project_revision))
            if history is None or history.data_json["id"] != task.project_id:
                raise ValueError("缺少发起生成时的项目历史版本")
            project, owner, asset = session.get(Project, task.project_id), session.get(User, task.owner_id), session.get(Asset, task.asset_id)
            if project is None or owner is None or asset is None or (
                project.owner_id, asset.owner_id, asset.project_id, asset.generation_id
            ) != (owner.id, owner.id, project.id, task.id):
                raise ValueError("任务、项目与视频的归属不一致")
            expected_metadata = {
                "model": frozen["model"], "remoteTaskId": task.remote_task_id, "inputHash": task.input_hash,
            }
            if not asset.mime_type.startswith("video/") or any(asset.metadata_json.get(key) != value for key, value in expected_metadata.items()):
                raise ValueError("项目视频缺少一致的实际生成标识")
            validate_adopted_clip(project.data_json, task.scene_id, task.clip_id, asset.id)
            budget = session.get(Budget, "global")
            spent, reserved = session.execute(select(func.coalesce(func.sum(Generation.actual_cents), 0),
                func.coalesce(func.sum(Generation.reserved_cents), 0))).one()
            if budget is None or (budget.spent_cents, budget.reserved_cents) != (spent, reserved):
                raise ValueError("视频任务资金记录与项目预算不一致")
            media_path = (directory / asset.relpath).resolve(strict=True)
            if not media_path.is_relative_to(directory / "media") or not media_path.is_file():
                raise ValueError("生成视频文件不在项目媒体目录")
            media = validate_media(media_path)
            if (media["bytes"], media["sha256"], media["width"], media["height"]) != (asset.bytes, asset.sha256, asset.width, asset.height):
                raise ValueError("实际视频大小、摘要或尺寸与项目素材记录不一致")
            if asset.duration_ms is None or abs(round(media["durationSeconds"] * 1000) - asset.duration_ms) > 100:
                raise ValueError("实际视频的时长与项目素材记录不一致")
            started, finished = (datetime.fromtimestamp(value, timezone.utc) for value in (task.started_at, task.finished_at))
            record = {
                "provider": "volcengine-ark", "model": frozen["model"], "generationId": task.id, "remoteTaskId": task.remote_task_id,
                "remoteRequestId": task.remote_request_id, "projectId": project.id, "sceneId": task.scene_id, "clipId": task.clip_id,
                "assetId": asset.id, "usage": usage, "media": media, "actualCents": task.actual_cents, "quotedCents": task.quoted_cents,
                "priceVersion": frozen["priceVersion"], "confirmedInputSha256": task.input_hash,
                "retryCount": task.retry_count, "startedAt": started.isoformat(), "finishedAt": finished.isoformat(),
                "elapsedSeconds": (finished - started).total_seconds(),
            }
    engine.dispose()
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="只读核对已经完成的真实 Seedance 任务，并按实际后端重启前后证据登记阶段 3-1")
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
        parser.error("正式登记前须核对后端真实重启")
    record = read_generation(arguments.data_dir, arguments.task_id)
    if arguments.capture_before_restart or arguments.verify_after_restart:
        path = snapshot_path(arguments.snapshot_file, record["generationId"])
        identity = service_identity(arguments.base_url, arguments.data_dir)
        current = RestartSnapshot(schemaVersion=1, capturedAt=datetime.now(timezone.utc), dataDirectory=identity.dataDirectory,
            generationId=record["generationId"], remoteRequestId=record["remoteRequestId"], service=identity,
            fingerprints={"seedanceGeneration": fingerprint(record)})
        if arguments.capture_before_restart:
            if path.exists():
                raise ValueError("已有重启基线，请使用新的中间文件路径")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_private_json(path, current.model_dump(mode="json"))
            record["beforeRestartSnapshot"] = str(path.relative_to(PRODUCT_ROOT))
        else:
            if not path.is_file() or path.stat().st_mode & 0o077:
                raise ValueError("缺少权限为 0600 的重启基线文件")
            previous = RestartSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
            if current.model_dump(exclude={"capturedAt", "service"}) != previous.model_dump(exclude={"capturedAt", "service"}):
                raise ValueError("重启前后的真实任务、结算、项目采用或媒体有差异")
            if previous.service.baseUrl != identity.baseUrl or previous.service.pid == identity.pid or previous.service.processStarted == identity.processStarted:
                raise ValueError("当前未取得该后端实际重启的证据")
            record["restartVerified"] = True
            record["restartVerification"] = {"capturedAt": previous.capturedAt.isoformat(), "beforePid": previous.service.pid, "afterPid": identity.pid}
            if arguments.register:
                record["verifiedAt"] = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
                changed = EvidenceStore(PRODUCT_ROOT, "阶段3-1").register(record)
                print(json.dumps({"generationId": record["generationId"], "registered": changed}, ensure_ascii=False))
                return
    print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()
