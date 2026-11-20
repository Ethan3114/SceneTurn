from sqlalchemy import select

from app.agent_models import AgentRun, AgentStep
from app.core.errors import AppError
from app.services.projects import payload_digest


UPSTREAM_ROLES = {
    "director": (),
    "writer": (),
    "character_designer": ("writer",),
    "scene_designer": ("writer",),
    "storyboarder": ("writer", "character_designer", "scene_designer"),
    "media_producer": ("storyboarder",),
    "sound_designer": ("writer", "storyboarder"),
}


def adopted_material_inputs(session, run, canvas, target_ids):
    targets = set(target_ids)
    if not targets:
        return []
    nodes = {node["id"]: node for node in canvas.data_json["nodes"] if node["id"] in targets}
    known = {}
    for node_id, node in nodes.items():
        artifact = node.get("metadata", {}).get("creativeArtifact")
        if artifact and artifact["role"] in UPSTREAM_ROLES:
            known[node_id] = {"nodeId": node_id, "role": artifact["role"], "artifactHash": payload_digest(artifact)}
    if not known:
        return []
    results = session.execute(select(AgentRun.id, AgentRun.result_json).where(
        AgentRun.owner_id == run.owner_id, AgentRun.project_id == run.project_id, AgentRun.scene_id == run.scene_id,
        AgentRun.result_json.is_not(None),
    ).order_by(AgentRun.created_at.desc())).all()
    adopted = {}
    for run_id, result in results:
        result = result or {}
        reviews = [result.get("materialReview") or {}, *reversed(result.get("materialReviewHistory", []))]
        for review in reviews:
            source = known.get(review.get("nodeId"))
            if source and review.get("status") == "approved" and review.get("artifactHash") == source["artifactHash"]:
                adopted.setdefault(source["nodeId"], {**source, "runId": run_id})
    return [adopted[node_id] for node_id in target_ids if node_id in adopted]


def material_plan(session, run, canvas, plan):
    producers = {}
    for index, item in enumerate(plan):
        role = item["role"]
        if item["kind"] != "produce":
            continue
        if role in producers:
            raise AppError(422, "MATERIAL_ROLE_DUPLICATE", "每轮同一岗位统一交付一次，修订使用该岗位的确认卡")
        producers[role] = index
    referenced = adopted_material_inputs(session, run, canvas, run.target_ids_json)
    for index, item in enumerate(plan):
        if item["kind"] != "produce":
            continue
        role = item["role"]
        required = {producers[upstream] for upstream in UPSTREAM_ROLES[role] if upstream in producers}
        if any(upstream >= index for upstream in required):
            raise AppError(422, "MATERIAL_HANDOFF_ORDER", "请将负责上游成果的岗位排在使用成果的岗位之前")
        if role == "media_producer" and not required and not any(source["role"] == "storyboarder" for source in referenced):
            raise AppError(422, "MATERIAL_STORYBOARD_REQUIRED", "视频师需要本轮分镜师或用户明确选择的已采用分镜")
        item["dependsOn"] = sorted(set(item["dependsOn"]) | required)
    return plan


def prepare_material_handoff(session, run, step, canvas):
    context = run.limits_json["materialContext"]
    handoffs = context.get("handoffs", {})
    parents = set(run.plan_json[step.index]["dependsOn"])
    parent_nodes = []
    if parents:
        steps = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.index.in_(parents)).order_by(AgentStep.index)).all()
        for upstream in steps:
            if upstream.status != "completed":
                raise AppError(409, "AGENT_DEPENDENCY_NOT_READY", "等待前置岗位交付并采用成果")
            if upstream.role in UPSTREAM_ROLES[step.role]:
                receipt = next((item for item in reversed(upstream.tool_results_json) if item["tool"] in {"write_artifact", "patch_artifact"}), None)
                if receipt is None:
                    raise AppError(409, "AGENT_DEPENDENCY_NOT_READY", "前置岗位缺少已保存的成果")
                parent_nodes.append(receipt["result"]["sourceNodeId"])
    adopted = adopted_material_inputs(session, run, canvas, [*parent_nodes, *run.target_ids_json])
    if any(not any(item["nodeId"] == node_id for item in adopted) for node_id in parent_nodes):
        raise AppError(409, "AGENT_DEPENDENCY_NOT_READY", "前置成果尚未采用或版本已经变化")
    inputs = list({item["nodeId"]: item for item in adopted if item["role"] in UPSTREAM_ROLES[step.role]}.values())
    if step.role == "media_producer" and not inputs:
        raise AppError(409, "AGENT_DEPENDENCY_NOT_READY", "视频准备需要采用后的分镜成果")
    frozen = {"role": step.role, "inputs": inputs}
    stored = handoffs.get(step.id)
    if stored is not None and stored != frozen:
        raise AppError(409, "AGENT_ARTIFACT_SOURCE_CHANGED", "交接的上游成果已变化，请核对后重新制作")
    if stored is None:
        run.limits_json = {**run.limits_json, "materialContext": {**context, "handoffs": {**handoffs, step.id: frozen}}}
    return inputs


def read_material_handoff(session, run, step, project, inputs, settings):
    from app.tools.project_tools import execute_tool

    if not inputs:
        return []
    session.flush()
    artifacts = []
    for source in inputs:
        node_id, digest = source["nodeId"], source["artifactHash"]
        result = next((receipt["result"] for receipt in reversed(step.tool_results_json)
            if receipt["tool"] == "read_artifact" and receipt["revision"] == project.revision
            and receipt["result"].get("targetId") == node_id and receipt["result"].get("artifactSha256") == digest
            and receipt["result"].get("referenceScope") == "current" and receipt["result"].get("sourcesValidated") is True), None)
        if result is None:
            operation_id = "agent-handoff-" + payload_digest([step.id, node_id, digest, project.revision])
            result = execute_tool(session, run, step.role, "read_artifact", {"targetId": node_id}, operation_id,
                project.revision, settings, step, manage_transaction=False, execution_origin="runtime")["result"]
            session.flush()
        artifact = result.get("artifact") or {}
        if (result.get("targetId") != node_id or result.get("artifactSha256") != digest
                or result.get("referenceScope") != "current" or result.get("sourcesValidated") is not True
                or artifact.get("role") != source["role"] or payload_digest(artifact) != digest):
            raise AppError(409, "AGENT_ARTIFACT_SOURCE_CHANGED", "交接的上游成果已变化，请核对后重新制作")
        display = {**source, "artifact": artifact}
        if step.role == "media_producer" and "approvedVideoSegments" in result:
            display["approvedVideoSegments"] = result["approvedVideoSegments"]
        artifacts.append(display)
    return artifacts


def validate_material_handoff(session, run, step, canvas, artifact):
    if step is None:
        raise AppError(422, "MATERIAL_STEP_REQUIRED", "素材交付必须属于明确岗位")
    required = prepare_material_handoff(session, run, step, canvas)
    provided = {item["referenceId"] for item in artifact["sources"] if item["kind"] in {"artifact", "canvas_node"}}
    if any(item["nodeId"] not in provided for item in required):
        raise AppError(422, "MATERIAL_HANDOFF_SOURCE_REQUIRED", "保存成果前请在 sources 中引用全部已交接的上游成果")
    read_versions = {(receipt["result"].get("targetId"), receipt["result"].get("artifactSha256")) for receipt in step.tool_results_json
                     if receipt["tool"] == "read_artifact"}
    if any((item["nodeId"], item["artifactHash"]) not in read_versions for item in required):
        raise AppError(422, "MATERIAL_HANDOFF_READ_REQUIRED", "请先使用 read_artifact 读取 context.adoptedHandoff 中对应的已采用版本")
