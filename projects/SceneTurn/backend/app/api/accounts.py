import time
from math import ceil

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.deps import LEGACY_SESSION_COOKIE, SESSION_COOKIE, current_user, expired_session, get_db, login_token_hash
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.security import hash_password, new_token, token_hash, verify_password
from app.models import LoginFailure
from app.models import Session as LoginSession
from app.models import User, new_id
from app.schemas import Credentials, OkResult, UserResult, UserView
from app.services.auth_repository import AcceptedLogin, CloudBaseAuthRepository, LockedLogin
from app.services.cloudbase_database import gateway_enabled
from app.services.projects import lock_write


router = APIRouter(prefix="/auth", tags=["accounts"])
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCK_SECONDS = 15 * 60


def issue_session(session: Session, response: Response, user_id: str) -> None:
    settings = get_settings()
    token = new_token()
    session.add(LoginSession(token_hash=token_hash(token), user_id=user_id, expires_at=time.time() + settings.session_seconds))
    set_session_cookie(response, token, settings)


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(SESSION_COOKIE, token, max_age=settings.session_seconds, httponly=True,
                        secure=settings.cookie_secure, samesite="strict", path="/")


def login_locked(seconds: float) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": "LOGIN_RATE_LIMITED", "message": "登录失败次数过多，请稍后重试"}},
        status_code=429,
        headers={"Retry-After": str(ceil(seconds)), "Cache-Control": "no-store"},
    )


@router.post("/register", response_model=UserResult, status_code=201)
def register(body: Credentials, response: Response, session: Session = Depends(get_db)):
    email = str(body.email).lower()
    settings = get_settings()
    if gateway_enabled(settings):
        token = new_token()
        user = CloudBaseAuthRepository(settings).register(new_id(), email, hash_password(body.password), token_hash(token))
        if user is None:
            raise AppError(409, "EMAIL_EXISTS", "该邮箱已注册，请登录")
        set_session_cookie(response, token, settings)
        return UserResult(user=user)
    if session.scalar(select(User.id).where(User.email == email)):
        raise AppError(409, "EMAIL_EXISTS", "该邮箱已注册，请登录")
    user = User(email=email, password_hash=hash_password(body.password))
    session.add(user)
    session.flush()
    issue_session(session, response, user.id)
    session.commit()
    return {"user": {"id": user.id, "email": user.email}}


@router.post("/login", response_model=UserResult)
def login(body: Credentials, request: Request, response: Response, session: Session = Depends(get_db)):
    source = token_hash(request.state.client_ip)
    email = str(body.email).lower()
    settings = get_settings()
    if gateway_enabled(settings):
        token = new_token()
        result = CloudBaseAuthRepository(settings).login(email, body.password, source, token_hash(token))
        if isinstance(result, LockedLogin):
            return login_locked(result.retry_after)
        if isinstance(result, AcceptedLogin):
            set_session_cookie(response, token, settings)
            return UserResult(user=result.user)
        raise AppError(401, "INVALID_CREDENTIALS", "邮箱或密码错误")
    now = time.time()
    locked_until = session.scalar(select(LoginFailure.expires_at).where(
        LoginFailure.source_hash == source, LoginFailure.failure_count >= LOGIN_FAILURE_LIMIT, LoginFailure.expires_at > now
    ))
    if locked_until is not None:
        return login_locked(locked_until - now)
    account = session.execute(
        select(User.id, User.email, User.password_hash).where(User.email == email)
    ).one_or_none()
    session.rollback()
    if account is not None and not verify_password(body.password, account.password_hash):
        account = None

    lock_write(session)
    now = time.time()
    session.execute(delete(LoginFailure).where(LoginFailure.expires_at <= now))
    failures = session.get(LoginFailure, source)
    if failures is not None and failures.failure_count >= LOGIN_FAILURE_LIMIT:
        retry_after = failures.expires_at - now
        session.commit()
        return login_locked(retry_after)
    if account is not None:
        if failures is not None:
            session.delete(failures)
        issue_session(session, response, account.id)
        session.commit()
        return {"user": {"id": account.id, "email": account.email}}

    count = failures.failure_count + 1 if failures is not None else 1
    if failures is None:
        session.add(LoginFailure(source_hash=source, failure_count=count, expires_at=now + LOGIN_LOCK_SECONDS))
    else:
        failures.failure_count = count
        failures.expires_at = now + LOGIN_LOCK_SECONDS
    session.commit()
    if count >= LOGIN_FAILURE_LIMIT:
        return login_locked(LOGIN_LOCK_SECONDS)
    raise AppError(401, "INVALID_CREDENTIALS", "邮箱或密码错误")


@router.get("/me", response_model=UserResult)
def me(user: UserView = Depends(current_user)):
    return UserResult(user=user)


@router.post("/logout", response_model=OkResult)
def logout(request: Request, response: Response, session: Session = Depends(get_db)):
    token = login_token_hash(request)
    settings = get_settings()
    if gateway_enabled(settings):
        if not CloudBaseAuthRepository(settings).logout(token):
            raise expired_session()
    else:
        current_user(request, session)
        stored = session.get(LoginSession, token)
        if stored is not None:
            session.delete(stored)
            session.commit()
    for cookie in (SESSION_COOKIE, LEGACY_SESSION_COOKIE):
        response.delete_cookie(cookie, path="/", httponly=True, secure=settings.cookie_secure, samesite="strict")
    return {"ok": True}
