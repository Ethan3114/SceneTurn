import argparse
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.connection_schemas import CodexUsage
from app.creative_artifacts import ARTIFACT_CONTRACT_VERSION, render_artifact, validate_artifact
from agent_smoke_evidence import AgentCallEvidence, digest, load_agent_evidence, register_agent_evidence, write_private_json
from smoke_agents import database_snapshot, read_database, response_json, utc
from smoke_text import start_service, stop_service


SETTING = "雨夜的南站，修表师林秋发现怀表的指针倒着走。老人陈伯在候车室等待。两人都没有超能力，情节保持写实。"
BRIDGE = ROOT / "scripts" / "codex-bridge"
TOOLS = ["read_project", "retrieve_context", "write_artifact"]
TERMINAL = {"completed", "failed", "stopped", "conflict", "submission_unknown", "waiting_confirmation", "waiting_connection"}


def private_json(path: Path) -> dict:
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("验收文件缺失或权限超过0600")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("验收文件必须为JSON对象")
    return value


def snapshot(data: Path) -> dict:
    result = database_snapshot(data)
    with read_database(data) as database:
        result["codex_pairings"] = [dict(row) for row in database.execute("SELECT * FROM codex_pairings ORDER BY id")]
        result["codex_connection_events"] = [dict(row) for row in database.execute("SELECT * FROM codex_connection_events ORDER BY id")]
        result["codex_connections"] = [dict(row) for row in database.execute(
            "SELECT id, owner_id, project_id, token_hash, tools_json, client_version, expires_at, revoked_at, created_at FROM codex_connections ORDER BY id")]
    return result


def bridge_command(directory: Path, command: list[str], timeout: float) -> int:
    state = directory / "bridge"
    environment = {**os.environ, "OPENFMV_BRIDGE_STATE_DIR": str(state)}
    descriptor = os.open(directory / "bridge.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as log:
        process = subprocess.Popen(["node", "--import", "tsx", "src/cli.ts", *command], cwd=BRIDGE,
            env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, umask=0o077)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=15)
            raise TimeoutError("本人Codex验证超时，原任务和实际执行记录继续保留") from None
    if code != 0:
        raise RuntimeError("本人Codex连接程序未成功退出，详情保存在本产品私有验证目录")
    return process.pid


def create_sample(client: httpx.Client) -> dict:
    envelope = response_json(client.post("/projects", json={"title": "本人Codex真实工具与恢复验证"}))
    project_id = envelope["project"]["id"]
    scene_id = next(node["id"] for node in envelope["project"]["graphData"]["nodes"] if node["type"] == "scene")
    controller_id, target_id = str(uuid.uuid4()), str(uuid.uuid4())
    goal = (f"请依次执行read_project、retrieve_context、write_artifact，读取本项目已保存的创作约定，"
        f"只通过write_artifact一次写入现有targetId={target_id}，提交完整编剧产物，包含林秋、陈伯、南站和怀表。"
        "本次交付范围为20秒主场景、两个选择及各自的短结局：继续询问怀表往事，或先着手检查修理。"
        "交付故事核心、人物动机与关系、可拍摄场景动作、具体对白、互动选择及不同结果、连续性与拍摄要求。"
        "保留写实设定，倒走表针需要现实依据；场景和分支细节完整，供后续角色、场景和分镜设计直接使用。"
        "本次仅交付编剧产物，给下游岗位列出输入要求。"
        "按本角色产物Schema提供全部字段。工具回执成功后说明实际保存结果并结束回合。"
        "不创建节点、不调用其他工具、不申请图片、视频或音频，不改动其他对象。")
    controller = {"id": controller_id, "type": "agent", "title": "本人Codex编剧", "position": {"x": 40, "y": 80}, "width": 340, "height": 360,
        "metadata": {"role": "writer", "executor": "codex", "goal": goal, "targetIds": [target_id]}}
    target = {"id": target_id, "type": "text", "title": "本人授权的剧本结果", "position": {"x": 500, "y": 80}, "width": 360, "height": 260, "metadata": {"content": ""}}
    canvas = response_json(client.put(f"/projects/{project_id}/canvases/{scene_id}", json={"expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()),
        "nodes": [controller, target], "connections": [{"id": str(uuid.uuid4()), "fromNodeId": controller_id, "toNodeId": target_id}], "viewport": {"x": 20, "y": 30, "k": 0.8}}))
    memory = response_json(client.post(f"/projects/{project_id}/memories", json={"expectedRevision": canvas["revision"], "operationId": str(uuid.uuid4()),
        "kind": "setting", "subject": "创作约定", "content": SETTING, "sources": [{"kind": "project", "id": project_id, "revision": canvas["revision"]}]}))
    payload = {"operationId": str(uuid.uuid4()), "expectedRevision": memory["revision"], "sceneId": scene_id, "controllerNodeId": controller_id,
        "role": "writer", "executor": "codex", "deliveryMode": "professional", "goal": goal, "targetIds": [target_id], "limits": {"maxSteps": 6, "maxTokens": 8192, "maxCostMicros": 1000000}}
    run = response_json(client.post(f"/projects/{project_id}/agent-runs", json=payload))["run"]
    replay = response_json(client.post(f"/projects/{project_id}/agent-runs", json=payload))["run"]
    if replay["id"] != run["id"] or run["status"] != "waiting_connection":
        raise ValueError("真实Codex运行没有按唯一操作等待连接")
    return {"projectId": project_id, "sceneId": scene_id, "controllerId": controller_id, "targetId": target_id,
        "runId": run["id"], "memoryId": memory["memory"]["id"], "controller": controller, "request": payload}


def verify_sample(client: httpx.Client, data: Path, sample: dict, state: Path) -> dict:
    run = response_json(client.get(f"/projects/{sample['projectId']}/agent-runs/{sample['runId']}"))["run"]
    if run["status"] != "completed" or run["executor"] != "codex" or run["role"] != "writer" or run["targetIds"] != [sample["targetId"]] or run["createdNodeIds"]:
        raise ValueError("本人Codex真实任务尚未完成或授权对象不一致")
    if run.get("deliveryMode") != "professional" or run.get("artifactContractVersion") != ARTIFACT_CONTRACT_VERSION:
        raise ValueError("本人Codex没有保存本次专业交付模式及契约版本")
    if not run["threadId"] or not run["turnId"] or not run["finishedAt"]:
        raise ValueError("本人Codex缺少真实线程、回合或完成时间")
    canvas = response_json(client.get(f"/projects/{sample['projectId']}/canvases/{sample['sceneId']}"))
    nodes = {node["id"]: node for node in canvas["canvas"]["nodes"]}
    if set(nodes) != {sample["controllerId"], sample["targetId"]} or nodes[sample["controllerId"]] != sample["controller"]:
        raise ValueError("本人Codex修改了授权范围以外的对象")
    output = nodes[sample["targetId"]]["metadata"].get("content")
    if not isinstance(output, str) or not output.strip():
        raise ValueError("本人Codex没有交付有效的目标正文")
    artifact = validate_artifact("writer", nodes[sample["targetId"]]["metadata"].get("creativeArtifact"))
    if render_artifact(artifact) != output or not nodes[sample["targetId"]]["metadata"].get("artifactContractVersion"):
        raise ValueError("本人Codex的结构化产物、完整正文及契约版本不一致")
    quality_issues = []
    missing_names = [name for name in ("林秋", "陈伯", "南站", "怀表") if name not in output]
    if missing_names:
        quality_issues.append("本次正文未直接出现：" + "、".join(missing_names) + "，待本人核对表达")
    receipts = [receipt for step in run["steps"] for receipt in step["toolResults"]]
    kinds = [receipt["tool"] for receipt in receipts]
    if any(kind not in TOOLS for kind in kinds) or any(kind not in kinds for kind in TOOLS):
        raise ValueError("本人Codex没有完成本次真实项目工具链路")
    if not kinds.index("read_project") < kinds.index("retrieve_context") < kinds.index("write_artifact"):
        raise ValueError("本人Codex没有按本次要求读取项目与上下文后再写入")
    writes = [receipt for receipt in receipts if receipt["tool"] == "write_artifact"]
    contexts = [receipt for receipt in receipts if receipt["tool"] == "retrieve_context"]
    if len(writes) != 1 or writes[0]["arguments"].get("targetId") != sample["targetId"] or validate_artifact("writer", writes[0]["arguments"]["artifact"]) != artifact or writes[0]["result"].get("artifact") != artifact:
        raise ValueError("本人Codex实际写入与绑定文字目标不一致")
    if not any(any(memory["id"] == sample["memoryId"] and memory["content"] == SETTING and memory["valid"] for memory in receipt["result"]["memories"]) for receipt in contexts):
        raise ValueError("本人Codex未通过工具取得真实记忆及其来源")
    checkpoint = private_json(state / f"{run['id']}.json")
    if (checkpoint["runId"], checkpoint["threadId"], checkpoint["turnId"], checkpoint["pendingCallId"]) != (run["id"], run["threadId"], run["turnId"], None):
        raise ValueError("本机持久检查点与实际运行标识不一致")
    with read_database(data) as database:
        for receipt in receipts:
            row = database.execute("SELECT response_json FROM operations WHERE project_id=? AND operation_id=? AND kind='agent.tool'", (sample["projectId"], receipt["operationId"])).fetchone()
            if row is None or json.loads(row["response_json"]) != {"result": receipt["result"], "revision": receipt["revision"]}:
                raise ValueError("本人Codex工具结果缺少真实幂等操作记录")
        history = database.execute("SELECT canvases_json FROM project_versions WHERE project_id=? AND revision=?", (sample["projectId"], run["projectRevision"])).fetchone()
        if history is None:
            raise ValueError("本人Codex实际写入缺少历史版本")
        frozen_canvas = next(value for value in json.loads(history["canvases_json"]) if value["sceneId"] == sample["sceneId"])
        frozen_metadata = next(node for node in frozen_canvas["nodes"] if node["id"] == sample["targetId"])["metadata"]
        if frozen_metadata.get("content") != output or frozen_metadata.get("creativeArtifact") != artifact:
            raise ValueError("本人Codex实际写入与项目历史不一致")
    return {"run": run, "canvas": canvas, "artifact": artifact, "qualityIssues": quality_issues, "outputSha256": hashlib.sha256(output.encode()).hexdigest(), "artifactSha256": digest(artifact), "checkpointSha256": digest(checkpoint)}


def call_records(data: Path, state: Path, *, success: bool, restarted: bool) -> list[dict]:
    if not (data / "openfmv.sqlite3").is_file():
        return []
    records = []
    with read_database(data) as database:
        rows = database.execute("SELECT c.*, r.project_id, r.started_at AS run_started, r.thread_id, r.turn_id, r.status AS run_status FROM agent_calls c JOIN agent_runs r ON r.id=c.run_id ORDER BY c.created_at, c.id").fetchall()
        for row in rows:
            if row["provider"] != "personal-codex" or row["cost_micros"] is not None or not row["remote_request_id"]:
                raise ValueError("本人Codex验证出现错误的调用来源或费用归属")
            frozen, response, usage = json.loads(row["input_json"]), json.loads(row["response_json"]), json.loads(row["usage_json"])
            if frozen != {"threadId": row["thread_id"], "turnId": row["turn_id"]} or row["remote_request_id"] != row["turn_id"]:
                raise ValueError("本人Codex真实线程、回合与调用账目不一致")
            checkpoint = private_json(state / f"{row['run_id']}.json")
            if (checkpoint["threadId"], checkpoint["turnId"]) != (row["thread_id"], row["turn_id"]):
                raise ValueError("本人Codex调用缺少相同的本机真实检查点")
            reported = checkpoint.get("usage")
            if reported is None:
                if usage or any(row[field] is not None for field in ("input_tokens", "output_tokens", "total_tokens")) or row["model"] != "unknown":
                    raise ValueError("无法核验的本人Codex用量应保持未知")
            else:
                actual = CodexUsage.model_validate(reported).model_dump(by_alias=True)
                saved = CodexUsage.model_validate(usage).model_dump(by_alias=True)
                if actual != saved or (row["input_tokens"], row["output_tokens"], row["total_tokens"], row["model"]) != (saved["inputTokens"], saved["outputTokens"], saved["totalTokens"], saved["model"]):
                    raise ValueError("本人Codex原生上报用量、检查点与保存账目不一致")
                if saved["inputTokens"] + saved["outputTokens"] != saved["totalTokens"]:
                    raise ValueError("本人Codex实际Token合计不一致")
            event = database.execute("SELECT created_at FROM codex_connection_events WHERE run_id=? AND event_type='turn_started' AND thread_id=? AND turn_id=? ORDER BY created_at LIMIT 1", (row["run_id"], row["thread_id"], row["turn_id"])).fetchone()
            started = event["created_at"] if event is not None else row["run_started"]
            if started is None or row["finished_at"] is None or started > row["finished_at"]:
                raise ValueError("本人Codex调用缺少可核验的原始运行时间")
            record = AgentCallEvidence(callId=row["id"], runId=row["run_id"], projectId=row["project_id"], role=row["role"], provider=row["provider"], model=row["model"], execution="personal-codex",
                requestId=row["remote_request_id"], inputTokens=row["input_tokens"], outputTokens=row["output_tokens"], totalTokens=row["total_tokens"], costMicros=None,
                startedAt=utc(started), finishedAt=utc(row["finished_at"]), structureValid=success and row["run_status"] == "completed", restartVerified=restarted,
                retryCount=0, result="通过" if success and restarted and row["run_status"] == "completed" else "失败", inputSha256=digest(frozen), outputSha256=digest(response)).model_dump()
            records.append(record)
    return records + incomplete_call_records(data, state)


def incomplete_call_records(data: Path, state: Path) -> list[dict]:
    records = []
    with read_database(data) as database:
        runs = database.execute("SELECT r.* FROM agent_runs r WHERE r.executor='codex' AND r.turn_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM agent_calls c WHERE c.run_id=r.id AND c.remote_request_id=r.turn_id)").fetchall()
        for run in runs:
            if run["status"] == "completed":
                raise ValueError("已完成的本人Codex回合缺少正式调用记录")
            events = database.execute("SELECT * FROM codex_connection_events WHERE run_id=? AND thread_id=? AND turn_id=? ORDER BY created_at,id", (run["id"], run["thread_id"], run["turn_id"])).fetchall()
            started = next((event for event in events if event["event_type"] == "turn_started"), None)
            if started is None:
                raise ValueError("本人Codex未完成回合缺少真实开始事件")
            checkpoint = private_json(state / f"{run['id']}.json")
            if (checkpoint["threadId"], checkpoint["turnId"]) != (run["thread_id"], run["turn_id"]):
                raise ValueError("未完成回合的实际检查点不一致")
            observed_usage = CodexUsage.model_validate(checkpoint["usage"]).model_dump(by_alias=True) if checkpoint.get("usage") else None
            actual_events = [{"id": event["id"], "type": event["event_type"], "createdAt": utc(event["created_at"]), "details": json.loads(event["details_json"])} for event in events]
            record = AgentCallEvidence(callId=started["id"], runId=run["id"], projectId=run["project_id"], role=run["role"], provider="personal-codex",
                model=observed_usage["model"] if observed_usage else "unknown", execution="personal-codex", requestId=run["turn_id"],
                inputTokens=None, outputTokens=None, totalTokens=None, costMicros=None, startedAt=utc(started["created_at"]), finishedAt=None,
                structureValid=False, restartVerified=False, retryCount=0, result="失败",
                inputSha256=digest({"threadId": run["thread_id"], "turnId": run["turn_id"], "goal": run["goal"], "initialRevision": run["initial_revision"], "limits": json.loads(run["limits_json"])}),
                outputSha256=digest({"events": actual_events, "observedUsage": observed_usage})).model_dump()
            records.append(record)
    return records


def attempts(data: Path) -> list[dict]:
    if not (data / "openfmv.sqlite3").is_file():
        return []
    with read_database(data) as database:
        return [{**dict(row), "observedUsage": json.loads(row["result_json"] or "{}").get("reportedCodexUsage")} for row in database.execute("SELECT id, status, thread_id, turn_id, started_at, finished_at, result_json FROM agent_runs WHERE executor='codex' ORDER BY created_at")]


def stop_queued_sample(client: httpx.Client, sample: dict) -> str:
    project = response_json(client.get(f"/projects/{sample['projectId']}"))
    response = response_json(client.post(f"/projects/{sample['projectId']}/agent-runs", json={"operationId": str(uuid.uuid4()), "expectedRevision": project["revision"],
        "sceneId": sample["sceneId"], "role": "writer", "executor": "codex", "goal": "检查本人对排队任务的停止操作，本任务不启动模型", "targetIds": [sample["targetId"]]}))
    run_id = response["run"]["id"]
    stopped = response_json(client.post(f"/projects/{sample['projectId']}/agent-runs/{run_id}/stop", json={"operationId": str(uuid.uuid4())}))["run"]
    if stopped["status"] != "stopped" or stopped["threadId"] or stopped["turnId"] or stopped["calls"]:
        raise ValueError("本人停止排队任务后仍存在模型回合或调用")
    return run_id


def verify_existing(verification_id: str) -> dict:
    directory = ROOT / ".intermediate" / "codex-smoke" / verification_id
    proof = private_json(directory / "proof.json")
    sample_result = directory / "sample-result.json"
    if proof.get("sampleResultSha256") is not None and (not sample_result.is_file() or hashlib.sha256(sample_result.read_bytes()).hexdigest() != proof["sampleResultSha256"]):
        raise ValueError("本人Codex实际样例和质量检查文件发生变化")
    data, state = directory / "data", directory / "bridge"
    if proof.get("schemaVersion") != 1 or proof.get("verificationId") != verification_id or not proof.get("completed") or not proof.get("restartVerified") or not proof.get("revocationVerified"):
        raise ValueError("本人Codex原始验证尚未完整通过")
    if digest(snapshot(data)) != proof["finalSnapshotSha256"]:
        raise ValueError("本人Codex持久数据与原验证哈希不一致")
    for name, expected in proof["snapshotFiles"].items():
        if name not in {"before-replay.json", "after-replay.json", "before-restart.json", "after-restart.json", "after-revoke.json", "after-denied.json"} or digest(private_json(directory / name)) != expected:
            raise ValueError("本人Codex实际恢复及撤销检查的保存快照不一致")
    private_json(state / "credentials.json")
    if hashlib.sha256((state / "credentials.json").read_bytes()).hexdigest() != proof["credentialsSha256"]:
        raise ValueError("本人Codex连接凭据文件与原验证不一致")
    calls = call_records(data, state, success=True, restarted=True)
    if calls != proof["calls"] or len(calls) != 1:
        raise ValueError("本人Codex实际调用、用量或原始时间发生变化")
    registry = load_agent_evidence()
    if any(not any(entry["callId"] == call["callId"] for entry in registry["records"]) for call in calls):
        raise ValueError("本人Codex实际调用缺少正式登记")
    return {"verificationId": verification_id, "readonlyVerified": True, "calls": len(calls), "personalAccountCost": "unknown"}


def main() -> None:
    parser = argparse.ArgumentParser(description="使用本人真实Codex连接完成受控项目工具、停止、重连、实际重启与撤销验证；本人账户费用未知，不调用图片或视频")
    parser.add_argument("--timeout", type=float, default=600, help="本人真实Codex任务等待秒数，30至600秒")
    parser.add_argument("--verify-run", help="只读复核原验证ID，不启动服务、连接或模型回合")
    parser.add_argument("--record-incomplete", help="依据原真实开始事件补记未完成回合，不改原证明、不启动模型")
    arguments = parser.parse_args()
    if arguments.verify_run:
        print(json.dumps(verify_existing(str(uuid.UUID(arguments.verify_run))), ensure_ascii=False))
        return
    if arguments.record_incomplete:
        verification_id = str(uuid.UUID(arguments.record_incomplete))
        directory = ROOT / ".intermediate" / "codex-smoke" / verification_id
        source = private_json(directory / "proof.json")
        if source.get("completed"):
            raise ValueError("此入口仅登记真实未完成的回合")
        records = incomplete_call_records(directory / "data", directory / "bridge")
        if not records:
            raise ValueError("没有尚未登记且具有真实开始事件的回合")
        proof_path = directory / "incomplete-evidence.json"
        if proof_path.exists():
            saved = private_json(proof_path)
            if saved["calls"] != records:
                raise ValueError("原未完成回合事件已有变化，需核实原执行记录")
            print(json.dumps({"verificationId": verification_id, "alreadyRecorded": True, "modelRequests": 0}, ensure_ascii=False))
            return
        proof = {"schemaVersion": 1, "verificationId": verification_id, "completed": False, "restartVerified": False,
            "callEvidenceSource": "codex_connection_events.turn_started", "calls": records, "attempts": attempts(directory / "data"),
            "sourceProofSha256": hashlib.sha256((directory / "proof.json").read_bytes()).hexdigest(),
            "sourceDatabaseSha256": hashlib.sha256((directory / "data" / "openfmv.sqlite3").read_bytes()).hexdigest(),
            "usageComplete": False, "personalAccountCost": "unknown"}
        write_private_json(proof_path, proof)
        registered = register_agent_evidence(records, proof_path)
        print(json.dumps({"verificationId": verification_id, "registeredCalls": registered, "usageComplete": False, "modelRequests": 0}, ensure_ascii=False))
        return
    if not 30 <= arguments.timeout <= 600:
        raise ValueError("本人Codex任务等待时间必须在30至600秒之间")
    verification_id = str(uuid.uuid4())
    directory = ROOT / ".intermediate" / "codex-smoke" / verification_id
    directory.mkdir(parents=True, mode=0o700)
    data, state = directory / "data", directory / "bridge"
    sample, connection_id, stopped_run_id = None, None, None
    completed, restarted, revoked = False, False, False
    snapshot_files, pids = {}, {}
    process = None
    admin, password = f"codex-smoke-{uuid.uuid4().hex}@example.com", secrets.token_urlsafe(24)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (directory / "service.log").open("w", encoding="utf-8") as log:
        try:
            process = start_service(data, admin, port, log)
            base_url = f"http://127.0.0.1:{port}/api/v1"
            with httpx.Client(base_url=base_url, headers={"Origin": "http://127.0.0.1:3210"}, timeout=30, trust_env=False) as client:
                response_json(client.post("/auth/register", json={"email": admin, "password": password}))
                sample = create_sample(client)
                write_private_json(directory / "request.json", sample)
                pairing = response_json(client.post(f"/projects/{sample['projectId']}/connections/pairings", json={"operationId": str(uuid.uuid4()), "tools": TOOLS, "expiresInSeconds": 3600}))
                request_file = directory / "pairing.json"
                write_private_json(request_file, {"apiBase": base_url, "pairingId": pairing["pairingId"], "pairingCode": pairing["pairingCode"]})
                pids["bridgePair"] = bridge_command(directory, ["pair", "--request-file", str(request_file)], 60)
                credentials = private_json(state / "credentials.json")
                connection_id = credentials["connectionId"]
                if credentials["projectId"] != sample["projectId"]:
                    raise ValueError("本人Codex配对绑定了其他项目")
                credentials_hash = hashlib.sha256((state / "credentials.json").read_bytes()).hexdigest()
                headers = {"Authorization": f"Bearer {credentials['token']}"}
                pids["bridgeTurn"] = bridge_command(directory, ["run", "--once"], arguments.timeout)
                original = verify_sample(client, data, sample, state)
                write_private_json(directory / "sample-result.json", original)
                stopped_run_id = stop_queued_sample(client, sample)
                before = snapshot(data)
                write_private_json(directory / "before-replay.json", before)
                snapshot_files["before-replay.json"] = digest(before)
                pids["bridgeReplay"] = bridge_command(directory, ["run", "--once"], 60)
                after = snapshot(data)
                write_private_json(directory / "after-replay.json", after)
                snapshot_files["after-replay.json"] = digest(after)
                if after != before or verify_sample(client, data, sample, state) != original:
                    raise ValueError("本人Codex停止后重新连接出现重复回合、项目写入或调用")
                pids["beforeRestart"] = process.pid
                write_private_json(directory / "before-restart.json", after)
                snapshot_files["before-restart.json"] = digest(after)
                stop_service(process)
                if process.poll() is None:
                    raise ValueError("本人Codex验收服务尚未实际停止")
                process = start_service(data, admin, port, log)
                pids["afterRestart"] = process.pid
                if pids["beforeRestart"] == pids["afterRestart"]:
                    raise ValueError("本人Codex验收没有实际更换后端进程")
                if verify_sample(client, data, sample, state) != original:
                    raise ValueError("实际重启后本人Codex任务、工具、记忆或结果发生变化")
                heartbeat = response_json(client.post("/connections/heartbeat", json={}, headers=headers))
                if heartbeat["connection"]["id"] != connection_id:
                    raise ValueError("实际重启后本人原连接未恢复")
                pids["bridgeRestored"] = bridge_command(directory, ["run", "--once"], 60)
                restored = snapshot(data)
                write_private_json(directory / "after-restart.json", restored)
                snapshot_files["after-restart.json"] = digest(restored)
                if restored != after:
                    raise ValueError("实际重启后本人Codex项目或原调用发生变化")
                restarted = True
                response = response_json(client.post(f"/projects/{sample['projectId']}/connections/{connection_id}/revoke", json={"operationId": str(uuid.uuid4())}))
                if response["status"] != "revoked":
                    raise ValueError("本人Codex连接未完成撤销")
                revoked_snapshot = snapshot(data)
                write_private_json(directory / "after-revoke.json", revoked_snapshot)
                snapshot_files["after-revoke.json"] = digest(revoked_snapshot)
                denied = client.post("/connections/heartbeat", json={}, headers=headers)
                if denied.status_code != 403 or denied.json()["error"]["code"] != "CONNECTION_REVOKED":
                    raise ValueError("撤销后的原本人连接仍可访问项目")
                denied_snapshot = snapshot(data)
                write_private_json(directory / "after-denied.json", denied_snapshot)
                snapshot_files["after-denied.json"] = digest(denied_snapshot)
                if denied_snapshot != revoked_snapshot or hashlib.sha256((state / "credentials.json").read_bytes()).hexdigest() != credentials_hash:
                    raise ValueError("撤销后的拒绝请求改变了项目、任务或原连接文件")
                if denied_snapshot["assets"] or denied_snapshot["generations"] or denied_snapshot["text_generations"] or denied_snapshot["text_usage_ledger"] or denied_snapshot["budget_reservations"]:
                    raise ValueError("本人Codex文本验证出现其他收费或媒体任务")
                completed, revoked = True, True
        finally:
            stop_service(process)
            calls = call_records(data, state, success=completed, restarted=restarted)
            proof = {"schemaVersion": 1, "verificationId": verification_id, "dataDirectory": str(data), "bridgeStateDirectory": str(state),
                "completed": completed, "restartVerified": restarted, "revocationVerified": revoked, "processes": pids,
                "sample": sample, "connectionId": connection_id, "stoppedRunId": stopped_run_id, "calls": calls, "attempts": attempts(data), "snapshotFiles": snapshot_files,
                "credentialsSha256": hashlib.sha256((state / "credentials.json").read_bytes()).hexdigest() if (state / "credentials.json").is_file() else None,
                "finalSnapshotSha256": digest(snapshot(data)) if (data / "openfmv.sqlite3").is_file() else None,
                "heartbeatVolatileFields": ["codex_connections.last_seen_at"], "personalAccountCost": "unknown",
                "sampleResultSha256": hashlib.sha256((directory / "sample-result.json").read_bytes()).hexdigest() if (directory / "sample-result.json").is_file() else None}
            proof_path = directory / "proof.json"
            write_private_json(proof_path, proof)
            registered = register_agent_evidence(calls, proof_path) if calls else 0
            print(json.dumps({"verificationId": verification_id, "completed": completed, "restartVerified": restarted, "revocationVerified": revoked,
                "runId": sample["runId"] if sample else None, "recordedCalls": len(calls), "registeredCalls": registered,
                "personalAccountCost": "unknown", "proof": str(proof_path.relative_to(ROOT))}, ensure_ascii=False), flush=True)
    if not completed:
        raise RuntimeError("本人Codex真实验收尚未完成")


if __name__ == "__main__":
    main()
