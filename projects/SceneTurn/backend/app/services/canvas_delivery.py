from collections import defaultdict

from sqlalchemy import or_, select, true

from app.agent_models import AgentRun, AgentStep
from app.db import json_array_items
from app.services.material_delivery import writer_body


MATERIAL_PROVENANCE_METADATA = {"materialArtifact", "materialVideoInput", "publicMaterialRole"}
PUBLISHED_ARTIFACT_METADATA = {"creativeArtifact", "artifactContractVersion", "artifactSources", "artifactContentSha256",
    "generationPrompts", "reusedBoundaryFrames"}
ARTIFACT_LAYOUT_METADATA = {"groupId", "fontSize"}


def _metadata(node):
    value = node.get("metadata")
    return value if isinstance(value, dict) else {}


def _artifact(metadata):
    value = metadata.get("creativeArtifact")
    return value if isinstance(value, dict) else None


def _internal_video_source(node):
    return node["type"] == "config" and _metadata(node).get("materialVideoInput") is True


def _material_artifact(node, material_node_ids):
    metadata = _metadata(node)
    return _artifact(metadata) if node.get("id") in material_node_ids or metadata.get("materialArtifact") is True else None


def material_canvas_ids(session, project_id, owner_id, scene_id=None):
    scope = (AgentRun.project_id == project_id, AgentRun.owner_id == owner_id,
        or_(AgentRun.limits_json["workspace"].as_string() == "materials",
            AgentRun.limits_json["materialConversationId"].as_string().is_not(None)))
    if scene_id is not None:
        scope = (*scope, AgentRun.scene_id == scene_id)
    by_scene = defaultdict(set)
    for scene, identifiers in session.execute(select(AgentRun.scene_id, AgentRun.created_ids_json).where(*scope)):
        by_scene[scene].update(identifiers)
    receipt = json_array_items(session, AgentStep.tool_results_json)
    source = receipt.c.value["result"]["sourceNodeId"].as_string()
    operation = receipt.c.value["tool"].as_string()
    written = select(AgentRun.scene_id, source).select_from(AgentRun).join(AgentStep, AgentStep.run_id == AgentRun.id).join(receipt, true())
    for scene, identifier in session.execute(written.where(*scope, operation.in_(("write_artifact", "patch_artifact")), source.is_not(None))):
        by_scene[scene].add(identifier)
    return by_scene


def public_canvas_node(node, material_node_ids=frozenset()):
    if "metadata" not in node:
        return node
    metadata = _metadata(node)
    artifact = _material_artifact(node, material_node_ids)
    if artifact is None:
        return {**node, "metadata": {key: value for key, value in metadata.items() if key not in MATERIAL_PROVENANCE_METADATA}}
    role = artifact["role"]
    content = writer_body(artifact) if role == "writer" else artifact["productionBrief"]["objective"] if role == "director" else ""
    layout = {key: value for key, value in metadata.items() if key in ARTIFACT_LAYOUT_METADATA}
    return {**node, "metadata": {**layout, "publicMaterialRole": role, "content": content}}


def _connected_to(connection, identifiers):
    return connection["fromNodeId"] in identifiers or connection["toNodeId"] in identifiers


def public_canvas(snapshot, material_node_ids=frozenset()):
    hidden_ids = {node["id"] for node in snapshot["nodes"] if _internal_video_source(node)}
    return {**snapshot,
        "nodes": [public_canvas_node(node, material_node_ids) for node in snapshot["nodes"] if node["id"] not in hidden_ids],
        "connections": [edge for edge in snapshot["connections"] if not _connected_to(edge, hidden_ids)]}


def public_project_envelope(session, owner_id, envelope):
    by_scene = material_canvas_ids(session, envelope["project"]["id"], owner_id)
    return {**envelope, "canvases": [public_canvas(canvas, by_scene[canvas["sceneId"]]) for canvas in envelope["canvases"]]}


def _restore_node(submitted, original, material_node_ids):
    submitted_metadata = _metadata(submitted)
    original_metadata = _metadata(original)
    if _material_artifact(original, material_node_ids) is not None:
        metadata = {key: value for key, value in original_metadata.items() if key not in ARTIFACT_LAYOUT_METADATA}
        metadata.update({key: value for key, value in submitted_metadata.items() if key in ARTIFACT_LAYOUT_METADATA})
        return {**submitted, "type": original["type"], "metadata": metadata}
    reserved = MATERIAL_PROVENANCE_METADATA if _artifact(original_metadata) is not None else MATERIAL_PROVENANCE_METADATA | PUBLISHED_ARTIFACT_METADATA
    metadata = {key: value for key, value in submitted_metadata.items() if key not in reserved}
    return {**submitted, "metadata": metadata} if "metadata" in submitted or metadata else submitted


def restore_canvas_private_data(current, submitted, material_node_ids=frozenset()):
    original_nodes = {node["id"]: node for node in current["nodes"]}
    hidden_nodes = {identifier: node for identifier, node in original_nodes.items() if _internal_video_source(node)}
    visible_nodes = [_restore_node(node, original_nodes.get(node["id"], {}), material_node_ids) for node in submitted["nodes"] if node["id"] not in hidden_nodes]
    nodes = [*visible_nodes, *hidden_nodes.values()]
    node_ids = {node["id"] for node in nodes}
    hidden_edges = {edge["id"]: edge for edge in current["connections"] if _connected_to(edge, hidden_nodes)}
    visible_edges = [edge for edge in submitted["connections"] if edge["id"] not in hidden_edges and not _connected_to(edge, hidden_nodes)]
    internal_edges = [edge for edge in hidden_edges.values() if edge["fromNodeId"] in node_ids and edge["toNodeId"] in node_ids]
    return {**submitted, "nodes": nodes, "connections": [*visible_edges, *internal_edges]}
