import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings
from app.core.security import decode_secret
from app.models import Asset, BudgetReservation, TextProviderSettings
from app.services.media import resolve_asset_path
from app.services.projects import lock_write
from app.services.text_generation import _reserve
from app.services.text_provider import MODEL
from agent_smoke_evidence import digest, register_agent_evidence, write_private_json
from smoke_agents import accounting, call_records, database_snapshot, reconcile, response_json, stop_pending_runs
from smoke_text import start_service, stop_service


def main():
    os.environ["SCENETURN_TEXT_MAX_OUTPUT_TOKENS"] = "32768"
    os.environ["SCENETURN_TEXT_TIMEOUT"] = "600"
    settings = Settings(data_dir=ROOT / "data")
    source_engine = create_engine(settings.database_url, connect_args={"timeout": 10})
    verification_id = str(uuid.uuid4())
    directory = ROOT / ".intermediate" / "agent-smoke" / verification_id
    directory.mkdir(parents=True, mode=0o700)
    data = directory / "data"
    with Session(source_engine) as session:
        provider = session.get(TextProviderSettings, "platform")
        if not provider or not provider.api_key_encrypted:
            raise ValueError("平台Flash配置尚未保存")
        secret = decode_secret(provider.api_key_encrypted, settings)
        media = session.scalar(select(Asset).where(Asset.generation_id.is_not(None), Asset.mime_type == "video/mp4", Asset.duration_ms >= 5000).order_by(Asset.created_at))
        if media is None:
            raise ValueError("缺少已完成的真实视频，本次未调用媒体模型")
        media_path = resolve_asset_path(settings, media.relpath)
        media_name = media.name
        session.commit()
        lock_write(session)
        _reserve(session, 10000000, settings)
        session.add(BudgetReservation(id=verification_id, purpose="real-agent-smoke", reserved_micros=10000000, cost_micros=0, status="reserved",
            details_json={"isolatedData": str(data), "roles": ["director", "editor", "interaction_designer", "writer"], "workspace": "project", "limits": {"maxSteps": 24, "maxTokens": 32768, "maxCostMicros": 10000000}}))
        session.commit()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    admin, password = f"story-{uuid.uuid4().hex}@example.com", secrets.token_urlsafe(24)
    completed, restarted, process = False, False, None
    run_id, before_pid, after_pid, snapshot_sha = None, None, None, None
    print(json.dumps({"phase": "started", "verificationId": verification_id, "maximumCostYuan": 10, "mediaModelCalls": 0}), flush=True)
    with (directory / "service.log").open("w", encoding="utf-8") as log:
        try:
            process = start_service(data, admin, port, log)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", headers={"Origin": "http://127.0.0.1:3210"}, timeout=30, trust_env=False) as client:
                response_json(client.post("/auth/register", json={"email": admin, "password": password}))
                response_json(client.put("/settings/text", json={"provider": "deepseek", "model": MODEL, "apiKey": secret}))
                del secret
                project = response_json(client.post("/projects", json={"title": "树林互动短片·真实Agent工作流"}))
                project_id = project["project"]["id"]
                first = next(node["id"] for node in project["project"]["graphData"]["nodes"] if node["type"] == "scene")
                project = response_json(client.post(f"/projects/{project_id}/scenes", json={"title": "进入林间", "expectedRevision": project["revision"], "operationId": str(uuid.uuid4())}))
                second = next(node["id"] for node in project["project"]["graphData"]["nodes"] if node["type"] == "scene" and node["id"] != first)
                project = response_json(client.post(f"/projects/{project_id}/scenes", json={"title": "停在入口", "expectedRevision": project["revision"], "operationId": str(uuid.uuid4())}))
                third = next(node["id"] for node in project["project"]["graphData"]["nodes"] if node["type"] == "scene" and node["id"] not in {first, second})
                end = next(node["id"] for node in project["project"]["graphData"]["nodes"] if node["type"] == "end")
                with media_path.open("rb") as stream:
                    uploaded = response_json(client.post(f"/projects/{project_id}/assets", files={"file": (media_name, stream, "video/mp4")}))
                asset_id = uploaded["asset"]["id"]
                goal = (f"请完成真实故事编辑，并由对应岗位实际执行工具：剪辑师在场景{first}加入已登记视频{asset_id}，从原视频第1秒裁切3秒，场景内从0秒开始。"
                    f"互动设计师在同一场景添加一个普通按钮‘进入林间’，0.2秒出现、持续1秒，跳转至{second}；"
                    f"再添加一个QTE‘立即前进’，1.5秒出现、持续1秒，点击成功到{third}，超时到结尾{end}。"
                    f"编剧在场景{second}保存80至150字的环境剧情，再通过prepare_scene_media准备一组树林场景图片节点，"
                    "提示词必须独立详细覆盖主体、动作、场景、构图、写实风格、晨光色彩、细节、避免项和16:9规格。只准备图片，不调用媒体模型。"
                    "最后只读检查实际保存的时间线和互动连接。无需其他角色或额外步骤；禁止只输出文字方案。")
                run = response_json(client.post(f"/projects/{project_id}/agent-runs", json={"operationId": str(uuid.uuid4()), "expectedRevision": uploaded["revision"], "sceneId": first,
                    "workspace": "project", "role": "director", "executor": "platform", "deliveryMode": "tool_action", "goal": goal, "targetIds": [first, second],
                    "limits": {"maxSteps": 24, "maxTokens": 32768, "maxCostMicros": 10000000}}))["run"]
                run_id = run["id"]
                write_private_json(directory / "requests.json", {"projectId": project_id, "runId": run_id, "first": first, "second": second, "third": third, "end": end, "assetId": asset_id, "goal": goal})
                deadline, last_calls = time.monotonic() + 1800, -1
                while time.monotonic() < deadline:
                    run = response_json(client.get(f"/projects/{project_id}/agent-runs/{run_id}"))["run"]
                    if len(run["calls"]) != last_calls:
                        last_calls = len(run["calls"])
                        print(json.dumps({"phase": "progress", "verificationId": verification_id, "status": run["status"], "calls": last_calls, "costMicros": run["usage"]["costMicros"]}), flush=True)
                    if run["status"] == "completed":
                        break
                    if run["status"] in {"failed", "conflict", "submission_unknown", "stopped", "waiting_confirmation"}:
                        raise ValueError(f"真实工作流未完成：{run['status']} {run.get('error')}")
                    time.sleep(1)
                else:
                    raise TimeoutError("真实工作流等待超时")
                saved = response_json(client.get(f"/projects/{project_id}"))
                scene = next(node for node in saved["project"]["graphData"]["nodes"] if node["id"] == first)
                clips = [clip for track in scene["data"]["timeline"]["tracks"] for clip in track["clips"]]
                videos = [clip for clip in clips if clip["type"] == "video"]
                buttons = [clip for clip in clips if clip["type"] == "button"]
                assert len(videos) == 1 and videos[0]["assetId"] == asset_id and videos[0]["sourceStart"] == 1 and videos[0]["duration"] == 3
                assert len(buttons) == 2 and {clip["mode"] for clip in buttons} == {"normal", "qte"}
                canvas = next(value for value in saved["canvases"] if value["sceneId"] == second)
                assert {"text", "config", "image"} <= {node["type"] for node in canvas["nodes"]}
                assert response_json(client.get(f"/projects/{project_id}/generations"))["generations"] == []
                write_private_json(directory / "project.json", saved)
                write_private_json(directory / "run.json", run)
                subprocess_result = subprocess.run(["node", str(ROOT / "scripts" / "verify_story_runtime.mjs"), str(directory / "project.json")], check=True, capture_output=True, text=True)
                print(subprocess_result.stdout, end="", flush=True)
                before = database_snapshot(data)
                snapshot_sha, before_pid = digest(before), process.pid
                stop_service(process)
                process = start_service(data, admin, port, log)
                after_pid = process.pid
                assert before_pid != after_pid and digest(database_snapshot(data)) == snapshot_sha
                assert response_json(client.get(f"/projects/{project_id}")) == saved
                assert response_json(client.get(f"/projects/{project_id}/agent-runs/{run_id}"))["run"] == run
                completed, restarted = True, True
        finally:
            stop_service(process)
            stop_pending_runs(data, verification_id)
            budget = reconcile(source_engine, data, verification_id)
            calls = call_records(data, restarted=restarted, success=completed)
            proof = {"schemaVersion": 1, "verificationId": verification_id, "dataDirectory": str(data), "providerDataDirectory": str(ROOT / "data"),
                "workspace": "project", "completed": completed, "restartVerified": restarted, "beforePid": before_pid, "afterPid": after_pid, "snapshotSha256": snapshot_sha,
                "fullStageCoverage": False, "selectedRoles": ["director", "editor", "interaction_designer", "writer"], "runIds": [run_id] if run_id else [],
                "calls": calls, "accounting": accounting(data), "budget": budget, "contentAccepted": False}
            proof_path = directory / "proof.json"
            write_private_json(proof_path, proof)
            if calls:
                register_agent_evidence(calls, proof_path)
            source_engine.dispose()
            print(json.dumps({"phase": "finished", "verificationId": verification_id, "completed": completed, "restartVerified": restarted, "calls": len(calls), "costMicros": budget["spentMicros"], "reservedMicros": budget["reservedMicros"]}), flush=True)


if __name__ == "__main__":
    main()
