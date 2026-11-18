import hashlib
import time

from sqlalchemy import func, inspect, select

from app.agent_models import AgentRun, AgentStep
from app.agent_schemas import RunCreate
from app.core.errors import AppError
from app.material_models import MaterialConversation, MaterialMessage
from app.material_schemas import material_request_payload
from app.image_models import ImageAccount, ImageJob
from app.models import Canvas, Generation, ProviderSettings, TextProviderSettings
from app.services.projects import get_operation, iso_timestamp, lock_write, payload_digest, require_project, require_revision, require_scene, save_operation
from app.services.image_queue import public_job
from app.services.material_delivery import public_node, public_run
from app.services.material_review import public_review
from app.services.material_references import validate_reference_assets
from app.tools.project_tools import REUSABLE_BOUNDARY_TOOL_CATALOG_VERSION


BUSY = {"queued", "running", "stopping", "waiting_connection", "waiting_confirmation", "conflict", "submission_unknown"}
MATERIAL_INSTRUCTIONS = (
    "你负责当前场景的素材制作对话。总监在plan中梳理本轮需求并安排岗位；仅在用户明确要求制作简报时，安排director产出简报。"
    "按实际需要安排编剧、角色设计师、场景设计师、分镜师、视频师或声音设计师。引用已有产物改稿时，直接安排对应岗位修订和核对。"
    "每个岗位只完成自己的专业产物，使用write_artifact保存；后续岗位必须读取前面已经保存的真实产物，保持角色、场景与镜头的一致性。"
    "用户选中的references是本轮修订对象，包含准确节点与内容版本；先read_artifact，再使用patch_artifact或write_artifact保存完整有效的新版本。"
    "只安排本轮必要步骤。用户只要脚本就由编剧交付脚本；用户要求修改某一产物时只安排相关岗位。"
    "需求缺少决定性信息时，总监把已确认要求、待补充问题写入自己的专业产物，等待用户回答。"
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
    "每次只调用一个函数，收到实际结果后再调用下一个。计划title使用用户能理解的制作目标；节点标识和工具操作只写入后台instruction。"
    "本轮只使用本次需求、本轮已保存产物和用户明确选择的references；其他历史素材保留在项目中。"
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


def view(session, value):
    from app.services.agent import run_view

    project = require_project(session, value.project_id, value.owner_id)
    messages = session.scalars(select(MaterialMessage).where(MaterialMessage.conversation_id == value.id,
        MaterialMessage.owner_id == value.owner_id).order_by(MaterialMessage.sequence)).all()
    nodes = canvas_nodes(session, value.project_id, value.scene_id, value.owner_id)
    producers = {}
    video_results = {}
    current = session.get(AgentRun, messages[-1].run_id) if messages else None
    reviewed = current and current.limits_json.get("materialContext", {}).get("requireHumanReview")
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == current.id).order_by(ImageJob.created_at, ImageJob.id)).all() if reviewed else []
    visible_ids = set(current.created_ids_json) if current else set()
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
    return {**summary(value), "revision": project.revision, "capabilities": capabilities(session, value.owner_id),
        "selectedReferenceAssets": current.limits_json.get("materialContext", {}).get("referenceAssets", []) if current else [],
        "pendingReview": public_review(session, current) if current else None,
        "nodes": [public_node({**node, "metadata": {**node.get("metadata", {}), **({"assetId": video_results[node["id"]]} if node["id"] in video_results else {})}}, producers.get(node["id"]), node_hash(node),
            [public_job(job) for job in jobs if job.node_id == node["id"] and job.status != "cancelled" and job.artifact_hash == payload_digest(node.get("metadata", {}).get("creativeArtifact"))])
            for node in nodes if node["type"] != "agent" and node["id"] in visible_ids and node.get("metadata", {}).get("creativeArtifact", {}).get("role") not in {"media_producer", "sound_designer"}],
        "messages": [{"id": item.id, "content": item.content, "references": item.references_json,
            "run": public_run(run_view(session, session.get(AgentRun, item.run_id))), "createdAt": iso_timestamp(item.created_at)} for item in messages]}


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
    active = session.get(AgentRun, conversation.active_run_id) if conversation.active_run_id else None
    if active and active.status in BUSY:
        raise AppError(409, "MATERIAL_CONVERSATION_BUSY", "当前素材任务尚未结束，请等待完成或停止后继续")
    project = require_project(session, project_id, user.id)
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
    run = create_run(session, project_id, user, RunCreate(operation_id=f"material-{body.operation_id}", expected_revision=body.expected_revision,
        scene_id=scene_id, workspace="materials", role="director", executor="platform", delivery_mode="professional", goal=body.content,
        target_ids=[item["nodeId"] for item in references], limits={"maxSteps": 24, "maxTokens": 32768, "maxCostMicros": 10000000}), settings, commit=False)
    run.limits_json = {**run.limits_json, "toolCatalogVersion": REUSABLE_BOUNDARY_TOOL_CATALOG_VERSION, "materialConversationId": conversation_id, "materialContext": {"instructions": MATERIAL_INSTRUCTIONS,
        "requireHumanReview": True,
        "referenceAssets": reference_assets, "referenceAssetsRevision": project.revision,
        "references": references, "scope": "current_round", "readableNodeIds": [item["nodeId"] for item in references],
        "capabilities": capabilities(session, user.id), "history": []}}
    sequence = (session.scalar(select(func.max(MaterialMessage.sequence)).where(MaterialMessage.conversation_id == conversation_id)) or 0) + 1
    session.add(MaterialMessage(owner_id=user.id, conversation_id=conversation_id, operation_id=body.operation_id, sequence=sequence,
        content=body.content, references_json=references, run_id=run.id))
    conversation.active_run_id, conversation.updated_at = run.id, time.time()
    if sequence == 1:
        conversation.title = body.content[:60]
    save_operation(session, user.id, project_id, body.operation_id, "materials.send", payload, {"runId": run.id})
    session.commit()
    return conversation
