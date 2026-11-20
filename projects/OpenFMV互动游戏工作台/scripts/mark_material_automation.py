import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def mark_message(connection, entry, apply):
    row = connection.execute(
        """
        SELECT m.content, m.sequence, c.id AS conversation_id, c.title, r.id AS run_id, r.limits_json
        FROM material_messages AS m
        JOIN material_conversations AS c ON c.id = m.conversation_id AND c.owner_id = m.owner_id
        JOIN agent_runs AS r ON r.id = m.run_id AND r.owner_id = c.owner_id
            AND r.project_id = c.project_id AND r.scene_id = c.scene_id
        WHERE m.id = ? AND c.project_id = ?
        """,
        (entry["messageId"], entry["projectId"]),
    ).fetchone()
    if row is None:
        raise ValueError("来源台账中的消息不存在或不属于指定项目")
    digest = hashlib.sha3_256(row["content"].encode("utf-8")).hexdigest()
    if digest != entry["contentSha3"]:
        raise ValueError("历史消息内容已经变化，需重新核对来源")
    summary = entry["summary"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 200:
        raise ValueError("请输入不超过200字的公开说明")

    limits = json.loads(row["limits_json"])
    if limits.get("workspace") != "materials":
        raise ValueError("只能标注素材对话消息")
    context = limits["materialContext"]
    source = {"kind": "automation", "summary": summary}
    origin = context.get("messageSource", {"kind": "user"})
    if origin == source:
        return False
    if origin["kind"] != "user":
        raise ValueError("消息已有不同的来源标注，需重新核对")

    if apply:
        context["messageSource"] = source
        connection.execute("UPDATE agent_runs SET limits_json = ? WHERE id = ?",
            (json.dumps(limits, ensure_ascii=False), row["run_id"]))
        if row["sequence"] == 1 and row["title"] == row["content"].strip()[:60]:
            connection.execute("UPDATE material_conversations SET title = ? WHERE id = ?",
                (summary, row["conversation_id"]))
    return True


def main():
    parser = argparse.ArgumentParser(description="按已核实的消息身份及内容摘要标注历史制作助手输入")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="将核对结果写入指定的工作台数据库")
    args = parser.parse_args()
    entries = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries or len({item["messageId"] for item in entries}) != len(entries):
        raise ValueError("来源台账需要包含不重复的消息")
    path = args.database.resolve().as_uri()
    mode = "rw" if args.apply else "ro"
    with sqlite3.connect(f"{path}?mode={mode}", uri=True, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE" if args.apply else "BEGIN")
        changed = sum(mark_message(connection, entry, args.apply) for entry in entries)
    connection.close()
    print(json.dumps({"verified": len(entries), "changed": changed if args.apply else 0,
        "pending": 0 if args.apply else changed}, ensure_ascii=False))


if __name__ == "__main__":
    main()
