from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.core.errors import AppError
from app.provider_schemas import ModelProviderRegistryView, ModelProviderUpdate, ProjectModelDefaultsUpdate, ProjectModelDefaultsView, ProviderId
from app.schemas import UserView
from app.services.cloudbase_database import gateway_enabled
from app.services.model_provider_repository import CloudBaseModelProviderRepository
from app.services.providers import project_defaults, registry_view, set_project_defaults, update_provider
from app.services.projects import require_project


router = APIRouter(tags=["model-providers"])


@router.get("/settings/model-providers", response_model=ModelProviderRegistryView)
def list_model_providers(projectId: str | None = None, user: UserView = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseModelProviderRepository(settings).registry(user, projectId)
    return registry_view(session, user, settings, projectId)


@router.put("/settings/model-providers/{provider_id}", response_model=ModelProviderRegistryView)
def configure_model_provider(provider_id: ProviderId, body: ModelProviderUpdate, user: UserView = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseModelProviderRepository(settings).update(user, provider_id, body)
    return update_provider(session, user, provider_id, body, settings)


@router.get("/projects/{project_id}/model-defaults", response_model=ProjectModelDefaultsView)
def get_project_model_defaults(project_id: str, user: UserView = Depends(current_user), session: Session = Depends(get_db)):
    settings = get_settings()
    if gateway_enabled(settings):
        return CloudBaseModelProviderRepository(settings).project_defaults(user, project_id)
    project = require_project(session, project_id, user.id)
    return {"defaults": project_defaults(project), "revision": project.revision}


@router.put("/projects/{project_id}/model-defaults", response_model=ProjectModelDefaultsView)
def update_project_model_defaults(project_id: str, body: ProjectModelDefaultsUpdate, user: UserView = Depends(current_user), session: Session = Depends(get_db)):
    if gateway_enabled(get_settings()):
        raise AppError(503, "MODEL_DEFAULTS_GATEWAY_UNAVAILABLE", "项目默认模型的保存服务尚未连接")
    return set_project_defaults(session, project_id, user, body)
