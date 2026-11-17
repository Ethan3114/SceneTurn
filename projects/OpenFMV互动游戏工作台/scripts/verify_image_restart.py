import argparse
import hashlib
import json
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import SessionLocal
from app.image_models import ImageAccount, ImageJob
from app.models import Asset
from agent_smoke_evidence import write_private_json


def snapshot(directory):
    request = json.loads((directory / "request.json").read_bytes())
    with SessionLocal() as session:
        jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == request["runId"]).order_by(ImageJob.id)).all()
        result = {"jobs": [], "accounts": []}
        for job in jobs:
            asset = session.get(Asset, job.asset_id) if job.asset_id else None
            actual_hash = hashlib.sha256((ROOT / "data" / asset.relpath).read_bytes()).hexdigest() if asset else None
            if asset and actual_hash != asset.sha256:
                raise ValueError("图片文件摘要校验失败")
            result["jobs"].append({"id": job.id, "status": job.status, "assetId": job.asset_id, "sha256": actual_hash,
                "accountId": job.account_id, "submittedAt": job.submitted_at, "finishedAt": job.finished_at})
        for account in session.scalars(select(ImageAccount).where(ImageAccount.owner_id == jobs[0].owner_id).order_by(ImageAccount.slot)):
            result["accounts"].append({"slot": account.slot, "status": account.status, "activeJobId": account.active_job_id})
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["before", "after"])
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(ROOT / ".intermediate"):
        raise ValueError("验证目录必须位于本产品.intermediate内")
    result = snapshot(directory)
    if args.action == "before":
        write_private_json(directory / "before-restart.json", result)
    else:
        if json.loads((directory / "before-restart.json").read_bytes()) != result:
            raise ValueError("图片任务、实际文件或账号占用在重启后发生变化")
        write_private_json(directory / "after-restart.json", {"verified": True, "snapshot": result})
    print(json.dumps({"action": args.action, "readyImages": sum(job["status"] == "ready" for job in result["jobs"]),
        "unknownRequestsRetained": sum(job["status"] == "submission_unknown" for job in result["jobs"])}))


if __name__ == "__main__":
    main()
