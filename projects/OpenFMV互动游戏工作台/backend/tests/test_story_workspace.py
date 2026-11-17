import uuid
from contextlib import ExitStack

import pytest

from app.services.connections import SUPPORTED_CODEX_VERSION
from app.tools.project_tools import PROJECT_ROLE_TOOLS, role_catalog, tool_catalog
from test_api import create_project, error_code, registered_client, scene_id


@pytest.fixture
def workspace(live_service, sample_video):
    with ExitStack() as resources:
        client, _ = registered_client(live_service)
        resources.callback(client.close)
        project = create_project(client)
        project_id = project["project"]["id"]
        first = scene_id(project)
        response = client.post(f"/projects/{project_id}/scenes", json={"title": "第二幕", "expectedRevision": project["revision"], "operationId": str(uuid.uuid4())})
        assert response.status_code == 201, response.text
        with sample_video.open("rb") as stream:
            uploaded = client.post(f"/projects/{project_id}/assets", files={"file": ("真实视频.mp4", stream, "video/mp4")})
        assert uploaded.status_code == 201, uploaded.text
        project = uploaded.json()
        second = next(node["id"] for node in project["project"]["graphData"]["nodes"] if node["type"] == "scene" and node["id"] != first)
        created = client.post(f"/projects/{project_id}/agent-runs", json={"operationId": str(uuid.uuid4()), "expectedRevision": project["revision"], "sceneId": first,
            "workspace": "project", "role": "director", "executor": "codex", "deliveryMode": "tool_action", "goal": "编辑真实时间线和互动", "targetIds": [first]})
        assert created.status_code == 202, created.text
        run = created.json()["run"]
        pair = client.post(f"/projects/{project_id}/connections/pairings", json={"operationId": str(uuid.uuid4()), "tools": sorted(PROJECT_ROLE_TOOLS["director"])})
        assert pair.status_code == 201, pair.text
        value = pair.json()
        claimed = client.post(f"/connections/pairings/{value['pairingId']}/claim", json={"pairingCode": value["pairingCode"], "clientVersion": SUPPORTED_CODEX_VERSION})
        assert claimed.status_code == 200, claimed.text
        bridge = resources.enter_context(live_service.client())
        bridge.headers["Authorization"] = f"Bearer {claimed.json()['token']}"
        claimed_run = bridge.post("/connections/claim")
        assert claimed_run.status_code == 200, claimed_run.text
        assert claimed_run.json()["result"]["run"]["toolCatalogVersion"] == "R3"
        def execute(name, arguments, operation=None, revision=None):
            current = client.get(f"/projects/{project_id}/agent-runs/{run['id']}").json()["run"]
            return bridge.post("/connections/tools", json={"runId": run["id"], "operationId": operation or str(uuid.uuid4()), "expectedRevision": current["projectRevision"] if revision is None else revision, "toolName": name, "arguments": arguments})
        yield {"client": client, "execute": execute, "projectId": project_id, "first": first, "second": second, "asset": project["asset"], "run": run}


def test_workspace_roles_and_frozen_catalogs():
    assert {role["id"] for role in role_catalog("project")} == {"director", "writer", "editor", "interaction_designer"}
    assert not {"director", "editor", "interaction_designer"} & {role["id"] for role in role_catalog("scene")}
    assert "edit_interaction" in {tool["name"] for tool in tool_catalog("interaction_designer", "R3")}
    assert "edit_timeline" not in {tool["name"] for tool in tool_catalog("interaction_designer", "R3")}
    assert "read_story" not in {tool["name"] for tool in tool_catalog("director", "R2")}


def add_clip(workspace):
    response = workspace["execute"]("edit_timeline", {"sceneId": workspace["first"], "assetId": workspace["asset"]["id"], "startTime": 0, "duration": 1, "sourceStart": 0})
    assert response.status_code == 200, response.text
    return response.json()["result"]["result"]


def test_actual_timeline_and_qte_edges_are_saved(workspace):
    media = add_clip(workspace)
    response = workspace["execute"]("edit_interaction", {"sceneId": workspace["first"], "label": "立即进入第二幕", "startTime": 0, "duration": 1, "mode": "qte", "targetId": workspace["second"], "timeoutTargetId": workspace["second"]})
    assert response.status_code == 200, response.text
    result = response.json()["result"]["result"]
    saved = workspace["client"].get(f"/projects/{workspace['projectId']}").json()
    scene = next(item for item in saved["project"]["graphData"]["nodes"] if item["id"] == workspace["first"])
    clips = [clip for track in scene["data"]["timeline"]["tracks"] for clip in track["clips"]]
    assert media["clipId"] in {clip["id"] for clip in clips}
    assert result["clip"]["qte"]["input"] == "click"
    assert {edge["sourceHandle"] for edge in result["edges"]} == {f"button:{result['clipId']}:click", f"button:{result['clipId']}:timeout"}
    validated = workspace["execute"]("validate_story", {})
    assert validated.status_code == 200, validated.text
    assert validated.json()["result"]["result"]["valid"] is True
    assert validated.json()["result"]["result"]["verifiedRevision"] == saved["revision"]


def test_scope_and_actual_media_duration_are_enforced(workspace):
    response = workspace["execute"]("edit_timeline", {"sceneId": workspace["second"], "assetId": workspace["asset"]["id"], "startTime": 0, "duration": 1})
    assert error_code(response, 403) == "AGENT_SCENE_NOT_ALLOWED"
    response = workspace["execute"]("edit_timeline", {"sceneId": workspace["first"], "assetId": workspace["asset"]["id"], "startTime": 0, "duration": 86400})
    assert error_code(response, 422) == "MEDIA_DURATION_CONFLICT"


def test_story_revision_and_idempotence(workspace):
    operation = str(uuid.uuid4())
    revision = workspace["run"]["projectRevision"]
    arguments = {"sceneId": workspace["first"], "title": "开场", "bodyText": "保留现场声音，慢慢靠近树林。"}
    response = workspace["execute"]("apply_story_ops", arguments, operation, revision)
    assert response.status_code == 200, response.text
    repeated = workspace["execute"]("apply_story_ops", arguments, operation, revision)
    assert repeated.status_code == 200 and repeated.json() == response.json()
    outdated = workspace["execute"]("apply_story_ops", arguments, revision=revision)
    assert error_code(outdated, 409) == "REVISION_CONFLICT"


def test_prepared_media_is_in_target_small_canvas_without_submission(workspace):
    response = workspace["execute"]("prepare_scene_media", {"sceneId": workspace["first"], "title": "树林概念图", "prompt": "清晨树林，薄雾，横向中景，自然侧光，不生成文字。", "mediaType": "image"})
    assert response.status_code == 200, response.text
    value = response.json()["result"]["result"]
    assert value["submitted"] is False and value["confirmationRequired"] is True
    canvas = workspace["client"].get(f"/projects/{workspace['projectId']}/canvases/{workspace['first']}").json()["canvas"]
    assert {node["type"] for node in canvas["nodes"]} == {"text", "config", "image"}
    config = next(node for node in canvas["nodes"] if node["type"] == "config")
    assert config["metadata"]["generationMode"] == "image"
    assert len(canvas["connections"]) == 2
    jobs = workspace["client"].get(f"/projects/{workspace['projectId']}/generations").json()["generations"]
    assert jobs == []


def test_manual_asset_adoption_uses_real_timeline_and_owner_checks(workspace, live_service):
    client = workspace["client"]
    project = client.get(f"/projects/{workspace['projectId']}").json()
    payload = {"operationId": str(uuid.uuid4()), "expectedRevision": project["revision"], "sceneId": workspace["first"], "assetId": workspace["asset"]["id"]}
    adopted = client.post(f"/projects/{workspace['projectId']}/timeline-assets", json=payload)
    assert adopted.status_code == 200, adopted.text
    repeated = client.post(f"/projects/{workspace['projectId']}/timeline-assets", json=payload)
    assert repeated.json() == adopted.json()
    adopted_value = adopted.json()
    scene = next(node for node in adopted_value["project"]["graphData"]["nodes"] if node["id"] == workspace["first"])
    clip = next(clip for track in scene["data"]["timeline"]["tracks"] for clip in track["clips"])
    assert clip["duration"] == pytest.approx(workspace["asset"]["durationMs"] / 1000)
    clip.update(playbackRate=2, duration=1, sourceDuration=2)
    saved = client.put(f"/projects/{workspace['projectId']}", json={"project": adopted_value["project"], "expectedRevision": adopted_value["revision"], "operationId": str(uuid.uuid4())})
    assert saved.status_code == 200, saved.text
    trimmed = client.post(f"/projects/{workspace['projectId']}/timeline-assets", json={**payload, "operationId": str(uuid.uuid4()), "expectedRevision": saved.json()["revision"], "clipId": clip["id"], "startTime": 0, "sourceStart": 0.5, "duration": 0.5})
    assert trimmed.status_code == 200, trimmed.text
    restored_scene = next(node for node in trimmed.json()["project"]["graphData"]["nodes"] if node["id"] == workspace["first"])
    restored_clip = next(item for track in restored_scene["data"]["timeline"]["tracks"] for item in track["clips"] if item["id"] == clip["id"])
    assert restored_clip["playbackRate"] == 2 and restored_clip["sourceDuration"] == 1
    outsider, _ = registered_client(live_service)
    assert error_code(outsider.post(f"/projects/{workspace['projectId']}/timeline-assets", json=payload), 404) == "PROJECT_NOT_FOUND"
    outsider.close()


def test_project_run_rejects_material_roles_and_missing_scope(workspace):
    client = workspace["client"]
    project = client.get(f"/projects/{workspace['projectId']}").json()
    body = {"operationId": str(uuid.uuid4()), "expectedRevision": project["revision"], "sceneId": workspace["first"], "workspace": "project", "role": "character_designer", "executor": "codex", "deliveryMode": "tool_action", "goal": "创作角色", "targetIds": [workspace["first"]]}
    assert error_code(client.post(f"/projects/{workspace['projectId']}/agent-runs", json=body), 422) == "AGENT_WORKSPACE_ROLE_INVALID"
    body.update(operationId=str(uuid.uuid4()), role="editor", targetIds=[])
    assert error_code(client.post(f"/projects/{workspace['projectId']}/agent-runs", json=body), 422) == "AGENT_SCENE_SCOPE_REQUIRED"
