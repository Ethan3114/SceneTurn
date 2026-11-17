import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun
from app.conversation_models import ConversationMessage, ProjectConversation
from app.conversation_schemas import ConversationCreate, ConversationSend
from app.core.errors import AppError
from app.models import User
from app.services.projects import get_operation, iso_timestamp, lock_write, require_project, require_revision, save_operation


SKILL_NAMES = ["openfmv-story", "openfmv-layout", "openfmv-timeline", "openfmv-interaction", "openfmv-check"]
BUSY = {"queued", "running", "stopping", "waiting_connection", "waiting_confirmation", "conflict", "submission_unknown"}


def require_conversation(session, project_id, conversation_id, owner_id):
    require_project(session, project_id, owner_id)
    value = session.scalar(select(ProjectConversation).where(ProjectConversation.id == conversation_id, ProjectConversation.project_id == project_id, ProjectConversation.owner_id == owner_id))
    if value is None:
        raise AppError(404, "CONVERSATION_NOT_FOUND", "项目对话不存在或无权访问")
    return value


def conversation_summary(value):
    return {"id": value.id, "projectId": value.project_id, "title": value.title, "createdAt": iso_timestamp(value.created_at), "updatedAt": iso_timestamp(value.updated_at)}


def conversation_view(session, value):
    from app.services.agent import run_view

    messages = session.scalars(select(ConversationMessage).where(ConversationMessage.conversation_id == value.id, ConversationMessage.owner_id == value.owner_id).order_by(ConversationMessage.sequence)).all()
    run = session.get(AgentRun, value.active_run_id) if value.active_run_id else None
    return {**conversation_summary(value), "messages": [{"id": item.id, "role": item.role, "content": item.content, "context": item.context_json, "runId": item.run_id, "createdAt": iso_timestamp(item.created_at)} for item in messages],
            "run": run_view(session, run) if run else None}


def create_conversation(session: Session, project_id: str, user: User, body: ConversationCreate):
    lock_write(session)
    require_project(session, project_id, user.id)
    payload = body.model_dump(by_alias=True)
    previous = get_operation(session, user.id, project_id, body.operation_id, "conversation.create", payload)
    if previous:
        return require_conversation(session, project_id, previous["conversationId"], user.id)
    value = ProjectConversation(owner_id=user.id, project_id=project_id, title=body.title)
    session.add(value)
    session.flush()
    save_operation(session, user.id, project_id, body.operation_id, "conversation.create", payload, {"conversationId": value.id})
    session.commit()
    return value


def send_message(session: Session, project_id: str, conversation_id: str, user: User, body: ConversationSend):
    lock_write(session)
    conversation = require_conversation(session, project_id, conversation_id, user.id)
    payload = {"conversationId": conversation_id, **body.model_dump(by_alias=True)}
    previous = get_operation(session, user.id, project_id, body.operation_id, "conversation.send", payload)
    if previous:
        return conversation
    active = session.get(AgentRun, conversation.active_run_id) if conversation.active_run_id else None
    if active and active.status in BUSY:
        raise AppError(409, "CONVERSATION_BUSY", "当前消息仍在处理，请停止或恢复当前操作后继续")
    project = require_project(session, project_id, user.id)
    require_revision(project, body.expected_revision)
    scenes = [item for item in project.data_json["graphData"]["nodes"] if item["type"] == "scene"]
    if not scenes:
        raise AppError(422, "SCENE_REQUIRED", "请先在剧情编排中创建一个场景")
    scene_ids = {item["id"] for item in scenes}
    scene_id = body.context.scene_id or scenes[0]["id"]
    if scene_id not in scene_ids:
        raise AppError(422, "SCENE_NOT_FOUND", "当前场景不属于此项目")
    node_ids = {item["id"] for item in project.data_json["graphData"]["nodes"]}
    clip_ids = {clip["id"] for scene in scenes for track in scene["data"].get("timeline", {}).get("tracks", []) for clip in track["clips"]}
    if not set(body.context.selected_node_ids) <= node_ids or not set(body.context.selected_clip_ids) <= clip_ids:
        raise AppError(422, "CHAT_CONTEXT_INVALID", "选中对象已经变化，请读取当前项目后重试")
    counts = session.scalar(select(func.count()).select_from(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id))
    run = AgentRun(owner_id=user.id, project_id=project_id, scene_id=scene_id, operation_id=body.operation_id, role="director", executor="codex", goal=body.content,
        target_ids_json=[item["id"] for item in scenes], created_ids_json=[], initial_revision=project.revision, project_revision=project.revision, plan_json=[], status="waiting_connection",
        thread_id=conversation.thread_id or (active.thread_id if active else None), limits_json={"maxSteps": 40, "maxTokens": 32768, "maxCostMicros": 10000000, "deliveryMode": "tool_action", "workspace": "project",
            "toolCatalogVersion": "R4", "conversationId": conversation_id, "editingContext": body.context.model_dump(by_alias=True), "skills": SKILL_NAMES})
    if active and run.thread_id:
        run.limits_json = {**run.limits_json, "codexThreadUsageBaseline": (active.result_json or {}).get("usage", {}).get("threadTotals") if (active.result_json or {}).get("usage") else None}
    session.add(run)
    session.flush()
    sequence = (session.scalar(select(func.max(ConversationMessage.sequence)).where(ConversationMessage.conversation_id == conversation_id)) or 0) + 2
    session.add(ConversationMessage(owner_id=user.id, project_id=project_id, conversation_id=conversation_id, operation_id=body.operation_id, sequence=sequence,
        role="user", content=body.content, context_json=body.context.model_dump(by_alias=True), run_id=run.id))
    conversation.active_run_id, conversation.updated_at = run.id, time.time()
    if counts == 0:
        conversation.title = body.content[:60]
    save_operation(session, user.id, project_id, body.operation_id, "conversation.send", payload, {"runId": run.id})
    session.commit()
    return conversation


def complete_conversation_turn(session: Session, run: AgentRun):
    conversation_id = run.limits_json.get("conversationId")
    if not conversation_id:
        return
    conversation = require_conversation(session, run.project_id, conversation_id, run.owner_id)
    if conversation.active_run_id == run.id:
        conversation.thread_id, conversation.updated_at = run.thread_id, time.time()
    previous = session.scalar(select(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id, ConversationMessage.operation_id == f"reply-{run.id}"))
    if previous is None:
        request = session.scalar(select(ConversationMessage).where(ConversationMessage.conversation_id == conversation.id, ConversationMessage.run_id == run.id, ConversationMessage.role == "user"))
        if request is None:
            raise AppError(409, "CONVERSATION_MESSAGE_MISSING", "Codex任务缺少对应的用户消息")
        session.add(ConversationMessage(owner_id=run.owner_id, project_id=run.project_id, conversation_id=conversation.id, operation_id=f"reply-{run.id}", sequence=request.sequence + 1,
            role="assistant", content=str((run.result_json or {}).get("finalText", "")), context_json={"status": run.status}, run_id=run.id))
