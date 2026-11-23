import asyncio
import copy
import hashlib
import hmac
import json
import re
import subprocess
import time
import uuid
from decimal import ROUND_CEILING, Decimal

from pydantic import ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.agent_models import AgentRun, AgentStep
from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import decode_secret
from app.models import Asset, Budget, Canvas, Generation, Operation, Project, ProviderSettings
from app.services import canvas_video_inputs
from app.services.media import download_video, resolve_asset_path
from app.services.projects import iso_timestamp, lock_write, require_project, require_revision
from app.services.video_join import join_videos
from app.services.wan import MODEL, PRICES, PRICE_VERSION, REGION, WanClient, WanFailure, endpoint
from app.services.providers import freeze_selection, frozen_key, project_defaults
from app.services.ark import ArkVideoClient, seedance_billing_limit


ACTIVE_STATUSES = ("submitting", "submission_unknown", "processing", "downloading", "query_interrupted")
LEASE_SECONDS = 60
COMPOSER_REFERENCE = re.compile(r"@\[node:([^\]]+)\]")


def digest(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _node_metadata(node: dict) -> dict:
    metadata = node.get("metadata", {})
    if not isinstance(metadata, dict):
        raise AppError(422, "GENERATION_SOURCE_INVALID", "来源节点的生成输入结构无效")
    return metadata


def generation_view(generation: Generation) -> dict:
    usage = {key: value for key, value in generation.usage_json.items() if key not in {"resultUrl", "resultUrlIssuedAt", "providerResponse"}}
    visible_input = {key: value for key, value in generation.input_json.items() if key not in {"localInputs", "billingLimit", "includeConnectedText"}}
    if "canvasMedia" in visible_input:
        canvas_media = visible_input["canvasMedia"]
        visible_input["canvasMedia"] = [{"nodeId": item["nodeId"], "assetId": item["assetId"], "type": item["type"]} for item in canvas_media]
        media_order = {item["nodeId"]: index for index, item in enumerate(canvas_media)}
        visible_input["inputs"] = sorted(visible_input["inputs"], key=lambda item: media_order.get(item["nodeId"], len(canvas_media)))
    return {
        "id": generation.id,
        "projectId": generation.project_id,
        "sceneId": generation.scene_id,
        "sourceNodeId": generation.source_node_id,
        "outputNodeId": generation.input_json.get("outputNodeId"),
        "clipId": generation.clip_id,
        "projectRevision": generation.project_revision,
        "status": generation.status,
        "input": visible_input,
        "confirmationHash": generation.input_hash,
        "retryOf": generation.input_json.get("request", {}).get("retryOf"),
        "quotedCents": generation.quoted_cents,
        "reservedCents": generation.reserved_cents,
        "actualCents": generation.actual_cents if generation.usage_json.get("settled") else None,
        "remoteTaskId": generation.remote_task_id,
        "remoteRequestId": generation.remote_request_id,
        "assetId": generation.asset_id,
        "error": {"code": generation.error_code, "message": generation.error_message} if generation.error_code else None,
        "usage": usage,
        "retryCount": generation.retry_count,
        "createdAt": iso_timestamp(generation.created_at),
        "updatedAt": iso_timestamp(generation.updated_at),
        "startedAt": iso_timestamp(generation.started_at) if generation.started_at is not None else None,
        "finishedAt": iso_timestamp(generation.finished_at) if generation.finished_at is not None else None,
    }


def require_generation(session: Session, generation_id: str, owner_id: str) -> Generation:
    generation = session.get(Generation, generation_id)
    if generation is None or generation.owner_id != owner_id:
        raise AppError(404, "GENERATION_NOT_FOUND", "生成任务不存在")
    require_project(session, generation.project_id, owner_id)
    return generation


def scene_node(project: Project, scene_id: str) -> dict:
    scene = next((node for node in project.data_json["graphData"]["nodes"] if node["id"] == scene_id and node.get("type") == "scene"), None)
    if scene is None:
        raise AppError(404, "SCENE_NOT_FOUND", "生成任务绑定的剧情场景不存在")
    return scene


def _canvas_source(session: Session, project: Project, scene_id: str, source_node_id: str) -> tuple[dict, str]:
    scene_node(project, scene_id)
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.scene_id == scene_id, Canvas.owner_id == project.owner_id))
    if canvas is None:
        raise AppError(404, "CANVAS_NOT_FOUND", "请先保存所属剧情画布")
    nodes = canvas.data_json["nodes"]
    node = next((item for item in nodes if item.get("id") == source_node_id), None)
    if node is None:
        raise AppError(404, "SOURCE_NODE_NOT_FOUND", "生成任务的来源节点不存在")
    connections = [item for item in canvas.data_json.get("connections", []) if item.get("toNodeId") == source_node_id or item.get("target") == source_node_id]
    reference_ids = {item.get("fromNodeId", item.get("source")) for item in connections}
    metadata = _node_metadata(node)
    references = metadata.get("references", [])
    if not isinstance(references, list) or any(not isinstance(item, str) for item in references):
        raise AppError(422, "GENERATION_REFERENCES_INVALID", "来源节点的参考素材标识必须是字符串列表")
    if any(not any(candidate.get("id") == reference_id for candidate in nodes) for reference_id in reference_ids):
        raise AppError(422, "GENERATION_REFERENCE_MISSING", "来源节点引用的素材节点不存在")
    source = {
        "node": node,
        "connections": sorted(connections, key=lambda item: item.get("id", "")),
        "references": sorted((item for item in nodes if item.get("id") in reference_ids), key=lambda item: item["id"]),
    }
    return node, digest(source)


def _source_prompt(node: dict) -> str:
    metadata = _node_metadata(node)
    keys = ("composerContent", "prompt")
    for key in keys:
        value = metadata.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise AppError(422, "GENERATION_SOURCE_INVALID", "来源节点的提示词必须是文字")
            return value
    value = node.get("prompt", metadata.get("content", "") if node.get("type") == "text" else "")
    if not isinstance(value, str):
        raise AppError(422, "GENERATION_SOURCE_INVALID", "来源节点的提示词必须是文字")
    return value


def _ready_video_resources(session: Session, project: Project, scene_id: str) -> dict[str, dict]:
    tasks = session.execute(select(
        Generation.id, Generation.source_node_id,
        Generation.input_json["outputNodeId"].as_string().label("output_node_id"), Generation.status,
        Asset.id.label("asset_id"), Asset.owner_id, Asset.project_id, Asset.generation_id, Asset.mime_type,
    ).outerjoin(Asset, Asset.id == Generation.asset_id).where(
        Generation.owner_id == project.owner_id, Generation.project_id == project.id, Generation.scene_id == scene_id,
    ).order_by(Generation.created_at.desc(), Generation.id.desc())).all()
    resources: dict[str, dict] = {}
    seen: set[str] = set()
    for task in tasks:
        node_id = task.output_node_id or task.source_node_id
        if node_id in seen:
            continue
        seen.add(node_id)
        if task.status == "ready" and task.asset_id is not None and task.owner_id == project.owner_id and task.project_id == project.id and task.generation_id == task.id and task.mime_type.startswith("video/"):
            resources[node_id] = {"assetId": task.asset_id, "generationId": task.id}
    return resources


def _resource_input(node: dict, ready_videos: dict[str, dict]) -> dict | None:
    kind = node.get("type")
    if kind not in {"text", "image", "video", "audio"}:
        return None
    metadata = _node_metadata(node)
    content = metadata.get("content")
    if content is not None and not isinstance(content, str):
        raise AppError(422, "GENERATION_REFERENCES_INVALID", "参考素材的内容必须是文字或媒体地址")
    if kind == "text":
        content = content or metadata.get("prompt")
        if content is not None and not isinstance(content, str):
            raise AppError(422, "GENERATION_REFERENCES_INVALID", "参考文字的内容必须是文字")
    result = {"nodeId": node["id"], "type": kind, "title": node.get("title", "")}
    if kind == "text":
        if not content:
            return None
        result["text"] = content
        return result
    storage_key = metadata.get("storageKey")
    if storage_key is not None and not isinstance(storage_key, str):
        raise AppError(422, "GENERATION_REFERENCES_INVALID", "参考媒体的存储标识必须是文字")
    binding = ready_videos.get(node["id"]) if kind == "video" else None
    if not content and not storage_key and binding is None:
        return None
    if content:
        result["url"] = content
    if storage_key:
        result["storageKey"] = storage_key
    if binding is not None:
        result.update(binding)
    return result


def _flatten_inputs(inputs: list[dict]) -> list[dict]:
    resources: dict[str, dict] = {}
    for item in inputs:
        for resource in item["children"] if item["type"] == "group" else [item]:
            resources[resource["nodeId"]] = resource
    return list(resources.values())


def _compose_prompt(source: dict, inputs: list[dict], raw_prompt: str, *, accept_images: bool = False, video_media: tuple = (),
        ignore_unassigned_media: bool = False, include_connected_text: bool = False) -> tuple[str, list[dict]]:
    metadata = _node_metadata(source)
    composer = metadata.get("composerContent")
    explicit = (source.get("type") == "config" or metadata.get("generationMode") == "text") and isinstance(composer, str) and bool(composer.strip()) and COMPOSER_REFERENCE.search(raw_prompt) is not None
    selected: dict[str, dict] = {}
    text_blocks: list[str] = []
    media_labels: dict[str, list[str]] = {}
    reference_image_number = 0
    for kind, identifier in video_media:
        if kind == "reference_image":
            reference_image_number += 1
            label = f"图{reference_image_number}"
        else:
            label = {"first_frame": "首帧", "last_frame": "尾帧", "reference_video": "视频1"}[kind]
        media_labels.setdefault(identifier, []).append(label)

    def select_resource(resource: dict) -> str:
        node_id = resource["nodeId"]
        if node_id in media_labels and resource["type"] in {"image", "video"}:
            if node_id not in selected:
                selected[node_id] = {**{key: value for key, value in resource.items() if key != "url"},
                    "label": "与".join(media_labels[node_id]), "assetId": resource.get("storageKey") or resource.get("assetId")}
            return f"【{selected[node_id]['label']}】"
        if accept_images and resource["type"] == "image":
            if node_id not in selected:
                index = sum(item["type"] == "image" for item in selected.values()) + 1
                selected[node_id] = {**resource, "label": f"参考图{index}"}
            return f"【{selected[node_id]['label']}】"
        if resource["type"] != "text":
            message = "当前图片生成支持文字和图片参考，请移除其他媒体输入" if accept_images else "请选择本次提示词引用的素材，并设置素材用途"
            raise AppError(422, "GENERATION_MEDIA_INPUT_UNSUPPORTED", message)
        if node_id not in selected:
            item = {**resource, "label": f"文本{len(text_blocks) + 1}"}
            selected[node_id] = item
            text_blocks.append(f"【{item['label']}】\n{item['text']}")
        return f"【{selected[node_id]['label']}】"

    flattened = _flatten_inputs(inputs)
    if video_media:
        for resource in flattened:
            if resource["nodeId"] in media_labels:
                select_resource(resource)

    if explicit:
        inputs_by_id = {item["nodeId"]: item for item in inputs}

        def replace_reference(match: re.Match) -> str:
            item = inputs_by_id.get(match.group(1))
            if item is None:
                raise AppError(422, "GENERATION_REFERENCE_MISSING", "提示词引用的素材未连接到当前生成输入，请检查画布连线")
            return "、".join(select_resource(resource) for resource in _flatten_inputs([item]))

        prompt = COMPOSER_REFERENCE.sub(replace_reference, raw_prompt)
        if include_connected_text:
            for resource in flattened:
                if resource["type"] == "text":
                    select_resource(resource)
    else:
        for resource in flattened:
            if (not video_media and not ignore_unassigned_media) or resource["nodeId"] in media_labels or resource["type"] == "text":
                select_resource(resource)
        prompt = raw_prompt
    if text_blocks:
        prompt = f"{prompt.strip() if explicit else prompt}\n\n" + "\n\n".join(text_blocks)
    prompt = prompt.strip()
    if not prompt:
        raise AppError(422, "GENERATION_INPUT_EMPTY", "请填写提示词或连接有正文的文字节点")
    if len(prompt) > 20000:
        raise AppError(422, "GENERATION_INPUT_TOO_LONG", "组装后的实际提示词超过20000字符，请缩短文字输入")
    return prompt, list(selected.values())


def _canvas_composition(session: Session, project: Project, scene_id: str, source_node_id: str, raw_prompt: str, output_node_id: str | None,
        *, generation_mode: str = "video", output_type: str = "video", video_media: tuple = (), ignore_unassigned_media: bool = False,
        include_connected_text: bool = False, text_image_input: bool = False) -> tuple[dict, str, dict]:
    scene_node(project, scene_id)
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.scene_id == scene_id, Canvas.owner_id == project.owner_id))
    if canvas is None:
        raise AppError(404, "CANVAS_NOT_FOUND", "请先保存所属剧情画布")
    nodes = canvas.data_json["nodes"]
    nodes_by_id = {node["id"]: node for node in nodes}
    source = nodes_by_id.get(source_node_id)
    if source is None:
        raise AppError(404, "SOURCE_NODE_NOT_FOUND", "生成任务的来源节点不存在")
    metadata = _node_metadata(source)
    if source.get("type") == "config" and metadata.get("generationMode") not in {None, generation_mode}:
        raise AppError(422, "GENERATION_MODE_UNSUPPORTED", "节点选择的生成类型与当前请求不一致")
    source_prompt = _source_prompt(source)
    if source_prompt.strip() != raw_prompt.strip():
        raise AppError(409, "GENERATION_INPUT_CHANGED", "请先把当前提示词保存到来源节点，再查看报价")
    output = nodes_by_id.get(output_node_id) if output_node_id is not None else None
    if output_node_id is not None and (output is None or output_node_id == source_node_id or output.get("type") != output_type):
        raise AppError(422, "GENERATION_OUTPUT_INVALID", "结果节点必须位于同一剧情画布，与来源不同且符合生成类型")

    connections = canvas.data_json.get("connections", [])
    ready_videos = _ready_video_resources(session, project, scene_id)

    def connected_inputs(target_id: str) -> tuple[list[dict], list[dict]]:
        resources: list[dict] = []
        links: list[dict] = []
        for connection in connections:
            if connection.get("toNodeId") != target_id:
                continue
            node = nodes_by_id.get(connection.get("fromNodeId"))
            if node is None:
                raise AppError(422, "GENERATION_REFERENCE_MISSING", "来源节点引用的素材节点不存在")
            if node.get("type") == "group":
                children = [resource for child in nodes if _node_metadata(child).get("groupId") == node["id"] and (resource := _resource_input(child, ready_videos)) is not None]
                resource = {"nodeId": node["id"], "type": "group", "title": node.get("title", ""), "children": children} if children else None
            elif node.get("type") == "config" and _node_metadata(node).get("generationMode") == "image":
                output_id = _node_metadata(node).get("outputNodeId")
                image = nodes_by_id.get(output_id, {}) if isinstance(output_id, str) else {}
                resource = None
                if image.get("type") == "image" and _node_metadata(image).get("storageKey") and any(
                    edge.get("fromNodeId") == node["id"] and edge.get("toNodeId") == output_id for edge in connections
                ):
                    resource = _resource_input({**image, "id": node["id"], "title": node.get("title", "")}, ready_videos)
            else:
                resource = _resource_input(node, ready_videos)
            if resource is not None:
                resources.append(resource)
                links.append(connection)
        return resources, links

    config_connection = next((connection for connection in connections if connection.get("fromNodeId") == source_node_id and nodes_by_id.get(connection.get("toNodeId"), {}).get("type") == "config"), None)
    inputs: list[dict] = []
    input_connections: list[dict] = []
    config = None
    if config_connection is not None and source.get("type") != "config" and not (source.get("type") == "text" and metadata.get("generationMode") == "text"):
        config = nodes_by_id[config_connection["toNodeId"]]
        config_inputs, config_links = connected_inputs(config["id"])
        for resource, connection in zip(config_inputs, config_links, strict=True):
            if resource["nodeId"] != source_node_id:
                inputs.append(resource)
                input_connections.append(connection)
    if not inputs:
        inputs, input_connections = connected_inputs(source_node_id)
        config = None
        config_connection = None
    prompt, used_inputs = _compose_prompt(source, inputs, source_prompt, accept_images=generation_mode == "image" or text_image_input,
        video_media=video_media, ignore_unassigned_media=ignore_unassigned_media, include_connected_text=include_connected_text)
    if generation_mode in {"image", "video"} and metadata.get("filmGuidance"):
        labels = {"style": "风格", "palette": "配色", "composition": "构图", "rhythm": "节奏", "camera": "相机", "lens": "镜头", "focal": "焦距", "aperture": "光圈"}
        guidance = metadata["filmGuidance"]
        if not isinstance(guidance, dict) or any(key not in labels or not isinstance(value, str) or len(value) > 250 for key, value in guidance.items()):
            raise AppError(422, "FILM_GUIDANCE_INVALID", "当前镜头的影视指导格式无效，请重新选择")
        directions = [f"{label}：{guidance[key].strip()}" for key, label in labels.items()
            if guidance.get(key, "").strip() not in {"", "Auto"} and (generation_mode == "video" or key != "rhythm")]
        if directions:
            prompt += "\n\n" + "\n".join(directions)
        if len(prompt) > 20000:
            raise AppError(422, "GENERATION_INPUT_TOO_LONG", "包含影视指导的提示词超过20000字符")
    composition = {"inputCompositionVersion": 1, "sourcePrompt": source_prompt, "prompt": prompt, "inputs": used_inputs}
    if output_node_id is not None:
        composition["outputNodeId"] = output_node_id
    source_digest = digest({"node": source, "inputs": inputs, "connections": input_connections, "config": config, "configConnection": config_connection, "outputNode": output})
    return source, source_digest, composition


def _clip(scene: dict, clip_id: str) -> dict | None:
    timeline = scene.get("data", {}).get("timeline", {})
    return next((clip for track in timeline.get("tracks", []) for clip in track.get("clips", []) if clip.get("id") == clip_id), None)


def quote_generation(session: Session, project_id: str, owner_id: str, payload: dict, settings: Settings, *, manage_transaction: bool = True, retry_of: Generation | None = None) -> Generation:
    if manage_transaction:
        lock_write(session)
    project = require_project(session, project_id, owner_id)
    if payload.get("modelProfileId") is None:
        payload = {key: value for key, value in payload.items() if key != "modelProfileId"}
    if payload.get("outputNodeId") is None:
        payload = {key: value for key, value in payload.items() if key != "outputNodeId"}
    optional = {"ratio", "firstFrameNodeId", "lastFrameNodeId", "referenceVideoNodeId", "referenceImageNodeIds", "videoInputMode"}
    payload = {key: value for key, value in payload.items() if key not in optional or value is not None}
    if payload.get("referenceImageNodeIds") == []:
        payload.pop("referenceImageNodeIds")
    previous = session.scalar(select(Generation).where(Generation.project_id == project_id, Generation.operation_id == payload["operationId"]))
    if previous is not None and "inputCompositionVersion" not in previous.input_json:
        payload = {**payload, "prompt": payload["prompt"].strip()}
    used_operation = session.scalar(select(Operation).where(Operation.project_id == project_id, Operation.owner_id == owner_id, Operation.operation_id == payload["operationId"]))
    if used_operation is not None and (used_operation.kind != "generation.quote" or used_operation.payload_hash != digest(payload)):
        raise AppError(409, "OPERATION_CONFLICT", "该操作标识已用于不同的项目操作")
    if previous is not None:
        if previous.owner_id != owner_id or previous.input_json["request"] != payload:
            raise AppError(409, "OPERATION_CONFLICT", "该操作标识已用于不同的生成输入")
        return previous
    requested_media = canvas_video_inputs.requested_media(payload)
    include_connected_text = bool(retry_of.input_json.get("includeConnectedText")) if retry_of else bool(payload.get("videoInputMode") or requested_media)
    source, source_digest, composition = _canvas_composition(session, project, payload["sceneId"], payload["sourceNodeId"], payload["prompt"], payload.get("outputNodeId"),
        video_media=tuple(requested_media), ignore_unassigned_media=payload.get("videoInputMode") == "text", include_connected_text=include_connected_text)
    drama_task = _node_metadata(source).get("dramaTask")
    if drama_task not in {None, "clip"}:
        raise AppError(422, "GENERATION_TASK_UNSUPPORTED", "视频模型用于分镜视频，请选择对应的生成功能")
    if payload.get("clipId"):
        target = _clip(scene_node(project, payload["sceneId"]), payload["clipId"])
        if target is None or target.get("type") not in {"video", "image"}:
            raise AppError(409, "GENERATION_TARGET_INVALID", "指定片段不存在或无法采用视频")
    provider = session.get(ProviderSettings, owner_id)
    selected_id = payload.get("modelProfileId") or project_defaults(project)["video"]
    selected = freeze_selection(session, owner_id, project, "video", selected_id) if selected_id != "wan-3-0" else None
    model = provider.model if provider else settings.generation_model
    region = provider.region if provider else settings.generation_region
    if selected is None and (model != MODEL or region != REGION):
        raise AppError(422, "WAN_CONFIGURATION_UNSUPPORTED", "请使用北京地域的wan3.0-video配置")
    if selected is not None:
        capabilities = selected["capabilities"]
        if payload["resolution"] not in capabilities["resolutions"] or not capabilities["minDuration"] <= payload["duration"] <= capabilities["maxDuration"]:
            raise AppError(422, "MODEL_VIDEO_PARAMETERS_UNSUPPORTED", "所选视频模型不支持当前清晰度或时长")
        if (payload.get("boundaryFrames") or payload.get("lastFrameNodeId")) and not capabilities["lastFrame"]:
            raise AppError(422, "MODEL_LAST_FRAME_UNSUPPORTED", "所选模型不支持首尾帧视频")
        if payload.get("firstFrameNodeId") and not capabilities["firstFrame"]:
            raise AppError(422, "MODEL_FIRST_FRAME_UNSUPPORTED", "所选模型不支持固定首帧")
        if payload.get("videoInputMode") not in {None, "text"}:
            raise AppError(422, "MODEL_VIDEO_REFERENCE_UNSUPPORTED", "当前画布视频参考功能使用 Wan 3.0")
        rates = selected["price"]["centsPerSecond"]
        if payload["resolution"] not in rates:
            raise AppError(409, "MODEL_PRICE_NOT_CONFIGURED", "当前清晰度的报价上界尚未核实")
        model, region = selected["model"], selected["region"]
    else:
        rates = PRICES
    ratio = payload.get("ratio", "16:9")
    if payload.get("videoInputMode") == "extend" or (payload.get("firstFrameNodeId") and selected and selected["capabilities"].get("fixedFrameRatioMode") == "adaptive"):
        if payload.get("ratio") not in {None, "adaptive"}:
            raise AppError(422, "MODEL_VIDEO_RATIO_ADAPTIVE_REQUIRED", "当前生成模式须使用自适应画幅")
        ratio = "adaptive"
    workspace_id = selected.get("workspaceId") if selected else provider.workspace_id if provider else ""
    legacy_receipt = retry_of is not None and canvas_video_inputs.uses_legacy_receipt(retry_of.input_json)
    canvas_media = canvas_video_inputs.freeze_media(session, project, composition["inputs"], requested_media, payload["duration"], workspace_id, settings, legacy_receipt=legacy_receipt)
    input_seconds = (sum(item.get("durationMs", 0) for item in canvas_media) + 999) // 1000
    quoted = (payload["duration"] + input_seconds) * rates[payload["resolution"]]
    frozen = {
        "provider": selected["providerId"] if selected else "aliyun-wan",
        "workspaceId": workspace_id,
        "region": region,
        "model": model,
        **composition,
        "duration": payload["duration"],
        "resolution": payload["resolution"],
        "count": 1,
        "ratio": ratio,
        "audio": (selected["capabilities"]["audio"] if selected else True) and _node_metadata(source).get("generateAudio") != "false",
        "promptExtend": False,
        "watermark": False,
        "priceVersion": selected["price"]["version"] if selected else PRICE_VERSION,
        "centsPerSecond": rates[payload["resolution"]],
        "maxAllowedCents": settings.max_generation_cents,
        "replaceExisting": bool(payload.get("clipId")),
        "request": payload,
    }
    if selected is not None:
        frozen["modelProfile"] = selected
        billing_limit = seedance_billing_limit(selected, payload["resolution"], payload["duration"], ratio, quoted)
        if billing_limit:
            frozen["billingLimit"] = billing_limit
    if include_connected_text:
        frozen["includeConnectedText"] = True
    if canvas_media:
        frozen["canvasMedia"] = canvas_media
        frozen["billableInputSeconds"] = input_seconds
        if payload.get("videoInputMode"):
            frozen["videoInputMode"] = payload["videoInputMode"]
    if drama_task is not None:
        frozen["dramaTask"] = drama_task
    if payload.get("boundaryFrames"):
        from app.services.video_boundaries import validate_frozen_boundaries
        validate_frozen_boundaries(session, owner_id, payload["boundaryFrames"], settings)
        frozen["boundaryFrames"] = payload["boundaryFrames"]
    if payload.get("reservedClipId") and payload.get("boundaryFrames", {}).get("sourceType") != "media_sequence":
        raise AppError(422, "GENERATION_TARGET_INVALID", "固定新片段标识必须来自真实连续视频序列")
    clip_id = retry_of.clip_id if retry_of else payload.get("clipId") or payload.get("reservedClipId") or str(uuid.uuid4())
    if retry_of:
        changed = any(frozen.get(key) != retry_of.input_json.get(key) for key in (
            "workspaceId", "region", "model", "sourcePrompt", "prompt", "inputs", "outputNodeId",
            "duration", "resolution", "count", "ratio", "audio", "promptExtend", "watermark",
            "replaceExisting", "boundaryFrames", "canvasMedia", "videoInputMode",
        ))
        if changed or not hmac.compare_digest(source_digest, retry_of.source_digest):
            raise AppError(409, "GENERATION_INPUT_CHANGED", "原镜头输入或业务空间已有变化，请重新核对视频方案")
    confirmation = digest({
        "input": frozen, "projectId": project_id, "sceneId": payload["sceneId"], "sourceNodeId": payload["sourceNodeId"],
        "clipId": clip_id, "projectRevision": project.revision, "sourceDigest": source_digest, "quotedCents": quoted,
    })
    generation = Generation(
        owner_id=owner_id, project_id=project_id, scene_id=payload["sceneId"], source_node_id=payload["sourceNodeId"],
        clip_id=clip_id, operation_id=payload["operationId"], project_revision=project.revision,
        input_json=frozen, input_hash=confirmation, source_digest=source_digest, quoted_cents=quoted,
        status="awaiting_confirmation", usage_json={},
    )
    session.add(generation)
    session.flush()
    session.add(Operation(owner_id=owner_id, project_id=project_id, operation_id=payload["operationId"], kind="generation.quote", payload_hash=digest(payload), response_json={"generationId": generation.id}))
    if manage_transaction:
        session.commit()
    return generation


def retry_generation_quote(session: Session, generation_id: str, owner_id: str, expected_revision: int, settings: Settings) -> Generation:
    lock_write(session)
    original = require_generation(session, generation_id, owner_id)
    request = original.input_json.get("request", {})
    if (original.status != "submission_unknown" or original.remote_task_id or request.get("retryOf")
            or original.input_json.get("provider") != "aliyun-wan" or not request.get("videoArtifactNodeId")
            or not original.input_json.get("boundaryFrames")):
        raise AppError(409, "VIDEO_RETRY_UNAVAILABLE", "仅可重新准备提交结果待核实且没有任务编号的原始镜头")
    project = require_project(session, original.project_id, owner_id)
    require_revision(project, expected_revision)
    original_hash = digest({
        "input": original.input_json, "projectId": original.project_id, "sceneId": original.scene_id,
        "sourceNodeId": original.source_node_id, "clipId": original.clip_id,
        "projectRevision": original.project_revision, "sourceDigest": original.source_digest,
        "quotedCents": original.quoted_cents,
    })
    if not hmac.compare_digest(original.input_hash, original_hash):
        raise AppError(409, "CONFIRMATION_INPUT_INVALID", "原视频报价输入与确认摘要不一致，请重新核对")
    operation_id = hashlib.sha256(f"generation-retry:{original.id}".encode()).hexdigest()
    receipt = func.json_each(AgentStep.tool_results_json).table_valued("value", joins_implicitly=True)
    step = session.scalars(select(AgentStep).join(AgentRun, AgentRun.id == AgentStep.run_id).where(
        AgentRun.owner_id == owner_id, AgentRun.project_id == original.project_id, AgentRun.scene_id == original.scene_id,
        AgentRun.limits_json["workspace"].as_string() == "materials", AgentStep.role == "media_producer",
        func.json_extract(receipt.c.value, "$.tool") == "submit_generation",
        func.json_extract(receipt.c.value, "$.result.generation.id") == original.id,
    )).unique().one_or_none()
    if step is None:
        raise AppError(409, "VIDEO_RETRY_UNAVAILABLE", "原镜头缺少可核对的素材制作记录")
    run = session.get(AgentRun, step.run_id)
    result = run.result_json or {}
    reviews = [*result.get("materialReviewHistory", []), result.get("materialReview") or {}]
    canvas = session.scalar(select(Canvas).where(Canvas.owner_id == owner_id, Canvas.project_id == original.project_id,
        Canvas.scene_id == original.scene_id))
    artifact_node = next((node for node in canvas.data_json["nodes"] if node["id"] == request["videoArtifactNodeId"]), None) if canvas else None
    artifact = (artifact_node or {}).get("metadata", {}).get("creativeArtifact")
    approved = any(review.get("stepId") == step.id and review.get("nodeId") == request["videoArtifactNodeId"]
        and review.get("artifactHash") == request.get("videoArtifactHash") and review.get("status") in {"approved", "not_required"}
        for review in reviews)
    if not approved or not artifact or digest(artifact) != request.get("videoArtifactHash"):
        raise AppError(409, "VIDEO_ARTIFACT_UNCONFIRMED", "原镜头方案已变化，请重新核对")
    generation = quote_generation(session, original.project_id, owner_id,
        {**request, "operationId": operation_id, "retryOf": original.id}, settings, manage_transaction=False, retry_of=original)
    if not any(item["operationId"] == operation_id for item in step.tool_results_json):
        step.tool_results_json = [*step.tool_results_json, {"operationId": operation_id, "tool": "submit_generation",
            "arguments": {"videoArtifactNodeId": request["videoArtifactNodeId"], "shotKey": original.input_json["boundaryFrames"]["shotKey"]},
            "result": {"generation": generation_view(generation), "confirmationRequired": True, "executionOrigin": "owner_retry"},
            "revision": project.revision}]
    session.commit()
    return generation


def _provider_client(session: Session, generation: Generation, settings: Settings) -> WanClient:
    if generation.input_json.get("provider") == "volcengine-ark":
        return ArkVideoClient(settings, frozen_key(session, generation.owner_id, generation.input_json["modelProfile"], settings), generation.input_json["model"])
    provider = session.get(ProviderSettings, generation.owner_id)
    if provider is None or not provider.api_key_encrypted:
        raise AppError(503, "WAN_NOT_CONFIGURED", "请先保存阿里云业务空间和API Key")
    if any((provider.workspace_id != generation.input_json["workspaceId"], provider.region != generation.input_json["region"], provider.model != generation.input_json["model"])):
        raise AppError(409, "WAN_CONFIGURATION_CHANGED", "模型或业务空间与报价时不同，请重新查看报价；已有任务保留原配置")
    endpoint(provider.workspace_id, provider.region)
    return WanClient(settings, provider.workspace_id, decode_secret(provider.api_key_encrypted, settings), provider.region)


def create_join_generation(session: Session, project_id: str, owner_id: str, payload: dict, settings: Settings, *, manage_transaction: bool = True) -> Generation:
    if manage_transaction:
        lock_write(session)
    project = require_project(session, project_id, owner_id)
    previous = session.scalar(select(Generation).where(Generation.project_id == project_id, Generation.operation_id == payload["operationId"]))
    used_operation = session.scalar(select(Operation).where(Operation.project_id == project_id, Operation.owner_id == owner_id, Operation.operation_id == payload["operationId"]))
    if used_operation is not None and (used_operation.kind != "generation.join" or used_operation.payload_hash != digest(payload)):
        raise AppError(409, "OPERATION_CONFLICT", "该操作标识已用于不同的项目操作")
    if previous is not None:
        if previous.owner_id != owner_id or previous.input_json.get("request") != payload:
            raise AppError(409, "OPERATION_CONFLICT", "该操作标识已用于不同的拼接输入")
        return previous
    if project.revision != payload["expectedRevision"]:
        raise AppError(409, "PROJECT_REVISION_CONFLICT", "项目已有新编辑，请保存后重新提交拼接")
    source, source_digest = _canvas_source(session, project, payload["sceneId"], payload["sourceNodeId"])
    if source.get("type") != "config" or _node_metadata(source).get("dramaTask") != "join" or _node_metadata(source).get("generationMode") != "video":
        raise AppError(422, "VIDEO_JOIN_SOURCE_INVALID", "请使用视频类型中的拼接成片节点")
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project_id, Canvas.scene_id == payload["sceneId"], Canvas.owner_id == owner_id))
    nodes = canvas.data_json["nodes"]
    by_id = {node["id"]: node for node in nodes}
    output = by_id.get(payload["outputNodeId"])
    if output is None or output["type"] != "video" or output["id"] == source["id"]:
        raise AppError(422, "GENERATION_OUTPUT_INVALID", "拼接结果需要绑定同一剧情画布中的独立视频节点")
    ready_videos = _ready_video_resources(session, project, payload["sceneId"])
    selected = {}
    for connection in canvas.data_json.get("connections", []):
        if connection["toNodeId"] != source["id"]:
            continue
        reference = by_id[connection["fromNodeId"]]
        members = [node for node in nodes if _node_metadata(node).get("groupId") == reference["id"]] if reference["type"] == "group" else [reference]
        for member in members:
            if member["type"] != "video":
                raise AppError(422, "VIDEO_JOIN_INPUT_INVALID", "拼接成片需要连接视频素材，输入按连线和组合内的顺序排列")
            asset_id = _node_metadata(member).get("storageKey") or ready_videos.get(member["id"], {}).get("assetId")
            asset = session.scalar(select(Asset).where(Asset.id == asset_id, Asset.owner_id == owner_id, Asset.project_id == project_id)) if asset_id else None
            if asset is None or not asset.mime_type.startswith("video/") or not asset.duration_ms or not asset.width or not asset.height:
                raise AppError(422, "VIDEO_JOIN_INPUT_NOT_READY", "请等待所有参考视频完成并保存为当前项目的素材")
            resolve_asset_path(settings, asset.relpath)
            selected[member["id"]] = {"nodeId": member["id"], "type": "video", "title": member["title"], "assetId": asset.id, "durationMs": asset.duration_ms, "width": asset.width, "height": asset.height, "sha256": asset.sha256, "bytes": asset.bytes, "relpath": asset.relpath}
    if not 2 <= len(selected) <= 100:
        raise AppError(422, "VIDEO_JOIN_INPUT_COUNT", "请连接2至100段视频后提交拼接")
    inputs = list(selected.values())
    frozen = {"provider": "local-ffmpeg", "dramaTask": "join", "model": "FFmpeg", "prompt": "按连接顺序拼接视频，保留画面与声音。", "duration": sum(item["durationMs"] for item in inputs) / 1000, "resolution": f"{inputs[0]['height']}P", "outputNodeId": output["id"], "inputs": [{key: value for key, value in item.items() if key != "relpath"} for item in inputs], "localInputs": inputs, "replaceExisting": False, "request": payload}
    generation = Generation(owner_id=owner_id, project_id=project_id, scene_id=payload["sceneId"], source_node_id=source["id"], clip_id=str(uuid.uuid4()), operation_id=payload["operationId"], project_revision=project.revision, input_json=frozen, input_hash=digest(frozen), source_digest=source_digest, quoted_cents=0, reserved_cents=0, status="queued", usage_json={"settled": True, "localProcessing": True})
    session.add(generation)
    session.flush()
    session.add(Operation(owner_id=owner_id, project_id=project_id, operation_id=payload["operationId"], kind="generation.join", payload_hash=digest(payload), response_json={"generationId": generation.id}))
    if manage_transaction:
        session.commit()
    return generation


def confirm_generation(session: Session, generation_id: str, owner_id: str, confirmation_hash: str, expected_revision: int, settings: Settings) -> Generation:
    lock_write(session)
    generation = require_generation(session, generation_id, owner_id)
    frozen_hash = digest({
        "input": generation.input_json, "projectId": generation.project_id, "sceneId": generation.scene_id,
        "sourceNodeId": generation.source_node_id, "clipId": generation.clip_id,
        "projectRevision": generation.project_revision, "sourceDigest": generation.source_digest,
        "quotedCents": generation.quoted_cents,
    })
    if not hmac.compare_digest(generation.input_hash, frozen_hash):
        raise AppError(409, "CONFIRMATION_INPUT_INVALID", "已保存的报价输入与确认摘要不一致，请重新查看报价")
    if not hmac.compare_digest(generation.input_hash, confirmation_hash):
        raise AppError(409, "CONFIRMATION_MISMATCH", "确认摘要与本次生成输入不一致")
    if generation.status != "awaiting_confirmation":
        return generation
    project = require_project(session, generation.project_id, owner_id)
    if expected_revision != project.revision or expected_revision != generation.project_revision:
        raise AppError(409, "PROJECT_REVISION_CONFLICT", "项目已有新编辑，请保存后重新查看报价")
    if generation.input_json.get("boundaryFrames"):
        from app.services.video_boundaries import validate_frozen_boundaries
        validate_frozen_boundaries(session, owner_id, generation.input_json["boundaryFrames"], settings)
    composition_version = generation.input_json.get("inputCompositionVersion")
    if composition_version is None:
        _, current_source = _canvas_source(session, project, generation.scene_id, generation.source_node_id)
    elif composition_version == 1:
        request = generation.input_json["request"]
        requested_media = canvas_video_inputs.requested_media(request)
        _, current_source, composition = _canvas_composition(session, project, generation.scene_id, generation.source_node_id, generation.input_json["sourcePrompt"], generation.input_json.get("outputNodeId"),
            video_media=tuple(requested_media), ignore_unassigned_media=request.get("videoInputMode") == "text",
            include_connected_text=generation.input_json.get("includeConnectedText", False))
        if any(composition.get(key) != generation.input_json.get(key) for key in ("sourcePrompt", "prompt", "inputs", "outputNodeId")):
            raise AppError(409, "GENERATION_INPUT_CHANGED", "来源节点或参考素材已变化，请重新查看报价")
        if requested_media:
            current = canvas_video_inputs.freeze_media(session, project, composition["inputs"], requested_media, generation.input_json["duration"],
                generation.input_json["workspaceId"], settings, legacy_receipt=canvas_video_inputs.uses_legacy_receipt(generation.input_json))
            if current != generation.input_json["canvasMedia"]:
                raise AppError(409, "VIDEO_MEDIA_INPUT_CHANGED", "画布中的固定帧或参考素材已变化，请重新查看报价")
    else:
        raise AppError(422, "GENERATION_INPUT_VERSION_UNSUPPORTED", "该任务的输入组装版本暂不支持")
    if not hmac.compare_digest(current_source, generation.source_digest):
        raise AppError(409, "GENERATION_INPUT_CHANGED", "来源节点或参考素材已变化，请重新查看报价")
    if generation.quoted_cents > settings.max_generation_cents:
        raise AppError(422, "GENERATION_COST_LIMIT", "本次生成超过当前单次最高费用")
    _provider_client(session, generation, settings)
    locked = session.execute(update(Project).where(Project.id == project.id, Project.revision == expected_revision).values(updated_at=Project.updated_at))
    if locked.rowcount != 1:
        session.rollback()
        raise AppError(409, "PROJECT_REVISION_CONFLICT", "项目已有新编辑，请重新查看报价")
    claimed = session.execute(update(Generation).where(Generation.id == generation.id, Generation.status == "awaiting_confirmation").values(status="queued", reserved_cents=generation.quoted_cents, updated_at=time.time()))
    if claimed.rowcount != 1:
        session.rollback()
        session.refresh(generation)
        return generation
    reserved = session.execute(update(Budget).where(
        Budget.id == "global",
        (Budget.spent_cents + Budget.reserved_cents + generation.quoted_cents) * 10000 + Budget.text_spent_micros + Budget.text_reserved_micros <= Budget.limit_cents * 10000,
        (Budget.spent_cents + Budget.reserved_cents + generation.quoted_cents) * 10000 + Budget.text_spent_micros + Budget.text_reserved_micros <= settings.budget_cents * 10000,
    ).values(reserved_cents=Budget.reserved_cents + generation.quoted_cents))
    if reserved.rowcount != 1:
        session.rollback()
        raise AppError(402, "BUDGET_EXCEEDED", "本次生成将超过累计验证预算，已停止新增收费请求")
    session.commit()
    session.refresh(generation)
    return generation


def resume_generation(session: Session, generation_id: str, owner_id: str) -> Generation:
    lock_write(session)
    generation = require_generation(session, generation_id, owner_id)
    if generation.status == "ready":
        return generation
    if not generation.remote_task_id or generation.status not in {"query_interrupted", "processing", "downloading", "submission_unknown"}:
        raise AppError(409, "GENERATION_NOT_RESUMABLE", "该任务没有可继续查询的远端标识；提交结果待核实时需要先核实原任务")
    generation.status = "processing"
    generation.error_code = None
    generation.error_message = None
    generation.next_poll_at = time.time()
    generation.updated_at = time.time()
    session.commit()
    return generation


def _operation_response(session: Session, owner_id: str, project_id: str, operation_id: str, payload: dict) -> dict | None:
    operation = session.scalar(select(Operation).where(Operation.owner_id == owner_id, Operation.project_id == project_id, Operation.operation_id == operation_id))
    if operation is None:
        return None
    if operation.kind != "generation-adopt" or operation.payload_hash != digest(payload):
        raise AppError(409, "OPERATION_CONFLICT", "该操作标识已用于不同的项目操作")
    return operation.response_json


def adopt_generation(session: Session, generation_id: str, owner_id: str, expected_revision: int, operation_id: str, settings: Settings, *, manage_transaction: bool = True) -> dict:
    from app.media_schemas import AdoptionCheck, AdoptionConfirm
    from app.services.media_adoptions import apply_adoption, check_adoption

    if manage_transaction:
        lock_write(session)
    generation = require_generation(session, generation_id, owner_id)
    payload = {"generationId": generation_id, "expectedRevision": expected_revision}
    previous = _operation_response(session, owner_id, generation.project_id, operation_id, payload)
    if previous is not None:
        return previous
    project = require_project(session, generation.project_id, owner_id)
    if project.revision != expected_revision:
        raise AppError(409, "PROJECT_REVISION_CONFLICT", "项目已有新编辑，请重新读取后采用素材")
    if generation.status != "ready" or not generation.asset_id:
        raise AppError(409, "GENERATION_NOT_READY", "视频尚未完成文件检查，暂时无法采用")
    check_operation = hashlib.sha256(f"adoption-check:{operation_id}".encode()).hexdigest()
    check = AdoptionCheck(expected_revision=expected_revision, operation_id=check_operation,
        scene_id=generation.scene_id, asset_id=generation.asset_id, generation_id=generation.id,
        clip_id=generation.clip_id, mode="replace" if generation.input_json["replaceExisting"] else "append")
    adoption = check_adoption(session, project.id, owner_id, check, settings, manage_transaction=False)
    if adoption.status != "ready":
        if manage_transaction:
            session.commit()
        raise AppError(409, "MEDIA_DURATION_CONFLICT" if adoption.status == "duration_conflict" else "MEDIA_ADOPTION_BLOCKED", adoption.proposal_json["message"])
    confirm = AdoptionConfirm(expected_revision=expected_revision,
        operation_id=hashlib.sha256(f"adoption-apply:{operation_id}".encode()).hexdigest(), confirmation_hash=adoption.confirmation_hash)
    response = apply_adoption(session, project.id, adoption.id, owner_id, confirm, settings, manage_transaction=False)["project"]
    session.add(Operation(owner_id=owner_id, project_id=project.id, operation_id=operation_id, kind="generation-adopt", payload_hash=digest(payload), response_json=response))
    if manage_transaction:
        session.commit()
    return response


def _settle(session: Session, generation: Generation, actual_cents: int) -> None:
    if generation.usage_json.get("settled"):
        return
    changed = session.execute(update(Budget).where(Budget.id == "global", Budget.reserved_cents >= generation.reserved_cents).values(
        reserved_cents=Budget.reserved_cents - generation.reserved_cents,
        spent_cents=Budget.spent_cents + actual_cents,
    ))
    if changed.rowcount != 1:
        raise AppError(500, "BUDGET_STATE_INVALID", "费用预留记录不一致，已停止继续处理")
    generation.reserved_cents = 0
    generation.actual_cents = actual_cents
    generation.usage_json = {**generation.usage_json, "settled": True}


def _finish_failure(session: Session, generation: Generation, code: str, message: str, release_reservation: bool) -> None:
    generation.status = "failed"
    generation.error_code = code
    generation.error_message = message
    generation.updated_at = time.time()
    generation.finished_at = time.time()
    if release_reservation:
        _settle(session, generation, 0)


def _worker_generation(session: Session, generation_id: str, worker_id: str) -> Generation:
    budget = session.get(Budget, "global")
    generation = session.get(Generation, generation_id)
    if budget is None or budget.lease_owner != worker_id or (budget.lease_expires_at or 0) <= time.time() or generation is None or generation.lease_owner != worker_id:
        raise AppError(409, "WORKER_LEASE_LOST", "任务执行租约已失效，已停止当前处理")
    return generation


async def _submit(settings: Settings, factory: sessionmaker, generation_id: str, worker_id: str, client: WanClient | ArkVideoClient, frozen_input: dict) -> None:
    if frozen_input.get("boundaryFrames") or frozen_input.get("canvasMedia"):
        from app.services.video_boundaries import media_payload
        try:
            with factory() as session:
                generation = _worker_generation(session, generation_id, worker_id)
                media = canvas_video_inputs.media_payload(session, generation, settings) if frozen_input.get("canvasMedia") else media_payload(session, generation.owner_id, frozen_input["boundaryFrames"], settings)
            provider_media = []
            for item in media:
                if "path" in item:
                    if not isinstance(client, WanClient):
                        raise AppError(422, "MODEL_VIDEO_REFERENCE_UNSUPPORTED", "当前画布视频参考功能使用 Wan 3.0")
                    item = {"type": item["type"], "url": await client.upload_reference_video(item["path"])}
                provider_media.append(item)
            frozen_input = {**frozen_input, "media": provider_media}
        except (AppError, OSError, WanFailure) as failure:
            with factory() as session:
                generation = _worker_generation(session, generation_id, worker_id)
                if isinstance(failure, WanFailure):
                    _finish_failure(session, generation, failure.code, failure.message, True)
                else:
                    _finish_failure(session, generation, "VIDEO_MEDIA_UNAVAILABLE", "已确认的参考素材发生变化或不可用，请重新选择素材", True)
                session.commit()
            return
    with factory() as session:
        generation = _worker_generation(session, generation_id, worker_id)
        generation.status = "submitting"
        generation.started_at = time.time()
        generation.updated_at = generation.started_at
        session.commit()
    try:
        result = await client.submit(frozen_input)
    except WanFailure as failure:
        with factory() as session:
            generation = _worker_generation(session, generation_id, worker_id)
            if failure.submission_unknown:
                generation.status = "submission_unknown"
                generation.error_code = failure.code
                generation.error_message = failure.message
                generation.updated_at = time.time()
            else:
                _finish_failure(session, generation, failure.code, failure.message, True)
            session.commit()
        return
    with factory() as session:
        generation = _worker_generation(session, generation_id, worker_id)
        generation.remote_task_id = result.output.task_id
        generation.remote_request_id = result.request_id
        generation.status = "processing"
        generation.next_poll_at = time.time() + settings.poll_interval
        generation.updated_at = time.time()
        generation.error_code = None
        generation.error_message = None
        session.commit()


async def _query(settings: Settings, factory: sessionmaker, generation_id: str, worker_id: str, client: WanClient, remote_task_id: str) -> None:
    try:
        result = await client.query(remote_task_id)
    except WanFailure as failure:
        with factory() as session:
            generation = _worker_generation(session, generation_id, worker_id)
            generation.retry_count += 1
            generation.status = "processing" if generation.retry_count <= 2 else "query_interrupted"
            generation.error_code = failure.code
            generation.error_message = failure.message
            generation.next_poll_at = time.time() + settings.poll_interval * 2
            generation.updated_at = time.time()
            session.commit()
        return
    with factory() as session:
        generation = _worker_generation(session, generation_id, worker_id)
        generation.remote_request_id = result.request_id
        generation.updated_at = time.time()
        status = result.output.task_status
        if status in {"FAILED", "CANCELED"}:
            if generation.input_json.get("provider") == "volcengine-ark":
                generation.status, generation.error_code, generation.error_message = "query_interrupted", "ARK_FAILURE_COST_UNVERIFIED", "供应商任务未成功，原任务和费用预留继续保留，请核实实际账单"
                generation.usage_json = {**generation.usage_json, "providerResponse": result.provider_response}
            else:
                _finish_failure(session, generation, "WAN_GENERATION_FAILED", "供应商任务未生成成功，输入与原任务标识已保留", True)
        elif status == "UNKNOWN":
            generation.status = "query_interrupted"
            generation.error_code = "WAN_TASK_UNKNOWN"
            generation.error_message = "供应商无法识别原任务，可能已超过24小时查询期限，费用预留继续保留"
        elif status == "SUCCEEDED":
            usage = result.usage
            input_seconds = generation.input_json.get("billableInputSeconds", 0)
            output_seconds = generation.input_json["duration"]
            if (usage is None or not result.output.video_url or not 0 <= usage.input_video_duration <= input_seconds
                    or bool(input_seconds) != bool(usage.input_video_duration) or usage.SR != int(generation.input_json["resolution"][:-1])
                    or usage.duration > input_seconds + output_seconds or usage.output_video_duration > output_seconds
                    or (generation.input_json.get("request", {}).get("ratio") not in {None, "adaptive"} and usage.ratio != generation.input_json["ratio"])):
                generation.status = "query_interrupted"
                generation.error_code = "WAN_RESULT_INVALID"
                generation.error_message = "供应商用量或结果与已确认参数不一致，需要核实原任务与费用"
            else:
                duration = max(Decimal(str(usage.duration)), Decimal(str(usage.input_video_duration)) + Decimal(str(usage.output_video_duration)))
                actual = int((duration * generation.input_json["centsPerSecond"]).to_integral_value(rounding=ROUND_CEILING))
                provider_response = getattr(result, "provider_response", None)
                if provider_response is not None:
                    price = generation.input_json["modelProfile"]["price"]
                    output_rate = price.get("outputMicrosPerMillion")
                    if output_rate is None:
                        generation.status, generation.error_code, generation.error_message = "query_interrupted", "MODEL_PRICE_NOT_CONFIGURED", "方舟实际Token价表缺失，原预留继续保留"
                        session.commit()
                        return
                    output_tokens = provider_response["usage"]["completion_tokens"]
                    actual = int((Decimal(output_tokens) * output_rate / 10000000000).to_integral_value(rounding=ROUND_CEILING))
                    generation.usage_json = {**generation.usage_json, "providerResponse": provider_response, "completionTokens": output_tokens}
                    billing_limit = generation.input_json.get("billingLimit")
                    if actual > generation.reserved_cents or (billing_limit and output_tokens > billing_limit["maxCompletionTokens"]):
                        generation.status, generation.error_code, generation.error_message = "query_interrupted", "GENERATION_COST_OUT_OF_BOUNDS", "供应商实际用量超过报价预留，请核实原任务，系统不会自动重发"
                        session.commit()
                        return
                generation.usage_json = {**generation.usage_json, **usage.model_dump(), "resultUrl": result.output.video_url, "resultUrlIssuedAt": time.time()}
                _settle(session, generation, actual)
                generation.status = "downloading"
                generation.next_poll_at = time.time()
                generation.error_code = None
                generation.error_message = None
        else:
            generation.status = "processing"
            generation.next_poll_at = time.time() + settings.poll_interval
            generation.error_code = None
            generation.error_message = None
        session.commit()


async def _download(settings: Settings, factory: sessionmaker, generation_id: str, worker_id: str, url: str) -> None:
    try:
        media = await download_video(url, settings)
    except AppError as failure:
        with factory() as session:
            generation = _worker_generation(session, generation_id, worker_id)
            generation.retry_count += 1
            generation.status = "downloading" if generation.retry_count <= 2 and failure.status >= 500 else "query_interrupted"
            generation.error_code = failure.code
            generation.error_message = failure.message
            generation.next_poll_at = time.time() + settings.poll_interval * 2
            generation.updated_at = time.time()
            session.commit()
        return
    with factory() as session:
        generation = _worker_generation(session, generation_id, worker_id)
        asset = Asset(
            owner_id=generation.owner_id, project_id=generation.project_id, generation_id=generation.id,
            name=media.name, relpath=media.relpath, poster_relpath=media.poster_relpath,
            mime_type=media.mime_type, bytes=media.bytes, sha256=media.sha256,
            duration_ms=media.duration_ms, width=media.width, height=media.height,
            metadata_json={"model": generation.input_json["model"], "remoteTaskId": generation.remote_task_id, "inputHash": generation.input_hash},
        )
        session.add(asset)
        session.flush()
        generation.asset_id = asset.id
        generation.status = "ready"
        generation.finished_at = time.time()
        generation.updated_at = time.time()
        generation.error_code = None
        generation.error_message = None
        session.commit()


async def _join_generation(settings: Settings, factory: sessionmaker, generation_id: str, worker_id: str, inputs: list[dict]) -> None:
    try:
        media = await join_videos(inputs, settings)
    except (AppError, OSError, subprocess.TimeoutExpired, ValidationError) as failure:
        code = failure.code if isinstance(failure, AppError) else "VIDEO_JOIN_FAILED"
        message = failure.message if isinstance(failure, AppError) else "视频拼接未完成，来源素材和任务记录已保留"
        with factory() as session:
            generation = _worker_generation(session, generation_id, worker_id)
            _finish_failure(session, generation, code, message, False)
            session.commit()
        return
    with factory() as session:
        generation = _worker_generation(session, generation_id, worker_id)
        asset = Asset(owner_id=generation.owner_id, project_id=generation.project_id, generation_id=generation.id, name=media.name, relpath=media.relpath, poster_relpath=media.poster_relpath, mime_type=media.mime_type, bytes=media.bytes, sha256=media.sha256, duration_ms=media.duration_ms, width=media.width, height=media.height, metadata_json={"provider": "local-ffmpeg", "sourceAssetIds": [item["assetId"] for item in inputs]})
        session.add(asset)
        session.flush()
        generation.asset_id = asset.id
        generation.status = "ready"
        generation.finished_at = time.time()
        generation.updated_at = time.time()
        generation.error_code = None
        generation.error_message = None
        session.commit()


async def _process_one(settings: Settings, factory: sessionmaker, worker_id: str) -> None:
    with factory() as session:
        generation = session.scalar(select(Generation).where(
            Generation.status.in_(ACTIVE_STATUSES),
            Generation.status != "query_interrupted",
            or_(Generation.status != "submission_unknown", Generation.remote_task_id.is_not(None)),
        ).order_by(Generation.created_at, Generation.id).limit(1))
        if generation is None:
            generation = session.scalar(select(Generation).where(Generation.status == "queued").order_by(Generation.created_at, Generation.id).limit(1))
        if generation is None:
            return
        generation.lease_owner = worker_id
        generation.lease_expires_at = time.time() + LEASE_SECONDS
        if generation.status == "submitting":
            generation.status = "processing" if generation.remote_task_id else "submission_unknown"
            generation.updated_at = time.time()
            if not generation.remote_task_id:
                generation.error_code = "WAN_SUBMISSION_UNKNOWN"
                generation.error_message = "服务在提交期间中断，原任务结果待核实，费用预留继续保留"
            session.commit()
        if generation.status == "submission_unknown" and not generation.remote_task_id:
            session.commit()
            return
        if generation.next_poll_at and generation.next_poll_at > time.time():
            session.commit()
            return
        generation_id = generation.id
        if generation.input_json.get("provider") == "local-ffmpeg":
            inputs = generation.input_json["localInputs"]
            generation.status = "processing"
            generation.started_at = generation.started_at or time.time()
            generation.updated_at = time.time()
            session.commit()
            action = "join"
        elif generation.status == "downloading":
            url = generation.usage_json.get("resultUrl")
            if not url:
                raise AppError(500, "GENERATION_RESULT_MISSING", "下载状态缺少原视频结果地址")
            session.commit()
            action = "download"
        else:
            try:
                client = _provider_client(session, generation, settings)
            except AppError as failure:
                if generation.status == "queued":
                    _finish_failure(session, generation, failure.code, failure.message, True)
                else:
                    generation.status = "query_interrupted"
                    generation.error_code = failure.code
                    generation.error_message = failure.message
                    generation.updated_at = time.time()
                session.commit()
                return
            frozen_input = generation.input_json
            if generation.status == "queued":
                session.commit()
                action = "submit"
            else:
                remote_task_id = generation.remote_task_id
                if not remote_task_id:
                    raise AppError(500, "GENERATION_REMOTE_ID_MISSING", "处理中的任务缺少远端任务标识")
                session.commit()
                action = "query"
    if action == "submit":
        await _submit(settings, factory, generation_id, worker_id, client, frozen_input)
    elif action == "query":
        await _query(settings, factory, generation_id, worker_id, client, remote_task_id)
    elif action == "download":
        await _download(settings, factory, generation_id, worker_id, url)
    else:
        await _join_generation(settings, factory, generation_id, worker_id, inputs)


def _acquire_lease(factory: sessionmaker, worker_id: str) -> bool:
    now = time.time()
    with factory() as session:
        result = session.execute(update(Budget).where(Budget.id == "global", or_(Budget.lease_owner == worker_id, Budget.lease_expires_at.is_(None), Budget.lease_expires_at <= now)).values(lease_owner=worker_id, lease_expires_at=now + LEASE_SECONDS))
        session.commit()
        return result.rowcount == 1


async def _wait_event(event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


async def _heartbeat(factory: sessionmaker, worker_id: str, completed: asyncio.Event, processing: asyncio.Task) -> None:
    while not await _wait_event(completed, LEASE_SECONDS / 4):
        now = time.time()
        with factory() as session:
            result = session.execute(update(Budget).where(Budget.id == "global", Budget.lease_owner == worker_id, Budget.lease_expires_at > now).values(lease_expires_at=now + LEASE_SECONDS))
            if result.rowcount != 1:
                session.rollback()
                processing.cancel()
                raise AppError(409, "WORKER_LEASE_LOST", "任务执行租约已失效，已停止当前处理")
            session.execute(update(Generation).where(Generation.lease_owner == worker_id, Generation.status.in_(ACTIVE_STATUSES)).values(lease_expires_at=now + LEASE_SECONDS))
            session.commit()


async def worker_loop(settings: Settings, factory: sessionmaker, stop_event: asyncio.Event) -> None:
    worker_id = str(uuid.uuid4())
    while not stop_event.is_set():
        if _acquire_lease(factory, worker_id):
            completed = asyncio.Event()
            processing = asyncio.create_task(_process_one(settings, factory, worker_id))
            heartbeat = asyncio.create_task(_heartbeat(factory, worker_id, completed, processing))
            try:
                await processing
            finally:
                completed.set()
                await heartbeat
                with factory() as session:
                    session.execute(update(Budget).where(Budget.id == "global", Budget.lease_owner == worker_id).values(lease_owner=None, lease_expires_at=None))
                    session.commit()
        await _wait_event(stop_event, min(settings.poll_interval, 15))
