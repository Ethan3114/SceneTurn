import time

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.security import token_hash
from app.db import get_session
from app.models import Session as LoginSession
from app.models import User


get_db = get_session


def current_user(request: Request, session: Session = Depends(get_db)) -> User:
    token = request.cookies.get("openfmv_session")
    if not token:
        raise AppError(401, "AUTH_REQUIRED", "请先登录")
    stored = session.get(LoginSession, token_hash(token))
    if stored is None or stored.expires_at <= time.time():
        raise AppError(401, "SESSION_EXPIRED", "登录已失效，请重新登录")
    user = session.get(User, stored.user_id)
    if user is None:
        raise AppError(401, "AUTH_REQUIRED", "请重新登录")
    return user
