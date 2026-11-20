import argparse
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent_models import AgentRun
from app.agent_schemas import RunCreate
from app.core.config import Settings
from app.db import SessionLocal
from app.image_models import ImageAccount, ImageJob
from app.material_models import MaterialConversation, MaterialMessage
from app.models import Asset, Canvas, User, new_id
from app.services.agent import create_run
from app.services.image_queue import IMAGE_REQUEST_GAP_SECONDS, image_queue_awaits_result
from app.services.projects import create_project_record, lock_write, payload_digest, record_project_version
from agent_smoke_evidence import write_private_json


LABELS = ["木桌上的红色纸船", "木桌上的蓝色纸鹤", "木桌上的黄色纸风车", "木桌上的绿色纸盒"]


def prepare(email, directory):
    with SessionLocal() as session:
        lock_write(session)
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            raise ValueError("目标用户不存在")
        accounts = session.scalars(select(ImageAccount).where(ImageAccount.owner_id == user.id)).all()
        if len(accounts) != 3 or any(account.status != "available" for account in accounts):
            raise ValueError("三个商汤账号必须全部可用且没有在途请求")
        if session.scalar(select(ImageAccount.id).where(ImageAccount.active_job_id.is_not(None)).limit(1)) is not None:
            raise ValueError("图片队列还有未结束的请求")
        project = create_project_record(session, user.id, "商汤队列真实验证")
        canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id))
        run = create_run(session, project.id, user, RunCreate(operation_id=str(uuid.uuid4()), expected_revision=project.revision,
            scene_id=canvas.scene_id, workspace="materials", role="director", executor="platform", delivery_mode="professional",
            goal="四张静物图片真实队列验证：" + "、".join(LABELS), limits={"maxSteps": 1, "maxTokens": 32768, "maxCostMicros": 10000000}), Settings(text_admin_email=email), commit=False)
        run.status = "waiting_confirmation"
        run.limits_json = {**run.limits_json, "materialContext": {"requireHumanReview": True, "scope": "current_round"},
            "imageValidation": {"confirmedBy": "user_conversation", "model": "sensenova-u1.5-fast", "count": 4, "width": 2048, "height": 1152}}
        conversation = MaterialConversation(owner_id=user.id, project_id=project.id, scene_id=canvas.scene_id, title="商汤队列真实验证", active_run_id=run.id)
        session.add(conversation)
        session.flush()
        session.add(MaterialMessage(owner_id=user.id, conversation_id=conversation.id, operation_id=str(uuid.uuid4()), sequence=1,
            content=run.goal, references_json=[], run_id=run.id))
        nodes = []
        for index, label in enumerate(LABELS):
            identifier = new_id()
            nodes.append({"id": identifier, "type": "image", "title": label, "position": {"x": index * 500, "y": 100}, "width": 460, "height": 300, "metadata": {"status": "idle"}})
            session.add(ImageJob(owner_id=user.id, project_id=project.id, scene_id=canvas.scene_id, run_id=run.id,
                node_id=identifier, artifact_hash=payload_digest(None), operation_id=str(uuid.uuid4()), label=label,
                status="queued", confirmed_at=time.time(), request_json={"prompt": f"{label}，单一主体，完整入镜，写实静物摄影，柔和侧光，干净背景，横向16:9构图，不出现文字、标志和水印。", "width": 2048, "height": 1152}, reference_asset_ids=[], dependency_ids=[]))
        canvas.data_json = {**canvas.data_json, "nodes": nodes}
        run.created_ids_json = [node["id"] for node in nodes]
        project.revision += 1
        run.project_revision = project.revision
        session.flush()
        record_project_version(session, project)
        session.commit()
        result = {"runId": run.id, "projectId": project.id, "sceneId": canvas.scene_id, "conversationId": conversation.id}
        write_private_json(directory / "request.json", result)
        print(json.dumps({**result, "url": f"http://127.0.0.1:3210/zh-CN/editor?id={project.id}"}), flush=True)


def record(directory):
    request = json.loads((directory / "request.json").read_bytes())
    with SessionLocal() as session:
        jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == request["runId"]).order_by(ImageJob.submitted_at, ImageJob.id)).all()
        originals = [job for job in jobs if not job.request_json.get("retryOf")]
        ready = {job.label: job for job in jobs if job.status == "ready"}
        if len(originals) != 4 or set(ready) != set(LABELS):
            print(json.dumps({"statuses": [job.status for job in jobs]}), flush=True)
            return False
        submissions = []
        for job in jobs:
            expired = job.request_json.get("expiredAttempts", [])
            submissions.extend((attempt["submittedAt"], attempt["releasedAt"]) for attempt in expired)
            if not expired or expired[-1]["attempt"] != job.attempt_count:
                submissions.append((job.submitted_at, job.finished_at))
        if any(started is None or released is None for started, released in submissions):
            raise ValueError("图片请求缺少实际提交或释放时间")
        submissions.sort()
        gaps = []
        for previous, current in zip(submissions, submissions[1:]):
            gap = current[0] - previous[1]
            if gap < IMAGE_REQUEST_GAP_SECONDS:
                raise ValueError("相邻商汤生成请求的间隔不足5秒")
            gaps.append(gap)
        queue_proof = {"serial": True, "minimumObservedGapSeconds": min(gaps)}
        records = []
        for job in jobs:
            if not job.asset_id:
                if job.status != "failed" or job.finished_at is None:
                    raise ValueError("仍有未完成的已确认请求")
                records.append({"jobId": job.id, "accountSlot": session.get(ImageAccount, job.account_id).slot,
                    "status": job.status, "assetId": None, "submittedAt": job.submitted_at, "finishedAt": job.finished_at, "attempts": job.attempt_count})
                continue
            asset = session.get(Asset, job.asset_id)
            image = ROOT / "data" / asset.relpath
            if hashlib.sha256(image.read_bytes()).hexdigest() != asset.sha256:
                raise ValueError("实际图片摘要不一致")
            records.append({"jobId": job.id, "accountSlot": session.get(ImageAccount, job.account_id).slot,
                "status": job.status, "assetId": asset.id, "sha256": asset.sha256, "bytes": asset.bytes, "width": asset.width, "height": asset.height,
                "submittedAt": job.submitted_at, "finishedAt": job.finished_at, "attempts": job.attempt_count})
        proof = {**request, "model": "sensenova-u1.5-fast", **queue_proof,
            "freePlanConfirmedByUser": True, "records": records, "totalRequests": len(submissions), "readyImages": 4,
            "unknownRequestsRetained": sum(len(job.request_json.get("expiredAttempts", [])) for job in jobs)}
        write_private_json(directory / "proof.json", proof)
        run = session.get(AgentRun, request["runId"])
        run.status, run.finished_at = "completed", time.time()
        run.result_json = {"imageValidation": {"completedImages": 4, **queue_proof}}
        session.commit()
    evidence = ROOT / "docs/evidence/阶段2-2/图片冒烟记录.md"
    if not evidence.exists():
        evidence.write_text("# 商汤真实图片验证\n\n| 时间 | 模型 | 账号位置 | 图片任务 | 尺寸 | 用时秒 | 实际文件校验 | 尝试次数 |\n| --- | --- | --- | --- | --- | --- | --- | --- |\n", encoding="utf-8")
    with evidence.open("a", encoding="utf-8") as stream:
        for item in records:
            if item["status"] != "ready":
                continue
            stamp = datetime.fromtimestamp(item["submittedAt"], ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
            stream.write(f"| {stamp} | sensenova-u1.5-fast | {item['accountSlot']} | {item['jobId']} | {item['width']}×{item['height']} | {item['finishedAt'] - item['submittedAt']:.2f} | 通过 | {item['attempts']} |\n")
    print(json.dumps({**queue_proof, "images": 4, "proof": str(directory / 'proof.json')}), flush=True)
    return True


def retry_confirmed(directory):
    request = json.loads((directory / "request.json").read_bytes())
    with SessionLocal() as session:
        lock_write(session)
        if image_queue_awaits_result(session):
            raise ValueError("上一单回执待返回，超时后队列会自动继续。")
        original = next((job for job in session.scalars(select(ImageJob).where(ImageJob.run_id == request["runId"],
            ImageJob.label == LABELS[2], ImageJob.status == "failed", ImageJob.finished_at.is_not(None), ImageJob.submitted_at.is_not(None)))
            if not job.request_json.get("retryOf") and job.error_code not in {"IMAGE_RESPONSE_TIMEOUT", "IMAGE_ALTERNATE_ACCOUNT_UNAVAILABLE"}), None)
        if original is None:
            raise ValueError("仅限已明确结束且失败的原请求重试")
        operation = f"{original.id}:user-confirmed-retry"
        if session.scalar(select(ImageJob).where(ImageJob.owner_id == original.owner_id, ImageJob.operation_id == operation)):
            raise ValueError("本次确认的重试已提交，不得重复执行")
        job = ImageJob(owner_id=original.owner_id, project_id=original.project_id, scene_id=original.scene_id,
            run_id=original.run_id, node_id=original.node_id, artifact_hash=original.artifact_hash,
            operation_id=operation, label=original.label, status="queued", confirmed_at=time.time(),
            request_json={**original.request_json, "retryOf": original.id}, reference_asset_ids=original.reference_asset_ids,
            dependency_ids=[])
        session.add(job)
        session.commit()
        write_private_json(directory / "confirmed-retry.json", {"jobId": job.id, "originalJobId": original.id, "originalRetained": True})
        print(json.dumps({"retryQueued": True, "originalRetained": True}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "record", "retry"])
    parser.add_argument("--confirmed", action="store_true")
    parser.add_argument("--email", default="1090444703@qq.com")
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(ROOT / ".intermediate"):
        raise ValueError("验证目录必须位于本产品.intermediate内")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == "prepare":
        if (directory / "request.json").exists():
            raise ValueError("验证请求已经存在，不得重复提交")
        prepare(args.email, directory)
    elif args.action == "retry":
        if not args.confirmed:
            raise ValueError("重试需要用户明确确认")
        retry_confirmed(directory)
    elif (directory / "proof.json").exists():
        raise ValueError("验证结果已经登记")
    else:
        record(directory)


if __name__ == "__main__":
    main()
