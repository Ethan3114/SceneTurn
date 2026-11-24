import time

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import token_hash
from app.db import get_session
from app.models import Session as LoginSession
from app.models import User
from app.schemas import UserView
from app.services.auth_repository import CloudBaseAuthRepository
from app.services.cloudbase_database import gateway_enabled


get_db = get_session


def login_token_hash(request: Request) -> str:
    token = request.cookies.get("openfmv_session")
    if not token:
        raise AppError(401, "AUTH_REQUIRED", "请先登录")
    return token_hash(token)


def expired_session() -> AppError:
    return AppError(401, "SESSION_EXPIRED", "登录已失效，请重新登录")


def current_user(request: Request, session: Session = Depends(get_db)) -> UserView:
    token = login_token_hash(request)
    settings = get_settings()
    if gateway_enabled(settings):
        user = CloudBaseAuthRepository(settings).session_user(token)
        if user is None:
            raise expired_session()
        return user
    stored = session.get(LoginSession, token)
    if stored is None or stored.expires_at <= time.time():
        raise expired_session()
    user = session.get(User, stored.user_id)
    if user is None:
        raise AppError(401, "AUTH_REQUIRED", "请重新登录")
    return UserView(id=user.id, email=user.email)
