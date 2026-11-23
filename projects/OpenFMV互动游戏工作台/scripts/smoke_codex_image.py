import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from agent_smoke_evidence import write_private_json
from smoke_text import ROOT, start_service, stop_service


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="执行本人明确授权的一次真实Codex原生图片任务")
    args = parser.parse_args()
    if not args.execute:
        parser.error("真实图片验证需要明确传入--execute")
    directory = ROOT / ".intermediate" / "codex-image-smoke" / str(uuid.uuid4())
    directory.mkdir(parents=True, mode=0o700)
    runtime = directory / "runtime"
    runtime.mkdir(mode=0o700)
    (ROOT / ".intermediate/text-smoke").mkdir(parents=True, exist_ok=True)
    bridge_state = directory / "bridge-state"
    bridge_state.mkdir(mode=0o700)
    data = directory / "data"
    port = free_port()
    email = f"native-image-{uuid.uuid4().hex}@example.com"
    environment = {**os.environ, "OPENFMV_BRIDGE_STATE_DIR": str(bridge_state.relative_to(ROOT)), "TMPDIR": str(runtime)}
    bridge_command = ["node", "--import", "tsx", "src/cli.ts"]
    bridge_root = ROOT / "scripts/codex-bridge"
    service = None
    proof = {"verificationId": directory.name, "provider": "codex-local", "costStatus": "personal_account_unknown", "status": "started"}
    with (directory / "service.log").open("wb") as service_log, (directory / "bridge.log").open("wb") as bridge_log:
        try:
            service = start_service(data, email, port, service_log)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", timeout=30, trust_env=False) as client:
                client.post("/auth/register", json={"email": email, "password": secrets.token_urlsafe(24)}).raise_for_status()
                created = client.post("/projects", json={"title": "本人Codex原生图片真实验证"})
                created.raise_for_status()
                project_id, revision = created.json()["project"]["id"], created.json()["revision"]
                pairing = client.post(f"/projects/{project_id}/connections/pairings", json={"operationId": str(uuid.uuid4()), "tools": ["read_project"], "expiresInSeconds": 3600})
                pairing.raise_for_status()
                pair = pairing.json()
                request_file = directory / "pairing-request.json"
                write_private_json(request_file, {"apiBase": str(client.base_url).rstrip("/"), "pairingId": pair["pairingId"], "pairingCode": pair["pairingCode"]})
                subprocess.run([*bridge_command, "pair", "--request-file", str(request_file)], cwd=bridge_root, env=environment,
                    stdout=bridge_log, stderr=bridge_log, check=True, timeout=30)
                credentials = json.loads((bridge_state / "credentials.json").read_text())
                headers = {"Authorization": f"Bearer {credentials['token']}"}
                client.post("/connections/heartbeat", json={"capabilities": ["native_image_v1"]}, headers=headers).raise_for_status()
                job_response = client.post(f"/projects/{project_id}/codex-images", json={"operationId": str(uuid.uuid4()), "expectedRevision": revision,
                    "prompt": "原创极简图片：深蓝色纯色背景，中央一个金色圆环，圆环下方一条柔和的金色倒影。无文字、无标志、无水印。横向构图。", "name": "原生Codex原创金环图"})
                job_response.raise_for_status()
                job_id = job_response.json()["job"]["id"]
                path = f"/projects/{project_id}/codex-images/{job_id}"
                client.post(path + "/confirm", json={"operationId": str(uuid.uuid4()), "connectionId": credentials["connectionId"], "accountUsageConfirmed": True}).raise_for_status()
                proof.update(projectId=project_id, jobId=job_id, startedAt=time.time())
                write_private_json(directory / "proof.json", proof)
                print(json.dumps({"verificationId": directory.name, "jobId": job_id, "status": "confirmed"}), flush=True)
                completed = subprocess.run([*bridge_command, "run", "--once"], cwd=bridge_root, env=environment,
                    stdout=bridge_log, stderr=bridge_log, timeout=660, check=False)
                response = client.get(path)
                response.raise_for_status()
                job = response.json()["job"]
                proof.update(job=job, bridgeExitCode=completed.returncode, finishedAt=time.time())
                write_private_json(directory / "proof.json", proof)
                if job["status"] != "ready":
                    raise RuntimeError(f"原生图片任务未完成，status={job['status']}，error={(job.get('error') or {}).get('code')}")
                asset = job["asset"]
                media = client.get(asset["url"].removeprefix("/api/v1"))
                media.raise_for_status()
                if hashlib.sha256(media.content).hexdigest() != asset["sha256"]:
                    raise ValueError("实际导入图片与登记SHA不一致")
                before = client.get(f"/projects/{project_id}").json()["revision"]
                subprocess.run([*bridge_command, "run", "--once"], cwd=bridge_root, env=environment,
                    stdout=bridge_log, stderr=bridge_log, timeout=30, check=True)
                assert client.get(f"/projects/{project_id}").json()["revision"] == before
                stop_service(service)
                service = start_service(data, email, port, service_log)
                restored = client.get(path)
                restored.raise_for_status()
                assert restored.json()["job"] == job
                assert client.get(asset["url"].removeprefix("/api/v1")).content == media.content
                client.post(f"/projects/{project_id}/connections/{credentials['connectionId']}/revoke", json={"operationId": str(uuid.uuid4())}).raise_for_status()
                assert client.post("/connections/images/claim", json={}, headers=headers).status_code == 403
                proof.update(status="passed", restartVerified=True, duplicateRunUnchanged=True, revoked403=True, mediaSha256=asset["sha256"])
                write_private_json(directory / "proof.json", proof)
                print(json.dumps({"verificationId": directory.name, "status": "passed", "assetId": asset["id"], "width": asset["width"], "height": asset["height"], "costStatus": "personal_account_unknown"}), flush=True)
        finally:
            stop_service(service)


if __name__ == "__main__":
    main()
