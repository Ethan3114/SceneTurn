from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.models import User
from app.provider_schemas import ModelProviderRegistryView, ModelProviderUpdate, ProjectModelDefaultsUpdate, ProjectModelDefaultsView, ProviderId
from app.services.providers import project_defaults, registry_view, set_project_defaults, update_provider
from app.services.projects import require_project


router = APIRouter(tags=["model-providers"])


@router.get("/settings/model-providers", response_model=ModelProviderRegistryView)
def list_model_providers(projectId: str | None = None, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return registry_view(session, user, get_settings(), projectId)


@router.put("/settings/model-providers/{provider_id}", response_model=ModelProviderRegistryView)
def configure_model_provider(provider_id: ProviderId, body: ModelProviderUpdate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return update_provider(session, user, provider_id, body, get_settings())


@router.get("/projects/{project_id}/model-defaults", response_model=ProjectModelDefaultsView)
def get_project_model_defaults(project_id: str, user: User = Depends(current_user), session: Session = Depends(get_db)):
    project = require_project(session, project_id, user.id)
    return {"defaults": project_defaults(project), "revision": project.revision}


@router.put("/projects/{project_id}/model-defaults", response_model=ProjectModelDefaultsView)
def update_project_model_defaults(project_id: str, body: ProjectModelDefaultsUpdate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return set_project_defaults(session, project_id, user, body)
