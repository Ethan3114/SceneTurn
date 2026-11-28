import copy
import json
import secrets
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.context_models import ProjectMemory
from app.context_schemas import ContextResult
from app.core.errors import AppError
from app.core.security import hash_password
from app.models import Base, Canvas, Project, ProjectVersion, User
from app.services.context import get_memory, get_source, list_memories, retrieve_context, write_memory
from app.services.projects import create_project_record, empty_canvas, lock_write, record_project_version, scene_node


@pytest.fixture
def memory_database(tmp_path):
    database = tmp_path / "memory.sqlite3"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        owner = User(email=f"memory-{uuid.uuid4().hex}@example.com", password_hash=hash_password(secrets.token_urlsafe(24)))
        other = User(email=f"memory-other-{uuid.uuid4().hex}@example.com", password_hash=hash_password(secrets.token_urlsafe(24)))
        session.add_all([owner, other])
        session.flush()
        project = create_project_record(session, owner.id, "共同历史与分支")
        session.commit()
        project_id, owner_id, other_id = project.id, owner.id, other.id
    yield engine, project_id, owner_id, other_id
    engine.dispose()


def branch_project(session, project_id):
    project = session.get(Project, project_id)
    original = copy.deepcopy(project.data_json)
    start = next(node for node in original["graphData"]["nodes"] if node["type"] == "start")
    end = next(node for node in original["graphData"]["nodes"] if node["type"] == "end")
    scene_ids = {key: str(uuid.uuid4()) for key in ("shared", "left", "right", "merge")}
    nodes = [start, *(scene_node(scene_ids[key], key) for key in scene_ids), end]
    pairs = [("initial", start["id"], scene_ids["shared"]), ("left", scene_ids["shared"], scene_ids["left"]),
        ("right", scene_ids["shared"], scene_ids["right"]), ("leftMerge", scene_ids["left"], scene_ids["merge"]),
        ("rightMerge", scene_ids["right"], scene_ids["merge"]), ("finish", scene_ids["merge"], end["id"])]
    edges = {name: {"id": str(uuid.uuid4()), "source": source, "target": target} for name, source, target in pairs}
    original["graphData"] = {"nodes": nodes, "edges": list(edges.values())}
    project.data_json = original
    project.revision += 1
    for scene_id in scene_ids.values():
        session.add(Canvas(owner_id=project.owner_id, project_id=project.id, scene_id=scene_id, data_json=empty_canvas(scene_id)))
    record_project_version(session, project)
    session.commit()
    return project, scene_ids, edges


def save_memory(session, project, owner, content, *, kind="story_event", scene_id=None, branch_id=None, sources=None, **extra):
    revision = session.get(Project, project.id).revision
    reference = {"kind": "scene", "id": scene_id, "revision": revision} if scene_id else {"kind": "project", "id": project.id, "revision": revision}
    return write_memory(session, project.id, owner, {"expectedRevision": revision, "operationId": str(uuid.uuid4()),
        "kind": kind, "content": content, "sceneId": scene_id, "branchId": branch_id, "sources": sources or [reference], **extra})


def test_exclusive_branches_and_merge_conditions_use_actual_project_graph(memory_database):
    engine, project_id, owner, _other = memory_database
    with Session(engine, expire_on_commit=False) as session:
        project, scenes, edges = branch_project(session, project_id)
        common = save_memory(session, project, owner, "老人已经介绍怀表", scene_id=scenes["shared"])
        left = save_memory(session, project, owner, "修表师交还怀表", scene_id=scenes["left"], branch_id=edges["left"]["id"])
        right = save_memory(session, project, owner, "修表师带走怀表", scene_id=scenes["right"], branch_id=edges["right"]["id"])
        left_context = retrieve_context(session, project_id, owner, scenes["left"])
        assert {item["id"] for item in left_context["storyFacts"]} == {common["memory"]["id"], left["memory"]["id"]}
        assert right["memory"]["id"] not in json.dumps(left_context)
        merged = retrieve_context(session, project_id, owner, scenes["merge"])
        assert {item["id"] for item in merged["storyFacts"]} == {common["memory"]["id"]}
        assert {item["id"] for item in merged["conditionalMemories"]} == {left["memory"]["id"], right["memory"]["id"]}
        assert all(item["fact"] is False and item["condition"]["requiresPathConfirmation"] for item in merged["conditionalMemories"])
        selected = retrieve_context(session, project_id, owner, scenes["merge"], edges["left"]["id"])
        assert {item["id"] for item in selected["storyFacts"]} == {common["memory"]["id"], left["memory"]["id"]}
        assert right["memory"]["id"] not in json.dumps(selected)
        ContextResult.model_validate(selected)


def test_dependency_sources_do_not_transfer_other_branch_into_global_memory(memory_database):
    engine, project_id, owner, _other = memory_database
    with Session(engine, expire_on_commit=False) as session:
        project, scenes, edges = branch_project(session, project_id)
        right = save_memory(session, project, owner, "老人只在右支交出钥匙", scene_id=scenes["right"], branch_id=edges["right"]["id"])
        summary = save_memory(session, project, owner, "右支钥匙事件摘要", kind="summary", sources=[{"kind": "memory", "id": right["memory"]["id"], "revision": project.revision}])
        left_context = retrieve_context(session, project_id, owner, scenes["left"])
        assert right["memory"]["id"] not in json.dumps(left_context)
        assert summary["memory"]["id"] not in json.dumps(left_context)
        merged = retrieve_context(session, project_id, owner, scenes["merge"])
        assert summary["memory"]["id"] in {item["id"] for item in merged["conditionalMemories"]}
        conditional_summary = next(item for item in merged["conditionalMemories"] if item["id"] == summary["memory"]["id"])
        assert conditional_summary["condition"]["branchIds"] == [edges["right"]["id"]]
        assert conditional_summary["condition"]["sceneIds"] == [scenes["right"]]


def test_implicit_text_context_and_explicit_targets_respect_branch_visibility(memory_database):
    engine, project_id, owner, _other = memory_database
    with Session(engine, expire_on_commit=False) as session:
        project, scenes, edges = branch_project(session, project_id)
        text_ids = {}
        for role, scene_id in scenes.items():
            canvas = session.scalar(select(Canvas).where(Canvas.project_id == project_id, Canvas.scene_id == scene_id))
            text_ids[role] = str(uuid.uuid4())
            value = copy.deepcopy(canvas.data_json)
            value["nodes"].append({"id": text_ids[role], "type": "text", "title": role, "position": {"x": 0, "y": 0}, "width": 300, "height": 200, "metadata": {"content": f"{role}场景的完整真实剧本"}})
            canvas.data_json = value
        project.revision += 1
        record_project_version(session, project)
        session.commit()
        context = retrieve_context(session, project_id, owner, scenes["left"], target_ids=[])
        assert {text["id"] for text in context["canvasTexts"]} == {text_ids["shared"], text_ids["left"]}
        assert text_ids["right"] not in json.dumps(context) and text_ids["merge"] not in json.dumps(context)
        with pytest.raises(AppError) as hidden:
            retrieve_context(session, project_id, owner, scenes["left"], target_ids=[text_ids["right"]])
        assert hidden.value.code == "CONTEXT_TARGET_NOT_FOUND"
        merged = retrieve_context(session, project_id, owner, scenes["merge"], branch_id=edges["left"]["id"])
        assert {text["id"] for text in merged["canvasTexts"]} == {text_ids["shared"], text_ids["left"], text_ids["merge"]}
        assert text_ids["right"] not in json.dumps(merged)


def test_manual_source_edit_invalidates_summary_and_keeps_original_history(memory_database):
    engine, project_id, owner, _other = memory_database
    with Session(engine, expire_on_commit=False) as session:
        project, scenes, _edges = branch_project(session, project_id)
        canvas = session.scalar(select(Canvas).where(Canvas.project_id == project_id, Canvas.scene_id == scenes["shared"]))
        node_id = str(uuid.uuid4())
        original_canvas = copy.deepcopy(canvas.data_json)
        original_canvas["nodes"].append({"id": node_id, "type": "text", "title": "角色设定", "position": {"x": 0, "y": 0}, "width": 300, "height": 200, "metadata": {"content": "老人身穿灰色大衣"}})
        canvas.data_json = original_canvas
        project.revision += 1
        record_project_version(session, project)
        session.commit()
        source_revision = project.revision
        source = {"kind": "canvas_node", "id": node_id, "sceneId": scenes["shared"], "revision": source_revision}
        setting = save_memory(session, project, owner, "老人穿灰色大衣", kind="setting", scene_id=scenes["shared"], sources=[source])
        summary = save_memory(session, project, owner, "老人造型摘要", kind="summary", scene_id=scenes["shared"], sources=[{"kind": "memory", "id": setting["memory"]["id"], "revision": project.revision}])
        changed = copy.deepcopy(canvas.data_json)
        changed["nodes"][0]["metadata"]["content"] = "老人身穿深蓝色大衣"
        canvas.data_json = changed
        project.revision += 1
        record_project_version(session, project)
        session.commit()
    engine.dispose()
    with Session(engine, expire_on_commit=False) as session:
        old = get_memory(session, project_id, owner, setting["memory"]["id"])["memory"]
        derived = get_memory(session, project_id, owner, summary["memory"]["id"])["memory"]
        assert old["valid"] is False and old["invalidReason"] == "source_changed"
        assert derived["valid"] is False
        original = get_source(session, project_id, owner, source)
        assert original["content"]["metadata"]["content"] == "老人身穿灰色大衣"
        assert original["source"]["current"] is False
        context = retrieve_context(session, project_id, owner, scenes["shared"])
        assert context["memories"] == []
        assert context["canvasTexts"][0]["content"] == "老人身穿深蓝色大衣"


def test_latest_decision_supersedes_same_subject_without_deleting_history(memory_database):
    engine, project_id, owner, _other = memory_database
    with Session(engine, expire_on_commit=False) as session:
        project = session.get(Project, project_id)
        first = save_memory(session, project, owner, "全片采用暖色调", kind="decision", subject="画面风格")
        second = save_memory(session, project, owner, "全片采用冷色调", kind="decision", subject="画面风格")
        memories = list_memories(session, project_id, owner)["memories"]
        assert len(memories) == 2
        old = next(memory for memory in memories if memory["id"] == first["memory"]["id"])
        assert old["status"] == "superseded" and old["supersededBy"] == second["memory"]["id"] and old["valid"] is False
        scene_id = next(node["id"] for node in project.data_json["graphData"]["nodes"] if node["type"] == "scene")
        context = retrieve_context(session, project_id, owner, scene_id)
        assert [memory["content"] for memory in context["memories"]] == ["全片采用冷色调"]


def test_project_decision_keeps_scope_when_story_graph_grows(memory_database):
    engine, project_id, owner, _other = memory_database
    with Session(engine, expire_on_commit=False) as session:
        project = session.get(Project, project_id)
        decision = save_memory(session, project, owner, "所有场景采用暖色调", kind="decision", subject="画面风格")
        project, scenes, _edges = branch_project(session, project_id)
        context = retrieve_context(session, project_id, owner, scenes["shared"])
        assert context["memories"][0]["id"] == decision["memory"]["id"]
        assert context["memories"][0]["valid"] is True


def test_ownership_revision_and_operation_checks_preserve_database(memory_database):
    engine, project_id, owner, other = memory_database
    with Session(engine, expire_on_commit=False) as session:
        project = session.get(Project, project_id)
        revision = project.revision
        payload = {"expectedRevision": revision, "operationId": str(uuid.uuid4()), "kind": "decision", "content": "确认剧本发生在雨夜", "sources": [{"kind": "project", "id": project_id, "revision": revision}]}
        first = write_memory(session, project_id, owner, payload)
        assert write_memory(session, project_id, owner, payload) == first
        assert len(list_memories(session, project_id, owner)["memories"]) == 1
        with pytest.raises(AppError) as conflict:
            write_memory(session, project_id, owner, {**payload, "content": "替换内容"})
        assert conflict.value.code == "OPERATION_CONFLICT"
        session.rollback()
        with pytest.raises(AppError) as stale:
            write_memory(session, project_id, owner, {**payload, "operationId": str(uuid.uuid4())})
        assert stale.value.code == "REVISION_CONFLICT"
        session.rollback()
        with pytest.raises(AppError) as forbidden:
            list_memories(session, project_id, other)
        assert forbidden.value.status == 404
        assert session.get(Project, project_id).revision == first["revision"]


def test_candidates_and_creation_events_remain_distinct_from_story_facts(memory_database):
    engine, project_id, owner, _other = memory_database
    with Session(engine, expire_on_commit=False) as session:
        project = session.get(Project, project_id)
        scene_id = next(node["id"] for node in project.data_json["graphData"]["nodes"] if node["type"] == "scene")
        candidate = save_memory(session, project, owner, "候选剧本：老人登上火车", kind="candidate", scene_id=scene_id)
        creation = save_memory(session, project, owner, "用户采用了一张火车图片", kind="creation_event", scene_id=scene_id)
        context = retrieve_context(session, project_id, owner, scene_id)
        assert context["storyFacts"] == []
        assert context["candidates"][0]["id"] == candidate["memory"]["id"]
        assert context["creationEvents"][0]["id"] == creation["memory"]["id"]
        assert context["candidates"][0]["fact"] is False
        assert context["creationEvents"][0]["fact"] is False


def test_agent_control_nodes_cannot_become_content_memory_sources(memory_database):
    engine, project_id, owner, _other = memory_database
    with Session(engine, expire_on_commit=False) as session:
        project = session.get(Project, project_id)
        scene_id = next(node["id"] for node in project.data_json["graphData"]["nodes"] if node["type"] == "scene")
        canvas = session.scalar(select(Canvas).where(Canvas.project_id == project_id, Canvas.scene_id == scene_id))
        node_id = str(uuid.uuid4())
        value = copy.deepcopy(canvas.data_json)
        value["nodes"].append({"id": node_id, "type": "agent", "title": "任务控制", "position": {"x": 0, "y": 0}, "metadata": {"goal": "读取历史并修改分镜", "content": "任务指令不能成为剧情事实"}})
        canvas.data_json = value
        project.revision += 1
        record_project_version(session, project)
        session.commit()
        with pytest.raises(AppError) as denied:
            save_memory(session, project, owner, "控制节点摘要", kind="summary", scene_id=scene_id, sources=[{"kind": "canvas_node", "id": node_id, "sceneId": scene_id, "revision": project.revision}])
        assert denied.value.code == "MEMORY_CONTROL_SOURCE_FORBIDDEN"
        session.rollback()
        with pytest.raises(AppError) as hidden:
            retrieve_context(session, project_id, owner, scene_id, target_ids=[node_id])
        assert hidden.value.code == "CONTEXT_CONTROL_NODE_PROTECTED"
        context = retrieve_context(session, project_id, owner, scene_id)
        assert context["canvasTexts"] == [] and context["storyFacts"] == []


def test_context_capacity_reports_excerpt_and_keeps_complete_source(memory_database):
    engine, project_id, owner, _other = memory_database
    with Session(engine, expire_on_commit=False) as session:
        project = session.get(Project, project_id)
        scene_id = next(node["id"] for node in project.data_json["graphData"]["nodes"] if node["type"] == "scene")
        complete = "小站的修表师保留怀表原始记录。" * 900
        memory = save_memory(session, project, owner, complete, kind="setting")
        context = retrieve_context(session, project_id, owner, scene_id, max_chars=3000)
        encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        assert len(encoded) <= 3000 and context["characterCount"] == len(encoded)
        assert context["truncated"] is True
        assert context["memories"][0]["truncated"] is True
        assert context["memories"][0]["originalLength"] == len(complete)
        assert get_memory(session, project_id, owner, memory["memory"]["id"])["memory"]["content"] == complete


def test_managed_transaction_rolls_back_memory_and_project_revision_together(memory_database):
    engine, project_id, owner, _other = memory_database
    with Session(engine, expire_on_commit=False) as session:
        project = session.get(Project, project_id)
        revision = project.revision
        session.rollback()
        lock_write(session)
        write_memory(session, project_id, owner, {"expectedRevision": revision, "operationId": str(uuid.uuid4()), "kind": "decision", "content": "停止前尚未提交的修改", "sources": [{"kind": "project", "id": project_id, "revision": revision}]}, manage_transaction=False)
        session.rollback()
    with Session(engine) as session:
        assert session.get(Project, project_id).revision == revision
        assert session.scalars(select(ProjectMemory).where(ProjectMemory.project_id == project_id)).all() == []
        assert len(session.scalars(select(ProjectVersion).where(ProjectVersion.project_id == project_id)).all()) == 1


def test_parallel_edges_and_cycles_preserve_exact_branch_identity(memory_database):
    engine, project_id, owner, _other = memory_database
    with Session(engine, expire_on_commit=False) as session:
        project, scenes, edges = branch_project(session, project_id)
        snapshot = copy.deepcopy(project.data_json)
        parallel = {"id": str(uuid.uuid4()), "source": edges["left"]["source"], "target": edges["left"]["target"]}
        snapshot["graphData"]["edges"].extend([parallel, {"id": str(uuid.uuid4()), "source": scenes["merge"], "target": scenes["shared"]}])
        project.data_json = snapshot
        project.revision += 1
        record_project_version(session, project)
        session.commit()
        event = save_memory(session, project, owner, "选择第一条同目标路线", scene_id=scenes["left"], branch_id=edges["left"]["id"])
        first_path = retrieve_context(session, project_id, owner, scenes["left"], edges["left"]["id"])
        assert event["memory"]["id"] in {item["id"] for item in first_path["storyFacts"]}
        second_path = retrieve_context(session, project_id, owner, scenes["left"], parallel["id"])
        assert event["memory"]["id"] not in json.dumps(second_path)


def test_real_context_http_routes_preserve_sources_and_enforce_account_isolation(live_service):
    with live_service.client() as owner, live_service.client() as other:
        for client in (owner, other):
            response = client.post("/auth/register", json={"email": f"context-api-{uuid.uuid4().hex}@example.com", "password": secrets.token_urlsafe(24)})
            assert response.status_code == 201, response.text
        response = owner.post("/projects", json={"title": "真实上下文接口"})
        assert response.status_code == 201, response.text
        envelope = response.json()
        project_id, revision = envelope["project"]["id"], envelope["revision"]
        scene_id = next(node["id"] for node in envelope["project"]["graphData"]["nodes"] if node["type"] == "scene")
        payload = {"expectedRevision": revision, "operationId": str(uuid.uuid4()), "kind": "decision", "subject": "场景风格", "content": "树林场景采用清晨自然光", "sources": [{"kind": "project", "id": project_id, "revision": revision}]}
        created = owner.post(f"/projects/{project_id}/memories", json=payload)
        assert created.status_code == 201, created.text
        memory_id = created.json()["memory"]["id"]
        assert owner.post(f"/projects/{project_id}/memories", json=payload).json() == created.json()
        response = owner.post(f"/projects/{project_id}/context/retrieve", json={"sceneId": scene_id, "maxChars": 8000})
        assert response.status_code == 200, response.text
        assert response.json()["memories"][0]["id"] == memory_id
        source = owner.post(f"/projects/{project_id}/context/source", json=payload["sources"][0])
        assert source.status_code == 200 and source.json()["source"]["current"] is True
        assert source.json()["content"]["title"] == "真实上下文接口"
        assert owner.get(f"/projects/{project_id}/memories/{memory_id}").json()["memory"]["content"] == payload["content"]
        for path in (f"/projects/{project_id}/memories", f"/projects/{project_id}/memories/{memory_id}"):
            forbidden = other.get(path)
            assert forbidden.status_code == 404 and set(forbidden.json()) == {"error"}
        forbidden = other.post(f"/projects/{project_id}/context/retrieve", json={"sceneId": scene_id})
        assert forbidden.status_code == 404 and set(forbidden.json()) == {"error"}


def test_real_context_http_rejects_stale_and_invalid_scope_without_writing(live_service):
    with live_service.client() as client:
        response = client.post("/auth/register", json={"email": f"context-scope-{uuid.uuid4().hex}@example.com", "password": secrets.token_urlsafe(24)})
        assert response.status_code == 201, response.text
        envelope = client.post("/projects", json={"title": "真实记忆边界"}).json()
        project_id, revision = envelope["project"]["id"], envelope["revision"]
        scene_id = next(node["id"] for node in envelope["project"]["graphData"]["nodes"] if node["type"] == "scene")
        payload = {"expectedRevision": revision, "operationId": str(uuid.uuid4()), "kind": "decision", "content": "角色保留怀表", "sources": [{"kind": "scene", "id": scene_id, "revision": revision}]}
        created = client.post(f"/projects/{project_id}/memories", json=payload)
        assert created.status_code == 201, created.text
        stale = client.post(f"/projects/{project_id}/memories", json={**payload, "operationId": str(uuid.uuid4())})
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "REVISION_CONFLICT"
        invalid = client.post(f"/projects/{project_id}/context/retrieve", json={"sceneId": scene_id, "branchId": str(uuid.uuid4())})
        assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "CONTEXT_BRANCH_INVALID"
        malformed = client.post(f"/projects/{project_id}/memories", json={**payload, "expectedRevision": created.json()["revision"], "sources": [{"kind": "canvas_node", "id": str(uuid.uuid4()), "revision": revision}]})
        assert malformed.status_code == 422
        result = client.get(f"/projects/{project_id}/memories")
        assert result.status_code == 200 and len(result.json()["memories"]) == 1
