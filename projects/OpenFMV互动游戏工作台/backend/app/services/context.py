import copy
import json
import time

import networkx as nx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.context_models import ProjectMemory
from app.context_schemas import ContextSource, MemoryView, MemoryWrite
from app.core.errors import AppError
from app.models import Asset, Canvas, Project, ProjectVersion, new_id
from app.services.projects import get_operation, iso_timestamp, lock_write, payload_digest, record_project_version, require_project, require_revision, require_scene, save_operation


def _project_source(snapshot: dict) -> dict:
    metadata = copy.deepcopy(snapshot.get("metadata", {}))
    metadata.pop("serverRevision", None)
    return {"id": snapshot["id"], "title": snapshot["title"], "metadata": metadata}


class SourceResolver:
    def __init__(self, session: Session, project: Project):
        self.session = session
        self.project = project
        self.versions: dict[int, ProjectVersion] = {}
        self.memory_validity: dict[str, tuple[bool, str | None]] = {}

    def history(self, revision: int) -> ProjectVersion:
        if revision > self.project.revision:
            raise AppError(422, "SOURCE_REVISION_INVALID", "来源版本超过当前项目版本")
        if revision not in self.versions:
            history = self.session.scalar(select(ProjectVersion).where(
                ProjectVersion.project_id == self.project.id,
                ProjectVersion.owner_id == self.project.owner_id,
                ProjectVersion.revision == revision,
            ))
            if history is None:
                raise AppError(404, "SOURCE_NOT_FOUND", "来源项目版本不存在或无权访问")
            self.versions[revision] = history
        return self.versions[revision]

    def memory(self, identifier: str) -> ProjectMemory | None:
        return self.session.scalar(select(ProjectMemory).where(
            ProjectMemory.id == identifier,
            ProjectMemory.project_id == self.project.id,
            ProjectMemory.owner_id == self.project.owner_id,
        ))

    def resolve(self, reference: dict, *, current: bool = False):
        source = ContextSource.model_validate(reference)
        history = self.history(source.revision)
        snapshot = self.project.data_json if current else history.data_json
        if source.kind == "project":
            return _project_source(snapshot) if source.id == self.project.id else None
        if source.kind == "scene":
            if source.scene_id is not None and source.scene_id != source.id:
                return None
            return next((node for node in snapshot["graphData"]["nodes"] if node["id"] == source.id and node["type"] == "scene"), None)
        if source.kind == "canvas_node":
            if current:
                canvas = self.session.scalar(select(Canvas).where(
                    Canvas.owner_id == self.project.owner_id,
                    Canvas.project_id == self.project.id,
                    Canvas.scene_id == source.scene_id,
                ))
                scene_exists = any(node["id"] == source.scene_id and node["type"] == "scene" for node in snapshot["graphData"]["nodes"])
                value = canvas.data_json if canvas is not None and scene_exists else None
            else:
                value = next((canvas for canvas in history.canvases_json if canvas["sceneId"] == source.scene_id), None)
            return next((node for node in value["nodes"] if node["id"] == source.id), None) if value else None
        if source.kind == "asset":
            asset = self.session.scalar(select(Asset).where(Asset.id == source.id, Asset.project_id == self.project.id, Asset.owner_id == self.project.owner_id))
            if asset is None or not current and asset.created_at > history.created_at:
                return None
            return {"id": asset.id, "name": asset.name, "sha256": asset.sha256, "mimeType": asset.mime_type, "bytes": asset.bytes, "durationMs": asset.duration_ms, "metadata": asset.metadata_json}
        memory = self.memory(source.id)
        if memory is None or memory.project_revision > source.revision:
            return None
        return {"id": memory.id, "kind": memory.kind, "content": memory.content, "sceneId": memory.scene_id, "branchId": memory.branch_id, "sources": memory.sources_json}

    def source_status(self, source: dict, seen: frozenset[str] = frozenset()) -> tuple[bool, str | None]:
        reference = {key: source[key] for key in ("kind", "id", "revision", "sceneId") if key in source}
        current = self.resolve(reference, current=True)
        if current is None:
            return False, "source_deleted"
        original = self.resolve(reference)
        if original is None or payload_digest(original) != source["digest"]:
            return False, "source_changed"
        if source["kind"] in {"canvas_node", "scene"}:
            original = {key: value for key, value in original.items() if key not in {"position", "width", "height"}}
            current = {key: value for key, value in current.items() if key not in {"position", "width", "height"}}
        if payload_digest(current) != payload_digest(original):
            return False, "source_changed"
        if source["kind"] == "memory":
            dependency = self.memory(source["id"])
            return self.validity(dependency, seen)
        return True, None

    def validity(self, memory: ProjectMemory, seen: frozenset[str] = frozenset()) -> tuple[bool, str | None]:
        if memory.id in self.memory_validity:
            return self.memory_validity[memory.id]
        if memory.id in seen:
            raise AppError(422, "MEMORY_SOURCE_CYCLE", "项目记忆来源存在循环引用")
        if memory.status != "active":
            result = (False, "superseded")
        else:
            states = [self.source_status(source, seen | {memory.id}) for source in memory.sources_json]
            invalid = next((state for state in states if not state[0]), None)
            result = invalid or (True, None)
        self.memory_validity[memory.id] = result
        return result


def _source_view(resolver: SourceResolver, source: dict) -> dict:
    valid, reason = resolver.source_status(source)
    return {**source, "current": valid, "reason": reason}


def memory_view(resolver: SourceResolver, memory: ProjectMemory) -> dict:
    valid, reason = resolver.validity(memory)
    result = {
        "id": memory.id, "schemaVersion": memory.schema_version, "kind": memory.kind,
        "subject": memory.subject, "content": memory.content, "sceneId": memory.scene_id,
        "branchId": memory.branch_id, "storyTime": memory.story_time,
        "sources": [_source_view(resolver, source) for source in memory.sources_json],
        "projectRevision": memory.project_revision, "status": memory.status, "valid": valid,
        "invalidReason": reason, "supersedesId": memory.supersedes_id,
        "supersededBy": memory.superseded_by, "createdAt": iso_timestamp(memory.created_at),
    }
    return MemoryView.model_validate(result).model_dump(by_alias=True)


def _graph_scope(project: Project, scene_id: str, branch_id: str | None) -> tuple[dict, nx.MultiDiGraph, str]:
    require_scene(project, scene_id)
    nodes = project.data_json["graphData"]["nodes"]
    edges = project.data_json["graphData"]["edges"]
    entry = project.data_json.get("metadata", {}).get("entryNodeId")
    if entry not in {node["id"] for node in nodes}:
        raise AppError(422, "CONTEXT_ENTRY_INVALID", "故事入口无效，请先保存有效入口")
    graph = nx.MultiDiGraph()
    graph.add_nodes_from(node["id"] for node in nodes)
    graph.add_edges_from((edge["source"], edge["target"], edge["id"]) for edge in edges)
    if branch_id:
        selected = next((edge for edge in edges if edge["id"] == branch_id), None)
        if selected is None:
            raise AppError(422, "CONTEXT_BRANCH_INVALID", "所选分支连线不存在")
        ancestors = nx.ancestors(graph, scene_id) | {scene_id}
        if selected["target"] not in ancestors or selected["source"] not in nx.descendants(graph, entry) | {entry}:
            raise AppError(422, "CONTEXT_BRANCH_INVALID", "所选分支不能到达当前场景")
        graph = nx.MultiDiGraph()
        graph.add_nodes_from(node["id"] for node in nodes)
        graph.add_edges_from((edge["source"], edge["target"], edge["id"]) for edge in edges if edge["source"] != selected["source"] or edge["id"] == branch_id)
    reachable = nx.descendants(graph, entry) | {entry}
    if scene_id not in reachable:
        if branch_id:
            raise AppError(422, "CONTEXT_BRANCH_INVALID", "所选分支不能到达当前场景")
        return {"reachable": False, "unconditionalSceneIds": [scene_id], "conditionalSceneIds": [], "note": "当前场景尚未接入故事入口，仅检索本场景资料"}, graph, entry
    ancestors = (nx.ancestors(graph, scene_id) | {scene_id}) & reachable
    parents = nx.immediate_dominators(graph, entry)
    dominators = {scene_id}
    cursor = scene_id
    while cursor != entry:
        cursor = parents[cursor]
        dominators.add(cursor)
    scenes = {node["id"] for node in nodes if node["type"] == "scene"}
    unconditional = dominators & scenes
    conditional = (ancestors - dominators) & scenes
    return {"reachable": True, "unconditionalSceneIds": sorted(unconditional), "conditionalSceneIds": sorted(conditional), "note": "条件历史保留分支条件，不能作为当前分支已发生事实"}, graph, entry


def _branch_condition(project: Project, graph: nx.MultiDiGraph, entry: str, scene_id: str, branch_id: str | None) -> tuple[bool, bool]:
    if not branch_id:
        return True, True
    edge = next((edge for edge in project.data_json["graphData"]["edges"] if edge["id"] == branch_id), None)
    if edge is None or not graph.has_edge(edge["source"], edge["target"], key=branch_id):
        return False, False
    if edge["source"] not in nx.descendants(graph, entry) | {entry} or edge["target"] not in nx.ancestors(graph, scene_id) | {scene_id}:
        return False, False
    without = graph.copy()
    without.remove_edge(edge["source"], edge["target"], key=branch_id)
    guaranteed = scene_id not in nx.descendants(without, entry) | {entry}
    return True, guaranteed


def _memory_scope(resolver: SourceResolver, memory: ProjectMemory, visible: set[str], certain: set[str], graph: nx.MultiDiGraph, entry: str, scene_id: str, seen: frozenset[str] = frozenset()) -> tuple[bool, bool]:
    if memory.id in seen:
        raise AppError(422, "MEMORY_SOURCE_CYCLE", "项目记忆来源存在循环引用")
    if memory.scene_id and memory.scene_id not in visible:
        return False, False
    possible, guaranteed = _branch_condition(resolver.project, graph, entry, scene_id, memory.branch_id)
    guaranteed = guaranteed and (memory.scene_id is None or memory.scene_id in certain)
    for source in memory.sources_json:
        source_scene = source["id"] if source["kind"] == "scene" else source.get("sceneId") if source["kind"] == "canvas_node" else None
        if source_scene:
            if source_scene not in visible:
                return False, False
            guaranteed = guaranteed and source_scene in certain
        if source["kind"] == "memory":
            dependency = resolver.memory(source["id"])
            if dependency is None:
                return False, False
            dependency_possible, dependency_guaranteed = _memory_scope(resolver, dependency, visible, certain, graph, entry, scene_id, seen | {memory.id})
            possible = possible and dependency_possible
            guaranteed = guaranteed and dependency_guaranteed
    return possible, guaranteed


def _memory_conditions(resolver: SourceResolver, memory: ProjectMemory, certain: set[str], seen: frozenset[str] = frozenset()) -> tuple[set[str], set[str]]:
    if memory.id in seen:
        raise AppError(422, "MEMORY_SOURCE_CYCLE", "项目记忆来源存在循环引用")
    scenes = {memory.scene_id} if memory.scene_id and memory.scene_id not in certain else set()
    branches = {memory.branch_id} if memory.branch_id else set()
    for source in memory.sources_json:
        source_scene = source["id"] if source["kind"] == "scene" else source.get("sceneId") if source["kind"] == "canvas_node" else None
        if source_scene and source_scene not in certain:
            scenes.add(source_scene)
        if source["kind"] == "memory":
            dependency = resolver.memory(source["id"])
            dependency_scenes, dependency_branches = _memory_conditions(resolver, dependency, certain, seen | {memory.id})
            scenes |= dependency_scenes
            branches |= dependency_branches
    return scenes, branches


def write_memory(session: Session, project_id: str, owner_id: str, payload: dict, *, manage_transaction: bool = True) -> dict:
    body = MemoryWrite.model_validate(payload)
    if manage_transaction:
        lock_write(session)
    project = require_project(session, project_id, owner_id)
    wire = body.model_dump(by_alias=True)
    replay = get_operation(session, owner_id, project_id, body.operation_id, "memory.write", wire)
    if replay is not None:
        if manage_transaction:
            session.commit()
        return replay
    require_revision(project, body.expected_revision)
    if body.scene_id:
        _graph_scope(project, body.scene_id, body.branch_id)
    resolver = SourceResolver(session, project)
    sources = []
    for source in body.sources:
        reference = source.model_dump(by_alias=True, exclude_none=True)
        original = resolver.resolve(reference)
        if original is None:
            raise AppError(404, "SOURCE_NOT_FOUND", "记忆来源对象不存在或无权访问")
        if source.kind == "canvas_node" and original["type"] == "agent":
            raise AppError(422, "MEMORY_CONTROL_SOURCE_FORBIDDEN", "Agent控制节点用于任务管理，请选择实际剧本或内容节点作为记忆来源")
        saved = {**reference, "digest": payload_digest(original)}
        valid, _reason = resolver.source_status(saved)
        if not valid:
            raise AppError(409, "MEMORY_SOURCE_CHANGED", "来源已变化，请读取最新来源后保存记忆")
        sources.append(saved)
    superseded = []
    if body.supersedes_id:
        previous = resolver.memory(body.supersedes_id)
        if previous is None:
            raise AppError(404, "MEMORY_NOT_FOUND", "被替代的记忆不存在或无权访问")
        if previous.kind != body.kind or previous.scene_id != body.scene_id or previous.branch_id != body.branch_id:
            raise AppError(422, "MEMORY_SCOPE_MISMATCH", "替代记忆必须具有相同类型、场景及分支")
        if previous.status != "active":
            raise AppError(409, "MEMORY_ALREADY_SUPERSEDED", "原记忆已有更新，请重新读取")
        superseded.append(previous)
    if body.subject:
        superseded.extend(session.scalars(select(ProjectMemory).where(
            ProjectMemory.project_id == project_id, ProjectMemory.owner_id == owner_id,
            ProjectMemory.kind == body.kind, ProjectMemory.subject == body.subject,
            ProjectMemory.scene_id == body.scene_id, ProjectMemory.branch_id == body.branch_id,
            ProjectMemory.status == "active",
        )).all())
    if any(source["kind"] == "memory" and source["id"] in {previous.id for previous in superseded} for source in sources):
        raise AppError(422, "MEMORY_SOURCE_SUPERSEDED", "新记忆不能依赖本次被替代的旧记忆，请引用当前有效原文")
    identifier = new_id()
    for previous in superseded:
        previous.status, previous.superseded_by = "superseded", identifier
    project.revision += 1
    project.updated_at = time.time()
    memory = ProjectMemory(id=identifier, owner_id=owner_id, project_id=project_id,
        operation_id=body.operation_id, kind=body.kind, subject=body.subject, content=body.content,
        scene_id=body.scene_id, branch_id=body.branch_id, story_time=body.story_time,
        sources_json=sources, project_revision=project.revision, status="active",
        supersedes_id=body.supersedes_id, created_at=time.time())
    session.add(memory)
    record_project_version(session, project)
    result = {"memory": memory_view(SourceResolver(session, project), memory), "revision": project.revision}
    save_operation(session, owner_id, project_id, body.operation_id, "memory.write", wire, result)
    if manage_transaction:
        session.commit()
    return result


def list_memories(session: Session, project_id: str, owner_id: str) -> dict:
    project = require_project(session, project_id, owner_id)
    records = session.scalars(select(ProjectMemory).where(ProjectMemory.owner_id == owner_id, ProjectMemory.project_id == project_id).order_by(ProjectMemory.created_at.desc(), ProjectMemory.id)).all()
    resolver = SourceResolver(session, project)
    return {"memories": [memory_view(resolver, record) for record in records], "revision": project.revision}


def get_memory(session: Session, project_id: str, owner_id: str, memory_id: str) -> dict:
    project = require_project(session, project_id, owner_id)
    resolver = SourceResolver(session, project)
    memory = resolver.memory(memory_id)
    if memory is None:
        raise AppError(404, "MEMORY_NOT_FOUND", "项目记忆不存在或无权访问")
    return {"memory": memory_view(resolver, memory), "revision": project.revision}


def get_source(session: Session, project_id: str, owner_id: str, payload: dict) -> dict:
    project = require_project(session, project_id, owner_id)
    reference = ContextSource.model_validate(payload).model_dump(by_alias=True, exclude_none=True)
    resolver = SourceResolver(session, project)
    content = resolver.resolve(reference)
    if content is None:
        raise AppError(404, "SOURCE_NOT_FOUND", "来源对象不存在或无权访问")
    saved = {**reference, "digest": payload_digest(content)}
    return {"source": _source_view(resolver, saved), "content": content}


def _length(value: dict) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def source_scope(session: Session, project_id: str, owner_id: str, scene_id: str, branch_id: str | None = None) -> dict:
    project = require_project(session, project_id, owner_id)
    scope, graph, entry = _graph_scope(project, scene_id, branch_id)
    certain = set(scope["unconditionalSceneIds"])
    visible = certain | set(scope["conditionalSceneIds"])
    resolver = SourceResolver(session, project)
    records = session.scalars(select(ProjectMemory).where(ProjectMemory.owner_id == owner_id, ProjectMemory.project_id == project_id, ProjectMemory.status == "active")).all()
    memory_ids = [memory.id for memory in records if _memory_scope(resolver, memory, visible, certain, graph, entry, scene_id)[0] and resolver.validity(memory)[0]]
    return {"allowedSceneIds": sorted(visible), "memoryIds": sorted(memory_ids), "branchAnalysis": scope, "revision": project.revision}


def retrieve_context(session: Session, project_id: str, owner_id: str, scene_id: str, branch_id: str | None = None, target_ids: list[str] | None = None, max_chars: int = 16000, *, material_only: bool = False) -> dict:
    if not 1024 <= max_chars <= 64000:
        raise AppError(422, "CONTEXT_CAPACITY_INVALID", "上下文容量必须在1024至64000字符之间")
    project = require_project(session, project_id, owner_id)
    scope, graph, entry = _graph_scope(project, scene_id, branch_id)
    certain = set(scope["unconditionalSceneIds"])
    conditional = set(scope["conditionalSceneIds"])
    visible = certain | conditional
    resolver = SourceResolver(session, project)
    result = {
        "projectId": project_id, "revision": project.revision, "sceneId": scene_id, "branchId": branch_id,
        "project": {"title": project.title}, "branchAnalysis": scope, "memories": [], "storyFacts": [],
        "conditionalMemories": [], "creationEvents": [], "candidates": [], "canvasTexts": [], "sources": [],
        "maxChars": max_chars, "characterCount": 0, "omittedCount": 0, "truncated": False,
    }
    if _length(result) + 256 > max_chars:
        raise AppError(422, "CONTEXT_CAPACITY_TOO_SMALL", "上下文容量不足以保存场景及分支来源，请增加容量")
    records = [] if material_only else session.scalars(select(ProjectMemory).where(ProjectMemory.owner_id == owner_id, ProjectMemory.project_id == project_id, ProjectMemory.status == "active").order_by(ProjectMemory.created_at.desc(), ProjectMemory.id)).all()
    ordered: list[tuple[str, dict]] = []
    for memory in records:
        if memory.scene_id and memory.scene_id not in visible:
            continue
        possible, guaranteed = _memory_scope(resolver, memory, visible, certain, graph, entry, scene_id)
        if not possible or not resolver.validity(memory)[0] and memory.kind not in {"creation_event", "candidate"}:
            continue
        item = memory_view(resolver, memory)
        item["source"] = {"kind": "memory", "id": memory.id, "revision": project.revision}
        item["fact"] = memory.kind == "story_event" and memory.scene_id in certain and guaranteed
        if memory.scene_id in conditional or not guaranteed:
            condition_scenes, condition_branches = _memory_conditions(resolver, memory, certain)
            item["condition"] = {"sceneIds": sorted(condition_scenes), "branchIds": sorted(condition_branches), "requiresPathConfirmation": True}
            group = "conditionalMemories"
        elif memory.kind == "story_event":
            group = "storyFacts"
        elif memory.kind == "creation_event":
            group = "creationEvents"
        elif memory.kind == "candidate":
            group = "candidates"
        else:
            group = "memories"
        ordered.append((group, item))
    canvases = session.scalars(select(Canvas).where(Canvas.owner_id == owner_id, Canvas.project_id == project_id, Canvas.scene_id.in_(visible))).all()
    available = {node["id"]: (canvas.scene_id, node) for canvas in canvases for node in canvas.data_json["nodes"]}
    targets = list(dict.fromkeys(target_ids or []))
    if len(targets) > 100 or any(identifier not in available for identifier in targets):
        raise AppError(404, "CONTEXT_TARGET_NOT_FOUND", "目标节点不在当前场景或可检索的剧情历史中")
    if any(available[identifier][1]["type"] == "agent" for identifier in targets):
        raise AppError(403, "CONTEXT_CONTROL_NODE_PROTECTED", "Agent控制节点不能作为剧情内容来源")
    selected = targets if material_only else targets or [identifier for identifier, (owner_scene, node) in available.items() if owner_scene in certain and node["type"] == "text"]
    selected.sort(key=lambda identifier: available[identifier][0] != scene_id)
    texts = []
    for identifier in selected:
        owner_scene, node = available[identifier]
        if node["type"] not in {"text", "config"}:
            continue
        content = node.get("metadata", {}).get("content")
        if not isinstance(content, str) or not content:
            content = node.get("metadata", {}).get("composerContent")
        if not isinstance(content, str) or not content:
            continue
        text_item = {"id": identifier, "title": node.get("title", ""), "content": content,
            "sceneId": owner_scene, "fact": False, "state": "current_source",
            "source": {"kind": "canvas_node", "id": identifier, "revision": project.revision, "sceneId": owner_scene}}
        if owner_scene in conditional:
            text_item["condition"] = {"sceneId": owner_scene, "requiresPathConfirmation": True}
        texts.append(("canvasTexts", text_item))
    ordered = texts + sorted(ordered, key=lambda pair: {"memories": 0, "storyFacts": 1, "conditionalMemories": 2, "creationEvents": 3, "candidates": 4}[pair[0]])
    for group, item in ordered:
        source = item["source"]
        candidate = copy.deepcopy(item)
        result[group].append(candidate)
        result["sources"].append(source)
        excess = _length(result) + 256 - max_chars
        if excess > 0:
            content = candidate["content"]
            candidate["truncated"], candidate["originalLength"] = True, len(content)
            remaining = len(content) - (_length(result) + 256 - max_chars)
            if remaining > 160:
                candidate["content"] = content[:remaining]
                result["truncated"] = True
            else:
                result[group].pop()
                result["sources"].pop()
                result["omittedCount"] += 1
                result["truncated"] = True
    while result["characterCount"] != _length(result):
        result["characterCount"] = _length(result)
    if _length(result) > max_chars:
        raise AppError(422, "CONTEXT_CAPACITY_TOO_SMALL", "上下文来源信息超过所选容量，请增加容量")
    return result
