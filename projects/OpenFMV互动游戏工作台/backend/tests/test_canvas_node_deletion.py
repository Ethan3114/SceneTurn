import hashlib
import json
import sqlite3
import uuid
from contextlib import closing

import pytest

from test_api import create_project, error_code, registered_client, scene_id
from test_generation_canvas import accepted_quote, connection, node, quote, save_canvas, upload_input


def database_rows(service, statement, parameters=()):
    with sqlite3.connect(f"file:{service.data_dir / 'openfmv.sqlite3'}?mode=ro", uri=True) as database:
        return database.execute(statement, parameters).fetchall()


def budget_state(service):
    return database_rows(service, "SELECT spent_cents,reserved_cents,text_spent_micros,text_reserved_micros FROM budgets WHERE id='global'")


@pytest.mark.parametrize("source_kind,deleted_id", [
    ("config", "source"),
    ("text", "source"),
    ("video", "source"),
    ("config", "output"),
    ("config", "reference"),
])
def test_node_deletion_keeps_quote_asset_history_and_rejects_confirmation(live_service, tmp_path, source_kind, deleted_id):
    client, _ = registered_client(live_service)
    with closing(client):
        original_text = "清晨，车站的灯光逐渐亮起。\n这是实际上传并保留的创作文字。"
        text_file = tmp_path / "创作资料.txt"
        text_file.write_text(original_text, encoding="utf-8")
        envelope, asset = upload_input(client, create_project(client), (text_file, "text/plain"))
        nodes = [
            node("reference", "text", "创作资料", content=original_text, storageKey=asset["id"]),
            node("source", source_kind, "固定来源", generationMode="video", composerContent="固定镜头，清晨车站。"),
            node("output", "video", "固定结果", pendingResult=True),
            node("unrelated", "text", "其他节点", content="独立保留的人工内容。"),
        ]
        links = [connection("reference", "source"), connection("source", "output")]
        envelope = save_canvas(client, envelope, nodes, links)
        project_id, target_scene = envelope["project"]["id"], scene_id(envelope)
        baseline_assets = envelope["project"]["assets"]
        baseline_graph = envelope["project"]["graphData"]
        baseline_budget = budget_state(live_service)
        generation = accepted_quote(quote(client, envelope, "source", "固定镜头，清晨车站。", "output"), live_service)
        assert client.get("/settings/wan").json()["configured"] is False
        persisted_statement = "SELECT status,reserved_cents,remote_task_id,asset_id,input_hash,source_digest FROM generations WHERE id=?"
        original_task = database_rows(live_service, persisted_statement, (generation["id"],))
        remaining_nodes = [item for item in nodes if item["id"] != deleted_id]
        remaining_links = [item for item in links if deleted_id not in {item["fromNodeId"], item["toNodeId"]}]
        changed = save_canvas(client, envelope, remaining_nodes, remaining_links)
        assert changed["revision"] == generation["projectRevision"] + 1
        assert changed["project"]["assets"] == baseline_assets
        assert changed["project"]["graphData"] == baseline_graph
        current = client.get(f"/projects/{project_id}/canvases/{target_scene}")
        assert current.status_code == 200
        assert current.json()["canvas"]["nodes"] == remaining_nodes
        assert current.json()["canvas"]["connections"] == remaining_links
        assert deleted_id not in {item["id"] for item in current.json()["canvas"]["nodes"]}
        for revision in (generation["projectRevision"], changed["revision"]):
            denied = client.post(f"/generations/{generation['id']}/confirm", json={"confirmationHash": generation["confirmationHash"], "expectedRevision": revision})
            assert error_code(denied, 409) == "PROJECT_REVISION_CONFLICT"
        repeated = client.post(f"/projects/{project_id}/generations/quote", json=generation["input"]["request"])
        assert repeated.status_code == 201 and repeated.json()["generation"] == generation
        assert client.get(f"/generations/{generation['id']}").json()["generation"] == generation
        assert client.get(f"/projects/{project_id}/generations").json()["generations"] == [generation]
        assert database_rows(live_service, persisted_statement, (generation["id"],)) == original_task
        assert budget_state(live_service) == baseline_budget
        content = client.get(f"/assets/{asset['id']}/content")
        assert content.status_code == 200 and content.content == text_file.read_bytes()
        assert hashlib.sha256(content.content).hexdigest() == hashlib.sha256(original_text.encode()).hexdigest()
        assert database_rows(live_service, "SELECT COUNT(*) FROM assets WHERE project_id=? AND id=?", (project_id, asset["id"])) == [(1,)]
        histories = database_rows(live_service, "SELECT revision,canvases_json FROM project_versions WHERE project_id=? AND revision IN (?,?) ORDER BY revision", (project_id, generation["projectRevision"], changed["revision"]))
        assert len(histories) == 2
        before = next(canvas for canvas in json.loads(histories[0][1]) if canvas["sceneId"] == target_scene)
        after = next(canvas for canvas in json.loads(histories[1][1]) if canvas["sceneId"] == target_scene)
        assert before["nodes"] == nodes and before["connections"] == links
        assert after["nodes"] == remaining_nodes and after["connections"] == remaining_links
        final_canvas = client.get(f"/projects/{project_id}/canvases/{target_scene}").json()
        assert final_canvas == current.json()


def test_node_deletion_with_orphan_connections_is_rejected_without_changes(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        nodes = [
            node("reference", "text", "输入文字", content="车站逐渐亮起。"),
            node("source", "config", "视频生成", generationMode="video", composerContent="固定镜头。"),
            node("output", "video", "视频结果"),
        ]
        links = [connection("reference", "source"), connection("source", "output")]
        envelope = save_canvas(client, create_project(client), nodes, links)
        project_id, target_scene = envelope["project"]["id"], scene_id(envelope)
        generation = accepted_quote(quote(client, envelope, "source", "固定镜头。", "output"), live_service)
        original = client.get(f"/projects/{project_id}/canvases/{target_scene}").json()
        original_budget = budget_state(live_service)
        response = client.put(f"/projects/{project_id}/canvases/{target_scene}", json={
            "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()),
            "nodes": [item for item in nodes if item["id"] != "source"], "connections": links,
            "viewport": original["canvas"]["viewport"],
        })
        assert client.get(f"/projects/{project_id}/canvases/{target_scene}").json() == original
        assert client.get(f"/generations/{generation['id']}").json()["generation"] == generation
        assert budget_state(live_service) == original_budget
        assert error_code(response, 422) == "VALIDATION_ERROR"
