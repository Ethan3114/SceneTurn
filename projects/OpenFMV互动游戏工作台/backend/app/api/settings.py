import time

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.core.security import encode_secret
from app.models import ProviderSettings, User
from app.schemas import WanSettingsInput, WanSettingsView


router = APIRouter(prefix="/settings", tags=["settings"])
PRICES = {"480P": 30, "720P": 60, "1080P": 120}


def settings_view(stored: ProviderSettings | None) -> dict:
    return {
        "configured": stored is not None,
        "workspaceId": stored.workspace_id if stored else None,
        "region": stored.region if stored else "cn-beijing",
        "model": stored.model if stored else get_settings().generation_model,
        "prices": PRICES,
    }


@router.get("/wan", response_model=WanSettingsView)
def get_wan_settings(user: User = Depends(current_user), session: Session = Depends(get_db)):
    return settings_view(session.get(ProviderSettings, user.id))


@router.put("/wan", response_model=WanSettingsView)
def put_wan_settings(body: WanSettingsInput, user: User = Depends(current_user), session: Session = Depends(get_db)):
    encrypted = encode_secret(body.api_key, get_settings())
    stored = session.get(ProviderSettings, user.id)
    if stored is None:
        stored = ProviderSettings(owner_id=user.id, api_key_encrypted=encrypted, workspace_id=body.workspace_id,
                                  region=body.region, model=body.model)
        session.add(stored)
    else:
        stored.api_key_encrypted = encrypted
        stored.workspace_id = body.workspace_id
        stored.region = body.region
        stored.model = body.model
        stored.updated_at = time.time()
    session.commit()
    return settings_view(stored)
