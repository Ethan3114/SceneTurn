import argparse
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings
from app.db import SessionLocal
from app.image_models import ImageJob
from app.material_models import MaterialConversation
from app.media_schemas import MediaOperation, SequenceCreate
from app.models import Generation, Project
from app.services.generation import confirm_generation
from app.services.media_sequences import create_sequence, quote_segment, sequence_view
from agent_smoke_evidence import write_private_json
from smoke_sensetime_queue import LABELS


def prepare(directory):
    source = json.loads((ROOT / ".intermediate/sensetime-queue-20261115/request.json").read_bytes())
    with SessionLocal() as session:
        conversation = session.get(MaterialConversation, source["conversationId"])
        jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == source["runId"], ImageJob.status == "ready")).all()
        by_label = {job.label: job for job in jobs}
        if set(by_label) != set(LABELS):
            raise ValueError("四张已验收的商汤图片尚未齐备")
        project = session.get(Project, conversation.project_id)
        body = SequenceCreate(operation_id=str(uuid.uuid4()), expected_revision=project.revision,
            title="纸艺连续首尾帧验证", conversation_id=conversation.id,
            frame_asset_ids=[by_label[label].asset_id for label in LABELS],
            segments=[{"title": f"{LABELS[index]} → {LABELS[index + 1]}", "prompt": "固定机位、写实摄影，木桌上的纸艺缓慢而平滑地折叠变化，严格保持给定首尾画面，背景、木桌和柔和侧光稳定，不出现文字、水印和额外物体。", "duration": 2, "resolution": "720P"} for index in range(3)])
        sequence = create_sequence(session, project.id, conversation.scene_id, conversation.owner_id, body, get_settings())
        generations = []
        for segment in sequence.input_json["segments"]:
            generation = quote_segment(session, project.id, sequence.id, segment["key"], conversation.owner_id,
                MediaOperation(operation_id=str(uuid.uuid4()), expected_revision=session.get(Project, project.id).revision), get_settings())
            generations.append({"id": generation.id, "confirmationHash": generation.input_hash, "projectRevision": generation.project_revision,
                "frames": generation.input_json["boundaryFrames"]["frames"], "clipId": generation.clip_id})
        request = {"projectId": project.id, "sceneId": conversation.scene_id, "conversationId": conversation.id,
            "sequenceId": sequence.id, "generations": generations, "sequence": sequence_view(session, sequence)}
        write_private_json(directory / "request.json", request)
        print(json.dumps({"projectId": project.id, "sequenceId": sequence.id, "segments": 3, "durationEach": 2, "resolution": "720P",
            "url": f"http://127.0.0.1:3210/zh-CN/editor?id={project.id}"}), flush=True)


def confirm(directory):
    request = json.loads((directory / "request.json").read_bytes())
    for item in request["generations"]:
        with SessionLocal() as session:
            generation = session.get(Generation, item["id"])
            if generation.project_id != request["projectId"] or generation.input_hash != item["confirmationHash"] or generation.input_json["boundaryFrames"]["frames"] != item["frames"]:
                raise ValueError("已准备的首尾帧视频输入发生变化")
            confirm_generation(session, generation.id, generation.owner_id, item["confirmationHash"], item["projectRevision"], get_settings())
    write_private_json(directory / "user-confirmation.json", {"confirmed": True, "generationIds": [item["id"] for item in request["generations"]]})
    print("已确认三个固定首尾帧任务；按照原队列顺序处理。", flush=True)


def status(directory):
    request = json.loads((directory / "request.json").read_bytes())
    with SessionLocal() as session:
        values = [session.get(Generation, item["id"]) for item in request["generations"]]
        print(json.dumps({"statuses": [value.status for value in values], "ready": sum(value.status == "ready" for value in values),
            "reservedCents": sum(value.reserved_cents for value in values), "actualCents": sum(value.actual_cents for value in values)}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "confirm", "status"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--confirmed", action="store_true")
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(ROOT / ".intermediate"):
        raise ValueError("验证目录必须在本产品.intermediate内")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == "prepare":
        if (directory / "request.json").exists():
            raise ValueError("视频序列已经准备，请使用原请求")
        prepare(directory)
    elif args.action == "confirm":
        if not args.confirmed:
            raise ValueError("本次三个真实视频请求需要本人明确确认")
        confirm(directory)
    else:
        status(directory)


if __name__ == "__main__":
    main()
