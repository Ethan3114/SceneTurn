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


def wait_turn(client, path, conversation_id):
    deadline = time.monotonic() + 1800
    last = None
    while time.monotonic() < deadline:
        value = response_json(client.get(f"{path}/{conversation_id}"))["conversation"]
        run = value["messages"][-1]["run"]
        progress = (run["status"], len(run["calls"]))
        if progress != last:
            print(json.dumps({"phase": "progress", "runId": run["id"], "status": run["status"], "calls": len(run["calls"])}), flush=True)
            last = progress
        if run["status"] == "completed":
            assert run["toolCatalogVersion"] == "R5" and run["workspace"] == "materials"
            assert all(call["model"] in {MODEL, "deepseek-v4-flash"} for call in run["calls"])
            return value
        if run["status"] in {"failed", "conflict", "submission_unknown", "stopped", "waiting_confirmation"}:
            raise ValueError(f"素材对话未完成：{run['status']} {run.get('error')}")
        time.sleep(1)
    raise TimeoutError("素材对话等待超时")


def main():
    os.environ["SCENETURN_TEXT_MAX_OUTPUT_TOKENS"] = "32768"
    os.environ["SCENETURN_TEXT_TIMEOUT"] = "600"
    settings = Settings(data_dir=ROOT / "data")
    engine = create_engine(settings.database_url, connect_args={"timeout": 10})
    verification_id = str(uuid.uuid4())
    directory = ROOT / ".intermediate" / "agent-smoke" / verification_id
    directory.mkdir(parents=True, mode=0o700)
    data = directory / "data"
    with Session(engine) as session:
        provider = session.get(TextProviderSettings, "platform")
        if not provider or not provider.api_key_encrypted:
            raise ValueError("DeepSeek Flash尚未配置")
        secret = decode_secret(provider.api_key_encrypted, settings)
        session.commit()
        lock_write(session)
        _reserve(session, 20000000, settings)
        session.add(BudgetReservation(id=verification_id, purpose="real-agent-smoke", reserved_micros=20000000, cost_micros=0, status="reserved",
            details_json={"isolatedData": str(data), "workspace": "materials", "roles": ["writer", "scene_designer"], "turns": 2}))
        session.commit()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    admin, password = f"materials-{uuid.uuid4().hex}@example.com", secrets.token_urlsafe(24)
    completed, restarted, process = False, False, None
    run_ids, before_pid, after_pid, snapshot_sha = [], None, None, None
    print(json.dumps({"phase": "started", "verificationId": verification_id, "mediaModelCalls": 0}), flush=True)
    with (directory / "service.log").open("w", encoding="utf-8") as log:
        try:
            process = start_service(data, admin, port, log)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", headers={"Origin": "http://127.0.0.1:3210"}, timeout=30, trust_env=False) as client:
                response_json(client.post("/auth/register", json={"email": admin, "password": password}))
                response_json(client.put("/settings/text", json={"provider": "deepseek", "model": MODEL, "apiKey": secret}))
                del secret
                project = response_json(client.post("/projects", json={"title": "真实素材岗位交接验证"}))
                project_id = project["project"]["id"]
                scene = next(node["id"] for node in project["project"]["graphData"]["nodes"] if node["type"] == "scene")
                path = f"/projects/{project_id}/scenes/{scene}/material-conversations"
                value = response_json(client.post(path, json={"operationId": str(uuid.uuid4())}))["conversation"]
                goal = ("请制作一个5秒的家庭回忆短片素材：70岁中国奶奶，短银发、自然皱纹、米白色针织开衫，在温暖整洁的家庭书房坐在木桌旁，"
                    "手持泛黄老照片，桌上有深灰色录音笔，午后柔和侧窗光，写实纪实风格，16:9，不生成任何可读文字。"
                    "先由编剧写完整短场景，再由场景设计师读取已保存剧本，交付一份完整场景设计及详细独立生图提示词。"
                    "只安排编剧和场景设计师两个产出步骤以及必要的只读核对；本次不生成媒体，不执行剪辑或互动。")
                value = response_json(client.post(f"{path}/{value['id']}/messages", json={"operationId": str(uuid.uuid4()), "expectedRevision": value["revision"], "content": goal}))["conversation"]
                run_ids.append(value["messages"][-1]["run"]["id"])
                value = wait_turn(client, path, value["id"])
                roles = {(node.get("metadata", {}).get("creativeArtifact") or {}).get("role") for node in value["nodes"]}
                assert {"writer", "scene_designer"} <= roles
                source = next(node for node in value["nodes"] if (node.get("metadata", {}).get("creativeArtifact") or {}).get("role") == "scene_designer")
                write_private_json(directory / "first-turn.json", value)
                value = response_json(client.post(f"{path}/{value['id']}/messages", json={"operationId": str(uuid.uuid4()), "expectedRevision": value["revision"], "intent": "start",
                    "content": "请只修改引用的场景设计：把午后侧窗光改为清晨柔和侧窗光，其他人物、道具和构图内容保持一致。由场景设计师修改同一产物并核对保存内容，不调用媒体模型。",
                    "references": [{"nodeId": source["id"], "contentHash": source["contentHash"]}]}))["conversation"]
                run_ids.append(value["messages"][-1]["run"]["id"])
                value = wait_turn(client, path, value["id"])
                changed = next(node for node in value["nodes"] if node["id"] == source["id"])
                assert changed["contentHash"] != source["contentHash"] and "清晨" in changed["metadata"]["content"]
                assert response_json(client.get(f"/projects/{project_id}/generations"))["generations"] == []
                write_private_json(directory / "conversation.json", value)
                saved = response_json(client.get(f"/projects/{project_id}"))
                write_private_json(directory / "project.json", saved)
                before = database_snapshot(data)
                snapshot_sha, before_pid = digest(before), process.pid
                stop_service(process)
                process = start_service(data, admin, port, log)
                after_pid = process.pid
                assert before_pid != after_pid and digest(database_snapshot(data)) == snapshot_sha
                assert response_json(client.get(f"{path}/{value['id']}"))["conversation"] == value
                completed, restarted = True, True
        finally:
            stop_service(process)
            stop_pending_runs(data, verification_id)
            budget = reconcile(engine, data, verification_id)
            calls = call_records(data, restarted=restarted, success=completed)
            proof = {"schemaVersion": 1, "verificationId": verification_id, "dataDirectory": str(data), "providerDataDirectory": str(ROOT / "data"),
                "workspace": "materials", "completed": completed, "restartVerified": restarted, "beforePid": before_pid, "afterPid": after_pid, "snapshotSha256": snapshot_sha,
                "fullStageCoverage": False, "selectedRoles": ["director", "writer", "scene_designer"], "runIds": run_ids, "calls": calls,
                "accounting": accounting(data), "budget": budget, "contentAccepted": False}
            proof_path = directory / "proof.json"
            write_private_json(proof_path, proof)
            if calls:
                register_agent_evidence(calls, proof_path)
            engine.dispose()
            print(json.dumps({"phase": "finished", "verificationId": verification_id, "completed": completed, "restartVerified": restarted,
                "calls": len(calls), "costMicros": budget["spentMicros"], "reservedMicros": budget["reservedMicros"]}), flush=True)


if __name__ == "__main__":
    main()
