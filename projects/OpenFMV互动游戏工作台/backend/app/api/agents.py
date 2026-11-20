from fastapi import APIRouter, Depends
from typing import Literal
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun, AgentStep
from app.agent_schemas import AgentRunView, RoleList, RunControl, RunCreate, RunEnvelope, RunList, RunResume, RunRetryUnknown, RunLimitExtension, ToolInvocation, ToolResult
from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.core.errors import AppError
from app.material_schemas import MaterialRunView
from app.models import User
from app.services.agent import create_run, extend_run_limit, require_run, resume_run, retry_unknown_run, run_view, stop_run
from app.services.canvas_delivery import material_canvas_ids, public_canvas, public_project_envelope
from app.services.material_delivery import is_material_generation, material_generation_ids, public_material_generation, public_run
from app.services.projects import require_project
from app.tools.project_tools import execute_tool, role_catalog


router = APIRouter(tags=["role-agents"])


class VisibleRunEnvelope(RunEnvelope):
    run: AgentRunView | MaterialRunView


class VisibleRunList(RunList):
    runs: list[AgentRunView | MaterialRunView]


def visible_run(session, run):
    value = run_view(session, run)
    if run.limits_json.get("workspace") == "materials":
        return public_run(value)
    for step in value["steps"]:
        step["toolResults"] = [{**receipt, "result": visible_tool_result(session, run, receipt["result"])}
            for receipt in step["toolResults"]]
    return value


def visible_tool_result(session, run, result):
    value = {**result}
    if {"canvas", "targetId", "sourceNodeId", "canvasTexts"} & value.keys():
        by_scene = material_canvas_ids(session, run.project_id, run.owner_id)
        protected = by_scene[run.scene_id]
        if (value.get("targetId") in protected or value.get("sourceNodeId") in protected) and "artifact" in value:
            return {key: value[key] for key in ("targetId", "sourceNodeId", "title", "revision", "persisted") if key in value}
        if "canvasTexts" in value:
            visible = [item for item in value["canvasTexts"] if item["id"] not in by_scene[item["sceneId"]]]
            value["canvasTexts"] = visible
        if "canvas" in value:
            value["canvas"] = public_canvas(value["canvas"], protected)
    if "canvases" in value:
        value = public_project_envelope(session, run.owner_id, value)
    if "generation" in value:
        generation = value["generation"]
        identifiers = material_generation_ids(session, run.project_id, run.owner_id)
        if is_material_generation(generation, identifiers):
            value["generation"] = public_material_generation(generation)
    return value


@router.get("/projects/{project_id}/agent-roles", response_model=RoleList)
def roles(project_id: str, workspace: Literal["project", "scene"] | None = None, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_project(session, project_id, user.id)
    return {"roles": role_catalog(workspace)}


@router.post("/projects/{project_id}/agent-runs", response_model=RunEnvelope, status_code=202)
def start(project_id: str, body: RunCreate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    if body.workspace == "materials":
        raise AppError(422, "MATERIAL_CONVERSATION_REQUIRED", "请在素材对话中安排制作任务")
    return {"run": visible_run(session, create_run(session, project_id, user, body, get_settings()))}


@router.get("/projects/{project_id}/agent-runs", response_model=VisibleRunList)
def list_runs(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_project(session, project_id, user.id)
    runs = session.scalars(select(AgentRun).where(AgentRun.project_id == project_id, AgentRun.owner_id == user.id).order_by(AgentRun.created_at.desc()).limit(100)).all()
    return {"runs": [visible_run(session, run) for run in runs]}


@router.get("/projects/{project_id}/agent-runs/{run_id}", response_model=VisibleRunEnvelope)
def get_run(project_id: str, run_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"run": visible_run(session, require_run(session, project_id, run_id, user.id))}


@router.post("/projects/{project_id}/agent-runs/{run_id}/stop", response_model=VisibleRunEnvelope)
def stop(project_id: str, run_id: str, body: RunControl, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"run": visible_run(session, stop_run(session, project_id, run_id, user.id, body.operation_id))}


@router.post("/projects/{project_id}/agent-runs/{run_id}/resume", response_model=VisibleRunEnvelope)
def resume(project_id: str, run_id: str, body: RunResume, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"run": visible_run(session, resume_run(session, project_id, run_id, user.id, body.operation_id, body.expected_revision))}


@router.post("/projects/{project_id}/agent-runs/{run_id}/retry-unknown", response_model=VisibleRunEnvelope)
def retry_unknown(project_id: str, run_id: str, body: RunRetryUnknown, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"run": visible_run(session, retry_unknown_run(session, project_id, run_id, user, body, get_settings()))}


@router.post("/projects/{project_id}/agent-tools/{tool_name}", response_model=ToolResult)
def tool(project_id: str, tool_name: str, body: ToolInvocation, user: User = Depends(current_user), session: Session = Depends(get_db)):
    run = require_run(session, project_id, body.run_id, user.id)
    if run.limits_json.get("workspace") == "materials":
        raise AppError(403, "MATERIAL_TOOL_INTERNAL_ONLY", "素材岗位工具由当前制作步骤执行")
    if tool_name in {"read_artifact", "patch_artifact", "write_artifact", "write_script", "apply_canvas_ops"}:
        protected = material_canvas_ids(session, project_id, user.id, run.scene_id)[run.scene_id]
        targets = [body.arguments.get("targetId", body.arguments.get("target_id"))]
        operations = body.arguments.get("operations")
        if isinstance(operations, list):
            targets.extend(item.get("nodeId", item.get("node_id")) for item in operations if isinstance(item, dict))
        if any(isinstance(target, str) and target in protected for target in targets):
            raise AppError(403, "MATERIAL_TOOL_INTERNAL_ONLY", "请在素材对话引用已采用成果并提交修改")
    step = session.get(AgentStep, body.step_id) if body.step_id else None
    if step is not None and step.run_id != run.id:
        raise AppError(404, "AGENT_STEP_NOT_FOUND", "工具步骤不属于本次任务")
    session.commit()
    output = execute_tool(session, run, step.role if step else run.role, tool_name, body.arguments, body.operation_id, body.expected_revision, get_settings(), step)
    return {**output, "result": visible_tool_result(session, run, output["result"])}


@router.post("/projects/{project_id}/agent-runs/{run_id}/extend-limit", response_model=VisibleRunEnvelope)
def extend_limit(project_id: str, run_id: str, body: RunLimitExtension, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"run": visible_run(session, extend_run_limit(session, project_id, run_id, user, body, get_settings()))}
