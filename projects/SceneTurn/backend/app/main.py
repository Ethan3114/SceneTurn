import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException

from app.api import accounts, agents, assets, connections, context, conversations, generations, materials, media_tools, payments, projects, text_generations, settings as provider_settings
from app.api import canvas_images, codex_images, deployment_dispatch, media_candidates, providers, publications, transfers
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.proxy import resolve_client_ip
from app.core.security import initialize_secret_store
from app.db import SessionLocal, initialize_database
from app.services.cloudbase_database import gateway_enabled
from app.services.generation import worker_loop
from app.services.text_generation import text_worker_loop
from app.services.agent import agent_worker_loop
from app.services.image_queue import image_worker_done, image_worker_loop, require_image_worker, wait_image_worker
from app.services.transfer_worker import HostedTransferWorker, transfer_worker_loop


logger = logging.getLogger("sceneturn")
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_secret_store(settings)
    initialize_database()
    stop_event = asyncio.Event()
    worker = asyncio.create_task(worker_loop(settings, SessionLocal, stop_event)) if settings.worker_enabled else None
    text_worker = asyncio.create_task(text_worker_loop(settings, SessionLocal, stop_event)) if (
        settings.worker_enabled and not gateway_enabled(settings)
    ) else None
    agent_worker = asyncio.create_task(agent_worker_loop(settings, SessionLocal, stop_event)) if settings.worker_enabled else None
    image_worker = asyncio.create_task(image_worker_loop(settings, SessionLocal, stop_event)) if settings.worker_enabled else None
    _app.state.image_worker = image_worker
    if image_worker is not None:
        image_worker.add_done_callback(image_worker_done)
    hosted_transfers = settings.transfer_queue_bucket is not None or settings.transfer_queue_write_token is not None
    _app.state.hosted_transfer_worker = HostedTransferWorker(settings, SessionLocal) if hosted_transfers else None
    transfer_worker = asyncio.create_task(transfer_worker_loop(settings, SessionLocal, stop_event)) if (
        settings.worker_enabled and not hosted_transfers
    ) else None
    yield
    stop_event.set()
    if worker is not None:
        await worker
    if text_worker is not None:
        await text_worker
    if agent_worker is not None:
        await agent_worker
    if image_worker is not None:
        await wait_image_worker(image_worker)
    if transfer_worker is not None:
        await transfer_worker


app = FastAPI(title="SceneTurn API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin], allow_credentials=True,
                   allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type", "Range"])


@app.middleware("http")
async def protect_origin(request: Request, call_next):
    request.state.client_ip = resolve_client_ip(request, settings)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        allowed = {settings.frontend_origin}
        if settings.deployment != "standalone":
            allowed.add(str(request.base_url).rstrip("/"))
        if origin is not None and origin not in allowed:
            return JSONResponse(status_code=403, content={"error": {"code": "ORIGIN_NOT_ALLOWED", "message": "请求来源未授权"}})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(AppError)
async def app_error(_request: Request, error: AppError):
    return JSONResponse(status_code=error.status, content={"error": {"code": error.code, "message": error.message}})


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, error: RequestValidationError):
    messages = [f"{'.'.join(str(part) for part in item['loc'])}：{item['msg']}" for item in error.errors()]
    return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR", "message": "；".join(messages)}})


@app.exception_handler(IntegrityError)
async def integrity_error(_request: Request, _error: IntegrityError):
    return JSONResponse(status_code=409, content={"error": {"code": "DATA_CONFLICT", "message": "数据已发生变化，请重新读取"}})


@app.exception_handler(HTTPException)
async def http_error(_request: Request, error: HTTPException):
    return JSONResponse(status_code=error.status_code, content={"error": {"code": "HTTP_ERROR", "message": "请求无法完成"}})


@app.exception_handler(Exception)
async def unexpected_error(_request: Request, error: Exception):
    logger.error("请求终止，错误类型：%s", type(error).__name__)
    return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": "请求处理失败，当前操作已终止"}})


@app.get("/api/v1/health")
def health(request: Request):
    require_image_worker(getattr(request.app.state, "image_worker", None))
    return {"status": "ok", "version": "0.1.0"}


for router in (accounts.router, projects.router, provider_settings.router, assets.router, payments.router, generations.router, media_tools.router, text_generations.router, agents.router, context.router, connections.router, conversations.router, materials.router, media_candidates.router, transfers.router):
    app.include_router(router, prefix="/api/v1")
app.include_router(publications.router, prefix="/api/v1")
app.include_router(providers.router, prefix="/api/v1")
app.include_router(codex_images.router, prefix="/api/v1")
app.include_router(canvas_images.router, prefix="/api/v1")
app.include_router(deployment_dispatch.router)
