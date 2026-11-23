import base64
import hashlib
import hmac
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connection_models import CodexConnection, CodexConnectionEvent, CodexPairing
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import new_token, token_hash
from app.models import User
from app.services.projects import get_operation, iso_timestamp, lock_write, require_project, save_operation


SUPPORTED_CODEX_VERSION = "0.168.0"
ONLINE_WINDOW_SECONDS = 30


def pairing_code(identifier: str) -> str:
    key = (get_settings().data_dir.resolve() / "secrets" / "master.key").read_bytes()
    digest = hmac.digest(key, f"openfmv-codex-pairing:{identifier}".encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def connection_view(connection: CodexConnection) -> dict:
    now = time.time()
    status = "revoked" if connection.revoked_at is not None else "expired" if connection.expires_at <= now else "online" if now - connection.last_seen_at <= ONLINE_WINDOW_SECONDS else "offline"
    return {"id": connection.id, "projectId": connection.project_id, "tools": connection.tools_json,
            "canGenerateImages": "native_image_v1" in connection.capabilities_json,
            "clientVersion": connection.client_version, "status": status,
            "expiresAt": iso_timestamp(connection.expires_at), "lastSeenAt": iso_timestamp(connection.last_seen_at),
            "createdAt": iso_timestamp(connection.created_at)}


def active_image_connection(session: Session, owner_id: str, project_id: str) -> CodexConnection | None:
    now = time.time()
    candidates = session.scalars(select(CodexConnection).where(CodexConnection.owner_id == owner_id,
        CodexConnection.project_id == project_id, CodexConnection.revoked_at.is_(None),
        CodexConnection.expires_at > now, CodexConnection.last_seen_at >= now - ONLINE_WINDOW_SECONDS)
        .order_by(CodexConnection.last_seen_at.desc(), CodexConnection.id))
    return next((candidate for candidate in candidates if "native_image_v1" in candidate.capabilities_json), None)


def require_image_connection(session: Session, owner_id: str, project_id: str) -> CodexConnection:
    connection = active_image_connection(session, owner_id, project_id)
    if connection is None:
        raise AppError(409, "CODEX_IMAGE_CONNECTION_UNAVAILABLE", "请在「问问 AI」连接支持图片生成的个人 Codex")
    return connection


def create_pairing(session: Session, owner_id: str, project_id: str, payload: dict) -> dict:
    lock_write(session)
    require_project(session, project_id, owner_id)
    replay = get_operation(session, owner_id, project_id, payload["operationId"], "codex.pair", payload)
    if replay is not None:
        pairing = session.get(CodexPairing, replay["pairingId"])
    else:
        now = time.time()
        pairing = CodexPairing(owner_id=owner_id, project_id=project_id, code_hash="pending",
                               tools_json=sorted(set(payload["tools"])), expires_at=now + 300,
                               connection_expires_at=now + payload["expiresInSeconds"])
        session.add(pairing)
        session.flush()
        pairing.code_hash = token_hash(pairing_code(pairing.id))
        save_operation(session, owner_id, project_id, payload["operationId"], "codex.pair", payload, {"pairingId": pairing.id})
        session.commit()
    return {"pairingId": pairing.id, "pairingCode": pairing_code(pairing.id), "projectId": project_id,
            "tools": pairing.tools_json, "expiresAt": iso_timestamp(pairing.expires_at),
            "connectionExpiresAt": iso_timestamp(pairing.connection_expires_at)}


def claim_pairing(session: Session, pairing_id: str, code: str, client_version: str) -> dict:
    if client_version != SUPPORTED_CODEX_VERSION:
        raise AppError(409, "CODEX_VERSION_UNSUPPORTED", f"当前连接程序验证的Codex版本为{SUPPORTED_CODEX_VERSION}，请核对本机版本")
    lock_write(session)
    pairing = session.get(CodexPairing, pairing_id)
    if pairing is None or not hmac.compare_digest(pairing.code_hash, token_hash(code)):
        raise AppError(403, "PAIRING_INVALID", "配对凭证无效")
    if pairing.expires_at <= time.time():
        raise AppError(409, "PAIRING_EXPIRED", "配对请求已过期，请在项目中重新创建")
    if pairing.claimed_at is not None:
        raise AppError(409, "PAIRING_ALREADY_CLAIMED", "配对请求已使用，请查看现有连接")
    require_project(session, pairing.project_id, pairing.owner_id)
    token = new_token()
    now = time.time()
    connection = CodexConnection(owner_id=pairing.owner_id, project_id=pairing.project_id,
                                  token_hash=token_hash(token), tools_json=pairing.tools_json,
                                  client_version=client_version, expires_at=pairing.connection_expires_at,
                                  last_seen_at=now)
    session.add(connection)
    pairing.claimed_at = now
    session.flush()
    session.add(CodexConnectionEvent(connection_id=connection.id, event_type="paired", details_json={"pairingId": pairing.id}))
    session.commit()
    return {"connection": connection_view(connection), "token": token}


def require_connection(session: Session, authorization: str | None) -> CodexConnection:
    if not authorization or not authorization.startswith("Bearer "):
        raise AppError(401, "CONNECTION_AUTH_REQUIRED", "需要有效的本机连接凭据")
    token = authorization.removeprefix("Bearer ")
    connection = session.scalar(select(CodexConnection).where(CodexConnection.token_hash == token_hash(token)))
    if connection is None:
        raise AppError(401, "CONNECTION_AUTH_REQUIRED", "连接凭据无效")
    assert_connection_active(connection)
    if session.get(User, connection.owner_id) is None:
        raise AppError(403, "CONNECTION_REVOKED", "连接所属账号不存在")
    require_project(session, connection.project_id, connection.owner_id)
    return connection


def assert_connection_active(connection: CodexConnection) -> None:
    if connection.revoked_at is not None:
        raise AppError(403, "CONNECTION_REVOKED", "连接授权已撤销")
    if connection.expires_at <= time.time():
        raise AppError(403, "CONNECTION_EXPIRED", "连接授权已到期")


def revoke_connection(session: Session, owner_id: str, project_id: str, identifier: str, operation_id: str) -> dict:
    from app.agent_models import AgentRun

    lock_write(session)
    require_project(session, project_id, owner_id)
    connection = session.scalar(select(CodexConnection).where(CodexConnection.id == identifier, CodexConnection.owner_id == owner_id, CodexConnection.project_id == project_id))
    if connection is None:
        raise AppError(404, "CONNECTION_NOT_FOUND", "连接不存在或无权访问")
    payload = {"connectionId": identifier, "operationId": operation_id}
    replay = get_operation(session, owner_id, project_id, operation_id, "codex.revoke", payload)
    if replay is not None:
        return connection_view(connection)
    now = time.time()
    connection.revoked_at = connection.revoked_at or now
    for run in session.scalars(select(AgentRun).where(AgentRun.owner_id == owner_id, AgentRun.project_id == project_id, AgentRun.lease_owner == connection.id)).all():
        if run.status not in {"completed", "failed", "stopped"}:
            run.status, run.lease_owner, run.lease_expires_at = "waiting_connection", None, None
            run.error_code, run.updated_at = "CONNECTION_REVOKED", now
    session.add(CodexConnectionEvent(connection_id=connection.id, event_type="revoked", details_json={}))
    result = connection_view(connection)
    save_operation(session, owner_id, project_id, operation_id, "codex.revoke", payload, result)
    session.commit()
    return result


def heartbeat(session: Session, connection: CodexConnection, capabilities: list[str] | None = None) -> dict:
    from app.agent_models import AgentRun

    lock_write(session)
    session.refresh(connection)
    assert_connection_active(connection)
    now = time.time()
    if capabilities is not None:
        connection.capabilities_json = capabilities
    connection.last_seen_at = now
    runs = session.scalars(select(AgentRun).where(AgentRun.owner_id == connection.owner_id, AgentRun.project_id == connection.project_id,
                                                AgentRun.lease_owner == connection.id, AgentRun.status.not_in(["completed", "failed", "stopped"]))).all()
    active = []
    for run in runs:
        if run.lease_expires_at is None or run.lease_expires_at <= now:
            run.status, run.lease_owner = "waiting_connection", None
            continue
        run.lease_expires_at = now + 90
        active.append({"id": run.id, "status": run.status, "stopRequested": bool(run.stop_requested), "threadId": run.thread_id, "turnId": run.turn_id})
    session.commit()
    return {"connection": connection_view(connection), "activeRuns": active}


def require_run_lease(session: Session, connection: CodexConnection, run_id: str):
    from app.agent_models import AgentRun

    run = session.scalar(select(AgentRun).where(AgentRun.id == run_id, AgentRun.owner_id == connection.owner_id, AgentRun.project_id == connection.project_id, AgentRun.executor == "codex"))
    if run is None:
        raise AppError(404, "AGENT_RUN_NOT_FOUND", "运行记录不存在或无权访问")
    if run.lease_owner != connection.id or run.lease_expires_at is None or run.lease_expires_at <= time.time():
        raise AppError(409, "CONNECTION_LEASE_LOST", "执行授权已变化，请重新领取并读取当前状态")
    return run


def record_event(session: Session, connection: CodexConnection, payload: dict) -> dict:
    lock_write(session)
    session.refresh(connection)
    assert_connection_active(connection)
    run = require_run_lease(session, connection, payload["runId"])
    event_type = payload["eventType"]
    thread_id, turn_id = payload.get("threadId"), payload.get("turnId")
    if run.thread_id and thread_id and run.thread_id != thread_id:
        raise AppError(409, "CODEX_THREAD_MISMATCH", "线程标识与已保存的运行不一致")
    if run.turn_id and turn_id and run.turn_id != turn_id:
        raise AppError(409, "CODEX_TURN_MISMATCH", "回合标识与已保存的运行不一致")
    if thread_id:
        run.thread_id = thread_id
    if turn_id:
        run.turn_id = turn_id
    now = time.time()
    run.updated_at, connection.last_seen_at = now, now
    run.lease_expires_at = now + 90
    if event_type in {"disconnected", "failed", "interrupted"} and run.status not in {"completed", "stopped", "failed"}:
        if run.stop_requested or event_type == "interrupted":
            run.status = "stopped"
        elif payload.get("errorCode") in {"REVISION_CONFLICT", "TARGET_CHANGED"}:
            run.status = "conflict"
        elif event_type == "failed":
            run.status = "failed"
        elif run.status not in {"conflict", "waiting_confirmation", "submission_unknown"}:
            run.status = "waiting_connection"
        run.error_code = payload.get("errorCode")
        if run.error_code is not None:
            run.error_message = "项目或目标已有变化，请读取当前版本并核对后恢复" if run.status == "conflict" else "本机任务已保存实际进度，请核对连接与任务状态"
        if run.status in {"stopped", "failed"}:
            run.finished_at = now
    details = {"usage": payload.get("usage"), "costMicros": None, "costStatus": "unknown", "errorCode": payload.get("errorCode")}
    if payload.get("usage") is not None:
        run.result_json = {**(run.result_json or {}), "reportedCodexUsage": payload["usage"]}
    session.add(CodexConnectionEvent(connection_id=connection.id, run_id=run.id, event_type=event_type,
                                     thread_id=run.thread_id, turn_id=run.turn_id, details_json=details))
    session.commit()
    return {"runId": run.id, "status": run.status}
