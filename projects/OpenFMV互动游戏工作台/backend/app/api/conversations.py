from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.conversation_models import ProjectConversation
from app.conversation_schemas import ConversationCapabilities, ConversationCreate, ConversationEnvelope, ConversationList, ConversationSend
from app.models import User
from app.services.conversations import SKILL_NAMES, conversation_summary, conversation_view, create_conversation, require_conversation, send_message
from app.services.projects import require_project
from app.tools.project_tools import tool_catalog


router = APIRouter(tags=["project-conversations"])


@router.get("/projects/{project_id}/conversation-capabilities", response_model=ConversationCapabilities)
def capabilities(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_project(session, project_id, user.id)
    return {"tools": [item["name"] for item in tool_catalog("director", "R4")], "skills": SKILL_NAMES}


@router.get("/projects/{project_id}/conversations", response_model=ConversationList)
def list_conversations(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_project(session, project_id, user.id)
    values = session.scalars(select(ProjectConversation).where(ProjectConversation.project_id == project_id, ProjectConversation.owner_id == user.id).order_by(ProjectConversation.updated_at.desc())).all()
    return {"conversations": [conversation_summary(value) for value in values]}


@router.post("/projects/{project_id}/conversations", response_model=ConversationEnvelope, status_code=201)
def create(project_id: str, body: ConversationCreate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"conversation": conversation_view(session, create_conversation(session, project_id, user, body))}


@router.get("/projects/{project_id}/conversations/{conversation_id}", response_model=ConversationEnvelope)
def get_conversation(project_id: str, conversation_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"conversation": conversation_view(session, require_conversation(session, project_id, conversation_id, user.id))}


@router.post("/projects/{project_id}/conversations/{conversation_id}/messages", response_model=ConversationEnvelope, status_code=202)
def send(project_id: str, conversation_id: str, body: ConversationSend, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"conversation": conversation_view(session, send_message(session, project_id, conversation_id, user, body))}
