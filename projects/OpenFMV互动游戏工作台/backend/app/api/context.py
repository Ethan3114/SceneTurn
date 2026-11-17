from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.context_schemas import ContextResult, ContextRetrieve, ContextSource, ContextSourceResult, MemoryList, MemoryResult, MemoryWrite
from app.models import User
from app.services.context import get_memory, get_source, list_memories, retrieve_context, write_memory


router = APIRouter(tags=["project-context"])


@router.post("/projects/{project_id}/memories", response_model=MemoryResult, status_code=201)
def create_memory(project_id: str, body: MemoryWrite, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return write_memory(session, project_id, user.id, body.model_dump(by_alias=True))


@router.get("/projects/{project_id}/memories", response_model=MemoryList)
def read_memories(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return list_memories(session, project_id, user.id)


@router.get("/projects/{project_id}/memories/{memory_id}", response_model=MemoryResult)
def read_memory(project_id: str, memory_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return get_memory(session, project_id, user.id, memory_id)


@router.post("/projects/{project_id}/context/retrieve", response_model=ContextResult)
def retrieve(project_id: str, body: ContextRetrieve, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return retrieve_context(session, project_id, user.id, body.scene_id, body.branch_id, body.target_ids, body.max_chars)


@router.post("/projects/{project_id}/context/source", response_model=ContextSourceResult)
def read_source(project_id: str, body: ContextSource, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return get_source(session, project_id, user.id, body.model_dump(by_alias=True))
