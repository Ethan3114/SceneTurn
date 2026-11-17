import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
RECORD_FILE = PRODUCT_ROOT / ".intermediate" / "legacy-quote" / "record.json"


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        json.dump(value, destination, ensure_ascii=False, indent=2)


def database_path(data_dir: Path) -> Path:
    directory = (PRODUCT_ROOT / data_dir).resolve(strict=True)
    if not directory.is_relative_to(PRODUCT_ROOT):
        raise ValueError("兼容验证的数据目录必须位于本产品内")
    database = (directory / "openfmv.sqlite3").resolve(strict=True)
    if not database.is_relative_to(directory):
        raise ValueError("数据库必须位于明确指定的数据目录内")
    return database


def saved_legacy_fields(database: Path, task_id: str, owner_id: str) -> dict:
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, owner_id, project_id, source_digest, input_hash, created_at, updated_at, started_at, finished_at "
            "FROM generations WHERE id = ? AND owner_id = ?",
            (task_id, owner_id),
        ).fetchone()
        if row is None:
            raise ValueError("旧报价记录与创建账号不匹配")
        return dict(row)


def capture(base_url: str, database: Path) -> None:
    if RECORD_FILE.exists():
        raise ValueError("已保存真实旧报价，原始记录保留不变")
    prompt = "旧版本兼容核查：清晨的空旷车站，固定镜头。"
    node_id = str(uuid.uuid4())
    with httpx.Client(base_url=base_url, headers={"Origin": "http://127.0.0.1:3210"}, timeout=30) as client:
        registered = client.post("/auth/register", json={
            "email": f"legacy-{uuid.uuid4().hex}@example.com",
            "password": secrets.token_urlsafe(24),
        })
        registered.raise_for_status()
        owner_id = registered.json()["user"]["id"]
        created = client.post("/projects", json={"title": "真实旧报价兼容验证"})
        created.raise_for_status()
        envelope = created.json()
        project_id = envelope["project"]["id"]
        scene_id = next(node["id"] for node in envelope["project"]["graphData"]["nodes"] if node["type"] == "scene")
        saved = client.put(f"/projects/{project_id}/canvases/{scene_id}", json={
            "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()),
            "nodes": [{"id": node_id, "type": "video", "title": "真实旧输入", "position": {"x": 80, "y": 80},
                       "width": 280, "height": 180, "metadata": {"prompt": prompt}}],
            "connections": [], "viewport": {"x": 0, "y": 0, "k": 1},
        })
        saved.raise_for_status()
        quoted = client.post(f"/projects/{project_id}/generations/quote", json={
            "sceneId": scene_id, "sourceNodeId": node_id, "prompt": prompt, "duration": 5,
            "resolution": "720P", "operationId": str(uuid.uuid4()),
        })
        quoted.raise_for_status()
        generation = quoted.json()["generation"]
        if generation["status"] != "awaiting_confirmation" or generation["reservedCents"] != 0 or generation["remoteTaskId"] is not None:
            raise ValueError("真实旧报价未保持待确认且零费用预留")
        if "inputCompositionVersion" in generation["input"] or "outputNodeId" in generation:
            raise ValueError("当前服务已经使用新增输入契约，无法登记为旧版本报价")
        private_json(RECORD_FILE, {
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "ownerId": owner_id,
            "generation": generation,
            "databaseFields": saved_legacy_fields(database, generation["id"], owner_id),
        })
        client.post("/auth/logout").raise_for_status()
    print(f"真实旧报价已保存：{generation['id']}；待确认、预留0、未提交远端任务。")


def verify(database: Path) -> None:
    sys.path.insert(0, str(PRODUCT_ROOT / "backend"))
    from app.models import Generation
    from app.services.generation import digest, generation_view

    record = json.loads(RECORD_FILE.read_text(encoding="utf-8"))
    original = record["generation"]
    if saved_legacy_fields(database, original["id"], record["ownerId"]) != record["databaseFields"]:
        raise ValueError("旧报价持久字段或原始时间发生变化")
    engine = create_engine(f"sqlite:///{database.as_uri()}?mode=ro&uri=true")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA query_only = ON")
        with Session(bind=connection, autoflush=False) as session:
            task = session.get(Generation, original["id"])
            if task is None or task.owner_id != record["ownerId"]:
                raise ValueError("本人创建的真实旧报价不存在")
            current = generation_view(task)
            for name, value in original.items():
                if current[name] != value:
                    raise ValueError(f"旧报价字段发生变化：{name}")
            if current.get("outputNodeId") is not None:
                raise ValueError("旧报价新增输出节点必须为空")
            expected = digest({
                "input": task.input_json, "projectId": task.project_id, "sceneId": task.scene_id,
                "sourceNodeId": task.source_node_id, "clipId": task.clip_id,
                "projectRevision": task.project_revision, "sourceDigest": task.source_digest,
                "quotedCents": task.quoted_cents,
            })
            if expected != original["confirmationHash"] or expected != task.input_hash:
                raise ValueError("真实旧报价的原确认摘要不一致")
    engine.dispose()
    print(f"真实旧报价只读核查通过：{original['id']}；旧字段、原始时间、旧确认摘要保持一致。")


def main() -> None:
    parser = argparse.ArgumentParser(description="通过正式API登记本人真实旧报价，再以只读数据库检查版本兼容")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8310/api/v1")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    arguments = parser.parse_args()
    database = database_path(arguments.data_dir)
    if arguments.capture:
        capture(arguments.base_url, database)
    else:
        verify(database)


if __name__ == "__main__":
    main()
