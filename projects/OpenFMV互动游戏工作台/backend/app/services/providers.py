import copy
import hashlib
import time

from sqlalchemy import select

from app.core.errors import AppError
from app.core.security import decode_secret, encode_secret
from app.image_models import ImageAccount
from app.models import ProviderSettings, TextProviderSettings
from app.provider_models import ModelProviderConnection
from app.provider_schemas import ProjectModelDefaults
from app.services.connections import active_image_connection, require_image_connection
from app.services.projects import get_operation, lock_write, record_project_version, require_project, require_revision, save_operation
from app.services.wan import MAX_REFERENCE_IMAGES, VIDEO_INPUT_MODES


ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
LEGACY_DEFAULTS = {"text": "deepseek-flash", "image": "sensetime-fast", "video": "wan-3-0"}
PROVIDER_IDS = ("deepseek", "aliyun-qwen", "sensetime", "volcengine-ark", "aliyun-wan")
VIDEO_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive")
SEEDREAM_PIXEL_RANGE = (921600, 4624220)


def _is_text_admin(user, settings):
    return bool(settings.text_admin_email) and user.email.casefold() == settings.text_admin_email.strip().casefold()


def _profile(identifier, provider, label, kind, model, **capabilities):
    return {"id": identifier, "providerId": provider, "label": label, "kind": kind, "model": model, "capabilities": capabilities}


MODEL_PROFILES = {
    item["id"]: item for item in [
        _profile("deepseek-flash", "deepseek", "DeepSeek Flash", "text", "deepseek-flash", structuredOutput=True, maxOutputTokens=32768),
        _profile("qwen-flash", "aliyun-qwen", "Qwen3.8 Flash", "text", "qwen3.8-flash", structuredOutput=True, maxOutputTokens=32768, maxReferences=5),
        _profile("sensetime-fast", "sensetime", "商汤 Fast", "image", "sensenova-u1.5-fast", maxReferences=5, minDimension=512, maxDimension=4096),
        _profile("seedream-5-5", "volcengine-ark", "Seedream 5.5", "image", "doubao-seedream-5-5-261028", maxReferences=5, minDimension=512, maxDimension=4096,
            minPixels=SEEDREAM_PIXEL_RANGE[0], maxPixels=SEEDREAM_PIXEL_RANGE[1]),
        _profile("codex-image", "codex-local", "个人 Codex", "image", "gpt-image-2", maxReferences=5, localConnection=True),
        _profile("wan-3-0", "aliyun-wan", "Wan 3.0", "video", "wan3.0-video", firstFrame=True, lastFrame=True, audio=True, minDuration=2, maxDuration=30, resolutions=["480P", "720P", "1080P"],
            ratios=list(VIDEO_RATIOS), videoInputModes=list(VIDEO_INPUT_MODES), maxReferenceImages=MAX_REFERENCE_IMAGES),
        *[_profile(identifier, "volcengine-ark", label, "video", model, firstFrame=True, lastFrame=last, audio=audio,
            ratios=list(VIDEO_RATIOS), **({"fixedFrameRatioMode": "adaptive"} if identifier.startswith("seedance-2-5") else {}),
            minDuration=2 if "1-0" in identifier else 4, maxDuration=30 if "2-5" in identifier else 12 if "1-0" in identifier else 15, resolutions=["480P", "720P"] if identifier in {"seedance-2-5-flash", "seedance-2-0-fast", "seedance-2-0-mini"} else ["480P", "720P", "1080P"], requiresEntitlement=True)
          for identifier, label, model, last, audio in [
            ("seedance-2-5", "Seedance 2.5", "doubao-seedance-2-5-260628", True, True),
            ("seedance-2-5-flash", "Seedance 2.5 Flash", "doubao-seedance-2-5-flash-261115", True, True),
            ("seedance-2-0", "Seedance 2.0", "doubao-seedance-2-0-260128", True, True),
            ("seedance-2-0-fast", "Seedance 2.0 Fast", "doubao-seedance-2-0-fast-260128", True, True),
            ("seedance-2-0-mini", "Seedance 2.0 Mini", "doubao-seedance-2-0-mini-260615", True, True),
            ("seedance-1-0-pro", "Seedance 1.0 Pro", "doubao-seedance-1-0-pro-250528", True, False),
            ("seedance-1-0-pro-fast", "Seedance 1.0 Pro Fast", "doubao-seedance-1-0-pro-fast-251015", False, False),
          ]],
    ]
}


def profile(identifier, kind=None):
    value = MODEL_PROFILES.get(identifier)
    if value is None or kind is not None and value["kind"] != kind:
        raise AppError(422, "MODEL_PROFILE_UNSUPPORTED", "所选模型不属于当前生成功能")
    return copy.deepcopy(value)


def connection(session, owner_id, provider_id):
    scope = "platform" if provider_id in {"deepseek", "aliyun-qwen"} else owner_id
    return session.scalar(select(ModelProviderConnection).where(ModelProviderConnection.scope_key == scope, ModelProviderConnection.provider_id == provider_id))


def project_defaults(project):
    values = project.data_json.get("metadata", {}).get("modelDefaults", {})
    return {**LEGACY_DEFAULTS, **ProjectModelDefaults.model_validate({**LEGACY_DEFAULTS, **values}).model_dump()}


def _configured(session, owner_id, provider_id, stored):
    if provider_id == "deepseek":
        legacy = session.get(TextProviderSettings, "platform")
        return bool(legacy and legacy.api_key_encrypted)
    if provider_id == "aliyun-wan":
        legacy = session.get(ProviderSettings, owner_id)
        return bool(legacy and legacy.api_key_encrypted)
    if provider_id == "sensetime":
        return len(session.scalars(select(ImageAccount).where(ImageAccount.owner_id == owner_id)).all()) == 3
    return bool(stored and stored.api_key_encrypted)


def registry_view(session, user, settings, project_id=None):
    project = require_project(session, project_id, user.id) if project_id else None
    connections = []
    records = {}
    for provider_id in PROVIDER_IDS:
        stored = connection(session, user.id, provider_id)
        records[provider_id] = stored
        config = stored.config_json if stored else {}
        legacy_wan = session.get(ProviderSettings, user.id) if provider_id == "aliyun-wan" else None
        can_manage = provider_id not in {"deepseek", "aliyun-qwen"} or _is_text_admin(user, settings)
        connections.append({"providerId": provider_id, "configured": _configured(session, user.id, provider_id, stored),
            "revision": stored.revision if stored else 0, "canManage": can_manage,
            "region": config.get("region", "cn-beijing"), "workspaceId": config.get("workspaceId", legacy_wan.workspace_id if legacy_wan else None),
            "entitledModelIds": config.get("entitledModelIds", []), "prices": config.get("prices", {}) if can_manage else {}})
    models = []
    for item in MODEL_PROFILES.values():
        if item["providerId"] == "codex-local":
            enabled = project is not None and active_image_connection(session, user.id, project.id) is not None
            models.append({**copy.deepcopy(item), "configured": enabled, "available": enabled,
                "unavailableReason": None if enabled else "请在「问问 AI」连接支持图片生成的个人 Codex", "requiresEntitlement": False})
            continue
        stored = records[item["providerId"]]
        config = stored.config_json if stored else {}
        entitled = not item["capabilities"].get("requiresEntitlement") or item["id"] in config.get("entitledModelIds", [])
        configured = _configured(session, user.id, item["providerId"], stored)
        priced = item["providerId"] in {"deepseek", "sensetime", "aliyun-wan"} or item["id"] in config.get("prices", {})
        reason = "请先配置供应商凭据" if not configured else "请核对并保存当前模型开通权益" if not entitled else "请核对并保存当前模型价表" if not priced else None
        if item["kind"] == "text" and not _is_text_admin(user, settings):
            reason = "平台文字额度尚未开放，当前仅管理员可使用"
        models.append({**copy.deepcopy(item), "configured": configured, "available": reason is None,
            "unavailableReason": reason, "requiresEntitlement": bool(item["capabilities"].get("requiresEntitlement"))})
    return {"models": models, "connections": connections, "defaults": project_defaults(project) if project else dict(LEGACY_DEFAULTS), "revision": project.revision if project else None}


def update_provider(session, user, provider_id, body, settings):
    if provider_id not in PROVIDER_IDS:
        raise AppError(422, "MODEL_PROVIDER_UNSUPPORTED", "供应商未获支持")
    if provider_id in {"deepseek", "aliyun-qwen"} and not _is_text_admin(user, settings):
        raise AppError(403, "TEXT_SETTINGS_FORBIDDEN", "只有平台管理员能够配置文字供应商")
    if body.api_keys is not None and provider_id != "sensetime" or body.api_key is not None and provider_id == "sensetime":
        raise AppError(422, "MODEL_CREDENTIAL_FIELDS_INVALID", "请使用当前供应商对应的凭据输入字段")
    if body.base_url is not None and body.base_url != (ARK_BASE_URL if provider_id == "volcengine-ark" else "https://api.deepseek.com" if provider_id == "deepseek" else "https://token.sensenova.cn/v1" if provider_id == "sensetime" else ""):
        raise AppError(422, "MODEL_ENDPOINT_FORBIDDEN", "请使用该供应商固定的官方地域地址")
    lock_write(session)
    stored = connection(session, user.id, provider_id)
    if body.expected_revision != (stored.revision if stored else 0):
        raise AppError(409, "MODEL_CONFIGURATION_CONFLICT", "供应商配置已有新版本，请重新读取")
    if stored is None:
        stored = ModelProviderConnection(owner_id=user.id, scope_key="platform" if provider_id in {"deepseek", "aliyun-qwen"} else user.id,
            provider_id=provider_id, config_json={}, revision=0)
        session.add(stored)
    secret = body.api_key.get_secret_value().strip() if body.api_key else None
    if secret is not None and (not 8 <= len(secret) <= 2048 or any(char.isspace() for char in secret)):
        raise AppError(422, "MODEL_KEY_INVALID", "请填写完整的供应商API Key")
    config = {**stored.config_json, "region": body.region}
    if body.workspace_id is not None:
        config["workspaceId"] = body.workspace_id
    if body.entitled_model_ids is not None:
        if len(set(body.entitled_model_ids)) != len(body.entitled_model_ids) or any(profile(item)["providerId"] != provider_id for item in body.entitled_model_ids):
            raise AppError(422, "MODEL_ENTITLEMENT_INVALID", "权益清单必须属于当前供应商且不可重复")
        config["entitledModelIds"] = body.entitled_model_ids
    if body.prices is not None:
        for identifier, price in body.prices.items():
            item = profile(identifier)
            if item["providerId"] != provider_id:
                raise AppError(422, "MODEL_PRICE_INVALID", "价表模型不属于当前供应商")
            if item["kind"] == "text" and any(value is None for value in (price.cache_hit_micros_per_million, price.cache_miss_micros_per_million, price.output_micros_per_million)) or item["kind"] == "image" and price.micros_per_image is None or item["kind"] == "video" and price.cents_per_second is None:
                raise AppError(422, "MODEL_PRICE_INVALID", "价表缺少当前模型的计费单位")
            if item["kind"] == "video" and provider_id == "volcengine-ark" and price.output_micros_per_million is None:
                raise AppError(422, "MODEL_PRICE_INVALID", "方舟视频需要输出Token单价及每秒报价上界")
            if item["kind"] == "text" and price.cache_hit_micros_per_million > price.cache_miss_micros_per_million:
                raise AppError(422, "MODEL_PRICE_INVALID", "缓存命中单价不得高于未命中单价")
        config["prices"] = {identifier: price.model_dump(by_alias=True, exclude_none=True) for identifier, price in body.prices.items()}
    if provider_id == "aliyun-qwen" and not config.get("workspaceId"):
        raise AppError(422, "MODEL_WORKSPACE_REQUIRED", "千问需要北京地域业务空间ID")
    if secret:
        stored.api_key_encrypted = encode_secret(secret, settings)
    if provider_id == "deepseek" and secret:
        legacy = session.get(TextProviderSettings, "platform")
        if legacy is None:
            legacy = TextProviderSettings(id="platform", admin_owner_id=user.id)
            session.add(legacy)
        legacy.api_key_encrypted, legacy.updated_at = stored.api_key_encrypted, time.time()
    if provider_id == "aliyun-wan" and (secret or body.workspace_id is not None):
        if not config.get("workspaceId"):
            raise AppError(422, "MODEL_WORKSPACE_REQUIRED", "视频生成需要北京地域业务空间ID")
        legacy = session.get(ProviderSettings, user.id)
        if legacy is None:
            if not secret:
                raise AppError(422, "MODEL_KEY_REQUIRED", "首次配置视频生成需要完整API Key")
            legacy = ProviderSettings(owner_id=user.id, api_key_encrypted=stored.api_key_encrypted, workspace_id=config["workspaceId"])
            session.add(legacy)
        if secret:
            legacy.api_key_encrypted = stored.api_key_encrypted
        legacy.workspace_id, legacy.region, legacy.updated_at = config["workspaceId"], body.region, time.time()
    if provider_id == "sensetime" and body.api_keys is not None:
        keys = [item.get_secret_value().strip() for item in body.api_keys]
        if len(set(keys)) != 3 or any(not 8 <= len(key) <= 2048 or any(char.isspace() for char in key) for key in keys):
            raise AppError(422, "MODEL_KEY_INVALID", "请填写三个不同的完整商汤账号凭据")
        accounts = session.scalars(select(ImageAccount).where(ImageAccount.owner_id == user.id)).all()
        if any(account.active_job_id for account in accounts):
            raise AppError(409, "IMAGE_ACCOUNT_BUSY", "图片账号仍有任务，请先处理原任务")
        for slot, key in enumerate(keys, 1):
            account = next((item for item in accounts if item.slot == slot), None)
            if account is None:
                account = ImageAccount(owner_id=user.id, slot=slot)
                session.add(account)
            account.api_key_encrypted, account.credential_hash, account.status = encode_secret(key, settings), hashlib.sha256(key.encode()).hexdigest(), "available"
    stored.config_json, stored.revision, stored.updated_at = config, stored.revision + 1, time.time()
    session.commit()
    return registry_view(session, user, settings)


def set_project_defaults(session, project_id, user, body):
    lock_write(session)
    project = require_project(session, project_id, user.id)
    payload = body.model_dump(by_alias=True)
    replay = get_operation(session, user.id, project_id, body.operation_id, "models.defaults", payload)
    if replay is not None:
        return {"defaults": replay["defaults"], "revision": replay["revision"]}
    require_revision(project, body.expected_revision)
    values = {kind: getattr(body, kind) for kind in LEGACY_DEFAULTS}
    previous = project_defaults(project)
    for kind, identifier in values.items():
        item = profile(identifier, kind)
        if identifier == "codex-image" and previous[kind] != identifier:
            require_image_connection(session, user.id, project_id)
        if item["capabilities"].get("requiresEntitlement"):
            stored = connection(session, user.id, item["providerId"])
            if stored is None or identifier not in stored.config_json.get("entitledModelIds", []):
                raise AppError(409, "MODEL_ENTITLEMENT_REQUIRED", "请先核对当前账号的模型开通权益")
    snapshot = copy.deepcopy(project.data_json)
    snapshot["metadata"] = {**snapshot["metadata"], "modelDefaults": values}
    project.data_json, project.revision, project.updated_at = snapshot, project.revision + 1, time.time()
    record_project_version(session, project)
    result = {"defaults": values, "revision": project.revision}
    save_operation(session, user.id, project.id, body.operation_id, "models.defaults", payload, result)
    session.commit()
    return result


def freeze_selection(session, owner_id, project, kind, identifier=None, *, require_ready=True):
    selected = identifier or project_defaults(project)[kind]
    item = profile(selected, kind)
    if item["providerId"] in {"deepseek", "sensetime", "aliyun-wan", "codex-local"}:
        return {"profileId": selected, "providerId": item["providerId"], "model": item["model"], "capabilities": item["capabilities"]}
    stored = connection(session, owner_id, item["providerId"])
    if stored is None or not stored.api_key_encrypted:
        raise AppError(503, "MODEL_NOT_CONFIGURED", "请先配置所选供应商凭据")
    config = stored.config_json
    if item["capabilities"].get("requiresEntitlement") and selected not in config.get("entitledModelIds", []):
        raise AppError(409, "MODEL_ENTITLEMENT_REQUIRED", "所选账号尚未确认当前模型权益")
    price = config.get("prices", {}).get(selected)
    if require_ready and price is None:
        raise AppError(409, "MODEL_PRICE_NOT_CONFIGURED", "所选模型价表尚未核实，已停止收费请求")
    return {"profileId": selected, "providerId": item["providerId"], "model": item["model"], "capabilities": item["capabilities"],
        "connectionId": stored.id, "connectionRevision": stored.revision, "workspaceId": config.get("workspaceId"), "region": config.get("region", "cn-beijing"), "price": copy.deepcopy(price)}


def frozen_key(session, owner_id, frozen, settings):
    stored = session.get(ModelProviderConnection, frozen.get("connectionId"))
    if stored is None or stored.scope_key not in {owner_id, "platform"} or stored.provider_id != frozen["providerId"] or stored.revision != frozen["connectionRevision"]:
        raise AppError(409, "MODEL_CONFIGURATION_CHANGED", "原任务的供应商配置已经变化，请保留原记录并重新确认")
    return decode_secret(stored.api_key_encrypted, settings)
