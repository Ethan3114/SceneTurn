import json
import os
import secrets
import socket
import sys
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings
from app.core.security import decode_secret
from app.models import BudgetReservation, TextProviderSettings
from app.services.projects import lock_write
from app.services.text_generation import _reserve
from app.services.text_provider import MODEL
from agent_smoke_evidence import digest, register_agent_evidence, write_private_json
from smoke_agents import accounting, call_records, database_snapshot, reconcile, response_json, stop_pending_runs
from smoke_text import start_service, stop_service


def main():
    os.environ["OPENFMV_TEXT_MAX_OUTPUT_TOKENS"] = "32768"
    os.environ["OPENFMV_TEXT_TIMEOUT"] = "600"
    settings = Settings(data_dir=ROOT / "data")
    engine = create_engine(settings.database_url, connect_args={"timeout": 10})
    identity = str(uuid.uuid4())
    directory = ROOT / ".intermediate/agent-smoke" / identity
    data = directory / "data"
    directory.mkdir(parents=True, mode=0o700)
    with Session(engine) as session:
        provider = session.get(TextProviderSettings, "platform")
        if not provider or not provider.api_key_encrypted:
            raise ValueError("DeepSeek Flash尚未配置")
        key = decode_secret(provider.api_key_encrypted, settings)
        session.commit()
        lock_write(session)
        _reserve(session, 10000000, settings)
        session.add(BudgetReservation(id=identity, purpose="real-agent-smoke", reserved_micros=10000000,
            cost_micros=0, status="reserved", details_json={"isolatedData": str(data), "workspace": "materials", "expectedStatus": "waiting_confirmation"}))
        session.commit()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    email, password = f"review-{uuid.uuid4().hex}@example.com", secrets.token_urlsafe(24)
    process, passed, restarted, run_id = None, False, False, None
    before_pid, after_pid, snapshot_hash = None, None, None
    print(json.dumps({"verificationId": identity, "expectedStatus": "waiting_confirmation", "model": MODEL}), flush=True)
    with (directory / "service.log").open("w", encoding="utf-8") as log:
        try:
            process = start_service(data, email, port, log)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", headers={"Origin": "http://127.0.0.1:3210"}, timeout=30, trust_env=False) as client:
                response_json(client.post("/auth/register", json={"email": email, "password": password}))
                response_json(client.put("/settings/text", json={"provider": "deepseek", "model": MODEL, "apiKey": key}))
                del key
                project = response_json(client.post("/projects", json={"title": "真实人工确认流程验证"}))
                project_id = project["project"]["id"]
                scene_id = next(node["id"] for node in project["project"]["graphData"]["nodes"] if node["type"] == "scene")
                path = f"/projects/{project_id}/scenes/{scene_id}/material-conversations"
                value = response_json(client.post(path, json={"operationId": str(uuid.uuid4())}))["conversation"]
                goal = "为9秒无对白短片编写完整剧本：雨夜书房，一位年轻修表师发现停止的怀表自行转动。结尾氛围由我确认后才能继续。仅编剧产出剧本，将结尾氛围列入confirmationNeeded=true，提出明确建议并等待本人确认；不生成图片和视频，不执行剪辑和互动。"
                value = response_json(client.post(f"{path}/{value['id']}/messages", json={"operationId": str(uuid.uuid4()), "expectedRevision": value["revision"], "content": goal}))["conversation"]
                run_id = value["messages"][-1]["run"]["id"]
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline:
                    value = response_json(client.get(f"{path}/{value['id']}"))["conversation"]
                    status = value["messages"][-1]["run"]["status"]
                    if status == "waiting_confirmation":
                        break
                    if status in {"failed", "submission_unknown", "completed", "conflict", "stopped"}:
                        raise ValueError(f"人工确认验证终止于{status}")
                    time.sleep(2)
                else:
                    raise TimeoutError("等待真实岗位成果超时")
                assert value["pendingReview"]["questions"] and value["pendingReview"]["canConfirm"]
                assert any(node["delivery"] and node["delivery"]["kind"] == "script" and node["delivery"]["text"] for node in value["nodes"])
                encoded = json.dumps(value, ensure_ascii=False)
                assert '"generationPrompts"' not in encoded and '"creativeArtifact"' not in encoded and '"savedTexts"' not in encoded
                assert all(step["status"] != "completed" for step in value["messages"][-1]["run"]["steps"])
                blocked = client.post(f"/projects/{project_id}/agent-runs/{run_id}/resume", json={"operationId": str(uuid.uuid4()), "expectedRevision": value["revision"]})
                assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "MATERIAL_CONFIRMATION_REQUIRED"
                before_pid = process.pid
                write_private_json(directory / "before-restart.json", value)
                snapshot_hash = digest(database_snapshot(data))
                stop_service(process)
                process = start_service(data, email, port, log)
                after_pid = process.pid
                restored = response_json(client.get(f"{path}/{value['id']}"))["conversation"]
                assert restored == value and before_pid != after_pid and digest(database_snapshot(data)) == snapshot_hash
                write_private_json(directory / "after-restart.json", restored)
                passed, restarted = True, True
        finally:
            stop_service(process)
            if not passed:
                stop_pending_runs(data, identity)
            budget = reconcile(engine, data, identity)
            calls = call_records(data, restarted=restarted, success=passed, run_ids={run_id} if run_id else set(), expected_status="waiting_confirmation")
            proof = {"schemaVersion": 1, "verificationId": identity, "dataDirectory": str(data), "providerDataDirectory": str(ROOT / "data"),
                "completed": False, "expectedStatus": "waiting_confirmation", "verificationPassed": passed, "restartVerified": restarted,
                "beforePid": before_pid, "afterPid": after_pid, "snapshotSha256": snapshot_hash, "sampleFiles": {},
                "runIds": [run_id] if run_id else [], "calls": calls, "accounting": accounting(data), "budget": budget, "contentAccepted": False}
            proof_path = directory / "proof.json"
            write_private_json(proof_path, proof)
            if calls:
                register_agent_evidence(calls, proof_path)
            engine.dispose()
            print(json.dumps({"verificationId": identity, "passed": passed, "restartVerified": restarted, "calls": len(calls), "costMicros": budget["spentMicros"]}), flush=True)


if __name__ == "__main__":
    main()
