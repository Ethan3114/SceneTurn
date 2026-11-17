import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


PRODUCT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="只读核对真实ready任务、媒体文件与画布视频引用绑定")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    arguments = parser.parse_args()
    sys.path.insert(0, str(PRODUCT_ROOT / "backend"))
    from app.models import Asset, Canvas, Generation, Project
    from app.services.generation import _ready_video_resources, _resource_input

    directory = (PRODUCT_ROOT / arguments.data_dir).resolve(strict=True)
    if not directory.is_relative_to(PRODUCT_ROOT):
        raise ValueError("实际媒体数据目录必须位于本产品内")
    database = (directory / "openfmv.sqlite3").resolve(strict=True)
    if not database.is_relative_to(directory):
        raise ValueError("数据库必须位于明确指定的数据目录内")
    engine = create_engine(f"sqlite:///{database.as_uri()}?mode=ro&uri=true")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA query_only = ON")
        with Session(bind=connection, autoflush=False) as session:
            task = session.execute(select(
                Generation.id, Generation.owner_id, Generation.project_id, Generation.scene_id,
                Generation.source_node_id, Generation.status, Generation.asset_id,
                Generation.input_json["outputNodeId"].as_string().label("output_node_id"),
            ).where(Generation.id == arguments.task_id)).one()
            if task.status != "ready" or task.asset_id is None:
                raise ValueError("实际媒体引用检查需要真实ready任务")
            project = session.get(Project, task.project_id)
            asset = session.get(Asset, task.asset_id)
            if project is None or asset is None:
                raise ValueError("真实任务缺少所属项目或实际素材")
            if project.owner_id != task.owner_id or asset.owner_id != task.owner_id or asset.project_id != task.project_id or asset.generation_id != task.id:
                raise ValueError("真实任务、项目和媒体的归属不一致")
            canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.scene_id == task.scene_id, Canvas.owner_id == task.owner_id))
            if canvas is None:
                raise ValueError("真实任务没有所属剧情画布")
            node_id = task.output_node_id or task.source_node_id
            node = next(item for item in canvas.data_json["nodes"] if item["id"] == node_id)
            if node["type"] != "video":
                raise ValueError("绑定节点必须是实际视频节点")
            resources = _ready_video_resources(session, project, task.scene_id)
            if resources.get(node_id) != {"assetId": asset.id, "generationId": task.id}:
                raise ValueError("当前视频节点没有绑定指定的真实ready任务")
            resource = _resource_input(node, resources)
            if resource is None or resource["type"] != "video" or resource.get("assetId") != asset.id or resource.get("generationId") != task.id:
                raise ValueError("实际视频未被识别为媒体输入")
            media_file = (directory / asset.relpath).resolve(strict=True)
            if not media_file.is_relative_to(directory / "media"):
                raise ValueError("实际视频文件不在指定数据目录的媒体范围内")
            if media_file.stat().st_size != asset.bytes or hashlib.sha256(media_file.read_bytes()).hexdigest() != asset.sha256:
                raise ValueError("实际视频文件与数据库大小或哈希不一致")
            metadata = node.get("metadata", {})
            result = {
                "verifiedAt": datetime.now(timezone.utc).isoformat(), "generationId": task.id,
                "assetId": asset.id, "nodeId": node_id, "bytes": asset.bytes, "sha256": asset.sha256,
                "hasRawContent": bool(metadata.get("content")), "hasStorageKey": bool(metadata.get("storageKey")),
                "recognizedMediaResource": resource,
            }
    engine.dispose()
    output = PRODUCT_ROOT / ".intermediate" / "real-media-reference" / f"{hashlib.sha256(arguments.task_id.encode()).hexdigest()}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        json.dump(result, destination, ensure_ascii=False, indent=2)
    print(f"真实ready视频引用只读核查通过：{arguments.task_id}；{asset.bytes}字节，实际素材绑定一致。")
    print(f"机器结果：{output.relative_to(PRODUCT_ROOT)}")


if __name__ == "__main__":
    main()
