import time
from fractions import Fraction

from sqlalchemy import select

from app.agent_models import AgentRun, AgentStep
from app.core.errors import AppError
from app.creative_artifacts import image_prompt_items
from app.image_models import ImageAccount, ImageJob
from app.material_schemas import material_request_payload
from app.models import Generation, new_id
from app.services.projects import payload_digest, require_project, require_revision
from app.services.material_references import frozen_reference_assets, validate_reference_assets


VISUAL_ROLES = {"character_designer", "scene_designer", "storyboarder"}


def require_video_delivery(session, run, step, review):
    from app.services.materials import canvas_nodes
    node = next(item for item in canvas_nodes(session, run.project_id, run.scene_id, run.owner_id) if item["id"] == review["nodeId"])
    expected = {shot["shotKey"] for shot in node["metadata"]["creativeArtifact"]["shots"]}
    delivered = set()
    for receipt in step.tool_results_json:
        if receipt["tool"] != "submit_generation":
            continue
        generation = session.get(Generation, receipt["result"]["generation"]["id"])
        if generation and generation.owner_id == run.owner_id and generation.project_id == run.project_id and generation.status == "ready" and generation.asset_id:
            boundary = generation.input_json.get("boundaryFrames", {})
            if boundary.get("runId") == run.id and len(boundary.get("frames", [])) == 2:
                delivered.add(boundary["shotKey"])
    if not expected <= delivered:
        raise AppError(409, "MATERIAL_VIDEO_NOT_READY", "请先完成已确认首尾帧对应的真实视频生成")


def supported_dimensions(spec):
    ratio = Fraction(spec["width"], spec["height"])
    factor = 2048 // (32 * max(ratio.numerator, ratio.denominator))
    width, height = 32 * ratio.numerator * factor, 32 * ratio.denominator * factor
    if min(width, height) < 512:
        raise AppError(422, "IMAGE_SIZE_UNSUPPORTED", "当前画幅无法生成有效商汤尺寸")
    return width, height


def pause_for_review(session, run, step, node, artifact, settings=None):
    if step is None:
        raise AppError(422, "MATERIAL_STEP_REQUIRED", "素材交付必须属于明确岗位")
    previous = (run.result_json or {}).get("materialReview")
    if previous and previous["status"] in {"pending", "generating", "review_images"}:
        raise AppError(409, "MATERIAL_CONFIRMATION_REQUIRED", "请先回答当前待确认事项")
    artifact_hash = payload_digest(artifact)
    questions = [{"id": new_id(), "question": item["topic"], "suggestedAnswer": item["proposedValue"]}
        for item in artifact["assumptions"] if item["confirmationNeeded"]]
    if artifact["role"] == "director":
        questions.extend({"id": new_id(), "question": text, "suggestedAnswer": "确认"} for text in artifact["approvalGates"])
    jobs = []
    if artifact["role"] in VISUAL_ROLES:
        selected_references = frozen_reference_assets(session, run, settings)
        if artifact["role"] == "storyboarder" and artifact.get("schemaVersion") != 2:
            raise AppError(422, "STORYBOARD_BOUNDARIES_REQUIRED", "请按连续首尾帧结构保存分镜")
        accounts = session.scalars(select(ImageAccount).where(ImageAccount.owner_id == run.owner_id)).all()
        if len(accounts) != 3:
            raise AppError(409, "IMAGE_ACCOUNTS_REQUIRED", "请配置三个商汤图片账号")
        prior = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id, ImageJob.status == "ready").order_by(ImageJob.created_at)).all()
        reviews = [*(run.result_json or {}).get("materialReviewHistory", []), previous or {}]
        approved = {(item.get("nodeId"), item.get("artifactHash")) for item in reviews if item.get("status") == "approved"}
        source_ids = {item["referenceId"] for item in artifact["sources"] if item["kind"] in {"artifact", "canvas_node"}}
        prior = [item for item in prior if (item.node_id, item.artifact_hash) in approved and item.node_id in source_ids]
        representatives = list({item.node_id: item for item in reversed(prior)}.values())
        selected_ids = [item["assetId"] for item in selected_references]
        prior_anchors = list(dict.fromkeys(item.asset_id for item in [*representatives, *prior] if item.asset_id))[:4]
        specifications = []
        for index, item in enumerate(image_prompt_items(artifact)):
            spec = item["outputSpec"]
            width, height = supported_dimensions(spec)
            if artifact["role"] == "storyboarder" and spec["imageCount"] != 1:
                raise AppError(422, "STORYBOARD_SINGLE_FRAME_REQUIRED", "每个边界帧必须生成一张独立图片")
            for variant in range(spec["imageCount"]):
                specifications.append((index, item, spec, width, height, variant))
        previous_frame_slots = 1 if artifact["role"] == "storyboarder" and len(specifications) > 1 else 0
        if len(selected_ids) + previous_frame_slots > 5:
            raise AppError(422, "MATERIAL_REFERENCE_CAPACITY_EXCEEDED",
                f"已选择{len(selected_ids)}张参考图片，连续分镜还需要{previous_frame_slots}张上一画面作为参考，共{len(selected_ids) + previous_frame_slots}张，超过单次5张上限；请在新素材对话中选择至多{5 - previous_frame_slots}张参考图片后继续")
        anchor_limit = max(len(selected_ids), min(4, 5 - previous_frame_slots))
        anchors = list(dict.fromkeys([*selected_ids, *prior_anchors]))[:anchor_limit]
        for index, item, spec, width, height, variant in specifications:
            job = ImageJob(id=new_id(), owner_id=run.owner_id, project_id=run.project_id, scene_id=run.scene_id,
                run_id=run.id, node_id=node["id"], artifact_hash=artifact_hash,
                operation_id=f"{run.id}:{artifact_hash}:{index}:{variant}", label=item["label"] + (f" · {variant + 1}" if spec["imageCount"] > 1 else ""),
                frame_key=artifact["keyframes"][index]["key"] if artifact["role"] == "storyboarder" else None,
                status="awaiting_confirmation", request_json={"prompt": item["prompt"], "width": width, "height": height,
                    "referenceAssets": selected_references},
                reference_asset_ids=anchors, dependency_ids=[jobs[-1].id] if artifact["role"] == "storyboarder" and jobs else [])
            session.add(job)
            jobs.append(job)
        session.flush()
    review = {"id": new_id(), "stepId": step.id, "nodeId": node["id"], "artifactHash": artifact_hash,
        "status": "pending", "questions": questions, "imageJobIds": [job.id for job in jobs],
        "createdAt": time.time(), "answers": {}, "role": artifact["role"]}
    if artifact["role"] == "media_producer" and not questions:
        review["status"] = "not_required"
    history = [*(run.result_json or {}).get("materialReviewHistory", [])]
    if previous:
        history.append(previous)
    run.result_json = {**(run.result_json or {}), "materialReview": review, "materialReviewHistory": history}
    if review["status"] == "pending":
        run.status, step.status = "waiting_confirmation", "waiting_confirmation"


def public_review(session, run):
    review = (run.result_json or {}).get("materialReview")
    if not review or review["status"] in {"approved", "revision_requested", "not_required"}:
        return None
    jobs = [session.get(ImageJob, identifier) for identifier in review["imageJobIds"]]
    status = review["status"]
    if status == "generating" and jobs and all(job.status == "ready" for job in jobs):
        status = "review_images"
    unanswered = [item for item in review["questions"] if item["id"] not in review.get("answers", {})] if status == "pending" else []
    return {"id": review["id"], "nodeId": review["nodeId"], "status": status,
        "questions": unanswered[:5], "remainingQuestions": len(unanswered),
        "imageCount": len(jobs), "images": [{"label": job.label, "width": job.request_json["width"], "height": job.request_json["height"]} for job in jobs],
        "canConfirm": status in {"pending", "review_images"},
        "message": "请回答视频制作前的问题；提交视频时还会核对首尾画面和参数。" if status == "pending" and review.get("role") == "media_producer" else "请核对以下事项，并确认当前成果。" if status == "pending" else "图片生成中，完成后请确认图片成果。" if status == "generating" else "图片已生成，请查看画布并确认后继续。"}


def answer_review(session, conversation, body, settings=None):
    run = session.get(AgentRun, conversation.active_run_id)
    if run is None or run.owner_id != conversation.owner_id:
        raise AppError(404, "MATERIAL_REVIEW_NOT_FOUND", "待确认事项不存在")
    review = (run.result_json or {}).get("materialReview")
    if not review or review["id"] != body.review_id:
        raise AppError(409, "MATERIAL_REVIEW_STALE", "待确认事项已经更新，请重新读取")
    if review.get("operationId") == body.operation_id:
        if review.get("answerPayload") != material_request_payload(body):
            raise AppError(409, "OPERATION_CONFLICT", "确认请求内容已经变化")
        return
    if run.status != "waiting_confirmation":
        raise AppError(409, "MATERIAL_REVIEW_STATE", "当前任务没有等待这次确认")
    project = require_project(session, run.project_id, run.owner_id)
    require_revision(project, body.expected_revision)
    context = run.limits_json["materialContext"]
    frozen = context.get("referenceAssets", [])
    previous_ids = [item["assetId"] for item in frozen]
    added_ids = [identifier for identifier in body.reference_asset_ids if identifier not in previous_ids]
    if added_ids and not body.requested_changes.strip():
        raise AppError(422, "MATERIAL_REFERENCE_CHANGES_REQUIRED", "新增参考图片后，请填写成果修改要求，当前岗位需要重新制作成果后再次确认")
    reference_assets = validate_reference_assets(session, project, [*previous_ids, *added_ids], settings, frozen)
    from app.services.materials import canvas_nodes
    node = next((item for item in canvas_nodes(session, run.project_id, run.scene_id, run.owner_id) if item["id"] == review["nodeId"]), None)
    if node is None or payload_digest(node.get("metadata", {}).get("creativeArtifact")) != review["artifactHash"]:
        raise AppError(409, "MATERIAL_REVIEW_STALE", "成果内容已经变化，请重新生成确认事项")
    public = public_review(session, run)
    if not public["canConfirm"]:
        raise AppError(409, "MATERIAL_IMAGES_PENDING", "请等待图片真实生成完成")
    expected = {item["id"] for item in public["questions"]}
    if set(body.answers) != expected or any(not value.strip() for value in body.answers.values()):
        raise AppError(422, "MATERIAL_ANSWERS_REQUIRED", "请逐项回答全部待确认问题")
    review = {**review, "answers": {**review.get("answers", {}), **body.answers}, "operationId": body.operation_id,
        "answerPayload": material_request_payload(body), "answeredAt": time.time()}
    changed = bool(body.requested_changes.strip()) or any(body.answers[item["id"]].strip() != item["suggestedAnswer"].strip() for item in public["questions"])
    answer_record = {"reviewId": review["id"], "nodeId": review["nodeId"], "artifactHash": review["artifactHash"],
        "questions": review["questions"], "answers": body.answers, "requestedChanges": body.requested_changes,
        "referenceAssets": reference_assets, "addedReferenceAssetIds": added_ids,
        "stepId": review["stepId"], "operationId": body.operation_id, "answeredAt": review["answeredAt"], "requiresRevision": changed}
    material_context = {**context, "referenceAssets": reference_assets,
        "referenceAssetsRevision": project.revision, "humanAnswers": [*context.get("humanAnswers", []), answer_record]}
    if changed:
        material_context["revisionRequest"] = {"reviewId": review["id"], "stepId": review["stepId"], "nodeId": review["nodeId"],
            "artifactHash": review["artifactHash"], "operationId": body.operation_id, "requestedAt": review["answeredAt"],
            "answers": review["answers"], "requestedChanges": body.requested_changes}
    run.limits_json = {**run.limits_json, "materialContext": material_context}
    step = session.get(AgentStep, review["stepId"])
    if changed:
        review["status"] = "revision_requested"
        for identifier in review["imageJobIds"]:
            job = session.get(ImageJob, identifier)
            if job.status == "awaiting_confirmation":
                job.status = "cancelled"
        run.status, step.status = "queued", "queued"
        step.result_json = None
        run.result_json = {key: value for key, value in run.result_json.items()
            if key not in {"nativeCorrection", "toolError", "manualNativeRepairOf", "manualRepairOperationId"}}
    elif public["status"] == "pending" and any(item["id"] not in review["answers"] for item in review["questions"]):
        review["status"] = "pending"
    elif public["status"] == "pending" and review["imageJobIds"]:
        for identifier in review["imageJobIds"]:
            job = session.get(ImageJob, identifier)
            if job.status != "awaiting_confirmation":
                raise AppError(409, "IMAGE_CONFIRMATION_STATE", "图片任务已发生变化")
            job.status, job.confirmed_at = "queued", time.time()
        review["status"] = "generating"
    else:
        review["status"], run.status, step.status = "approved", "queued", "queued"
    run.project_revision, run.updated_at = project.revision, time.time()
    run.result_json = {**run.result_json, "materialReview": review}
    conversation.updated_at = time.time()
