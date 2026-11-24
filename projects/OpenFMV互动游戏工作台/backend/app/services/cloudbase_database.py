import re

import httpx

from app.core.config import Settings
from app.core.errors import AppError


RPC_NAME = re.compile(r"sceneturn_[a-z][a-z0-9_]{1,51}", re.ASCII)
RPC_ERRORS = {
    ("PT401", "AUTH_REQUIRED"): (401, "AUTH_REQUIRED", "请先登录"),
    ("PT402", "TEXT_BILLING_NOT_CONFIGURED"): (402, "TEXT_BILLING_NOT_CONFIGURED", "文字生成额度尚未配置"),
    ("PT402", "BUDGET_EXCEEDED"): (402, "BUDGET_EXCEEDED", "可用额度不足"),
    ("PT403", "TEXT_SETTINGS_FORBIDDEN"): (403, "TEXT_SETTINGS_FORBIDDEN", "没有权限修改文字模型设置"),
    ("PT404", "PROJECT_NOT_FOUND"): (404, "PROJECT_NOT_FOUND", "项目不存在"),
    ("PT404", "SCENE_NOT_FOUND"): (404, "SCENE_NOT_FOUND", "目标剧情场景不存在"),
    ("PT404", "CANVAS_NOT_FOUND"): (404, "CANVAS_NOT_FOUND", "场景画布不存在"),
    ("PT404", "CONVERSATION_NOT_FOUND"): (404, "CONVERSATION_NOT_FOUND", "项目对话不存在或无权访问"),
    ("PT404", "ASSET_NOT_FOUND"): (404, "ASSET_NOT_FOUND", "素材不存在或无权访问"),
    ("PT404", "MODEL_OWNER_NOT_FOUND"): (404, "MODEL_OWNER_NOT_FOUND", "模型配置所属账号不存在"),
    ("PT404", "TRANSFER_NOT_FOUND"): (404, "TRANSFER_NOT_FOUND", "传输任务不存在"),
    ("PT404", "UPLOAD_NOT_FOUND"): (404, "UPLOAD_NOT_FOUND", "上传任务不存在"),
    ("PT404", "TEXT_GENERATION_NOT_FOUND"): (404, "TEXT_GENERATION_NOT_FOUND", "文字生成任务不存在"),
    ("PT404", "GENERATION_NOT_FOUND"): (404, "GENERATION_NOT_FOUND", "视频生成任务不存在"),
    ("PT404", "PUBLICATION_NOT_FOUND"): (404, "PUBLICATION_NOT_FOUND", "作品暂不可访问"),
    ("PT404", "PUBLICATION_MEDIA_NOT_FOUND"): (404, "PUBLICATION_MEDIA_NOT_FOUND", "作品媒体不存在或不属于该项目"),
    ("PT404", "GAME_VERSION_NOT_FOUND"): (404, "GAME_VERSION_NOT_FOUND", "固定游戏版本不可用"),
    ("PT404", "GAME_FILE_NOT_FOUND"): (404, "GAME_FILE_NOT_FOUND", "该游戏版本缺少所需文件"),
    ("PT409", "REVISION_CONFLICT"): (409, "REVISION_CONFLICT", "项目已经更新，请重新读取"),
    ("PT409", "PROJECT_REVISION_CONFLICT"): (409, "PROJECT_REVISION_CONFLICT", "项目已经更新，请重新读取"),
    ("PT409", "OPERATION_CONFLICT"): (409, "OPERATION_CONFLICT", "该操作标识已用于不同内容"),
    ("PT409", "CONVERSATION_BUSY"): (409, "CONVERSATION_BUSY", "当前消息仍在处理，请停止或恢复当前操作后继续"),
    ("PT409", "MODEL_CONFIGURATION_CONFLICT"): (409, "MODEL_CONFIGURATION_CONFLICT", "模型设置已更新，请重新读取"),
    ("PT409", "TRANSFER_DISPATCH_EXPIRED"): (409, "TRANSFER_DISPATCH_EXPIRED", "传输任务已更新，请重新读取"),
    ("PT409", "TRANSFER_DISPATCH_REQUIRED"): (409, "TRANSFER_DISPATCH_REQUIRED", "缺少传输调度记录"),
    ("PT409", "TRANSFER_CONFIRMATION_CHANGED"): (409, "TRANSFER_CONFIRMATION_CHANGED", "传输确认内容已变化"),
    ("PT409", "TRANSFER_NOT_CONFIRMABLE"): (409, "TRANSFER_NOT_CONFIRMABLE", "当前传输任务无法确认"),
    ("PT409", "TRANSFER_RETRY_NOT_ALLOWED"): (409, "TRANSFER_RETRY_NOT_ALLOWED", "当前传输任务无法重试"),
    ("PT409", "TRANSFER_WORKER_UNAVAILABLE"): (409, "TRANSFER_WORKER_UNAVAILABLE", "当前传输类型暂未接入后台处理"),
    ("PT409", "UPLOAD_SESSION_CHANGED"): (409, "UPLOAD_SESSION_CHANGED", "请在发起上传的会话中继续"),
    ("PT409", "UPLOAD_EXPIRED"): (409, "UPLOAD_EXPIRED", "上传授权已过期"),
    ("PT409", "IMPORT_UPLOAD_IN_PROGRESS"): (409, "IMPORT_UPLOAD_IN_PROGRESS", "同一项目包已在上传中"),
    ("PT409", "TEXT_INPUT_CHANGED"): (409, "TEXT_INPUT_CHANGED", "文字生成所用的内容已更新"),
    ("PT409", "TEXT_DISPATCH_REQUIRED"): (409, "TEXT_DISPATCH_REQUIRED", "缺少文字任务调度记录"),
    ("PT409", "TEXT_COST_OUT_OF_BOUNDS"): (409, "TEXT_COST_OUT_OF_BOUNDS", "模型用量超出本次授权范围"),
    ("PT409", "TEXT_BUDGET_INCONSISTENT"): (409, "TEXT_BUDGET_INCONSISTENT", "文字生成额度需要核实"),
    ("PT409", "TEXT_NOT_READY"): (409, "TEXT_NOT_READY", "文字结果尚未就绪"),
    ("PT409", "TEXT_OUTPUT_MISSING"): (409, "TEXT_OUTPUT_MISSING", "文字结果的目标节点已不存在"),
    ("PT409", "GAME_FILE_PROOF_MISSING"): (409, "GAME_FILE_PROOF_MISSING", "该版本缺少播放文件的完整性记录，请重新导出"),
    ("PT409", "GAME_FILE_CHANGED"): (409, "GAME_FILE_CHANGED", "该版本播放文件与固定内容不一致"),
    ("PT422", "PROJECT_ID_MISMATCH"): (422, "PROJECT_ID_MISMATCH", "项目标识与请求不一致"),
    ("PT422", "ASSET_NOT_REGISTERED"): (422, "ASSET_NOT_REGISTERED", "项目包含未登记的素材"),
    ("PT422", "ASSET_REGISTRATION_INVALID"): (422, "ASSET_REGISTRATION_INVALID", "素材登记内容无效"),
    ("PT422", "MODEL_PROVIDER_UNSUPPORTED"): (422, "MODEL_PROVIDER_UNSUPPORTED", "当前模型服务类型未开放"),
    ("PT422", "MODEL_SETTINGS_INVALID"): (422, "MODEL_SETTINGS_INVALID", "模型设置格式无效"),
    ("PT422", "MODEL_WORKSPACE_REQUIRED"): (422, "MODEL_WORKSPACE_REQUIRED", "需要填写模型业务空间"),
    ("PT422", "MODEL_KEY_REQUIRED"): (422, "MODEL_KEY_REQUIRED", "需要填写模型服务凭据"),
    ("PT422", "PROJECT_REQUEST_INVALID"): (422, "PROJECT_REQUEST_INVALID", "项目数据格式无效"),
    ("PT422", "SCENE_REQUIRED"): (422, "SCENE_REQUIRED", "请先在剧情编排中创建一个场景"),
    ("PT422", "SCENE_NOT_FOUND"): (422, "SCENE_NOT_FOUND", "当前场景不属于此项目"),
    ("PT422", "CHAT_CONTEXT_INVALID"): (422, "CHAT_CONTEXT_INVALID", "选中对象已经变化，请读取当前项目后重试"),
    ("PT422", "CANVAS_REQUEST_INVALID"): (422, "CANVAS_REQUEST_INVALID", "画布内容无效，请检查节点及连线"),
    ("PT422", "TRANSFER_REQUEST_INVALID"): (422, "TRANSFER_REQUEST_INVALID", "传输请求无效"),
    ("PT422", "TRANSFER_ARTIFACT_INVALID"): (422, "TRANSFER_ARTIFACT_INVALID", "传输产物无效"),
    ("PT422", "TRANSFER_FAILURE_INVALID"): (422, "TRANSFER_FAILURE_INVALID", "传输失败记录无效"),
    ("PT422", "UPLOAD_REQUEST_INVALID"): (422, "UPLOAD_REQUEST_INVALID", "上传请求无效"),
    ("PT422", "TEXT_KEY_INVALID"): (422, "TEXT_KEY_INVALID", "文字模型凭据格式无效"),
    ("PT422", "TEXT_REQUEST_INVALID"): (422, "TEXT_REQUEST_INVALID", "文字生成请求无效"),
    ("PT422", "TEXT_OUTPUT_LIMIT"): (422, "TEXT_OUTPUT_LIMIT", "文字输出超过允许长度"),
    ("PT422", "TEXT_COST_LIMIT"): (422, "TEXT_COST_LIMIT", "文字生成超过单次费用上限"),
    ("PT422", "TEXT_RESULT_INVALID"): (422, "TEXT_RESULT_INVALID", "文字生成结果无法校验"),
    ("PT422", "TEXT_USAGE_INVALID"): (422, "TEXT_USAGE_INVALID", "文字模型用量无法校验"),
    ("PT429", "TOO_MANY_PENDING_UPLOADS"): (429, "TOO_MANY_PENDING_UPLOADS", "待处理的上传过多，请稍后重试"),
    ("PT503", "TEXT_NOT_CONFIGURED"): (503, "TEXT_NOT_CONFIGURED", "文字模型尚未配置"),
    ("PT422", "PUBLICATION_TITLE_REQUIRED"): (422, "PUBLICATION_TITLE_REQUIRED", "请填写作品名称"),
}


def gateway_enabled(settings: Settings) -> bool:
    return settings.cloudbase_gateway_enabled


def call_cloudbase_rpc(settings: Settings, name: str, parameters: dict):
    if not RPC_NAME.fullmatch(name):
        raise ValueError("数据库函数名称无效")
    if settings.cloudbase_env_id is None or settings.cloudbase_api_key is None or settings.cloudbase_gateway_scope_token is None:
        raise AppError(503, "DATABASE_GATEWAY_UNAVAILABLE", "数据库网关尚未配置")

    endpoint = f"https://{settings.cloudbase_env_id}.api.tcloudbasegateway.com/v1/rdb/rest/rpc/{name}"
    headers = {
        "Authorization": f"Bearer {settings.cloudbase_api_key.get_secret_value()}",
        "X-SceneTurn-Database-Scope": settings.cloudbase_gateway_scope_token.get_secret_value(),
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=settings.request_timeout, trust_env=False, follow_redirects=False) as client:
            response = client.post(endpoint, json=parameters, headers=headers)
    except httpx.TimeoutException:
        raise AppError(504, "DATABASE_GATEWAY_TIMEOUT", "数据库响应超时，请先核实操作结果") from None
    except httpx.HTTPError:
        raise AppError(502, "DATABASE_GATEWAY_UNAVAILABLE", "无法连接数据库网关") from None

    try:
        value = response.json()
    except ValueError:
        raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "数据库网关返回内容无效") from None

    if response.status_code == 200:
        return value
    if isinstance(value, dict):
        code, message = value.get("code"), value.get("message")
        if isinstance(code, str) and isinstance(message, str):
            operation_error = RPC_ERRORS.get((code.removeprefix("DATABASE_"), message))
            if operation_error is not None and response.status_code == operation_error[0]:
                raise AppError(*operation_error)
    raise AppError(502, "DATABASE_GATEWAY_REJECTED", "数据库网关未完成操作")
