from fastapi import APIRouter, Depends
from typing import Literal
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun, AgentStep
from app.agent_schemas import RoleList, RunControl, RunCreate, RunEnvelope, RunList, RunResume, RunRetryUnknown, RunLimitExtension, ToolInvocation, ToolResult
from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.core.errors import AppError
from app.models import User
from app.services.agent import create_run, extend_run_limit, require_run, resume_run, retry_unknown_run, run_view, stop_run
from app.services.projects import require_project
from app.tools.project_tools import execute_tool, role_catalog


router = APIRouter(tags=["role-agents"])


@router.get("/projects/{project_id}/agent-roles", response_model=RoleList)
def roles(project_id: str, workspace: Literal["project", "scene"] | None = None, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_project(session, project_id, user.id)
    return {"roles": role_catalog(workspace)}


@router.post("/projects/{project_id}/agent-runs", response_model=RunEnvelope, status_code=202)
def start(project_id: str, body: RunCreate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"run": run_view(session, create_run(session, project_id, user, body, get_settings()))}


@router.get("/projects/{project_id}/agent-runs", response_model=RunList)
def list_runs(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_project(session, project_id, user.id)
    runs = session.scalars(select(AgentRun).where(AgentRun.project_id == project_id, AgentRun.owner_id == user.id).order_by(AgentRun.created_at.desc()).limit(100)).all()
    return {"runs": [run_view(session, run) for run in runs]}


@router.get("/projects/{project_id}/agent-runs/{run_id}", response_model=RunEnvelope)
def get_run(project_id: str, run_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"run": run_view(session, require_run(session, project_id, run_id, user.id))}


@router.post("/projects/{project_id}/agent-runs/{run_id}/stop", response_model=RunEnvelope)
def stop(project_id: str, run_id: str, body: RunControl, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"run": run_view(session, stop_run(session, project_id, run_id, user.id, body.operation_id))}


@router.post("/projects/{project_id}/agent-runs/{run_id}/resume", response_model=RunEnvelope)
def resume(project_id: str, run_id: str, body: RunResume, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"run": run_view(session, resume_run(session, project_id, run_id, user.id, body.operation_id, body.expected_revision))}


@router.post("/projects/{project_id}/agent-runs/{run_id}/retry-unknown", response_model=RunEnvelope)
def retry_unknown(project_id: str, run_id: str, body: RunRetryUnknown, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"run": run_view(session, retry_unknown_run(session, project_id, run_id, user, body, get_settings()))}


@router.post("/projects/{project_id}/agent-tools/{tool_name}", response_model=ToolResult)
def tool(project_id: str, tool_name: str, body: ToolInvocation, user: User = Depends(current_user), session: Session = Depends(get_db)):
    run = require_run(session, project_id, body.run_id, user.id)
    step = session.get(AgentStep, body.step_id) if body.step_id else None
    if step is not None and step.run_id != run.id:
        raise AppError(404, "AGENT_STEP_NOT_FOUND", "工具步骤不属于本次任务")
    session.commit()
    return execute_tool(session, run, step.role if step else run.role, tool_name, body.arguments, body.operation_id, body.expected_revision, get_settings(), step)


@router.post("/projects/{project_id}/agent-runs/{run_id}/extend-limit", response_model=RunEnvelope)
def extend_limit(project_id: str, run_id: str, body: RunLimitExtension, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"run": run_view(session, extend_run_limit(session, project_id, run_id, user, body, get_settings()))}
