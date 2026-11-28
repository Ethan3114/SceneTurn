import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings, sqlite_database_path
from app.core.security import decode_secret
from app.models import Budget, BudgetReservation, TextGeneration, TextProviderSettings
from app.services.projects import lock_write
from app.services.text_generation import _reserve
from app.services.text_provider import MODEL, RESULTS, cost_micros, task_instructions


PROMPTS = {
    "text": "为这段创意写一句不超过30字的剧情介绍。",
    "summary": "为这段创意写不超过50字的本集摘要。",
    "plot": "把这段创意写成3个镜头画面，编号1、2、3，每镜不超过20字，以两段各5秒的视频衔接。",
    "shot": "只写第1镜的画面提示词，不超过30字，编号1。",
}
IDEA = "雨夜，修表师在即将关闭的小站发现一只倒着走的怀表，决定把它交还给等待末班车的老人。"


def within_product(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError("验证数据必须位于本产品目录中")
    return resolved


def upper_cost(task: str, maximum: int) -> int:
    prompt = f"{PROMPTS[task]}\n\n【文本1】\n{IDEA}"
    upper = len(task_instructions(task).encode()) + len(prompt.encode()) + 1024
    return cost_micros(0, upper, maximum, {"cacheHitMicrosPerMillion": 40000, "cacheMissMicrosPerMillion": 2000000, "outputMicrosPerMillion": 8000000})


def start_service(data: Path, admin: str, port: int, log):
    environment = {**os.environ, "SCENETURN_DATA_DIR": str(data), "SCENETURN_TEXT_ADMIN_EMAIL": admin, "SCENETURN_WORKER_ENABLED": "true", "TMPDIR": str(ROOT / ".intermediate" / "text-smoke"), "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen([str(ROOT / "backend" / ".venv" / "bin" / "python"), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"], cwd=ROOT / "backend", env=environment, stdout=log, stderr=log, umask=0o077)
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("隔离文本服务启动失败，日志位于本产品中间目录")
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/api/v1/health", timeout=1, trust_env=False)
            if response.status_code == 200:
                return process
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    process.terminate()
    process.wait(timeout=10)
    raise TimeoutError("隔离文本服务未能在规定时间内启动")


def stop_service(process) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def record_smoke(job: dict, elapsed: float, stage: str) -> None:
    destination = ROOT / "docs" / "evidence" / stage / "文本冒烟记录.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_text("# 真实文本冒烟记录\n\n| 时间 | 任务 | 模型 | 请求与任务 | 输入/输出/总Token | 耗时秒 | 结构校验 | 重试次数 | 结果 | 供应商预估成本微元 |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n", encoding="utf-8")
    valid = job["result"] is not None
    if valid:
        RESULTS[job["task"]].model_validate(job["result"])
    tokens = f"{job['inputTokens']}/{job['outputTokens']}/{job['totalTokens']}" if job["usage"].get("settled") else "待核实"
    cost = str(job["costMicros"]) if job["costMicros"] is not None else "待核实"
    timestamp = job.get("startedAt") or job["createdAt"]
    with destination.open("a", encoding="utf-8") as output:
        output.write(f"| {timestamp} | {job['task']} | {job['model']} | {job['remoteRequestId'] or '远端待核实'} / {job['id']} | {tokens} | {elapsed:.3f} | {'通过' if valid else '未通过'} | 0 | {job['status']} | {cost} |\n")


def verify_restart_record(task_id: str, data: Path, base_url: str, *actions: str) -> dict:
    result = subprocess.run([
        sys.executable, str(ROOT / "scripts" / "smoke_record_text.py"),
        "--task-id", task_id, "--data-dir", str(data), "--base-url", base_url, *actions,
    ], cwd=ROOT, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def reconcile(source_engine, isolated_data: Path, reservation_id: str) -> dict:
    isolated_engine = create_engine(f"sqlite:///{sqlite_database_path(isolated_data).as_uri()}?mode=ro&uri=true")
    with Session(isolated_engine) as session:
        jobs = session.scalars(select(TextGeneration)).all()
        spent = sum(job.cost_micros for job in jobs if job.usage_json.get("settled"))
        unknown = sum(job.reserved_cost_micros for job in jobs if job.status in {"submitting", "running", "submission_unknown"})
        details = [{"id": job.id, "task": job.task, "status": job.status, "remoteRequestId": job.remote_request_id, "inputTokens": job.input_tokens, "outputTokens": job.output_tokens, "totalTokens": job.total_tokens, "costMicros": job.cost_micros if job.usage_json.get("settled") else None, "reservedCostMicros": job.reserved_cost_micros} for job in jobs]
    isolated_engine.dispose()
    with Session(source_engine) as session:
        lock_write(session)
        reservation = session.get(BudgetReservation, reservation_id)
        if reservation is None:
            raise ValueError("主预算预留不存在")
        additional = spent - reservation.cost_micros
        if additional < 0 or additional + unknown > reservation.reserved_micros:
            raise ValueError("隔离用量与主预算预留不一致，保留现有费用预留")
        updated = session.execute(update(Budget).where(Budget.id == "global", Budget.text_reserved_micros >= reservation.reserved_micros).values(text_reserved_micros=Budget.text_reserved_micros - reservation.reserved_micros + unknown, text_spent_micros=Budget.text_spent_micros + additional))
        if updated.rowcount != 1:
            raise ValueError("主预算结算发生冲突")
        reservation.cost_micros, reservation.reserved_micros = spent, unknown
        reservation.status, reservation.updated_at = ("unknown" if unknown else "settled"), time.time()
        reservation.details_json = {**reservation.details_json, "jobs": details, "reconciledAt": datetime.now(timezone.utc).isoformat()}
        session.commit()
    return {"runId": reservation_id, "costMicros": spent, "unknownReservedMicros": unknown, "jobs": details}


def main() -> None:
    parser = argparse.ArgumentParser(description="通过正式DeepSeek文本API验证真实结构、输出保存和重启恢复，并计入主预算")
    parser.add_argument("--provider-data", type=Path, default=ROOT / "data")
    parser.add_argument("--tasks", nargs="+", choices=list(PROMPTS), default=list(PROMPTS))
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--stage", choices=["阶段2-1", "阶段3-1"], default="阶段2-1")
    args = parser.parse_args()
    if not 64 <= args.max_tokens <= 4096:
        raise ValueError("真实冒烟输出上限必须在64至4096之间")
    source_data = within_product(args.provider_data)
    if not sqlite_database_path(source_data).is_file():
        raise ValueError("本产品尚未保存平台配置")
    os.environ["SCENETURN_DATA_DIR"] = str(source_data)
    configuration = Config(str(ROOT / "backend" / "alembic.ini"))
    command.upgrade(configuration, "head")
    source_settings = Settings(data_dir=source_data)
    source_engine = create_engine(source_settings.database_url, connect_args={"timeout": 10})
    with Session(source_engine) as session:
        provider = session.get(TextProviderSettings, "platform")
        if provider is None or not provider.api_key_encrypted:
            raise ValueError("请先在本产品安全设置保存真实平台Key；本次未发送模型请求")
        secret = decode_secret(provider.api_key_encrypted, source_settings)
    run_id = str(uuid.uuid4())
    run_dir = ROOT / ".intermediate" / "text-smoke" / run_id
    isolated_data = run_dir / "data"
    run_dir.mkdir(parents=True, mode=0o700)
    upper = sum(upper_cost(task, args.max_tokens) for task in args.tasks)
    with Session(source_engine) as session:
        lock_write(session)
        _reserve(session, upper, source_settings)
        session.add(BudgetReservation(id=run_id, purpose="real-text-smoke", reserved_micros=upper, cost_micros=0, status="reserved", details_json={"isolatedData": str(isolated_data), "tasks": args.tasks, "maxTokens": args.max_tokens}))
        session.commit()
    admin = f"text-smoke-{uuid.uuid4().hex}@example.com"
    password = secrets.token_urlsafe(24)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = None
    result_report = None
    verification_checks = {"beforeRestart": [], "afterRestart": []}
    with (run_dir / "service.log").open("w", encoding="utf-8") as log:
        try:
            process = start_service(isolated_data, admin, port, log)
            base_url = f"http://127.0.0.1:{port}/api/v1"
            with httpx.Client(base_url=base_url, headers={"Origin": "http://127.0.0.1:3210"}, timeout=20, trust_env=False) as client:
                response = client.post("/auth/register", json={"email": admin, "password": password}); response.raise_for_status()
                response = client.put("/settings/text", json={"provider": "deepseek", "model": MODEL, "apiKey": secret}); response.raise_for_status()
                del secret
                response = client.post("/projects", json={"title": "真实文本结构与恢复验证"}); response.raise_for_status()
                project = response.json()
                project_id = project["project"]["id"]
                scene_id = next(node["id"] for node in project["project"]["graphData"]["nodes"] if node["type"] == "scene")
                completed = []
                for task in args.tasks:
                    source_id, idea_id, output_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
                    canvas = client.get(f"/projects/{project_id}/canvases/{scene_id}"); canvas.raise_for_status()
                    snapshot = canvas.json()
                    nodes = snapshot["canvas"]["nodes"] + [
                        {"id": idea_id, "type": "text", "title": "真实创作输入", "position": {"x": 40, "y": len(completed) * 400}, "width": 300, "height": 220, "metadata": {"content": IDEA}},
                        {"id": source_id, "type": "config", "title": task, "position": {"x": 440, "y": len(completed) * 400}, "width": 300, "height": 260, "metadata": {"generationMode": "text", "composerContent": PROMPTS[task], "dramaTask": task}},
                        {"id": output_id, "type": "text", "title": "真实文本结果", "position": {"x": 840, "y": len(completed) * 400}, "width": 340, "height": 260, "metadata": {"pendingResult": True}},
                    ]
                    connections = snapshot["canvas"]["connections"] + [{"id": str(uuid.uuid4()), "fromNodeId": idea_id, "toNodeId": source_id}, {"id": str(uuid.uuid4()), "fromNodeId": source_id, "toNodeId": output_id}]
                    response = client.put(f"/projects/{project_id}/canvases/{scene_id}", json={"expectedRevision": snapshot["revision"], "operationId": str(uuid.uuid4()), "nodes": nodes, "connections": connections, "viewport": snapshot["canvas"]["viewport"]}); response.raise_for_status()
                    revision = response.json()["revision"]
                    payload = {"sceneId": scene_id, "sourceNodeId": source_id, "outputNodeId": output_id, "prompt": PROMPTS[task], "task": task, "expectedRevision": revision, "operationId": str(uuid.uuid4()), "maxTokens": args.max_tokens}
                    started = time.monotonic()
                    response = client.post(f"/projects/{project_id}/text-generations", json=payload); response.raise_for_status()
                    job = response.json()["textGeneration"]
                    retry = client.post(f"/projects/{project_id}/text-generations", json=payload); retry.raise_for_status()
                    if retry.json()["textGeneration"]["id"] != job["id"]:
                        raise ValueError("真实文本操作幂等验证失败")
                    deadline = time.monotonic() + 220
                    while job["status"] in {"queued", "starting", "submitting", "running"}:
                        if time.monotonic() > deadline:
                            raise TimeoutError("真实文本任务仍未返回，主费用预留继续保留")
                        time.sleep(0.5)
                        response = client.get(f"/text-generations/{job['id']}"); response.raise_for_status()
                        job = response.json()["textGeneration"]
                    record_smoke(job, time.monotonic() - started, args.stage)
                    if job["status"] != "ready" or job["result"] is None or not job["usage"].get("settled"):
                        raise RuntimeError(f"真实文本任务未完成：{job['status']}，任务{job['id']}")
                    RESULTS[task].model_validate(job["result"])
                    response = client.get(f"/projects/{project_id}/canvases/{scene_id}"); response.raise_for_status()
                    output = next(node for node in response.json()["canvas"]["nodes"] if node["id"] == output_id)
                    if output["metadata"]["content"] != job["outputText"] or output["metadata"]["result"] != job["result"]:
                        raise ValueError("正式画布没有保存真实文本结构和正文")
                    completed.append(job)
                response = client.get(f"/projects/{project_id}/text-usage"); response.raise_for_status()
                ledger = response.json()
                if ledger["costMicros"] != sum(job["costMicros"] for job in completed) or ledger["totalTokens"] != sum(job["totalTokens"] for job in completed):
                    raise ValueError("正式Token账目与真实任务用量不一致")
                for job in completed:
                    verification_checks["beforeRestart"].append(verify_restart_record(job["id"], isolated_data, base_url, "--capture-before-restart"))
                stop_service(process)
                process = start_service(isolated_data, admin, port, log)
                for before in completed:
                    response = client.get(f"/text-generations/{before['id']}"); response.raise_for_status()
                    after = response.json()["textGeneration"]
                    if any(after[key] != before[key] for key in ("result", "outputText", "usage", "costMicros", "createdAt", "startedAt", "finishedAt", "remoteRequestId")):
                        raise ValueError("服务重启后真实文本或原始用量发生变化")
                response = client.get(f"/projects/{project_id}/text-generations"); response.raise_for_status()
                if len(response.json()["textGenerations"]) != len(completed):
                    raise ValueError("重启后任务数量不一致或出现重复提交")
                for job in completed:
                    verification_checks["afterRestart"].append(verify_restart_record(job["id"], isolated_data, base_url, "--verify-after-restart", "--register", "--stage", args.stage))
                result_report = {"runId": run_id, "projectId": project_id, "dataDir": str(isolated_data), "contracts": args.tasks, "restartVerified": True, "resultDigests": {job["id"]: hashlib.sha256(json.dumps(job["result"], ensure_ascii=False, sort_keys=True).encode()).hexdigest() for job in completed}}
        finally:
            stop_service(process)
            result_report = {**(result_report or {"runId": run_id, "dataDir": str(isolated_data), "restartVerified": False}), "formalEvidence": verification_checks, "budget": reconcile(source_engine, isolated_data, run_id)}
            (run_dir / "result.json").write_text(json.dumps(result_report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result_report, ensure_ascii=False))
            source_engine.dispose()


if __name__ == "__main__":
    main()
