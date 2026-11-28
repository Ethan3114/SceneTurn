import time
from fractions import Fraction

from sqlalchemy import select

from app.agent_models import AgentStep
from app.core.errors import AppError
from app.creative_artifacts import image_prompt_items
from app.image_models import ImageAccount, ImageJob
from app.material_schemas import material_request_payload
from app.models import Generation, new_id
from app.services.connections import require_image_connection
from app.services.image_queue import IMAGE_REQUEST_GAP_SECONDS, image_queue_awaits_result, uses_codex_image
from app.services.projects import payload_digest, require_project, require_revision
from app.services.material_references import frozen_reference_assets, reusable_boundary_frames, validate_reference_assets
from app.services.providers import LEGACY_DEFAULTS, freeze_selection


VISUAL_ROLES = {"character_designer", "scene_designer", "storyboarder"}


def image_reference_groups(artifact):
    if artifact["role"] == "character_designer":
        return [subject["key"] for subject in artifact["characters"] for _ in subject["imagePrompts"]]
    if artifact["role"] == "scene_designer":
        return [scene["key"] for scene in artifact["scenes"] for _ in scene["imagePrompts"]]
    return [frame["key"] for frame in artifact.get("keyframes", []) if frame.get("existingAssetId") is None]


def pause_for_clarification(run, questions):
    result = run.result_json or {}
    gate = {"id": new_id(), "status": "pending", "createdAt": time.time(), "questions": [
        {"id": new_id(), **question} for question in questions]}
    history = result.get("materialClarificationHistory", [])
    previous = result.get("materialClarification")
    run.result_json = {**result, "materialClarification": gate,
                       "materialClarificationHistory": [*history, *([previous] if previous else [])]}
    run.status = "waiting_confirmation"
    return gate["id"]


def require_video_delivery(session, run, step, review):
    from app.services.materials import canvas_nodes
    node = next(item for item in canvas_nodes(session, run.project_id, run.scene_id, run.owner_id) if item["id"] == review["nodeId"])
    artifact = node["metadata"]["creativeArtifact"]
    strict_frames = artifact.get("schemaVersion") == 2
    expected = {(shot["storyboardNodeId"], shot["shotKey"], shot["firstFrameAssetId"], shot["lastFrameAssetId"])
                if strict_frames else shot["shotKey"] for shot in artifact["shots"]}
    delivered = set()
    for receipt in step.tool_results_json:
        if receipt["tool"] != "submit_generation":
            continue
        generation = session.get(Generation, receipt["result"]["generation"]["id"])
        if generation and generation.owner_id == run.owner_id and generation.project_id == run.project_id and generation.status == "ready" and generation.asset_id:
            boundary = generation.input_json.get("boundaryFrames", {})
            frames = boundary.get("frames", [])
            if strict_frames and len(frames) == 2:
                delivered.add((boundary["storyboardNodeId"], boundary["shotKey"], frames[0]["assetId"], frames[1]["assetId"]))
            elif boundary.get("runId") == run.id and len(frames) == 2:
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
    jobs, reused_frames = [], []
    if artifact["role"] in VISUAL_ROLES:
        selected_references = frozen_reference_assets(session, run, settings)
        if artifact["role"] == "storyboarder" and artifact.get("schemaVersion") not in {2, 3}:
            raise AppError(422, "STORYBOARD_BOUNDARIES_REQUIRED", "请按连续首尾帧结构保存分镜")
        reused_frames = reusable_boundary_frames(session, run, artifact, settings)
        reviews = [*(run.result_json or {}).get("materialReviewHistory", []), previous or {}]
        approved = {(run.id, item.get("nodeId"), item.get("artifactHash")) for item in reviews if item.get("status") == "approved"}
        handoff = run.limits_json.get("materialContext", {}).get("handoffs", {}).get(step.id, {}).get("inputs", [])
        adopted_visuals = {(item["runId"], item["nodeId"], item["artifactHash"]) for item in handoff if item["role"] in VISUAL_ROLES}
        approved.update(adopted_visuals)
        source_ids = {item["referenceId"] for item in artifact["sources"] if item["kind"] in {"artifact", "canvas_node"}}
        prior_jobs = session.scalars(select(ImageJob).where(ImageJob.run_id.in_({identifier for identifier, _, _ in approved}),
            ImageJob.owner_id == run.owner_id, ImageJob.project_id == run.project_id, ImageJob.status == "ready").order_by(ImageJob.created_at, ImageJob.id)).all()
        prior = [item for item in prior_jobs if (item.run_id, item.node_id, item.artifact_hash) in approved and item.node_id in source_ids]
        representatives = list({(item.node_id, item.request_json.get("referenceGroup")): item for item in reversed(prior)}.values())
        selected_ids = [item["assetId"] for item in selected_references]
        exchanged_anchors = [item.asset_id for item in representatives if item.asset_id and (item.run_id, item.node_id, item.artifact_hash) in adopted_visuals]
        required_anchors = list(dict.fromkeys([*selected_ids, *exchanged_anchors]))
        prior_anchors = list(dict.fromkeys(item.asset_id for item in [*representatives, *prior] if item.asset_id))[:4]
        specifications = []
        frame_indexes = [index for index, frame in enumerate(artifact.get("keyframes", [])) if frame.get("existingAssetId") is None]
        groups = image_reference_groups(artifact)
        for position, item in enumerate(image_prompt_items(artifact)):
            index = position
            if artifact["role"] == "storyboarder":
                index = frame_indexes[index]
            spec = item["outputSpec"]
            width, height = supported_dimensions(spec)
            if artifact["role"] == "storyboarder" and spec["imageCount"] != 1:
                raise AppError(422, "STORYBOARD_SINGLE_FRAME_REQUIRED", "每个边界帧必须生成一张独立图片")
            for variant in range(spec["imageCount"]):
                specifications.append((index, item, spec, width, height, variant, groups[position]))
        selected_id = run.limits_json.get("modelSelection", {}).get("image") or LEGACY_DEFAULTS["image"]
        image_selection = None
        if specifications and selected_id != "sensetime-fast":
            image_selection = freeze_selection(session, run.owner_id, require_project(session, run.project_id, run.owner_id), "image", selected_id)
            if image_selection["providerId"] == "volcengine-ark" and any(min(item[3], item[4]) < 1024 or max(item[3], item[4]) > 4096 for item in specifications):
                raise AppError(422, "ARK_IMAGE_SIZE_UNSUPPORTED", "Seedream当前接入要求宽高均为1024至4096，请调整生成规格")
        if specifications and image_selection is None:
            accounts = session.scalars(select(ImageAccount).where(ImageAccount.owner_id == run.owner_id)).all()
            if len(accounts) != 3:
                raise AppError(409, "IMAGE_ACCOUNTS_REQUIRED", "请配置三个商汤图片账号")
        generated_indexes = {item[0] for item in specifications}
        previous_frame_slots = int(artifact["role"] == "storyboarder" and any(index - 1 in generated_indexes for index in generated_indexes))
        if exchanged_anchors and len(required_anchors) + previous_frame_slots > 5:
            raise AppError(422, "MATERIAL_REFERENCE_CAPACITY_EXCEEDED",
                "已选择的参考图片、已采用的角色场景图与上一帧合计超过五张，请减少本轮手动选择的参考图片")
        if len(selected_ids) + previous_frame_slots > 5:
            raise AppError(422, "MATERIAL_REFERENCE_CAPACITY_EXCEEDED",
                f"已选择{len(selected_ids)}张参考图片，连续分镜还需要{previous_frame_slots}张上一画面作为参考，共{len(selected_ids) + previous_frame_slots}张，超过单次5张上限；请在新素材对话中选择至多{5 - previous_frame_slots}张参考图片后继续")
        anchor_limit = max(len(required_anchors), min(4, 5 - previous_frame_slots))
        anchors = list(dict.fromkeys([*required_anchors, *prior_anchors]))[:anchor_limit]
        by_frame_index = {}
        for index, item, spec, width, height, variant, group in specifications:
            dependency = by_frame_index.get(index - 1) if artifact["role"] == "storyboarder" else None
            job = ImageJob(id=new_id(), owner_id=run.owner_id, project_id=run.project_id, scene_id=run.scene_id,
                run_id=run.id, node_id=node["id"], artifact_hash=artifact_hash,
                operation_id=f"{run.id}:{artifact_hash}:{index}:{variant}", label=item["label"] + (f" · {variant + 1}" if spec["imageCount"] > 1 else ""),
                frame_key=artifact["keyframes"][index]["key"] if artifact["role"] == "storyboarder" else None,
                status="awaiting_confirmation", request_json={"prompt": item["prompt"], "width": width, "height": height,
                    "referenceAssets": selected_references, "referenceGroup": group, **({"modelProfile": image_selection} if image_selection else {})},
                reference_asset_ids=anchors, dependency_ids=[dependency.id] if dependency else [])
            session.add(job)
            jobs.append(job)
            by_frame_index[index] = job
        session.flush()
    review = {"id": new_id(), "stepId": step.id, "nodeId": node["id"], "artifactHash": artifact_hash,
        "status": "pending", "questions": questions, "imageJobIds": [job.id for job in jobs],
        "createdAt": time.time(), "answers": {}, "role": artifact["role"]}
    if artifact.get("schemaVersion") == 3 and artifact["role"] == "storyboarder":
        review["reusedFrames"] = reused_frames
    if artifact["role"] == "media_producer" and not questions:
        review["status"] = "not_required"
    history = [*(run.result_json or {}).get("materialReviewHistory", [])]
    if previous:
        history.append(previous)
    run.result_json = {**(run.result_json or {}), "materialReview": review, "materialReviewHistory": history}
    if review["status"] == "pending":
        run.status, step.status = "waiting_confirmation", "waiting_confirmation"


def public_review(session, run):
    clarification = (run.result_json or {}).get("materialClarification")
    if clarification and clarification["status"] == "pending" and run.status == "waiting_confirmation":
        return {
            "id": clarification["id"], "nodeId": None, "kind": "clarification", "status": "pending",
            "questions": clarification["questions"], "remainingQuestions": len(clarification["questions"]),
            "imageCount": 0, "images": [], "reusedImages": [], "canConfirm": True,
            "message": "请回答总编的问题，确认后会安排对应岗位。",
        }
    review = (run.result_json or {}).get("materialReview")
    if not review or review["status"] in {"approved", "revision_requested", "not_required"}:
        return None
    jobs = [session.get(ImageJob, identifier) for identifier in review["imageJobIds"]]
    codex_images = any(uses_codex_image(job) for job in jobs)
    status = review["status"]
    if status == "generating" and jobs and all(job.status == "ready" for job in jobs):
        status = "review_images"
    unanswered = [item for item in review["questions"] if item["id"] not in review.get("answers", {})] if status == "pending" else []
    if status == "pending":
        message = "请回答视频制作前的问题；提交视频时还会核对首尾画面和参数。" if review.get("role") == "media_producer" else "请核对以下事项，并确认当前成果。"
    elif status == "generating":
        message = "图片生成中，完成后请确认图片成果。"
    else:
        message = "图片已生成，请查看画布并确认后继续。"
    if status == "generating":
        codex_status = None
        if codex_images:
            from app.services.codex_images import material_connection_status
            codex_status = material_connection_status(session, jobs[0])
        counts = {state: sum(job.status == state for job in jobs) for state in ("ready", "submission_unknown", "failed", "cancelled")}
        counts["timeout"] = sum(job.status == "failed" and job.error_code == "IMAGE_RESPONSE_TIMEOUT" for job in jobs)
        counts["no_account"] = sum(job.status == "failed" and job.error_code == "IMAGE_ALTERNATE_ACCOUNT_UNAVAILABLE" for job in jobs)
        counts["failed"] -= counts["timeout"] + counts["no_account"]
        problems = [f"{counts[state]}张{label}" for state, label in (("submission_unknown", "请求结果待核实"), ("failed", "生成失败"), ("cancelled", "已取消")) if counts[state]]
        if counts["timeout"]:
            problems.append(f"{counts['timeout']}张等待回执超时")
        if counts["no_account"]:
            problems.append(f"{counts['no_account']}张超时且无其他可用账号")
        if problems:
            active = sum(job.status in {"queued", "claimed", "submitting", "response_saved", "waiting_connection"} for job in jobs)
            message = f"图片已完成{counts['ready']}/{len(jobs)}张，" + "，".join(problems) + "。"
            if active:
                message += f"另有{active}张仍在排队或处理中。"
            if counts["submission_unknown"]:
                if codex_images:
                    message += "请保持原本机 Codex 连接在线，以便核对已提交的回合。" if codex_status != "authorization_ended" else "原连接授权已失效，已提交的回合仍待核实，可以停止本轮制作。"
                else:
                    message += "付费图片的提交结果待核实，原预留继续保留。" if any(job.status == "submission_unknown" and job.request_json.get("modelProfile") for job in jobs) else "回执最多等待3分钟，超时后尝试换账号。"
            elif not active:
                message += "请先核对未完成的图片。"
        if codex_images and not problems:
            if codex_status == "authorization_ended":
                message = "本次图片任务的原连接授权已失效，可以停止本轮制作并重新连接。"
            elif codex_status == "offline":
                message = "本次图片任务正在等待原本机 Codex 连接恢复，已保存的进度会继续使用。"
            elif any(job.status == "queued" for job in jobs):
                message = "图片已进入本机 Codex 队列，将按顺序生成。"
        elif not codex_images and any(job.status == "queued" for job in jobs):
            if image_queue_awaits_result(session) and not counts["submission_unknown"]:
                waiting = "上一单回执待返回，超时后队列会继续。"
                message = message + waiting if problems else waiting
            elif all(job.status in {"ready", "queued"} for job in jobs):
                message = f"图片正在排队，上一单结束后至少间隔{IMAGE_REQUEST_GAP_SECONDS}秒再提交。"
    return {"id": review["id"], "nodeId": review["nodeId"], "status": status,
        "questions": unanswered[:5], "remainingQuestions": len(unanswered),
        "imageCount": len(jobs), "images": [{"label": job.label, "width": job.request_json["width"], "height": job.request_json["height"],
            "model": job.request_json.get("modelProfile", {}).get("model", "sensenova-u1.5-fast")} for job in jobs],
        "usesProjectQuota": any(job.request_json.get("modelProfile", {}).get("providerId") == "volcengine-ark" for job in jobs),
        **({"requiresPersonalAccountConfirmation": True} if codex_images and status == "pending" else {}),
        "reusedImages": review.get("reusedFrames", []),
        "canConfirm": status in {"pending", "review_images"},
        "message": message}


def _answer_clarification(session, run, conversation, body, gate):
    payload = material_request_payload(body)
    if gate.get("operationId") == body.operation_id:
        if gate["answerPayload"] != payload:
            raise AppError(409, "OPERATION_CONFLICT", "确认请求内容已经变化")
        return
    if gate["status"] != "pending" or run.status != "waiting_confirmation":
        raise AppError(409, "MATERIAL_REVIEW_STALE", "这组问题已经回答，请读取当前制作进度")
    project = require_project(session, run.project_id, run.owner_id)
    require_revision(project, body.expected_revision)
    expected = {question["id"] for question in gate["questions"]}
    if set(body.answers) != expected or any(not answer.strip() for answer in body.answers.values()):
        raise AppError(422, "MATERIAL_ANSWERS_REQUIRED", "请回答全部待确认问题")
    if body.reference_asset_ids or body.requested_changes.strip():
        raise AppError(422, "MATERIAL_CLARIFICATION_ANSWERS_ONLY", "请把本轮决定写在对应答案中")
    answered = {**gate, "status": "answered", "operationId": body.operation_id, "answerPayload": payload, "answeredAt": time.time()}
    context = run.limits_json["materialContext"]
    answers = [*context.get("clarificationAnswers", []), *[
        {"question": item["question"], "answer": body.answers[item["id"]]} for item in gate["questions"]
    ]]
    run.limits_json = {**run.limits_json, "materialContext": {**context, "clarificationAnswers": answers}}
    run.result_json = {**run.result_json, "materialClarification": answered}
    run.project_revision, run.status, run.updated_at = project.revision, "queued", time.time()
    conversation.updated_at = run.updated_at


def answer_review(session, conversation, body, settings=None):
    from app.services.materials import active_production, canvas_nodes

    run, _ = active_production(session, conversation)
    if run is None or run.owner_id != conversation.owner_id:
        raise AppError(404, "MATERIAL_REVIEW_NOT_FOUND", "待确认事项不存在")
    clarification = (run.result_json or {}).get("materialClarification")
    if clarification and clarification["id"] == body.review_id:
        _answer_clarification(session, run, conversation, body, clarification)
        return
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
    node = next((item for item in canvas_nodes(session, run.project_id, run.scene_id, run.owner_id) if item["id"] == review["nodeId"]), None)
    if node is None or payload_digest(node.get("metadata", {}).get("creativeArtifact")) != review["artifactHash"]:
        raise AppError(409, "MATERIAL_REVIEW_STALE", "成果内容已经变化，请重新生成确认事项")
    if reusable_boundary_frames(session, run, node["metadata"]["creativeArtifact"], settings) != review.get("reusedFrames", []):
        raise AppError(409, "STORYBOARD_EXISTING_FRAME_CHANGED", "复用边界图片与待确认版本不一致，请重新核对")
    public = public_review(session, run)
    if not public["canConfirm"]:
        raise AppError(409, "MATERIAL_IMAGES_PENDING", "请等待图片真实生成完成")
    expected = {item["id"] for item in public["questions"]}
    if set(body.answers) != expected or any(not value.strip() for value in body.answers.values()):
        raise AppError(422, "MATERIAL_ANSWERS_REQUIRED", "请逐项回答全部待确认问题")
    changed = bool(body.requested_changes.strip()) or any(body.answers[item["id"]].strip() != item["suggestedAnswer"].strip() for item in public["questions"])
    connection = None
    if not changed and public.get("requiresPersonalAccountConfirmation"):
        if body.account_usage_confirmed is not True:
            raise AppError(422, "CODEX_IMAGE_ACCOUNT_CONFIRMATION_REQUIRED", "请确认使用本人的 Codex 账户生成本批图片")
        connection = require_image_connection(session, run.owner_id, run.project_id)
    review = {**review, "answers": {**review.get("answers", {}), **body.answers}, "operationId": body.operation_id,
        "answerPayload": material_request_payload(body), "answeredAt": time.time()}
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
            if connection is not None:
                job.request_json = {**job.request_json, "codexConnectionId": connection.id}
            job.status, job.confirmed_at = "queued", time.time()
        review["status"] = "generating"
    else:
        review["status"], run.status, step.status = "approved", "queued", "queued"
    run.project_revision, run.updated_at = project.revision, time.time()
    run.result_json = {**run.result_json, "materialReview": review}
    conversation.updated_at = time.time()
