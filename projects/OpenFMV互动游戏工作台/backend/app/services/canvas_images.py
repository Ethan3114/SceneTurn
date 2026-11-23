import copy
import time

from sqlalchemy import select

from app.core.errors import AppError
from app.image_models import ImageAccount, ImageJob
from app.models import Canvas
from app.services.ark import seedream_cost_micros, seedream_image_size
from app.services.connections import require_image_connection
from app.services.generation import _canvas_composition
from app.services.material_references import validate_reference_assets
from app.services.projects import get_operation, iso_timestamp, lock_write, payload_digest, require_project, require_revision, save_operation
from app.services.providers import MODEL_PROFILES, freeze_selection, profile, project_defaults
from app.services.sensetime import image_size


JOB_MESSAGES = {
    "IMAGE_CONNECTION_FAILED": "无法连接图片供应商，请检查网络后重新创建任务",
    "IMAGE_PROVIDER_REJECTED": "图片供应商拒绝了本次请求，请核对模型参数与账号",
    "IMAGE_QUOTA_OR_RATE_LIMIT": "商汤账号的额度或请求频率达到限制",
    "IMAGE_RESPONSE_TIMEOUT": "等待供应商回执超时",
    "IMAGE_SUBMISSION_UNKNOWN": "供应商尚未确认原请求，请核对提交状态",
    "IMAGE_ALTERNATE_ACCOUNT_UNAVAILABLE": "其余商汤账号暂时无法使用",
    "IMAGE_RESULT_INVALID": "生成图片未通过格式、尺寸或账号校验",
    "IMAGE_REGISTRATION_FAILED": "图片已返回，保存到项目时遇到问题",
    "CODEX_IMAGE_CONNECTION_INTERRUPTED": "个人 Codex 的连接已中断，原任务仍保留",
    "CODEX_IMAGE_RESULT_MISSING": "个人 Codex 已结束任务，当前缺少可核验的图片",
}


def _job_view(job):
    request = job.request_json
    return {"id": job.id, "sceneId": job.scene_id, "sourceNodeId": job.node_id,
        "outputNodeId": request["canvas"]["outputNodeId"], "modelProfileId": request["modelProfile"]["profileId"], "model": request["modelProfile"]["model"],
        "status": job.status, "width": request["width"], "height": request["height"], "assetId": job.asset_id, "errorCode": job.error_code,
        "errorMessage": JOB_MESSAGES.get(job.error_code, "图片任务遇到问题，请检查模型设置和参考素材") if job.error_code else None,
        "createdAt": iso_timestamp(job.created_at), "updatedAt": iso_timestamp(job.updated_at)}


def list_jobs(session, owner_id, project_id):
    require_project(session, project_id, owner_id)
    jobs = session.scalars(select(ImageJob).where(ImageJob.project_id == project_id, ImageJob.owner_id == owner_id,
        ImageJob.run_id.is_(None)).order_by(ImageJob.created_at.desc(), ImageJob.id.desc()))
    return [_job_view(job) for job in jobs]


def _job(session, owner_id, project_id, identifier):
    job = session.get(ImageJob, identifier)
    if job is None or (job.owner_id, job.project_id, job.run_id) != (owner_id, project_id, None):
        raise AppError(404, "CANVAS_IMAGE_JOB_NOT_FOUND", "图片任务不存在或无权访问")
    return job


def _canvas(session, project, scene_id):
    return session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.owner_id == project.owner_id, Canvas.scene_id == scene_id))


def _empty_image_output(data, source_id, output_id):
    if not any(edge.get("fromNodeId") == source_id and edge.get("toNodeId") == output_id for edge in data["connections"]):
        return None
    target = next((node for node in data["nodes"] if node["id"] == output_id and node["type"] == "image"), None)
    if target and not any(target.get("metadata", {}).get(key) for key in ("assetId", "storageKey", "content")):
        return target
    return None


def _composition(session, project, scene_id, source_node_id, output_node_id, raw_prompt, model_profile_id, width, height):
    source, _, composition = _canvas_composition(session, project, scene_id, source_node_id, raw_prompt, output_node_id,
        generation_mode="image", output_type="image")
    canvas = _canvas(session, project, scene_id)
    output = _empty_image_output(canvas.data_json, source_node_id, output_node_id)
    if source["type"] != "config" or output is None:
        raise AppError(422, "CANVAS_IMAGE_OUTPUT_INVALID", "请将图像生成节点连到同一画布中的空图片节点")
    metadata = source.get("metadata", {})
    configured = metadata.get("modelProfileId")
    model = configured if isinstance(configured, str) and MODEL_PROFILES.get(configured, {}).get("kind") == "image" else project_defaults(project)["image"]
    if (model, metadata.get("imageWidth"), metadata.get("imageHeight"), metadata.get("outputNodeId")) != (model_profile_id, width, height, output_node_id):
        raise AppError(409, "CANVAS_IMAGE_PARAMETERS_CHANGED", "画布中的图片模型、尺寸或结果节点已经变化，请重新创建图片任务")
    identifiers = [item.get("storageKey") for item in composition["inputs"] if item["type"] == "image"]
    if any(not identifier for identifier in identifiers):
        raise AppError(422, "CANVAS_IMAGE_REFERENCE_UNREGISTERED", "请先将参考图片导入当前项目素材")
    return composition, identifiers, output


def _parameters(model_profile_id, width, height):
    selected = profile(model_profile_id, "image")
    if selected["providerId"] == "sensetime":
        try:
            image_size(width, height)
        except ValueError as error:
            raise AppError(422, "CANVAS_IMAGE_SIZE_UNSUPPORTED", str(error)) from error
    if selected["providerId"] == "volcengine-ark":
        seedream_image_size(width, height)
    if min(width, height) < selected["capabilities"].get("minDimension", 512):
        raise AppError(422, "CANVAS_IMAGE_SIZE_UNSUPPORTED", "图片短边小于所选模型支持的最小尺寸")


def _selection(session, project, identifier):
    selection = freeze_selection(session, project.owner_id, project, "image", identifier)
    if selection["providerId"] == "sensetime" and not session.scalar(select(ImageAccount.id).where(
        ImageAccount.owner_id == project.owner_id, ImageAccount.status.in_(("available", "quarantined"))).limit(1)):
        raise AppError(409, "CANVAS_IMAGE_ACCOUNT_UNAVAILABLE", "请在模型设置配置仍有额度的商汤 Token Plan 账号")
    if selection["providerId"] == "codex-local":
        return selection, require_image_connection(session, project.owner_id, project.id).id
    return selection, None


def _check_charge(selection, width, height, reference_count, settings):
    if selection["providerId"] == "volcengine-ark" and seedream_cost_micros({"modelProfile": selection, "width": width, "height": height}, reference_count) > settings.max_generation_cents * 10000:
        raise AppError(422, "IMAGE_COST_LIMIT", "所选图片模型超过当前授权范围")


def create_job(session, owner_id, project_id, body, settings):
    lock_write(session)
    project = require_project(session, project_id, owner_id)
    replay = get_operation(session, owner_id, project_id, body["operationId"], "canvas.image.create", body)
    if replay is not None:
        return _job_view(_job(session, owner_id, project_id, replay["jobId"]))
    require_revision(project, body["expectedRevision"])
    _parameters(body["modelProfileId"], body["width"], body["height"])
    composition, identifiers, output = _composition(session, project, body["sceneId"], body["sourceNodeId"], body["outputNodeId"], body["prompt"],
        body["modelProfileId"], body["width"], body["height"])
    if body["referenceAssetIds"] is not None and identifiers != body["referenceAssetIds"]:
        raise AppError(409, "CANVAS_IMAGE_REFERENCES_CHANGED", "画布参考图片与已保存的连线或提示词不一致")
    references = validate_reference_assets(session, project, identifiers, settings)
    selection, connection_id = _selection(session, project, body["modelProfileId"])
    _check_charge(selection, body["width"], body["height"], len(references), settings)
    job = ImageJob(owner_id=owner_id, project_id=project_id, scene_id=body["sceneId"], node_id=body["sourceNodeId"],
        artifact_hash=payload_digest(composition), operation_id=f"canvas:{project_id}:{body['operationId']}",
        label=output.get("title", "").strip()[:150] or "图片结果", reference_asset_ids=identifiers,
        request_json={"prompt": composition["prompt"], "width": body["width"], "height": body["height"], "modelProfile": selection,
            "referenceAssets": references, "codexConnectionId": connection_id,
            "canvas": {"outputNodeId": body["outputNodeId"], "rawPrompt": body["prompt"]}})
    session.add(job)
    session.flush()
    save_operation(session, owner_id, project_id, body["operationId"], "canvas.image.create", body, {"jobId": job.id})
    session.commit()
    return _job_view(job)


def confirm_job(session, owner_id, project_id, identifier, body, settings):
    lock_write(session)
    project = require_project(session, project_id, owner_id)
    job = _job(session, owner_id, project_id, identifier)
    payload = {"jobId": identifier, **body}
    if get_operation(session, owner_id, project_id, body["operationId"], "canvas.image.confirm", payload) is not None:
        return _job_view(job)
    if job.status != "awaiting_confirmation":
        raise AppError(409, "CANVAS_IMAGE_ALREADY_CONFIRMED", "图片任务已确认，请查看当前状态")
    require_revision(project, body["expectedRevision"])
    stored = job.request_json
    output_id = stored["canvas"]["outputNodeId"]
    composition, identifiers, _ = _composition(session, project, job.scene_id, job.node_id, output_id, stored["canvas"]["rawPrompt"],
        stored["modelProfile"]["profileId"], stored["width"], stored["height"])
    if payload_digest(composition) != job.artifact_hash:
        raise AppError(409, "CANVAS_IMAGE_INPUT_CHANGED", "已保存的画布输入发生变化，请重新创建图片任务")
    validate_reference_assets(session, project, identifiers, settings, stored["referenceAssets"])
    selection, connection_id = _selection(session, project, stored["modelProfile"]["profileId"])
    _check_charge(selection, stored["width"], stored["height"], len(identifiers), settings)
    if selection != stored["modelProfile"] or connection_id != stored["codexConnectionId"]:
        raise AppError(409, "MODEL_CONFIGURATION_CHANGED", "所选模型的配置发生变化，请重新创建图片任务")
    competing = session.scalar(select(ImageJob.id).where(ImageJob.project_id == project_id, ImageJob.owner_id == owner_id,
        ImageJob.scene_id == job.scene_id, ImageJob.run_id.is_(None), ImageJob.id != job.id,
        ImageJob.request_json["canvas"]["outputNodeId"].as_string() == output_id,
        ImageJob.status.not_in(("awaiting_confirmation", "failed", "cancelled"))).limit(1))
    if competing:
        raise AppError(409, "CANVAS_IMAGE_OUTPUT_IN_USE", "请使用新的空图片节点保存本次结果")
    job.confirmed_at = job.updated_at = time.time()
    job.status = "queued"
    save_operation(session, owner_id, project_id, body["operationId"], "canvas.image.confirm", payload, {"jobId": job.id})
    session.commit()
    return _job_view(job)


def bind_output(session, project, job, asset):
    canvas = _canvas(session, project, job.scene_id)
    if canvas is None:
        return
    data = copy.deepcopy(canvas.data_json)
    output = _empty_image_output(data, job.node_id, job.request_json["canvas"]["outputNodeId"])
    if output is None:
        return
    output["metadata"] = {**output.get("metadata", {}), "content": f"/api/v1/assets/{asset.id}/content", "storageKey": asset.id,
        "naturalWidth": asset.width, "naturalHeight": asset.height, "mimeType": asset.mime_type, "bytes": asset.bytes,
        "status": "success", "pendingResult": False, "imageGenerationId": job.id}
    canvas.data_json, canvas.updated_at = data, time.time()
