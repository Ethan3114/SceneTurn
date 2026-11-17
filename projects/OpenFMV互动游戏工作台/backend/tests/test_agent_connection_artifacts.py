import copy
import json
import sqlite3
import uuid
from contextlib import closing

import pytest

from app.tools.project_tools import ContextArguments
from test_agent_runtime import bridge_client, claim_run, create_run, tool
from test_api import create_project, error_code, registered_client, scene_id
from test_generation_canvas import connection, node, save_canvas


@pytest.mark.parametrize("action", ["connect", "disconnect"])
@pytest.mark.parametrize("changed", ["connection", "text", "position"])
def test_saved_connection_and_node_artifacts_reject_manual_changes(live_service, action, changed):
    with closing(registered_client(live_service)[0]) as client:
        original_links = [connection("source", "target")] if action == "disconnect" else []
        envelope = save_canvas(client, create_project(client), [node("source", "text", "来源", content="本人保存的原文"), node("target", "text", "目标", content="本人授权的正文")], original_links)
        project_id, scene = envelope["project"]["id"], scene_id(envelope)
        response, _ = create_run(client, envelope, targets=["source", "target"])
        assert response.status_code == 202
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            written = tool(bridge, run, "apply_canvas_ops", {"operations": [
                {"action": action, "nodeId": "source", "targetId": "target"},
                {"action": "move_node", "nodeId": "source", "x": 280, "y": 340},
                {"action": "update_text", "nodeId": "target", "text": "雨夜里，修表师保存了怀表的线索。"},
            ]})
            assert written.status_code == 200, written.text
            result = written.json()["result"]
            snapshot = result["result"]["connectionSnapshots"][0]
            assert snapshot["fromNodeId"] == "source" and snapshot["toNodeId"] == "target"
            assert snapshot["present"] == (action == "connect")
            run = {**run, "projectRevision": result["revision"]}
            checked = tool(bridge, run, "validate_artifacts", {})
            assert checked.status_code == 200, checked.text
            proof = checked.json()["result"]["result"]
            assert proof["valid"] and proof["readOnly"]
            assert {artifact["kind"] for artifact in proof["artifacts"]} == {"connection", "text", "node"}
            link_proof = next(artifact for artifact in proof["artifacts"] if artifact["kind"] == "connection")
            assert link_proof["sha256"] == snapshot["sha256"]
            assert link_proof["sourceRevision"] == link_proof["verifiedRevision"] == result["revision"]
            with sqlite3.connect(f"file:{live_service.data_dir / 'openfmv.sqlite3'}?mode=ro", uri=True) as database:
                history = database.execute("SELECT canvases_json FROM project_versions WHERE project_id=? AND revision=?", (project_id, result["revision"])).fetchone()
                original = next(item for item in json.loads(history[0]) if item["sceneId"] == scene)
                assert bool(original["connections"]) == snapshot["present"]
            canvas = client.get(f"/projects/{project_id}/canvases/{scene}").json()
            manual = copy.deepcopy(canvas["canvas"])
            if changed == "connection":
                manual["connections"] = [] if action == "connect" else [connection("source", "target")]
            elif changed == "text":
                next(item for item in manual["nodes"] if item["id"] == "target")["metadata"]["content"] = "本人重新编辑后的正文"
            else:
                next(item for item in manual["nodes"] if item["id"] == "source")["position"] = {"x": 560, "y": 680}
            saved = client.put(f"/projects/{project_id}/canvases/{scene}", json={"operationId": str(uuid.uuid4()), "expectedRevision": canvas["revision"],
                "nodes": manual["nodes"], "connections": manual["connections"], "viewport": manual["viewport"]})
            assert saved.status_code == 200, saved.text
            rejected = tool(bridge, run, "validate_artifacts", {})
            assert error_code(rejected, 409) == "REVISION_CONFLICT"
            event = bridge.post("/connections/events", json={"runId": run["id"], "eventType": "disconnected", "errorCode": rejected.json()["error"]["code"]})
            assert event.status_code == 200
            resumed = client.post(f"/projects/{project_id}/agent-runs/{run['id']}/resume", json={"operationId": str(uuid.uuid4()), "expectedRevision": saved.json()["revision"]})
            assert resumed.status_code == 200, resumed.text
            run = claim_run(bridge, run["id"])
            invalidated = tool(bridge, run, "validate_artifacts", {})
            assert error_code(invalidated, 409) == "AGENT_ARTIFACT_CHANGED"
            current = client.get(f"/projects/{project_id}/canvases/{scene}").json()
            assert current == saved.json()


@pytest.mark.parametrize("actions", [("connect", "disconnect"), ("disconnect", "connect")])
def test_same_endpoint_uses_last_saved_connection_state(live_service, actions):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("source", "text", "来源", content="线索"), node("target", "text", "目标", content="剧情")], [connection("source", "target")] if actions[0] == "disconnect" else [])
        project_id = envelope["project"]["id"]
        response, _ = create_run(client, envelope, targets=["source", "target"])
        assert response.status_code == 202
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            written = tool(bridge, run, "apply_canvas_ops", {"operations": [{"action": action, "nodeId": "source", "targetId": "target"} for action in actions]})
            assert written.status_code == 200, written.text
            result = written.json()["result"]
            assert len(result["result"]["connectionSnapshots"]) == 1
            run = {**run, "projectRevision": result["revision"]}
            checked = tool(bridge, run, "validate_artifacts", {})
            assert checked.status_code == 200, checked.text
            artifacts = checked.json()["result"]["result"]["artifacts"]
            assert len(artifacts) == 1 and artifacts[0]["kind"] == "connection"
            assert artifacts[0]["present"] == (actions[-1] == "connect")


def test_context_tool_schema_matches_shared_context_capacity():
    assert ContextArguments.model_json_schema(by_alias=True)["properties"]["maxChars"]["minimum"] == 1024
