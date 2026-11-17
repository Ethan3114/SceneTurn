import time

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import hash_password, new_token, token_hash, verify_password
from app.models import Session as LoginSession
from app.models import User
from app.schemas import Credentials, OkResult, UserResult


router = APIRouter(prefix="/auth", tags=["accounts"])


def issue_session(session: Session, response: Response, user: User) -> None:
    settings = get_settings()
    token = new_token()
    session.add(LoginSession(token_hash=token_hash(token), user_id=user.id, expires_at=time.time() + settings.session_seconds))
    response.set_cookie("openfmv_session", token, max_age=settings.session_seconds, httponly=True,
                        secure=settings.cookie_secure, samesite="strict", path="/")


@router.post("/register", response_model=UserResult, status_code=201)
def register(body: Credentials, response: Response, session: Session = Depends(get_db)):
    email = str(body.email).lower()
    if session.scalar(select(User.id).where(User.email == email)):
        raise AppError(409, "EMAIL_EXISTS", "该邮箱已注册，请登录")
    user = User(email=email, password_hash=hash_password(body.password))
    session.add(user)
    session.flush()
    issue_session(session, response, user)
    session.commit()
    return {"user": {"id": user.id, "email": user.email}}


@router.post("/login", response_model=UserResult)
def login(body: Credentials, response: Response, session: Session = Depends(get_db)):
    user = session.scalar(select(User).where(User.email == str(body.email).lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise AppError(401, "INVALID_CREDENTIALS", "邮箱或密码错误")
    issue_session(session, response, user)
    session.commit()
    return {"user": {"id": user.id, "email": user.email}}


@router.get("/me", response_model=UserResult)
def me(user: User = Depends(current_user)):
    return {"user": {"id": user.id, "email": user.email}}


@router.post("/logout", response_model=OkResult)
def logout(request: Request, response: Response, user: User = Depends(current_user), session: Session = Depends(get_db)):
    token = request.cookies["openfmv_session"]
    stored = session.get(LoginSession, token_hash(token))
    if stored is not None:
        session.delete(stored)
        session.commit()
    response.delete_cookie("openfmv_session", path="/", httponly=True, samesite="strict")
    return {"ok": True}
