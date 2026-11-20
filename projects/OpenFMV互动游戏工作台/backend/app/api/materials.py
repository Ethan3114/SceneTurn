from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.material_models import MaterialConversation
from app.material_schemas import MaterialAnswer, MaterialCreate, MaterialEnvelope, MaterialList, MaterialSend, material_request_payload
from app.models import User
from app.services import materials
from app.services.projects import require_project, require_scene
from app.services.projects import get_operation, lock_write, save_operation
from app.services.material_review import answer_review
from app.services.text_generation import require_text_admin
from app.image_models import ImageJob


router = APIRouter(prefix="/projects/{project_id}/scenes/{scene_id}/material-conversations", tags=["material-conversations"])


@router.get("", response_model=MaterialList)
def listing(project_id: str, scene_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_scene(require_project(session, project_id, user.id), scene_id)
    values = session.scalars(select(MaterialConversation).where(MaterialConversation.project_id == project_id,
        MaterialConversation.scene_id == scene_id, MaterialConversation.owner_id == user.id).order_by(MaterialConversation.updated_at.desc())).all()
    return {"conversations": [materials.summary(value) for value in values]}


@router.post("", response_model=MaterialEnvelope, status_code=201)
def create(project_id: str, scene_id: str, body: MaterialCreate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"conversation": materials.view(session, materials.create(session, project_id, scene_id, user, body))}


@router.get("/{conversation_id}", response_model=MaterialEnvelope)
def detail(project_id: str, scene_id: str, conversation_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"conversation": materials.view(session, materials.require_material(session, project_id, scene_id, conversation_id, user.id))}


@router.post("/{conversation_id}/messages", response_model=MaterialEnvelope, status_code=202)
def send(project_id: str, scene_id: str, conversation_id: str, body: MaterialSend, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"conversation": materials.view(session, materials.send(session, project_id, scene_id, conversation_id, user, body, get_settings()))}


@router.post("/{conversation_id}/answers", response_model=MaterialEnvelope)
def answer(project_id: str, scene_id: str, conversation_id: str, body: MaterialAnswer, user: User = Depends(current_user), session: Session = Depends(get_db)):
    lock_write(session)
    conversation = materials.require_material(session, project_id, scene_id, conversation_id, user.id)
    payload = {"conversationId": conversation_id, **material_request_payload(body)}
    if not get_operation(session, user.id, project_id, body.operation_id, "materials.answer", payload):
        answer_review(session, conversation, body, get_settings())
        save_operation(session, user.id, project_id, body.operation_id, "materials.answer", payload, {"reviewId": body.review_id})
        session.commit()
    return {"conversation": materials.view(session, conversation)}


@router.get("/{conversation_id}/developer")
def developer(project_id: str, scene_id: str, conversation_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    require_text_admin(user, get_settings())
    conversation = materials.require_material(session, project_id, scene_id, conversation_id, user.id)
    from app.services.agent import run_view
    records = materials.message_records(session, conversation)
    run, _ = materials.active_production(session, conversation, records)
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all() if run else []
    return {"run": run_view(session, run) if run else None,
        "messages": [{"id": item.id, "runId": item.run_id, "source": materials.message_source(message_run),
            "content": item.content, "kind": (receipt or {}).get("kind", "production")} for item, message_run, receipt in records],
        "nodes": materials.canvas_nodes(session, project_id, scene_id, user.id),
        "imageJobs": [{"id": job.id, "accountId": job.account_id, "status": job.status,
            "request": job.request_json, "referenceAssetIds": job.reference_asset_ids,
            "dependencyIds": job.dependency_ids, "errorCode": job.error_code} for job in jobs]}
