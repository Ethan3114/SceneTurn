import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import select


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT_ROOT / "backend"))

from app.core.config import get_settings
from app.db import SessionLocal
from app.models import Canvas, ProviderSettings, User
from app.services.generation import generation_view, quote_generation
from app.services.projects import create_project_record, project_envelope, record_project_version
from app.services.wan import MODEL, PRICES


def main() -> None:
    parser = argparse.ArgumentParser(description="本机操作者准备待本人确认的真实视频任务，仅创建项目与报价")
    parser.add_argument("--owner-id")
    arguments = parser.parse_args()
    if 5 * PRICES["720P"] > 300:
        raise RuntimeError("当前5秒720P报价超过3元，停止准备本次待确认草稿")
    with SessionLocal() as session:
        owners = session.scalars(select(ProviderSettings.owner_id)).all()
        owner_id = arguments.owner_id
        if owner_id is None:
            if len(owners) != 1:
                raise RuntimeError("请明确指定已配置Wan的账号ID")
            owner_id = owners[0]
        if owner_id not in owners or session.get(User, owner_id) is None:
            raise RuntimeError("指定账号没有已保存的Wan配置")
        project = create_project_record(session, owner_id, "真实视频验证")
        session.flush()
        scene_id = next(node["id"] for node in project.data_json["graphData"]["nodes"] if node["type"] == "scene")
        canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.scene_id == scene_id))
        text_id = str(uuid.uuid4())
        source_id = str(uuid.uuid4())
        output_id = str(uuid.uuid4())
        prompt = "清晨的树林中，阳光穿过树叶，镜头缓慢向前移动。画面稳定，光线自然，不出现文字。"
        composer = f"根据 @[node:{text_id}] 生成视频。"
        canvas.data_json = {
            "sceneId": scene_id,
            "nodes": [
                {"id": text_id, "type": "text", "title": "树林镜头描述", "position": {"x": 40, "y": 80},
                 "width": 340, "height": 220, "metadata": {"content": prompt, "fontSize": 16, "status": "idle"}},
                {"id": source_id, "type": "config", "title": "真实视频生成配置", "position": {"x": 450, "y": 80},
                 "width": 340, "height": 240, "metadata": {"generationMode": "video", "composerContent": composer,
                 "seconds": "5", "vquality": "720P", "model": MODEL, "outputNodeId": output_id, "status": "idle"}},
                {"id": output_id, "type": "video", "title": "树林镜头视频结果", "position": {"x": 880, "y": 80},
                 "width": 360, "height": 240, "metadata": {"status": "idle"}},
            ],
            "connections": [
                {"id": str(uuid.uuid4()), "fromNodeId": text_id, "toNodeId": source_id},
                {"id": str(uuid.uuid4()), "fromNodeId": source_id, "toNodeId": output_id},
            ],
            "viewport": {"x": 20, "y": 80, "k": 0.8},
        }
        project.revision += 1
        project.updated_at = time.time()
        record_project_version(session, project)
        session.commit()
        task = quote_generation(session, project.id, owner_id, {
            "sceneId": scene_id, "sourceNodeId": source_id, "outputNodeId": output_id, "clipId": None, "prompt": composer,
            "duration": 5, "resolution": "720P", "operationId": str(uuid.uuid4()),
        }, get_settings())
        if task.quoted_cents > 300 or task.status != "awaiting_confirmation" or task.reserved_cents != 0 or task.remote_task_id is not None:
            raise RuntimeError("草稿必须为最高3元、待确认、零预留且没有远端请求")
        result = {"projectId": project.id, "sceneId": scene_id, "textNodeId": text_id,
                  "sourceNodeId": source_id, "outputNodeId": output_id, "generationId": task.id, "maxCostCents": task.quoted_cents,
                  "url": f"http://127.0.0.1:3210/zh-CN/studio?id={project.id}&sceneId={scene_id}",
                  "generation": generation_view(task), "revision": project_envelope(session, project)["revision"]}
        output = PRODUCT_ROOT / ".intermediate" / "pending-smoke-task.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(result, destination, ensure_ascii=False, indent=2)
        print(json.dumps({key: result[key] for key in ("projectId", "sceneId", "textNodeId", "sourceNodeId", "outputNodeId", "generationId", "url", "maxCostCents")}, ensure_ascii=False))
        print(f"任务已报价，等待本人页面确认；最高费用{task.quoted_cents / 100:.2f}元；未提交生成请求。")


if __name__ == "__main__":
    main()
