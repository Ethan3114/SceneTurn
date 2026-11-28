from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.connection_models import CodexConnection
from app.connection_schemas import BridgeComplete, BridgeEvent, BridgeResult, BridgeToolCall, ConnectionClaimResult, ConnectionHeartbeat, ConnectionList, ConnectionRevoke, ConnectionView, HeartbeatResult, PairingClaim, PairingCreate, PairingResult
from app.core.errors import AppError
from app.models import User
from app.services import connections
from app.services.conversations import connection_skills
from app.services.projects import require_project


router = APIRouter(tags=["codex-connections"])


def current_connection(authorization: str | None = Header(default=None), session: Session = Depends(get_db)) -> CodexConnection:
    return connections.require_connection(session, authorization)


@router.post("/projects/{project_id}/connections/pairings", response_model=PairingResult, status_code=201)
def create_pairing(project_id: str, body: PairingCreate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return connections.create_pairing(session, user.id, project_id, body.model_dump(by_alias=True))


@router.post("/connections/pairings/{pairing_id}/claim", response_model=ConnectionClaimResult)
def claim_pairing(pairing_id: str, body: PairingClaim, session: Session = Depends(get_db)):
    return connections.claim_pairing(session, pairing_id, body.pairing_code, body.client_version)


@router.get("/projects/{project_id}/connections", response_model=ConnectionList)
def list_connections(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_project(session, project_id, user.id)
    records = session.scalars(select(CodexConnection).where(CodexConnection.owner_id == user.id, CodexConnection.project_id == project_id).order_by(CodexConnection.created_at.desc())).all()
    return {"connections": [connections.connection_view(record) for record in records]}


@router.post("/projects/{project_id}/connections/{identifier}/revoke", response_model=ConnectionView)
def revoke_connection(project_id: str, identifier: str, body: ConnectionRevoke, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return connections.revoke_connection(session, user.id, project_id, identifier, body.operation_id)


@router.post("/connections/heartbeat", response_model=HeartbeatResult)
def heartbeat(body: ConnectionHeartbeat | None = None, connection: CodexConnection = Depends(current_connection), session: Session = Depends(get_db)):
    return connections.heartbeat(session, connection, body.capabilities if body else None)


@router.post("/connections/claim", response_model=BridgeResult)
def claim_run(skill_format: str | None = Header(default=None, alias="X-SceneTurn-Skill-Format"),
              connection: CodexConnection = Depends(current_connection), session: Session = Depends(get_db)):
    from app.services.agent import claim_codex_run

    result = claim_codex_run(session, connection.owner_id, connection.project_id, connection.id)
    if result is not None:
        result["tools"] = [tool for tool in result["tools"] if tool["name"] in connection.tools_json]
        result["skills"] = connection_skills(result["skills"], skill_format)
    return {"result": result}


@router.post("/connections/events", response_model=BridgeResult)
def record_event(body: BridgeEvent, connection: CodexConnection = Depends(current_connection), session: Session = Depends(get_db)):
    return {"result": connections.record_event(session, connection, body.model_dump(by_alias=True))}


@router.post("/connections/tools", response_model=BridgeResult)
def execute_tool(body: BridgeToolCall, connection: CodexConnection = Depends(current_connection), session: Session = Depends(get_db)):
    from app.services.agent import execute_codex_tool

    connections.require_run_lease(session, connection, body.run_id)
    if body.tool_name not in connection.tools_json:
        raise AppError(403, "TOOL_NOT_ALLOWED", "连接授权不包含此项目工具")
    return {"result": execute_codex_tool(session, connection.owner_id, body.run_id, body.tool_name, body.arguments, body.operation_id, body.expected_revision, connection.id)}


@router.post("/connections/complete", response_model=BridgeResult)
def complete_run(body: BridgeComplete, connection: CodexConnection = Depends(current_connection), session: Session = Depends(get_db)):
    from app.services.agent import finish_codex_run

    connections.require_run_lease(session, connection, body.run_id)
    return {"result": finish_codex_run(session, connection.owner_id, body.run_id, body.result, body.thread_id, body.turn_id, body.usage.model_dump(by_alias=True) if body.usage is not None else None, connection.id)}
