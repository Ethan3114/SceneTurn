import copy
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing

import jsonpatch
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun, AgentStep
from app.core.config import Settings, sqlite_database_path
from app.creative_artifacts import image_prompt_items, render_artifact, validate_artifact
from app.models import Project
from app.services.agent import _tool_observation, require_step_delivery
from app.services.connections import SUPPORTED_CODEX_VERSION
from app.services.projects import payload_digest
from app.services.prompts.agent import codex_prompt, json_agent_prompt
from app.tools.project_tools import SCHEMAS, _patchable_paths, tool_catalog
from conftest import PRODUCT_ROOT
from test_agent_professional_artifacts import manual_artifact
from test_agent_runtime import claim_run, create_run, tool
from test_api import create_project, error_code, registered_client
from test_generation_canvas import node, save_canvas


def patch_bridge(service, client, project_id):
    response = client.post(f"/projects/{project_id}/connections/pairings", json={
        "operationId": str(uuid.uuid4()), "tools": sorted(SCHEMAS), "expiresInSeconds": 3600})
    assert response.status_code == 201, response.text
    pairing = response.json()
    response = client.post(f"/connections/pairings/{pairing['pairingId']}/claim", json={
        "pairingCode": pairing["pairingCode"], "clientVersion": SUPPORTED_CODEX_VERSION})
    assert response.status_code == 200, response.text
    bridge = service.client()
    bridge.headers["Authorization"] = f"Bearer {response.json()['token']}"
    return bridge


def archive_project(client, role="writer"):
    archive = create_project(client)
    artifact = validate_artifact(role, manual_artifact(role, archive["project"]["id"]))
    text = render_artifact(artifact)
    target = node("target", "text", "作者提供的结构化原稿", content=text, artifactDraft=artifact,
                  revisionLineage={"sourceProjectId": archive["project"]["id"]},
                  texts=[{"id": "original", "content": text, "status": "success"}], primaryTextId="original")
    envelope = save_canvas(client, create_project(client), [target, node("other", "text", "其他文字", content="本人其他资料")])
    response, _ = create_run(client, envelope, role=role, targets=["target"], delivery_mode="professional")
    assert response.status_code == 202, response.text
    return envelope, artifact, response.json()["run"]["id"]


def patch_arguments(baseline, patches):
    return {"targetId": baseline["targetId"], "expectedRevision": baseline["revision"],
            "baseArtifactSha256": baseline["baseArtifactSha256"], "patches": patches}


def test_real_http_archive_patch_preserves_original_candidates_and_complete_history(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope, original, run_id = archive_project(client)
        project_id = envelope["project"]["id"]
        with closing(patch_bridge(live_service, client, project_id)) as bridge:
            run = claim_run(bridge, run_id)
            assert run["toolCatalogVersion"] == "R2"
            read = tool(bridge, run, "read_artifact", {"targetId": "target"})
            assert read.status_code == 200, read.text
            baseline = read.json()["result"]["result"]
            assert baseline["artifact"] == original and baseline["referenceScope"] == "archive"
            assert baseline["sourcesValidated"] is False and baseline["baseArtifactSha256"] == payload_digest(original)
            assert "/scenes/0/action" in {item["path"] for item in baseline["patchablePaths"]}
            patches = [{"op": "test", "path": "/scenes/0/action", "value": original["scenes"][0]["action"]},
                       {"op": "replace", "path": "/scenes/0/action", "value": "林岚右手托着银色怀表，左手放在桌边，注视表盘后等待玩家选择。"},
                       {"op": "replace", "path": "/continuityRules", "value": ["怀表保持银色表壳和划痕，后盖保持关闭，林岚双手位置连续"]},
                       {"op": "replace", "path": "/sources", "value": [
                           {"kind": "project", "referenceId": project_id, "description": "本次真实项目资料"},
                           {"kind": "canvas_node", "referenceId": "target", "description": "作者提供的本节点原稿版本"}]}]
            operation = str(uuid.uuid4())
            arguments = patch_arguments(baseline, patches)
            response = tool(bridge, run, "patch_artifact", arguments, operation)
            assert response.status_code == 200, response.text
            receipt = response.json()["result"]
            result = receipt["result"]
            expected = validate_artifact("writer", jsonpatch.apply_patch(original, patches, in_place=False))
            assert result["artifact"] == expected and result["baseArtifact"] == original
            assert result["baseArtifactSha256"] == payload_digest(original) and result["patches"] == patches
            assert result["changedPaths"] == ["/scenes/0/action", "/continuityRules", "/sources"]
            assert result["referenceScope"] == "archive" and result["artifactSha256"] == payload_digest(expected)
            assert tool(bridge, run, "patch_artifact", arguments, operation).json() == response.json()
            saved = client.get(f"/projects/{project_id}").json()
            old_node, other = envelope["canvases"][0]["nodes"]
            current, unchanged = saved["canvases"][0]["nodes"]
            assert unchanged == other
            metadata = current["metadata"]
            assert metadata["artifactDraft"] == original and metadata["revisionLineage"] == old_node["metadata"]["revisionLineage"]
            assert metadata["texts"][:-1] == old_node["metadata"]["texts"]
            assert metadata["primaryTextId"] == metadata["texts"][-1]["id"]
            assert metadata["texts"][-1]["content"] == metadata["content"] == render_artifact(expected)
            assert metadata["generationPrompts"] == image_prompt_items(expected)
            assert hashlib.sha256(metadata["content"].encode()).hexdigest() == metadata["artifactContentSha256"]
            assert result["sourceBindings"][-1]["bindingMode"] == "input_snapshot"
            run = {**run, "projectRevision": saved["revision"]}
            reread = tool(bridge, run, "read_artifact", {"targetId": "target"})
            assert reread.status_code == 200, reread.text
            assert reread.json()["result"]["result"]["referenceScope"] == "current"
            assert reread.json()["result"]["result"]["sourcesValidated"] is True
            checked = tool(bridge, run, "validate_artifacts", {})
            assert checked.status_code == 200, checked.text
            assert {item["kind"] for item in checked.json()["result"]["result"]["artifacts"]} == {"text", "creative_artifact"}
            engine = create_engine(f"sqlite:///{live_service.data_dir / 'sceneturn.sqlite3'}")
            with Session(engine) as session:
                actual = session.get(AgentRun, run_id)
                step = session.scalar(select(AgentStep).where(AgentStep.run_id == run_id))
                actual_read = next(item for item in step.tool_results_json if item["tool"] == "read_artifact")
                assert _tool_observation(actual_read)["result"]["artifact"] == original
                assert require_step_delivery(session, actual, step, session.get(Project, project_id), Settings(data_dir=live_service.data_dir))["valid"]
            engine.dispose()
            with sqlite3.connect(f"file:{live_service.data_dir / 'sceneturn.sqlite3'}?mode=ro", uri=True) as database:
                archived = json.loads(database.execute("select canvases_json from project_versions where project_id=? and revision=?", (project_id, envelope["revision"])).fetchone()[0])
                assert archived[0]["nodes"][0] == old_node
                assert database.execute("select count(*) from agent_calls where run_id=?", (run_id,)).fetchone()[0] == 0
                assert database.execute("select count(*) from generations where project_id=?", (project_id,)).fetchone()[0] == 0


@pytest.mark.parametrize("role", ["writer", "character_designer", "scene_designer", "storyboarder", "media_producer", "editor", "interaction_designer", "sound_designer", "director"])
def test_patch_path_catalog_uses_schema_and_preserves_entity_identity(role):
    artifact = validate_artifact(role, manual_artifact(role, "author-project"))
    paths = {item["path"] for item in _patchable_paths(artifact)}
    assert "/title" in paths and "/sources" in paths
    assert "" not in paths and "/role" not in paths and "/schemaVersion" not in paths
    assert all(not path.endswith(("/key", "/shotKey")) for path in paths)
    if role == "writer":
        assert "/characters" not in paths and "/scenes/0" not in paths
        assert "/scenes/0/characters" in paths and "/scenes/0/dialogues" not in paths
    if role == "media_producer":
        assert "/shots/0/sourceBasis" in paths


@pytest.mark.parametrize("scenario,status,code", [
    ("whole", 422, "AGENT_ARTIFACT_PATCH_FORBIDDEN"),
    ("entity", 422, "AGENT_ARTIFACT_PATCH_FORBIDDEN"),
    ("entity_list", 422, "AGENT_ARTIFACT_PATCH_FORBIDDEN"),
    ("identity", 422, "AGENT_ARTIFACT_PATCH_FORBIDDEN"),
    ("no_change", 422, "AGENT_ARTIFACT_PATCH_NO_CHANGE"),
    ("invalid_schema", 422, "AGENT_ARTIFACT_INVALID"),
    ("bad_test", 409, "AGENT_ARTIFACT_PATCH_TEST_FAILED"),
    ("bad_hash", 409, "AGENT_ARTIFACT_BASE_CONFLICT"),
    ("bad_revision", 409, "REVISION_CONFLICT"),
    ("foreign_source", 422, "AGENT_ARTIFACT_SOURCE_INVALID"),
])
def test_real_http_patch_failures_are_atomic_and_preserve_all_user_data(live_service, scenario, status, code):
    with closing(registered_client(live_service)[0]) as client:
        envelope, artifact, run_id = archive_project(client)
        project_id = envelope["project"]["id"]
        with closing(patch_bridge(live_service, client, project_id)) as bridge:
            run = claim_run(bridge, run_id)
            response = tool(bridge, run, "read_artifact", {"targetId": "target"})
            assert response.status_code == 200, response.text
            baseline = response.json()["result"]["result"]
            changes = [{"op": "replace", "path": "/title", "value": "本人要求的专业修订"},
                       {"op": "replace", "path": "/sources", "value": [{"kind": "project", "referenceId": project_id, "description": "当前真实项目"}]}]
            paths = {"whole": ("", artifact), "entity": ("/scenes/0", artifact["scenes"][0]),
                     "entity_list": ("/scenes", artifact["scenes"]), "identity": ("/scenes/0/key", "replacement"),
                     "no_change": ("/title", artifact["title"]), "invalid_schema": ("/title", "")}
            if scenario in paths:
                path, value = paths[scenario]
                changes = [{"op": "replace", "path": path, "value": value}]
            if scenario == "bad_test":
                changes.append({"op": "test", "path": "/title", "value": "不相同的实际输入"})
            if scenario == "foreign_source":
                changes[-1]["value"] = artifact["sources"]
            arguments = patch_arguments(baseline, changes)
            if scenario == "bad_hash":
                arguments["baseArtifactSha256"] = "0" * 64
            if scenario == "bad_revision":
                arguments["expectedRevision"] += 1
            rejected = tool(bridge, run, "patch_artifact", arguments)
            assert error_code(rejected, status) == code
            assert client.get(f"/projects/{project_id}").json() == envelope
            with sqlite3.connect(f"file:{live_service.data_dir / 'sceneturn.sqlite3'}?mode=ro", uri=True) as database:
                assert database.execute("select count(*) from operations where project_id=? and kind=?", (project_id, "agent.tool")).fetchone()[0] == 1


def test_real_http_artifact_read_respects_authorization_and_manual_body_changes(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope, artifact, run_id = archive_project(client)
        project_id = envelope["project"]["id"]
        with closing(patch_bridge(live_service, client, project_id)) as bridge:
            run = claim_run(bridge, run_id)
            assert error_code(tool(bridge, run, "read_artifact", {"targetId": "other"}), 403) == "AGENT_TARGET_NOT_ALLOWED"
        client.post(f"/projects/{project_id}/agent-runs/{run_id}/stop", json={"operationId": str(uuid.uuid4())})
        current = client.get(f"/projects/{project_id}").json()
        current["canvases"][0]["nodes"][0]["metadata"]["content"] += "\n本人手工补充的内容"
        edited = save_canvas(client, current, current["canvases"][0]["nodes"])
        response, _ = create_run(client, edited, role="writer", targets=["target"], delivery_mode="professional")
        with closing(patch_bridge(live_service, client, project_id)) as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            assert error_code(tool(bridge, run, "read_artifact", {"targetId": "target"}), 409) == "AGENT_ARTIFACT_CHANGED"
            assert client.get(f"/projects/{project_id}").json() == edited


def test_r1_catalog_preserves_existing_tools_and_schemas():
    for role in ["writer", "director", "sound_designer"]:
        current = tool_catalog(role, "R2")
        assert tool_catalog(role, "R1") == [item for item in current if item["name"] not in {"read_artifact", "patch_artifact"}]


def test_real_http_current_artifact_patch_keeps_unmodified_prompt_fields_and_rejects_wrong_role(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("target", "text", "角色资料", content="本人填写的角色描述")])
        project_id = envelope["project"]["id"]
        response, _ = create_run(client, envelope, role="character_designer", targets=["target"], delivery_mode="professional")
        with closing(patch_bridge(live_service, client, project_id)) as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            source = manual_artifact("character_designer", project_id)
            written = tool(bridge, run, "write_artifact", {"targetId": "target", "artifact": source})
            assert written.status_code == 200, written.text
            run["projectRevision"] = written.json()["result"]["revision"]
            read = tool(bridge, run, "read_artifact", {"targetId": "target"})
            assert read.status_code == 200
            baseline = read.json()["result"]["result"]
            assert baseline["referenceScope"] == "current" and baseline["sourcesValidated"] is True
            patches = [{"op": "test", "path": "/characters/0/key", "value": "lin"},
                       {"op": "replace", "path": "/characters/0/imagePrompts/0/details", "value": "银色怀表右侧表壳有斜向划痕，表链自然垂在右手腕，深灰棉布工服与黄铜工牌保持原有材质和位置。"}]
            patched = tool(bridge, run, "patch_artifact", patch_arguments(baseline, patches))
            assert patched.status_code == 200, patched.text
            actual = patched.json()["result"]["result"]["artifact"]
            expected = copy.deepcopy(validate_artifact("character_designer", source))
            expected["characters"][0]["imagePrompts"][0]["details"] = patches[-1]["value"]
            assert actual == expected
            assert patched.json()["result"]["result"]["promptItems"] == image_prompt_items(actual)
        client.post(f"/projects/{project_id}/agent-runs/{run['id']}/stop", json={"operationId": str(uuid.uuid4())})
        envelope = client.get(f"/projects/{project_id}").json()
        response, _ = create_run(client, envelope, role="writer", targets=["target"], delivery_mode="professional")
        with closing(patch_bridge(live_service, client, project_id)) as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            read = tool(bridge, run, "read_artifact", {"targetId": "target"})
            assert read.status_code == 200
            baseline = read.json()["result"]["result"]
            rejected = tool(bridge, run, "patch_artifact", patch_arguments(baseline, [{"op": "replace", "path": "/title", "value": "本人要求的文字标题"}]))
            assert error_code(rejected, 403) == "AGENT_ARTIFACT_ROLE_MISMATCH"
            assert client.get(f"/projects/{project_id}").json() == envelope


def test_real_http_patch_requires_read_and_stops_after_manual_save_or_stop(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope, artifact, run_id = archive_project(client)
        project_id = envelope["project"]["id"]
        with closing(patch_bridge(live_service, client, project_id)) as bridge:
            run = claim_run(bridge, run_id)
            args = {"targetId": "target", "expectedRevision": envelope["revision"], "baseArtifactSha256": payload_digest(artifact),
                    "patches": [{"op": "replace", "path": "/title", "value": "本人指定的新标题"}]}
            assert error_code(tool(bridge, run, "patch_artifact", args), 409) == "AGENT_ARTIFACT_READ_REQUIRED"
            assert client.get(f"/projects/{project_id}").json() == envelope
        client.post(f"/projects/{project_id}/agent-runs/{run_id}/stop", json={"operationId": str(uuid.uuid4())})
        response, _ = create_run(client, envelope, role="writer", targets=["target"], delivery_mode="professional")
        with closing(patch_bridge(live_service, client, project_id)) as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            read = tool(bridge, run, "read_artifact", {"targetId": "target"})
            assert read.status_code == 200
            args = patch_arguments(read.json()["result"]["result"], [{"op": "replace", "path": "/title", "value": "本人指定的新标题"}])
            edited = copy.deepcopy(envelope["canvases"][0]["nodes"])
            edited[1]["metadata"]["content"] = "本人已经更新其他文字"
            newer = save_canvas(client, envelope, edited)
            assert error_code(tool(bridge, run, "patch_artifact", args), 409) == "REVISION_CONFLICT"
            assert client.get(f"/projects/{project_id}").json() == newer
        response, _ = create_run(client, newer, role="writer", targets=["target"], delivery_mode="professional")
        with closing(patch_bridge(live_service, client, project_id)) as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            read = tool(bridge, run, "read_artifact", {"targetId": "target"})
            assert read.status_code == 200
            args = patch_arguments(read.json()["result"]["result"], [{"op": "replace", "path": "/title", "value": "本人指定的新标题"}])
            assert client.post(f"/projects/{project_id}/agent-runs/{run['id']}/stop", json={"operationId": str(uuid.uuid4())}).status_code == 200
            assert error_code(tool(bridge, run, "patch_artifact", args), 409) == "RUN_STOPPED"
            assert client.get(f"/projects/{project_id}").json() == newer


def test_real_professional_supplier_artifact_can_be_archived_and_field_revised_via_http(live_service):
    database = sqlite_database_path(PRODUCT_ROOT / ".intermediate/agent-smoke/dac0c69c-d212-4a28-95b8-53dc8aa20911/data")
    assert database.is_file(), "需要本产品已完成的真实编剧产物"
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as source:
        row = next(row for row in source.execute("select id,response_json,raw_output from agent_calls where role=? and status=?", ("writer", "ready")) if json.loads(row[1]).get("tool") == "write_artifact")
    original = json.loads(row[1])["arguments"]["artifact"]
    assert validate_artifact("writer", original) == original
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("target", "text", "归档的真实编剧产物", content=render_artifact(original), artifactDraft=original)])
        project_id = envelope["project"]["id"]
        response, _ = create_run(client, envelope, role="writer", targets=["target"], delivery_mode="professional")
        with closing(patch_bridge(live_service, client, project_id)) as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            read = tool(bridge, run, "read_artifact", {"targetId": "target"})
            assert read.status_code == 200, read.text
            baseline = read.json()["result"]["result"]
            assert baseline["referenceScope"] == "archive" and baseline["artifact"] == original
            patches = [{"op": "replace", "path": "/scenes/2/action", "value": "林秋左手托住怀表，只注视表盘；右手放在膝上。陈伯左手握伞，右手自然垂下。"},
                       {"op": "replace", "path": "/sources", "value": [{"kind": "project", "referenceId": project_id, "description": "本次独立归档修订项目的真实资料"}]}]
            response = tool(bridge, run, "patch_artifact", patch_arguments(baseline, patches))
            assert response.status_code == 200, response.text
            actual = response.json()["result"]["result"]["artifact"]
            assert actual == validate_artifact("writer", jsonpatch.apply_patch(original, patches))
            run["projectRevision"] = response.json()["result"]["revision"]
            assert tool(bridge, run, "validate_artifacts", {}).status_code == 200
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as source:
        assert source.execute("select id,response_json,raw_output from agent_calls where id=?", (row[0],)).fetchone() == row


def test_r14_revision_prompts_preserve_old_catalog_and_require_all_relevant_fields():
    for prompt in [json_agent_prompt("writer", [], False, catalog_version="R2"), codex_prompt("writer", catalog_version="R2")]:
        assert "patch_artifact" in prompt and "read_artifact" in prompt
        assert "逐字段更换" in prompt and "baseArtifactSha256" in prompt
        assert "只追加新条件或验收句无法完成旧字段修订" in prompt
        assert "referenceScope=archive" in prompt
        assert "对需变更的允许字段直接提交replace" in prompt
        assert "expectedRevision填写实际read_artifact返回的revision" in prompt
        assert "版本与基准哈希由服务端在同一事务中核对" in prompt
        assert "无需重复抄写长篇原值" in prompt
        assert "test操作仍可用于确有需要的精确断言" in prompt
        assert "先用test核对相关旧字段" not in prompt
    assert "patch_artifact" not in json_agent_prompt("writer", [], False, catalog_version="R1")
    assert "patch_artifact" not in codex_prompt("writer", catalog_version="R1")
    planning = json_agent_prompt("writer", [], True, catalog_version="R2")
    assert "具体覆盖本次实际goal及全部审阅项" in planning and "禁止照搬其中通用instruction" in planning
    assert "具体覆盖本次实际goal及全部审阅项" not in json_agent_prompt("writer", [], True, catalog_version="R1")


@pytest.mark.parametrize("path,value", [
    ("/scenes/0/action", "林岚坐在木质工作台前，右手托住怀表注视表盘，左手保持放在桌边。"),
    ("/title", "本人修订的文档标题"),
])
def test_real_http_patch_keeps_custom_canvas_title_when_document_fields_change(live_service, path, value):
    with closing(registered_client(live_service)[0]) as client:
        envelope, artifact, run_id = archive_project(client)
        project_id = envelope["project"]["id"]
        canvas_title = envelope["canvases"][0]["nodes"][0]["title"]
        assert canvas_title != artifact["title"]
        with closing(patch_bridge(live_service, client, project_id)) as bridge:
            run = claim_run(bridge, run_id)
            read = tool(bridge, run, "read_artifact", {"targetId": "target"})
            assert read.status_code == 200
            baseline = read.json()["result"]["result"]
            patches = [{"op": "replace", "path": path, "value": value},
                       {"op": "replace", "path": "/sources", "value": [{"kind": "project", "referenceId": project_id, "description": "当前真实项目的制作资料"}]}]
            response = tool(bridge, run, "patch_artifact", patch_arguments(baseline, patches))
            assert response.status_code == 200, response.text
            result = response.json()["result"]["result"]
            loaded = client.get(f"/projects/{project_id}").json()
            current = loaded["canvases"][0]["nodes"][0]
            expected = validate_artifact("writer", jsonpatch.apply_patch(artifact, patches))
            assert current["title"] == result["savedTexts"][0]["title"] == canvas_title
            assert current["metadata"]["creativeArtifact"] == expected
            assert current["metadata"]["content"] == result["savedTexts"][0]["text"] == render_artifact(expected)
            assert current["metadata"]["texts"][0] == envelope["canvases"][0]["nodes"][0]["metadata"]["texts"][0]
            assert current["metadata"]["artifactDraft"] == artifact
