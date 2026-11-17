import uuid

import pytest

from app.services.connections import SUPPORTED_CODEX_VERSION
from app.tools.project_tools import tool_catalog
from test_api import create_project, error_code, registered_client, scene_id


@pytest.fixture
def chat(live_service):
    client, _ = registered_client(live_service)
    project = create_project(client, "持续项目对话验证")
    project_id = project["project"]["id"]
    response = client.post(f"/projects/{project_id}/conversations", json={"operationId": str(uuid.uuid4())})
    assert response.status_code == 201, response.text
    value = response.json()["conversation"]
    yield client, project, value
    client.close()


def send(client, project, conversation, text="请说明当前项目", operation=None):
    return client.post(f"/projects/{project['project']['id']}/conversations/{conversation['id']}/messages", json={"operationId": operation or str(uuid.uuid4()), "expectedRevision": project["revision"], "content": text, "context": {"sceneId": scene_id(project), "surface": "editor"}})


def claim(live_service, client, project):
    pid = project["project"]["id"]
    tools = [item["name"] for item in tool_catalog("director", "R4")]
    pairing = client.post(f"/projects/{pid}/connections/pairings", json={"operationId": str(uuid.uuid4()), "tools": tools}).json()
    claimed = client.post(f"/connections/pairings/{pairing['pairingId']}/claim", json={"pairingCode": pairing["pairingCode"], "clientVersion": SUPPORTED_CODEX_VERSION})
    assert claimed.status_code == 200, claimed.text
    bridge = live_service.client()
    bridge.headers["Authorization"] = f"Bearer {claimed.json()['token']}"
    value = bridge.post("/connections/claim")
    assert value.status_code == 200, value.text
    return bridge, value.json()["result"]


def test_message_is_persistent_idempotent_and_uses_only_personal_codex(chat):
    client, project, conversation = chat
    operation = str(uuid.uuid4())
    first = send(client, project, conversation, operation=operation)
    assert first.status_code == 202, first.text
    value = first.json()["conversation"]
    assert value["run"]["executor"] == "codex" and value["run"]["toolCatalogVersion"] == "R4"
    assert value["run"]["calls"] == []
    repeated = send(client, project, conversation, operation=operation)
    assert repeated.status_code == 202 and repeated.json() == first.json()
    restored = client.get(f"/projects/{project['project']['id']}/conversations/{conversation['id']}")
    assert restored.status_code == 200 and restored.json() == first.json()
    assert len(value["messages"]) == 1


def test_chat_busy_and_stale_project_inputs_are_rejected(chat):
    client, project, conversation = chat
    assert send(client, project, conversation).status_code == 202
    assert error_code(send(client, project, conversation, "追加操作"), 409) == "CONVERSATION_BUSY"


def test_conversation_ownership_is_checked_by_backend(chat, live_service):
    client, project, conversation = chat
    outsider, _ = registered_client(live_service)
    route = f"/projects/{project['project']['id']}/conversations/{conversation['id']}"
    assert error_code(outsider.get(route), 404) == "PROJECT_NOT_FOUND"
    assert error_code(outsider.post(route + "/messages", json={"operationId": str(uuid.uuid4()), "expectedRevision": project["revision"], "content": "修改项目"}), 404) == "PROJECT_NOT_FOUND"
    outsider.close()


def test_real_connection_claim_includes_skills_and_current_selection(chat, live_service):
    client, project, conversation = chat
    assert send(client, project, conversation).status_code == 202
    bridge, value = claim(live_service, client, project)
    assert len(value["skills"]) == 5
    assert {"arrange_story", "place_interaction", "undo_project_edit"} <= {tool["name"] for tool in value["tools"]}
    assert "editingContext" in value["instructions"] and scene_id(project) in value["instructions"]
    assert value["run"]["calls"] == []
    bridge.close()


def test_real_tools_save_graph_positions_and_connections(chat, live_service):
    client, project, conversation = chat
    first = send(client, project, conversation, "调整场景位置")
    assert first.status_code == 202
    bridge, value = claim(live_service, client, project)
    run = value["run"]
    scene = scene_id(project)
    end = next(node["id"] for node in project["project"]["graphData"]["nodes"] if node["type"] == "end")
    result = bridge.post("/connections/tools", json={"runId": run["id"], "operationId": str(uuid.uuid4()), "expectedRevision": run["projectRevision"], "toolName": "arrange_story",
        "arguments": {"operations": [{"action": "move", "nodeId": scene, "x": 900, "y": 340}, {"action": "connect", "sourceId": scene, "targetId": end}]}})
    assert result.status_code == 200, result.text
    restored = client.get(f"/projects/{project['project']['id']}").json()
    saved = next(node for node in restored["project"]["graphData"]["nodes"] if node["id"] == scene)
    assert saved["position"] == {"x": 900, "y": 340}
    assert any(edge.get("sourceHandle") == "node:default" and edge["source"] == scene and edge["target"] == end for edge in restored["project"]["graphData"]["edges"])
    bridge.close()


def test_context_rejects_foreign_selected_objects(chat):
    client, project, conversation = chat
    response = client.post(f"/projects/{project['project']['id']}/conversations/{conversation['id']}/messages", json={"operationId": str(uuid.uuid4()), "expectedRevision": project["revision"], "content": "移动选中的片段", "context": {"selectedClipIds": [str(uuid.uuid4())]}})
    assert error_code(response, 422) == "CHAT_CONTEXT_INVALID"
