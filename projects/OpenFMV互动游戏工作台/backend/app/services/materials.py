import hashlib
import time

from sqlalchemy import and_, func, inspect, or_, select

from app.agent_models import AgentRun, AgentStep
from app.agent_schemas import RunCreate
from app.core.errors import AppError
from app.material_models import MaterialConversation, MaterialMessage
from app.material_schemas import material_request_payload
from app.image_models import ImageAccount, ImageJob
from app.media_models import MediaSequence
from app.models import Canvas, Generation, Operation, ProviderSettings, TextProviderSettings
from app.services.projects import get_operation, iso_timestamp, lock_write, payload_digest, require_project, require_revision, require_scene, save_operation
from app.services.image_queue import public_job
from app.services.material_delivery import public_director_brief, public_material_error, public_node, public_run
from app.services.material_handoffs import adopted_material_inputs
from app.services.material_review import public_review
from app.services.material_references import validate_reference_assets


BUSY = {"queued", "running", "stopping", "waiting_connection", "waiting_confirmation", "conflict", "submission_unknown"}
PENDING_VIDEOS = {"awaiting_confirmation", "queued", "submitting", "submission_unknown", "processing", "downloading", "query_interrupted"}
RESUME_REQUESTS = {"继续", "继续吧", "请继续", "继续当前制作", "继续当前任务", "请继续当前任务", "恢复当前任务", "按当前版本继续"}
MATERIAL_INSTRUCTIONS = (
    "总编承接当前场景的素材需求，在制作简报中以自然语言复述目标与已确定事项。影响剧情、角色方向、画风或本轮交付范围的决策交给用户，等待答复后再安排相关岗位。"
    "总编职责限于需求沟通、派工与核对交接。制作简报供用户阅读，正文由编剧撰写，生图词、视频词及内部执行数据由对应岗位保存。"
    "按实际需要安排编剧、角色设计师、场景设计师、分镜师、视频师或声音设计师。引用某项成果修改时，由该成果原岗位修订，再核对引用它的下游成果。"
    "每个制作岗位使用write_artifact保存自己的专业成果。下游岗位通过工具读取并使用已采用的上游成果版本。"
    "编剧扩写剧本并交付明确角色定位与场景要求。角色设计师依据角色定位扩充外观、服装及连续性细节，场景设计师依据剧本明确空间布局与光向，两者分别为各自设计撰写生图提示词。"
    "分镜师基于剧本及采用的定妆、场景图设计镜头、动作、时长和边界帧，编写分镜生图提示词。视频师使用已经采用的真实首尾帧，交付运动与运镜要求、输出规格和生成确认。"
    "用户选中的references是本轮修订对象，包含准确节点与内容版本；先read_artifact，再使用patch_artifact或write_artifact保存完整有效的新版本。"
    "只安排本轮必要步骤。用户只要脚本就由编剧交付脚本；用户要求修改某一产物时只安排相关岗位。"
    "角色、场景和分镜的详细生图提示词保存在开发记录，由商汤sensenova-u1.5-fast生成真实图片，用户只看图片成果。"
    "每次write_artifact后执行器会暂停，向用户询问全部confirmationNeeded事项，并等待成果确认；不得自行答复或越过确认。"
    "humanAnswers是用户对准确成果版本的真实答复，必须执行其中修改要求，重新保存成果后等待再次确认。"
    "分镜使用schemaVersion=3：n段视频对应按时间排序的n+1个keyframes；每段firstFrameKey和lastFrameKey依次引用相邻边界帧。"
    "需要复用前段尾帧或共同分支入口时，existingAssetId只能填写referenceAssets中的实际图片标识，imagePrompt填写null；该帧不会重新生成。"
    "需要新画面时existingAssetId填写null，并提供独立完整imagePrompt；共享中间帧，保持人物、场景、动作连续。视频必须固定绑定同一组已保存首尾帧。"
    "视频生成仅在用户明确要求后准备真实任务，提交前由用户核对并确认；不向用户展示供应商报价或费用。"
    "排布、剧情编排、时间线剪辑与互动按钮由用户本人的Codex处理。这里只产出素材，不修改这些对象。"
    "执行记录只描述已发生的工具操作与结果。不要输出内部推理。"
    "materialNodes中的professionalRole为空时，该节点属于普通素材。普通文字正文通过read_project或retrieve_context读取；read_artifact只用于已有专业结构。"
    "每次只调用一个函数，收到实际结果后再调用下一个。公开简报使用用户能理解的制作目标与要求；节点标识、工具操作与完整创作提示词留在内部执行数据。"
    "本轮只使用本次需求、本轮已保存产物和用户明确选择的references；其他历史素材保留在项目中。"
    "history与context.conversationFollowups保留同一对话的用户原话。只将其中适用的背景与补充用于尚未确认的工作，已确认版本、费用与待核实的请求均不能由历史或跟进消息改变。"
    "referenceAssets是用户明确选择并冻结版本的项目参考图片，通过read_project读取；后续生图直接使用这些真实图片。不得声称已经观察到图片中未提供的细节。"
)


def summary(value):
    return {"id": value.id, "projectId": value.project_id, "sceneId": value.scene_id, "title": value.title,
            "createdAt": iso_timestamp(value.created_at), "updatedAt": iso_timestamp(value.updated_at)}


def require_material(session, project_id, scene_id, conversation_id, owner_id):
    project = require_project(session, project_id, owner_id)
    require_scene(project, scene_id)
    value = session.scalar(select(MaterialConversation).where(MaterialConversation.id == conversation_id,
        MaterialConversation.project_id == project_id, MaterialConversation.scene_id == scene_id, MaterialConversation.owner_id == owner_id))
    if value is None:
        raise AppError(404, "MATERIAL_CONVERSATION_NOT_FOUND", "素材对话不存在或无权访问")
    return value


def canvas_nodes(session, project_id, scene_id, owner_id):
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project_id, Canvas.scene_id == scene_id, Canvas.owner_id == owner_id))
    if canvas is None:
        raise AppError(404, "CANVAS_NOT_FOUND", "场景素材画布不存在")
    return canvas.data_json["nodes"]


def node_hash(node):
    return hashlib.sha256(str(node.get("metadata", {}).get("content", "")).encode()).hexdigest()


def capabilities(session, owner_id):
    text = session.get(TextProviderSettings, "platform")
    video = session.get(ProviderSettings, owner_id)
    accounts = session.scalars(select(ImageAccount).where(ImageAccount.owner_id == owner_id)).all() if inspect(session.get_bind()).has_table("image_accounts") else []
    return {"text": bool(text and text.api_key_encrypted), "textModel": "DeepSeek Flash", "image": len(accounts) == 3,
            "video": bool(video and video.api_key_encrypted), "videoModel": video.model if video else "wan3.0-video"}


def message_records(session, conversation):
    return session.execute(select(MaterialMessage, AgentRun, Operation.response_json)
        .join(AgentRun, AgentRun.id == MaterialMessage.run_id)
        .outerjoin(Operation, and_(Operation.owner_id == MaterialMessage.owner_id,
            Operation.project_id == AgentRun.project_id, Operation.operation_id == MaterialMessage.operation_id,
            Operation.kind == "materials.send")).where(
        MaterialMessage.conversation_id == conversation.id, MaterialMessage.owner_id == conversation.owner_id,
        AgentRun.owner_id == conversation.owner_id, AgentRun.project_id == conversation.project_id,
        AgentRun.scene_id == conversation.scene_id,
    ).order_by(MaterialMessage.sequence)).all()


def message_source(run):
    return run.limits_json.get("materialContext", {}).get("messageSource", {"kind": "user"})


def material_videos(session, run):
    generation_ids = {receipt["result"]["generation"]["id"]
        for step in session.scalars(select(AgentStep).where(AgentStep.run_id == run.id))
        for receipt in step.tool_results_json if receipt["tool"] == "submit_generation"}
    matches = [Generation.id.in_(generation_ids)]
    if inspect(session.get_bind()).has_table("media_sequences"):
        sequence_ids = select(MediaSequence.id).where(MediaSequence.project_id == run.project_id,
            MediaSequence.owner_id == run.owner_id, MediaSequence.scene_id == run.scene_id,
            MediaSequence.input_json["runId"].as_string() == run.id)
        matches.append(Generation.input_json["boundaryFrames"]["sequenceId"].as_string().in_(sequence_ids))
    return session.scalars(select(Generation).where(Generation.owner_id == run.owner_id,
        Generation.project_id == run.project_id, Generation.scene_id == run.scene_id,
        or_(*matches))
        .order_by(Generation.created_at, Generation.id)).all()


def can_start_production(run, videos):
    return run is None or (run.status not in BUSY and not any(video.status in PENDING_VIDEOS for video in current_video_attempts(videos)))


def current_video_attempts(videos):
    replaced = {video.input_json.get("request", {}).get("retryOf") for video in videos if video.status != "failed"}
    return [video for video in videos if video.id not in replaced]


def resume_request(content):
    return content.strip().rstrip("。！!").strip() in RESUME_REQUESTS


def legacy_continuation(session, item, run, receipt):
    review = (run.result_json or {}).get("materialClarification") or {}
    return (not (receipt or {}).get("kind") and message_source(run)["kind"] == "user" and resume_request(item.content)
        and run.status == "waiting_confirmation" and not run.plan_json and review.get("status") == "pending"
        and session.scalar(select(AgentStep.id).where(AgentStep.run_id == run.id).limit(1)) is None)


def active_production(session, conversation, records=None):
    if records is None:
        records = message_records(session, conversation)
    current = next(((item, run, receipt) for item, run, receipt in reversed(records)
        if run.id == conversation.active_run_id and (receipt or {}).get("kind") != "followup"), None)
    if current is None:
        return None, []
    item, run, receipt = current
    if legacy_continuation(session, item, run, receipt):
        checked = set()
        for prior, previous, previous_receipt in reversed(records):
            if prior.sequence >= item.sequence or (previous_receipt or {}).get("kind") == "followup" or previous.id in checked:
                continue
            checked.add(previous.id)
            videos = material_videos(session, previous)
            if any(video.status == "submission_unknown" for video in videos):
                return previous, videos
    return run, material_videos(session, run)


def followup_reply(run, videos, review):
    videos = current_video_attempts(videos)
    if unknown := next((video for video in videos if video.status == "submission_unknown"), None):
        reason = "已保留百炼任务编号，可继续查询原视频。" if unknown.remote_task_id else "平台尚未收到百炼任务编号，目前需要核实原请求。"
        return "已记录在当前对话。视频提交结果待核实，" + reason
    if any(video.status == "awaiting_confirmation" for video in videos):
        return "已记录在当前对话。视频仍在等待确认，请核对原卡片上的首尾帧和生成参数。"
    if any(video.status in {"queued", "submitting", "processing", "downloading"} for video in videos):
        return "已记录在当前对话。视频仍在排队或处理中，将沿用原任务。"
    if any(video.status == "query_interrupted" for video in videos):
        return "已记录在当前对话。视频的状态查询中断，可以继续查询原视频任务。"
    if review:
        return "已记录在当前对话。" + review["message"]
    statuses = {
        "waiting_connection": "当前制作在等待连接。",
        "waiting_confirmation": "当前制作仍在等待确认。",
        "submission_unknown": "上一次模型请求的结果待核实。",
        "failed": "上一轮制作未完成，已保留当时的成果和任务记录。",
        "stopped": "当前制作已停止，已保留原成果。",
        "stopping": "当前制作正在停止。",
        "conflict": "项目版本已有变化，原任务暂停等待核对。",
        "completed": "上一轮制作已完成，已保留采用的成果。",
    }
    return "已记录在当前对话。" + statuses.get(run.status, "当前制作仍在进行。")


def continue_from_followup(session, run, body, project, settings, videos, review):
    from app.services.agent import resume_run, run_view

    if (not resume_request(body.content) or body.references
            or body.reference_asset_ids or review or any(video.status in PENDING_VIDEOS for video in current_video_attempts(videos))):
        return None
    if run.status not in {"stopped", "conflict", "waiting_confirmation"}:
        if run.status != "failed" or not run_view(session, run, settings)["canResume"]:
            return None
    try:
        with session.begin_nested():
            resume_run(session, project.id, run.id, run.owner_id,
                f"material-resume-{payload_digest(body.operation_id)}", body.expected_revision, settings, commit=False)
    except AppError as problem:
        detail = public_material_error({"code": problem.code, "message": problem.message})
        return "已记录在当前对话。" + detail["message"]
    return "已恢复当前任务，将接着处理需要完成的步骤。"


def recent_messages(records):
    history = []
    remaining = 18000
    for item, run, receipt in reversed(records):
        kind = (receipt or {}).get("kind", "production")
        if kind != "followup" and message_source(run)["kind"] != "user":
            continue
        history.append({"kind": kind, "content": item.content[:remaining],
            "references": item.references_json, "referenceAssets": (receipt or {}).get("referenceAssets", [])})
        remaining -= len(history[-1]["content"])
        if remaining <= 0 or len(history) == 12:
            break
    return list(reversed(history))


def material_followups(session, run):
    conversation_id = run.limits_json.get("materialConversationId")
    if not conversation_id:
        return []
    conversation = session.get(MaterialConversation, conversation_id)
    if conversation is None or (conversation.owner_id, conversation.project_id, conversation.scene_id) != (run.owner_id, run.project_id, run.scene_id):
        return []
    return recent_messages([(item, source, receipt) for item, source, receipt in message_records(session, conversation)
        if source.id == run.id and (receipt or {}).get("kind") == "followup"])


def view(session, value):
    from app.services.agent import run_view

    project = require_project(session, value.project_id, value.owner_id)
    records = message_records(session, value)
    nodes = canvas_nodes(session, value.project_id, value.scene_id, value.owner_id)
    producers = {}
    video_results = {}
    current, current_videos = active_production(session, value, records)
    reviewed = current and current.limits_json.get("materialContext", {}).get("requireHumanReview")
    context = current.limits_json.get("materialContext", {}) if current else {}
    carried_ids = context.get("carriedNodeIds", [])
    jobs = session.scalars(select(ImageJob).where(ImageJob.project_id == value.project_id, ImageJob.scene_id == value.scene_id,
        ImageJob.owner_id == value.owner_id, or_(ImageJob.run_id == current.id, and_(ImageJob.node_id.in_(carried_ids),
        ImageJob.status == "ready"))).order_by(ImageJob.created_at, ImageJob.id)).all() if reviewed else []
    visible_ids = set([*carried_ids, *current.created_ids_json]) if current else set()
    for step in session.scalars(select(AgentStep).where(AgentStep.run_id == current.id)).all() if current else []:
        for receipt in step.tool_results_json:
            result = receipt.get("result", {})
            for node in result.get("nodeSnapshots", []):
                producers[node["nodeId"]] = step.role
                visible_ids.add(node["nodeId"])
            if receipt["tool"] == "submit_generation" and result.get("generation", {}).get("outputNodeId"):
                producers[result["generation"]["outputNodeId"]] = step.role
                visible_ids.add(result["generation"]["outputNodeId"])
                generation = session.get(Generation, result["generation"]["id"])
                if generation and generation.owner_id == value.owner_id and generation.project_id == value.project_id and generation.status == "ready" and generation.asset_id:
                    video_results[generation.input_json["outputNodeId"]] = generation.asset_id
    messages = []
    run_views = {}
    for item, run, receipt in records:
        receipt = receipt or {}
        if current and current.id != run.id and any(video.status == "submission_unknown" for video in current_videos) and legacy_continuation(session, item, run, receipt):
            run, receipt = current, {"kind": "followup", "reply": followup_reply(current, current_videos, None)}
        kind = receipt.get("kind", "production")
        source = {"kind": "user"} if kind == "followup" else message_source(run)
        content = source["summary"] if source["kind"] == "automation" else item.content
        if run.id not in run_views:
            run_views[run.id] = public_run(run_view(session, run))
        messages.append({"id": item.id, "source": source["kind"], "content": content, "references": item.references_json,
            "run": run_views[run.id], "createdAt": iso_timestamp(item.created_at), "kind": kind, "reply": receipt.get("reply"),
            "referenceAssets": receipt.get("referenceAssets") or (run.limits_json.get("materialContext", {}).get("referenceAssets", []) if kind == "production" else []),
            "directorBrief": public_director_brief(run.result_json) if kind == "production" else None})
    return {**summary(value), "revision": project.revision, "capabilities": capabilities(session, value.owner_id),
        "activeRunId": current.id if current else None,
        "canStartProduction": can_start_production(current, current_videos),
        "directorBrief": public_director_brief(current.result_json) if current else None,
        "selectedReferenceAssets": current.limits_json.get("materialContext", {}).get("referenceAssets", []) if current else [],
        "pendingReview": public_review(session, current) if current else None,
        "nodes": [public_node({**node, "metadata": {**node.get("metadata", {}), **({"assetId": video_results[node["id"]]} if node["id"] in video_results else {})}}, producers.get(node["id"]), node_hash(node),
            [public_job(job) for job in jobs if job.node_id == node["id"] and job.status != "cancelled" and job.artifact_hash == payload_digest(node.get("metadata", {}).get("creativeArtifact"))])
            for node in nodes if node["type"] != "agent" and node["id"] in visible_ids
            and not node.get("metadata", {}).get("materialVideoInput")
            and node.get("metadata", {}).get("creativeArtifact", {}).get("role") not in {"media_producer", "sound_designer"}],
        "messages": messages}


def create(session, project_id, scene_id, user, body):
    lock_write(session)
    project = require_project(session, project_id, user.id)
    require_scene(project, scene_id)
    payload = {"sceneId": scene_id, **body.model_dump(by_alias=True)}
    previous = get_operation(session, user.id, project_id, body.operation_id, "materials.create", payload)
    if previous:
        return require_material(session, project_id, scene_id, previous["conversationId"], user.id)
    canvas_nodes(session, project_id, scene_id, user.id)
    value = MaterialConversation(owner_id=user.id, project_id=project_id, scene_id=scene_id, title=body.title)
    session.add(value)
    session.flush()
    save_operation(session, user.id, project_id, body.operation_id, "materials.create", payload, {"conversationId": value.id})
    session.commit()
    return value


def send(session, project_id, scene_id, conversation_id, user, body, settings):
    from app.services.agent import create_run

    lock_write(session)
    conversation = require_material(session, project_id, scene_id, conversation_id, user.id)
    payload = {"conversationId": conversation_id, **material_request_payload(body)}
    if get_operation(session, user.id, project_id, body.operation_id, "materials.send", payload):
        return conversation
    records = message_records(session, conversation)
    active, videos = active_production(session, conversation, records)
    if conversation.active_run_id and active is None:
        raise AppError(409, "MATERIAL_CONVERSATION_RUN_INVALID", "当前对话找不到对应的素材任务")
    followup = body.intent == "followup" or (active is not None and body.intent is None)
    if followup and active is None:
        raise AppError(409, "MATERIAL_FOLLOWUP_NEEDS_PRODUCTION", "请先说明要制作的素材")
    if not followup and not can_start_production(active, videos):
        raise AppError(409, "MATERIAL_CONVERSATION_BUSY", "已有素材任务需要处理，请在原对话继续")
    project = require_project(session, project_id, user.id)
    if not followup or body.references or body.reference_asset_ids:
        require_revision(project, body.expected_revision)
    reference_assets = validate_reference_assets(session, project, body.reference_asset_ids, settings)
    nodes = {node["id"]: node for node in canvas_nodes(session, project_id, scene_id, user.id)}
    references = []
    if len({item.node_id for item in body.references}) != len(body.references):
        raise AppError(422, "MATERIAL_REFERENCE_DUPLICATE", "引用产物重复")
    for reference in body.references:
        node = nodes.get(reference.node_id)
        if node is None or node["type"] == "agent":
            raise AppError(422, "MATERIAL_REFERENCE_INVALID", "引用产物不属于当前场景")
        if node_hash(node) != reference.content_hash:
            raise AppError(409, "MATERIAL_REFERENCE_STALE", "引用产物已有更新，请重新选择当前版本")
        references.append({"nodeId": node["id"], "title": node.get("title", "素材"), "contentHash": reference.content_hash, "revision": project.revision})
    if followup:
        review = public_review(session, active)
        reply = continue_from_followup(session, active, body, project, settings, videos, review)
        conversation.active_run_id = active.id
        return save_message(session, conversation, body, payload, active, references,
            {"kind": "followup", "reply": reply or followup_reply(active, videos, review), "referenceAssets": reference_assets})

    history = recent_messages(records) if active else []
    carried = []
    if active:
        if not reference_assets:
            formerly_selected = next((item["referenceAssets"] for item in reversed(history)
                if item["kind"] == "followup" and item["referenceAssets"]),
                active.limits_json.get("materialContext", {}).get("referenceAssets", []))
            reference_assets = validate_reference_assets(session, project, [item["assetId"] for item in formerly_selected],
                settings, formerly_selected)
        canvas = session.scalar(select(Canvas).where(Canvas.project_id == project_id,
            Canvas.scene_id == scene_id, Canvas.owner_id == user.id))
        previous_ids = [*active.created_ids_json, *active.target_ids_json]
        carried = [source["nodeId"] for source in adopted_material_inputs(session, active, canvas, previous_ids)]
    selected = [item["nodeId"] for item in references]
    for identifier in carried:
        if identifier not in selected:
            node = nodes[identifier]
            references.append({"nodeId": identifier, "title": node.get("title", "素材"),
                "contentHash": node_hash(node), "revision": project.revision})
            selected.append(identifier)
    run = create_run(session, project_id, user, RunCreate(operation_id=f"material-{body.operation_id}", expected_revision=body.expected_revision,
        scene_id=scene_id, workspace="materials", role="director", executor="platform", delivery_mode="professional", goal=body.content,
        target_ids=selected, limits={"maxSteps": 24, "maxTokens": 32768, "maxCostMicros": 10000000}), settings, commit=False)
    run.limits_json = {**run.limits_json, "materialConversationId": conversation_id, "materialContext": {
        **run.limits_json["materialContext"], "instructions": MATERIAL_INSTRUCTIONS, "messageSource": {"kind": "user"},
        "requireHumanReview": True,
        "referenceAssets": reference_assets, "referenceAssetsRevision": project.revision,
        "references": references, "scope": "current_round", "readableNodeIds": selected, "carriedNodeIds": carried,
        "capabilities": capabilities(session, user.id), "history": history}}
    conversation.active_run_id = run.id
    return save_message(session, conversation, body, payload, run, references,
        {"kind": "production", "referenceAssets": reference_assets})


def save_message(session, conversation, body, payload, run, references, receipt):
    sequence = (session.scalar(select(func.max(MaterialMessage.sequence)).where(MaterialMessage.conversation_id == conversation.id)) or 0) + 1
    session.add(MaterialMessage(owner_id=conversation.owner_id, conversation_id=conversation.id, operation_id=body.operation_id, sequence=sequence,
        content=body.content, references_json=references, run_id=run.id))
    conversation.updated_at = time.time()
    if sequence == 1:
        conversation.title = body.content.strip()[:60]
    save_operation(session, conversation.owner_id, conversation.project_id, body.operation_id, "materials.send", payload,
        {"runId": run.id, **receipt})
    session.commit()
    return conversation
