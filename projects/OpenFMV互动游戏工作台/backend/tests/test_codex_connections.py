import secrets
import sqlite3
import uuid
from typing import get_args

from app.connection_schemas import ConnectionTool
from app.tools.project_tools import tool_catalog
from conftest import LiveService
from test_media_tools import RestartableMediaService


VERSION = "0.168.0"


def account_project(client):
    response = client.post("/auth/register", json={"email": f"connection-{uuid.uuid4().hex}@example.com", "password": secrets.token_urlsafe(18)})
    assert response.status_code == 201
    response = client.post("/projects", json={"title": "本人Codex连接验证"})
    assert response.status_code == 201
    return response.json()["project"]["id"]


def pair(client, project_id, tools=None):
    payload = {"operationId": str(uuid.uuid4()), "tools": tools or ["read_project", "retrieve_context"], "expiresInSeconds": 3600}
    response = client.post(f"/projects/{project_id}/connections/pairings", json=payload)
    assert response.status_code == 201, response.text
    pairing = response.json()
    repeated = client.post(f"/projects/{project_id}/connections/pairings", json=payload)
    assert repeated.json() == pairing
    response = client.post(f"/connections/pairings/{pairing['pairingId']}/claim", json={"pairingCode": pairing["pairingCode"], "clientVersion": VERSION})
    assert response.status_code == 200, response.text
    return pairing, response.json()


def test_real_pairing_hash_scope_and_single_claim(live_service):
    with live_service.client() as client:
        project_id = account_project(client)
        pairing, claimed = pair(client, project_id)
        repeated = client.post(f"/connections/pairings/{pairing['pairingId']}/claim", json={"pairingCode": pairing["pairingCode"], "clientVersion": VERSION})
        assert repeated.status_code == 409
        assert repeated.json()["error"]["code"] == "PAIRING_ALREADY_CLAIMED"
        with sqlite3.connect(f"file:{live_service.data_dir / 'openfmv.sqlite3'}?mode=ro", uri=True) as database:
            code_hash = database.execute("SELECT code_hash FROM codex_pairings WHERE id=?", (pairing["pairingId"],)).fetchone()[0]
            stored_token = database.execute("SELECT token_hash FROM codex_connections WHERE id=?", (claimed["connection"]["id"],)).fetchone()[0]
            operation = database.execute("SELECT response_json FROM operations WHERE kind='codex.pair' AND project_id=?", (project_id,)).fetchone()[0]
        assert code_hash != pairing["pairingCode"] and len(code_hash) == 64
        assert stored_token != claimed["token"] and len(stored_token) == 64
        assert pairing["pairingCode"] not in operation and claimed["token"] not in operation
        visible = client.get(f"/projects/{project_id}/connections").json()["connections"]
        assert visible[0]["tools"] == ["read_project", "retrieve_context"]
        assert "token" not in visible[0] and "tokenHash" not in visible[0]


def test_other_account_cannot_list_or_revoke_connection(live_service):
    with live_service.client() as owner, live_service.client() as other:
        project_id = account_project(owner)
        _, claimed = pair(owner, project_id)
        account_project(other)
        assert other.get(f"/projects/{project_id}/connections").status_code == 404
        response = other.post(f"/projects/{project_id}/connections/{claimed['connection']['id']}/revoke", json={"operationId": str(uuid.uuid4())})
        assert response.status_code == 404
        assert owner.get(f"/projects/{project_id}/connections").json()["connections"][0]["status"] == "online"


def test_connection_requires_bearer_and_revocation_is_immediate(live_service):
    with live_service.client() as client:
        project_id = account_project(client)
        _, claimed = pair(client, project_id)
        assert client.post("/connections/heartbeat", json={}).status_code == 401
        headers = {"Authorization": f"Bearer {claimed['token']}"}
        assert client.post("/connections/heartbeat", json={}, headers=headers).status_code == 200
        response = client.post(f"/projects/{project_id}/connections/{claimed['connection']['id']}/revoke", json={"operationId": str(uuid.uuid4())})
        assert response.status_code == 200 and response.json()["status"] == "revoked"
        for path in ("heartbeat", "claim"):
            rejected = client.post(f"/connections/{path}", json={}, headers=headers)
            assert rejected.status_code == 403
            assert rejected.json()["error"]["code"] == "CONNECTION_REVOKED"


def test_pairing_schema_rejects_arbitrary_commands_and_large_expiry(live_service):
    with live_service.client() as client:
        project_id = account_project(client)
        for update in ({"tools": ["command/exec"]}, {"expiresInSeconds": 86401}, {"method": "process/spawn"}):
            payload = {"operationId": str(uuid.uuid4()), "tools": ["read_project"], "expiresInSeconds": 3600, **update}
            response = client.post(f"/projects/{project_id}/connections/pairings", json=payload)
            assert response.status_code == 422
            assert set(response.json()) == {"error"}


def test_real_r2_pairing_claim_includes_new_tools_and_preserves_existing_authorization(live_service):
    with live_service.client() as client:
        project_id = account_project(client)
        _, original = pair(client, project_id, ["read_project"])
        tools = list(get_args(ConnectionTool))
        assert len(tools) == 25 and {"read_artifact", "patch_artifact", "read_story", "edit_timeline", "edit_interaction", "arrange_story", "move_timeline_clip", "place_interaction", "undo_project_edit"} <= set(tools)
        _, current = pair(client, project_id, tools)
        visible = client.get(f"/projects/{project_id}/connections").json()["connections"]
        assert next(item for item in visible if item["id"] == original["connection"]["id"])["tools"] == ["read_project"]
        assert set(next(item for item in visible if item["id"] == current["connection"]["id"])["tools"]) == set(tools)
        project = client.get(f"/projects/{project_id}").json()
        scene_id = next(node["id"] for node in project["project"]["graphData"]["nodes"] if node["type"] == "scene")
        response = client.post(f"/projects/{project_id}/agent-runs", json={"operationId": str(uuid.uuid4()), "expectedRevision": project["revision"], "sceneId": scene_id,
            "role": "director", "executor": "codex", "goal": "仅核对本人新连接的工具目录和授权，不启动模型回合。", "targetIds": []})
        assert response.status_code == 202, response.text
        claimed = client.post("/connections/claim", json={}, headers={"Authorization": f"Bearer {current['token']}"})
        assert claimed.status_code == 200, claimed.text
        result = claimed.json()["result"]
        assert result["run"]["toolCatalogVersion"] == "R2"
        assert {tool["name"] for tool in result["tools"]} == {tool["name"] for tool in tool_catalog("director", "R2")}
        assert result["run"]["calls"] == []
        assert client.get(f"/projects/{project_id}").json()["revision"] == project["revision"]


def test_real_connection_and_original_session_survive_backend_restart(tmp_path, request):
    service = RestartableMediaService(tmp_path)
    request.addfinalizer(service.stop)
    service.start()
    with service.client() as client:
        project_id = account_project(client)
        _, claimed = pair(client, project_id)
        before_pid = service.process.pid
        service.stop()
        service.start()
        assert service.process.pid != before_pid
        response = client.get(f"/projects/{project_id}/connections")
        assert response.status_code == 200
        assert response.json()["connections"][0]["id"] == claimed["connection"]["id"]
        heartbeat = client.post("/connections/heartbeat", json={}, headers={"Authorization": f"Bearer {claimed['token']}"})
        assert heartbeat.status_code == 200
        assert heartbeat.json()["connection"]["id"] == claimed["connection"]["id"]


def test_actual_run_claim_tool_scope_idempotency_and_revocation(live_service):
    with live_service.client() as client:
        project_id = account_project(client)
        project = client.get(f"/projects/{project_id}").json()
        scene_id = next(node["id"] for node in project["project"]["graphData"]["nodes"] if node["type"] == "scene")
        _, claimed = pair(client, project_id, ["read_project"])
        headers = {"Authorization": f"Bearer {claimed['token']}"}
        response = client.post(f"/projects/{project_id}/agent-runs", json={"operationId": str(uuid.uuid4()), "expectedRevision": project["revision"], "sceneId": scene_id,
            "role": "writer", "executor": "codex", "goal": "读取当前项目并检查实际版本", "targetIds": []})
        assert response.status_code == 202, response.text
        run_id = response.json()["run"]["id"]
        received = client.post("/connections/claim", json={}, headers=headers)
        assert received.status_code == 200, received.text
        assert received.json()["result"]["run"]["id"] == run_id
        assert [tool["name"] for tool in received.json()["result"]["tools"]] == ["read_project"]
        payload = {"runId": run_id, "operationId": str(uuid.uuid4()), "expectedRevision": project["revision"], "toolName": "read_project", "arguments": {}}
        first = client.post("/connections/tools", json=payload, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["result"]["revision"] == project["revision"]
        assert client.post("/connections/tools", json=payload, headers=headers).json() == first.json()
        forbidden = client.post("/connections/tools", json={**payload, "operationId": str(uuid.uuid4()), "toolName": "write_script", "arguments": {"text": "请求未授权写入"}}, headers=headers)
        assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "TOOL_NOT_ALLOWED"
        run = client.get(f"/projects/{project_id}/agent-runs/{run_id}").json()["run"]
        assert run["calls"] == []
        assert len(run["steps"][0]["toolResults"]) == 1
        response = client.post(f"/projects/{project_id}/connections/{claimed['connection']['id']}/revoke", json={"operationId": str(uuid.uuid4())})
        assert response.status_code == 200
        assert client.post("/connections/tools", json=payload, headers=headers).status_code == 403
        assert client.get(f"/projects/{project_id}").json()["revision"] == project["revision"]


def test_real_manual_edit_conflict_survives_connection_event(live_service):
    with live_service.client() as client:
        project_id = account_project(client)
        project = client.get(f"/projects/{project_id}").json()
        scene_id = next(node["id"] for node in project["project"]["graphData"]["nodes"] if node["type"] == "scene")
        _, claimed = pair(client, project_id, ["read_project"])
        headers = {"Authorization": f"Bearer {claimed['token']}"}
        response = client.post(f"/projects/{project_id}/agent-runs", json={"operationId": str(uuid.uuid4()), "expectedRevision": project["revision"], "sceneId": scene_id,
            "role": "writer", "executor": "codex", "goal": "读取本人当前项目", "targetIds": []})
        assert response.status_code == 202
        run_id = response.json()["run"]["id"]
        assert client.post("/connections/claim", json={}, headers=headers).status_code == 200
        canvas = client.get(f"/projects/{project_id}/canvases/{scene_id}").json()
        saved = client.put(f"/projects/{project_id}/canvases/{scene_id}", json={"operationId": str(uuid.uuid4()), "expectedRevision": canvas["revision"],
            "nodes": canvas["canvas"]["nodes"], "connections": canvas["canvas"]["connections"], "viewport": {"x": 24, "y": 36, "k": 1}})
        assert saved.status_code == 200
        rejected = client.post("/connections/tools", json={"runId": run_id, "operationId": str(uuid.uuid4()), "expectedRevision": project["revision"], "toolName": "read_project", "arguments": {}}, headers=headers)
        assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "REVISION_CONFLICT"
        event = client.post("/connections/events", json={"runId": run_id, "eventType": "disconnected", "errorCode": rejected.json()["error"]["code"]}, headers=headers)
        assert event.status_code == 200
        run = client.get(f"/projects/{project_id}/agent-runs/{run_id}").json()["run"]
        assert run["status"] == "conflict" and run["calls"] == []
        assert client.get(f"/projects/{project_id}/canvases/{scene_id}").json()["canvas"]["viewport"] == {"x": 24, "y": 36, "k": 1}
