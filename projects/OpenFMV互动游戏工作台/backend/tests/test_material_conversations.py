import hashlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.tools.project_tools import MATERIAL_ROLES, allowed_tools
from app.core.config import Settings
from app.services.agent_provider import MATERIAL_NATIVE_AGENT_PROTOCOL, agent_request_body, agent_tools, provider_endpoint
from app.agent_models import AgentCall, AgentRun, AgentStep
from app.models import Project
from app.material_models import MaterialConversation, MaterialMessage
from app.services.materials import view
from app.tools.project_tools import PatchArtifactArguments, _patch_artifact, require_canvas
from test_api import create_project, error_code, registered_client, scene_id


@pytest.fixture
def materials(live_service):
    client, _ = registered_client(live_service)
    project = create_project(client, "素材对话接口验证")
    path = f"/projects/{project['project']['id']}/scenes/{scene_id(project)}/material-conversations"
    yield client, project, path
    client.close()


def test_persistent_creation_is_idempotent_and_capabilities_are_truthful(materials):
    client, project, path = materials
    body = {"operationId": str(uuid.uuid4())}
    first = client.post(path, json=body)
    assert first.status_code == 201, first.text
    value = first.json()["conversation"]
    assert value["capabilities"]["image"] is False
    assert value["capabilities"]["text"] is False
    assert value["messages"] == []
    assert client.post(path, json=body).json() == first.json()
    assert client.get(f"{path}/{value['id']}").json() == first.json()
    assert client.get(path).json()["conversations"][0]["id"] == value["id"]


def test_material_conversation_is_isolated_by_owner_and_scene(materials, live_service):
    client, project, path = materials
    value = client.post(path, json={"operationId": str(uuid.uuid4())}).json()["conversation"]
    outsider, _ = registered_client(live_service)
    assert error_code(outsider.get(path), 404) == "PROJECT_NOT_FOUND"
    assert error_code(outsider.get(f"{path}/{value['id']}"), 404) == "PROJECT_NOT_FOUND"
    outsider.close()
    missing = path.replace(scene_id(project), "missing-scene")
    assert error_code(client.get(missing), 404) == "SCENE_NOT_FOUND"


def test_stale_reference_rejected_before_any_model_request(materials):
    client, project, path = materials
    node_id = str(uuid.uuid4())
    response = client.put(f"/projects/{project['project']['id']}/canvases/{scene_id(project)}", json={
        "operationId": str(uuid.uuid4()), "expectedRevision": project["revision"], "nodes": [{"id": node_id, "type": "text", "title": "当前文本",
            "position": {"x": 0, "y": 0}, "width": 320, "height": 240, "metadata": {"content": "当前真实输入"}}],
        "connections": [], "viewport": {"x": 0, "y": 0, "k": 1}})
    assert response.status_code == 200, response.text
    value = client.post(path, json={"operationId": str(uuid.uuid4())}).json()["conversation"]
    assert value["nodes"] == []
    saved_project = client.get(f"/projects/{project['project']['id']}").json()
    assert saved_project["canvases"][0]["nodes"][0]["metadata"]["content"] == "当前真实输入"
    body = {"operationId": str(uuid.uuid4()), "expectedRevision": response.json()["revision"], "content": "修改选中的文本", "references": [{"nodeId": node_id, "contentHash": "0" * 64}]}
    assert error_code(client.post(f"{path}/{value['id']}/messages", json=body), 409) == "MATERIAL_REFERENCE_STALE"
    body["references"][0]["nodeId"] = "foreign-node"
    assert error_code(client.post(f"{path}/{value['id']}/messages", json=body), 422) == "MATERIAL_REFERENCE_INVALID"
    assert client.get(f"{path}/{value['id']}").json()["conversation"]["messages"] == []


def test_material_tools_cannot_edit_story_timeline_or_interactions():
    for role in MATERIAL_ROLES:
        tools = allowed_tools(role, "R5")
        assert "write_artifact" in tools
        assert tools.isdisjoint({"apply_story_ops", "edit_timeline", "edit_interaction", "place_interaction", "arrange_story", "adopt_asset", "join_videos"})
    assert allowed_tools("editor", "R5") == set()
    assert allowed_tools("interaction_designer", "R5") == set()


def test_material_provider_contract_preserves_strict_function_schemas():
    for role in MATERIAL_ROLES:
        for planning in (True, False):
            tools = agent_tools(role, planning, protocol=MATERIAL_NATIVE_AGENT_PROTOCOL, catalog_version="R5")
            body = agent_request_body([{"role": "user", "content": "核对素材岗位的完整函数参数约束"}], 32768, tools, planning,
                Settings(text_max_output_tokens=32768), protocol=MATERIAL_NATIVE_AGENT_PROTOCOL, catalog_version="R5")
            assert body["model"] == "deepseek-flash"
            assert all(tool["function"]["strict"] is True for tool in body["tools"])
            assert {tool["function"]["name"] for tool in tools} == ({"submit_plan"} if planning else allowed_tools(role, "R5") | {"complete_step"})
            assert provider_endpoint(MATERIAL_NATIVE_AGENT_PROTOCOL).startswith("https://api.deepseek.com/beta/")


def test_consecutive_material_patch_uses_actual_complete_saved_receipt():
    data = Path(__file__).resolve().parents[2] / ".intermediate/agent-smoke/182dd93f-d4ae-430b-b58c-db41eec02a38/data"
    database = data / "openfmv.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        call = session.scalar(select(AgentCall).where(AgentCall.run_id == "b9a14daf-073b-4dd6-99d9-cfdf92e028c4", AgentCall.status == "failed"))
        assert call and call.response_json["tool"] == "patch_artifact"
        run = session.get(AgentRun, call.run_id)
        step = session.get(AgentStep, call.step_id)
        project = session.get(Project, run.project_id)
        canvas = require_canvas(session, run, project)
        body = PatchArtifactArguments.model_validate(call.response_json["arguments"])
        artifact, proof = _patch_artifact(session, run, step, project, canvas, body, step.role, Settings(data_dir=data))
        assert len(proof["changedPaths"]) == 3
        assert all("清晨" in artifact["scenes"][0]["imagePrompts"][index]["lighting"] for index in (1, 2, 3))
    engine.dispose()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_actual_two_turn_conversation_displays_latest_output_and_keeps_prior_artifacts():
    database = Path(__file__).resolve().parents[2] / ".intermediate/agent-smoke/84f354fc-6605-4a56-8c81-2569712d2899/data/openfmv.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    with Session(engine) as session:
        conversation = session.scalar(select(MaterialConversation))
        messages = session.scalars(select(MaterialMessage).where(MaterialMessage.conversation_id == conversation.id).order_by(MaterialMessage.sequence)).all()
        assert len(messages) == 2
        latest = session.get(AgentRun, messages[-1].run_id)
        assert latest.status == "completed"
        result = view(session, conversation)
        assert len(result["messages"]) == 2 and len(result["nodes"]) == 1
        assert result["nodes"][0]["producerRole"] == "scene_designer"
        canvas = require_canvas(session, latest, session.get(Project, latest.project_id))
        assert len([node for node in canvas.data_json["nodes"] if node["type"] != "agent"]) == 2
        assert any(node["metadata"]["creativeArtifact"]["role"] == "writer" for node in canvas.data_json["nodes"])
    engine.dispose()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
