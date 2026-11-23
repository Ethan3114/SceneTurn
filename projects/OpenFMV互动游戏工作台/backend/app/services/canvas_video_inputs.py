import base64
import hashlib
import io
import math
import os
import time
from fractions import Fraction

import httpx
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select

from app.core.errors import AppError
from app.models import Asset, Generation
from app.services.media import probe_media, resolve_asset_path
from app.services.wan import MAX_REFERENCE_IMAGES, VIDEO_INPUT_MODES


FRAME_TYPES = {"image/jpeg", "image/png", "image/bmp", "image/webp"}
WAN_RESULTS_HOST = "dashscope-result-bj.oss-cn-beijing.aliyuncs.com"
RESULT_LINK_LIFETIME = 24 * 60 * 60
RESULT_LINK_MARGIN = 5 * 60


def requested_media(request):
    first = request.get("firstFrameNodeId")
    last = request.get("lastFrameNodeId")
    video = request.get("referenceVideoNodeId")
    images = request.get("referenceImageNodeIds") or []
    mode = request.get("videoInputMode")
    has_reference = bool(video or images)
    if mode == "text":
        if first or last or has_reference:
            raise AppError(422, "VIDEO_MEDIA_COMBINATION_INVALID", "纯文字视频使用提示词与连接的文字节点；请为选中的媒体设置用途")
        return []
    if last and not first:
        raise AppError(422, "VIDEO_FIRST_FRAME_REQUIRED", "选择固定尾帧前须先选择固定首帧")
    if len(images) > MAX_REFERENCE_IMAGES or len(set(images)) != len(images):
        raise AppError(422, "VIDEO_REFERENCE_IMAGES_INVALID", f"请选择最多{MAX_REFERENCE_IMAGES}个不同的参考图片节点")
    if (mode is None and has_reference) or (mode is not None and (mode not in VIDEO_INPUT_MODES or first)):
        raise AppError(422, "VIDEO_MEDIA_COMBINATION_INVALID", "请选择固定帧或参考素材，并指定参考素材的用途")
    if mode in {"edit", "extend"} and not video:
        raise AppError(422, "VIDEO_REFERENCE_VIDEO_REQUIRED", "编辑或延长视频须选择原视频，可同时选择参考图片")
    if mode == "reference" and not has_reference:
        raise AppError(422, "VIDEO_REFERENCE_MEDIA_REQUIRED", "参考生成至少需要一张参考图片或一段参考视频")
    media = [(kind, node_id) for kind, node_id in (("first_frame", first), ("last_frame", last), ("reference_video", video)) if node_id]
    media.extend(("reference_image", node_id) for node_id in images)
    if media and request.get("boundaryFrames"):
        raise AppError(422, "VIDEO_MEDIA_COMBINATION_INVALID", "当前镜头已有固定的分镜帧来源")
    return media


def _frame_bytes(asset, settings, media_type):
    label = "参考图片" if media_type == "reference_image" else "固定帧图片"
    path = resolve_asset_path(settings, asset.relpath)
    if asset.mime_type not in FRAME_TYPES or not path.is_file() or path.stat().st_size != asset.bytes or asset.bytes > 20 * 1024 * 1024:
        raise AppError(422, "VIDEO_FRAME_FORMAT_INVALID", f"{label}须使用项目中不超过20MB的不透明图片")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != asset.sha256:
        raise AppError(409, "VIDEO_FRAME_CHANGED", f"{label}已经变化，请重新选择")
    try:
        with Image.open(io.BytesIO(data)) as image:
            shorter, longer = sorted(image.size)
            if image.mode not in {"RGB", "L"} or "transparency" in image.info or Image.MIME.get(image.format) != asset.mime_type or shorter < 240 or longer > 8000 or longer > shorter * 8:
                raise AppError(422, "VIDEO_FRAME_FORMAT_INVALID", f"{label}须是不透明图片，边长在240至8000像素内，长边不超过短边八倍")
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as error:
        raise AppError(422, "VIDEO_FRAME_FORMAT_INVALID", f"{label}无法读取") from error
    return data


def uses_legacy_receipt(frozen_input):
    return any("receiptSha256" in item for item in frozen_input.get("canvasMedia", []))


def _legacy_reference_video(session, asset, settings, workspace_id):
    source = session.get(Generation, asset.generation_id) if asset.generation_id else None
    if (source is None or (source.owner_id, source.project_id, source.asset_id) != (asset.owner_id, asset.project_id, asset.id)
            or source.status != "ready" or source.input_json.get("provider") != "aliyun-wan"
            or source.input_json.get("workspaceId") != workspace_id or not source.remote_task_id):
        raise AppError(422, "VIDEO_REFERENCE_PROVIDER_REQUIRED", "请选择当前项目中由同一百炼业务空间生成的近期 Wan 视频")
    url = source.usage_json.get("resultUrl")
    issued = min((value for value in (source.usage_json.get("resultUrlIssuedAt"), source.started_at) if value is not None), default=None)
    if not url or issued is None or time.time() + RESULT_LINK_MARGIN >= issued + RESULT_LINK_LIFETIME:
        raise AppError(422, "VIDEO_REFERENCE_LINK_EXPIRED", "原视频的百炼结果链接已过期，请选择近期生成的视频")
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as error:
        raise AppError(422, "VIDEO_REFERENCE_LINK_INVALID", "百炼原视频地址未通过校验") from error
    if (parsed.scheme, parsed.host) != ("https", WAN_RESULTS_HOST) or parsed.port not in (None, 443) or parsed.userinfo or parsed.fragment or not parsed.path.endswith(".mp4"):
        raise AppError(422, "VIDEO_REFERENCE_LINK_INVALID", "百炼原视频地址未通过校验")
    if asset.mime_type != "video/mp4" or not asset.duration_ms or not asset.width or not asset.height or asset.bytes > 100 * 1024 * 1024:
        raise AppError(422, "VIDEO_REFERENCE_FORMAT_INVALID", "参考须使用大小不超过100MB的 MP4 视频")
    shorter, longer = sorted((asset.width, asset.height))
    if shorter < 240 or longer > 4096 or longer > shorter * 8 or source.usage_json.get("fps", 0) < 16:
        raise AppError(422, "VIDEO_REFERENCE_FORMAT_INVALID", "参考视频的尺寸、比例或帧率超出百炼限制")
    duration = max(asset.duration_ms, math.ceil(source.usage_json.get("output_video_duration", 0) * 1000))
    if not 1000 <= duration <= 15000:
        raise AppError(422, "VIDEO_REFERENCE_DURATION_INVALID", "参考视频须为1至15秒")
    path = resolve_asset_path(settings, asset.relpath)
    if not path.is_file() or path.stat().st_size != asset.bytes:
        raise AppError(409, "VIDEO_REFERENCE_CHANGED", "参考视频文件已变化，请重新选择")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != asset.sha256:
            raise AppError(409, "VIDEO_REFERENCE_CHANGED", "参考视频内容已变化，请重新选择")
    return url, duration


def _local_reference_video(asset, settings):
    if asset.mime_type != "video/mp4" or not 0 < asset.bytes <= 100 * 1024 * 1024:
        raise AppError(422, "VIDEO_REFERENCE_FORMAT_INVALID", "参考视频须使用不超过100MB的项目内MP4文件")
    try:
        path = resolve_asset_path(settings, asset.relpath)
        with path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size != asset.bytes or hashlib.file_digest(stream, "sha256").hexdigest() != asset.sha256:
                raise AppError(409, "VIDEO_REFERENCE_CHANGED", "参考视频文件已变化，请重新选择")
    except (AppError, FileNotFoundError) as error:
        if isinstance(error, AppError) and error.code != "ASSET_FILE_NOT_FOUND":
            raise
        raise AppError(409, "VIDEO_REFERENCE_CHANGED", "参考视频文件已移走，请重新导入或选择其他视频") from error
    probe = probe_media(path)
    video = next((stream for stream in probe.streams if stream.codec_type == "video"), None)
    if video is None or "mp4" not in probe.format.format_name.split(","):
        raise AppError(422, "VIDEO_REFERENCE_FORMAT_INVALID", "参考须使用包含视频画面的MP4文件")
    shorter, longer = sorted((video.width, video.height))
    rate = Fraction(video.avg_frame_rate) if not video.avg_frame_rate.endswith("/0") else 0
    if shorter < 240 or longer > 4096 or longer > shorter * 8 or rate < 16:
        raise AppError(422, "VIDEO_REFERENCE_FORMAT_INVALID", "参考视频的边长须在240至4096像素内，比例不大于8:1，帧率至少16")
    exact_duration = max(probe.format.duration or 0, video.duration or 0)
    if not 1 <= exact_duration <= 15:
        raise AppError(422, "VIDEO_REFERENCE_DURATION_INVALID", "参考视频须为1至15秒")
    return path, math.ceil(exact_duration * 1000)


def freeze_media(session, project, resources, requested, output_duration, workspace_id, settings, *, legacy_receipt=False):
    by_node = {item["nodeId"]: item for item in resources}
    frozen = []
    for media_type, node_id in requested:
        resource = by_node.get(node_id)
        required_kind = "video" if media_type == "reference_video" else "image"
        asset_id = (resource or {}).get("storageKey") or (resource or {}).get("assetId")
        asset = session.scalar(select(Asset).where(Asset.id == asset_id, Asset.owner_id == project.owner_id, Asset.project_id == project.id))
        if resource is None or resource["type"] != required_kind or asset is None:
            raise AppError(422, "VIDEO_MEDIA_INPUT_NOT_READY", "请选择通过连线接入并已保存在项目内的对应素材节点")
        item = {"type": media_type, "nodeId": node_id, "assetId": asset.id, "sha256": asset.sha256}
        if required_kind == "image":
            _frame_bytes(asset, settings, media_type)
        else:
            if legacy_receipt:
                url, duration = _legacy_reference_video(session, asset, settings, workspace_id)
                item["receiptSha256"] = hashlib.sha256(url.encode()).hexdigest()
            else:
                _, duration = _local_reference_video(asset, settings)
            item["durationMs"] = duration
            if math.ceil(duration / 1000) + output_duration > 30:
                raise AppError(422, "VIDEO_REFERENCE_DURATION_INVALID", "参考与输出视频的总时长不得超过30秒")
        frozen.append(item)
    return frozen


def media_payload(session, generation, settings):
    payload = []
    for item in generation.input_json["canvasMedia"]:
        asset = session.get(Asset, item["assetId"])
        if asset is None or (asset.owner_id, asset.project_id, asset.sha256) != (generation.owner_id, generation.project_id, item["sha256"]):
            raise AppError(409, "VIDEO_MEDIA_INPUT_CHANGED", "已确认的画布素材发生变化")
        if item["type"] == "reference_video":
            if "receiptSha256" in item:
                url, duration = _legacy_reference_video(session, asset, settings, generation.input_json["workspaceId"])
                if hashlib.sha256(url.encode()).hexdigest() != item["receiptSha256"]:
                    raise AppError(409, "VIDEO_MEDIA_INPUT_CHANGED", "原视频回执已经变化，请重新确认")
                source = {"url": url}
            else:
                path, duration = _local_reference_video(asset, settings)
                source = {"path": path}
            if duration != item["durationMs"]:
                raise AppError(409, "VIDEO_MEDIA_INPUT_CHANGED", "原视频的时长已经变化，请重新确认")
        else:
            data = _frame_bytes(asset, settings, item["type"])
            source = {"url": f"data:{asset.mime_type};base64,{base64.b64encode(data).decode()}"}
        payload.append({"type": item["type"], **source})
    return payload
