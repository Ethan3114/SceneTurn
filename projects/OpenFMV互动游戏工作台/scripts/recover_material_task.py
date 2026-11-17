import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent_models import AgentRun
from app.core.config import Settings
from app.material_models import MaterialConversation, MaterialMessage
from app.agent_schemas import RunRetryUnknown, RunLimitExtension
from app.models import TextProviderSettings, User
from app.services.agent import extend_run_limit, resume_run, retry_unknown_run
from app.services.materials import view
from app.services.projects import require_project
from agent_smoke_evidence import load_agent_evidence, register_agent_evidence, write_private_json
from smoke_agents import call_records


def api_pid():
    result = subprocess.run(["lsof", "-tiTCP:8310", "-sTCP:LISTEN"], check=True, capture_output=True, text=True)
    identifiers = result.stdout.split()
    if len(identifiers) != 1:
        raise ValueError("8310端口必须由单个预览服务提供")
    return int(identifiers[0])


def main():
    parser = argparse.ArgumentParser(description="恢复本人素材任务并核验实际重启，不提交媒体生成确认")
    parser.add_argument("action", choices=["resume", "retry-unknown", "extend-limit", "snapshot", "record"])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--call-id")
    parser.add_argument("--confirmed", action="store_true")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--capture-id", type=uuid.UUID)
    args = parser.parse_args()
    settings = Settings(data_dir=ROOT / "data")
    engine = create_engine(settings.database_url, connect_args={"timeout": 10})
    directory = ROOT / ".intermediate/material-recovery" / args.run_id
    if args.capture_id:
        directory /= str(args.capture_id)
    with Session(engine) as session:
        run = session.get(AgentRun, args.run_id)
        provider = session.get(TextProviderSettings, "platform")
        if run is None or provider is None or run.owner_id != provider.admin_owner_id or run.limits_json.get("workspace") != "materials":
            raise ValueError("只能处理本机管理员自己的素材任务")
        project = require_project(session, run.project_id, run.owner_id)
        if args.action in {"resume", "retry-unknown", "extend-limit"}:
            operation = str(uuid.uuid4())
            if args.action == "resume":
                result = resume_run(session, run.project_id, run.id, run.owner_id, operation, project.revision)
                repeated = resume_run(session, run.project_id, run.id, run.owner_id, operation, project.revision)
            elif args.action == "retry-unknown":
                body = RunRetryUnknown(operation_id=operation, expected_revision=project.revision, call_id=args.call_id,
                    confirmed=args.confirmed, max_steps=args.max_steps)
                user = session.get(User, run.owner_id)
                result = retry_unknown_run(session, run.project_id, run.id, user, body, settings)
                repeated = retry_unknown_run(session, run.project_id, run.id, user, body, settings)
            else:
                body = RunLimitExtension(operation_id=operation, expected_revision=project.revision, confirmed=args.confirmed, max_steps=args.max_steps)
                user = session.get(User, run.owner_id)
                result = extend_run_limit(session, run.project_id, run.id, user, body, settings)
                repeated = extend_run_limit(session, run.project_id, run.id, user, body, settings)
            assert repeated.id == result.id
            print(json.dumps({"runId": run.id, "status": result.status, "idempotent": True}), flush=True)
            engine.dispose()
            return
        if run.status not in {"completed", "waiting_confirmation", "submission_unknown", "failed"}:
            raise ValueError("只能记录已经完成或明确暂停的素材任务")
        if (directory / "proof.json").exists():
            raise FileExistsError("该次核验已有证明，请使用新的capture-id保留全部记录")
        message = session.scalar(select(MaterialMessage).where(MaterialMessage.run_id == run.id))
        conversation = session.get(MaterialConversation, message.conversation_id)
        snapshot = view(session, conversation)
        signature = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        pid = api_pid()
        if args.action == "snapshot":
            write_private_json(directory / "before.json", {"pid": pid, "signature": signature, "conversation": snapshot})
            print(json.dumps({"runId": run.id, "status": run.status, "pid": pid}), flush=True)
        else:
            before = json.loads((directory / "before.json").read_text(encoding="utf-8"))
            if before["pid"] == pid or before["signature"] != signature:
                raise ValueError("实际服务进程必须改变，持久对话与产物必须一致")
            all_records = call_records(ROOT / "data", restarted=True, success=run.status == "completed", run_ids={run.id})
            previous = [item for item in load_agent_evidence()["records"] if item["runId"] == run.id]
            previous_ids = {item["callId"] for item in previous}
            records = [item for item in all_records if item["callId"] not in previous_ids]
            proof = {"schemaVersion": 1, "runId": run.id, "dataDirectory": str(ROOT / "data"), "recoveryVerified": True,
                "restartVerified": True, "beforePid": before["pid"], "afterPid": pid, "completed": run.status == "completed",
                "awaitingMediaConfirmation": run.status == "waiting_confirmation", "submissionUnknown": run.status == "submission_unknown",
                "failureCode": run.error_code, "requestLimitReached": run.error_code == "AGENT_STEP_LIMIT",
                "unresolvedModelCallIds": [item["callId"] for item in all_records if item["costMicros"] is None],
                "runCallCount": len(all_records), "previousProofPaths": sorted({item["proofPath"] for item in previous}),
                "fullStageCoverage": False, "calls": records}
            write_private_json(directory / "conversation.json", snapshot)
            write_private_json(directory / "proof.json", proof)
            if records:
                register_agent_evidence(records, directory / "proof.json")
            print(json.dumps({"runId": run.id, "status": run.status, "newCalls": len(records), "totalCalls": len(all_records), "restartVerified": True}), flush=True)
    engine.dispose()


if __name__ == "__main__":
    main()
