from sqlalchemy import func, select

from app.agent_models import AgentRun, AgentStep


def writer_body(artifact):
    paragraphs = [artifact["title"]]
    character_names = {item["key"]: item["name"] for item in artifact["characters"]}
    for scene in artifact["scenes"]:
        paragraphs.extend([scene["heading"], scene["action"]])
        for line in scene["dialogues"]:
            paragraphs.append(f"{character_names[line['speaker']]}（{line['performance']}）：{line['line']}")
        paragraphs.append(f"场景收尾：{scene['endingState']}")
    for index, ending in enumerate(artifact["endings"], 1):
        paragraphs.extend([f"结局 {index} · {ending['condition']}", ending["resolution"], f"结局状态：{ending['finalState']}"])
    return "\n\n".join(paragraphs)


PUBLIC_ROLE_TITLES = {
    "director": "梳理制作需求",
    "writer": "编写脚本",
    "character_designer": "设计角色形象",
    "scene_designer": "设计场景",
    "storyboarder": "制作分镜图片",
    "media_producer": "制作镜头视频",
    "sound_designer": "设计声音",
}


def public_step_title(item):
    return "核对制作成果" if item.get("kind") == "verify" else PUBLIC_ROLE_TITLES[item["role"]]


def public_director_brief(result):
    brief = (result or {}).get("directorBrief")
    if brief is None:
        return None
    return {"objective": brief["objective"], "confirmedRequirements": brief["confirmedRequirements"],
        "statusText": brief.get("statusText")}


PUBLIC_MATERIAL_ERRORS = {
    "AGENT_ARTIFACT_SOURCE_CHANGED": "上游成果版本已有更新，请重新核对。",
    "AGENT_DEPENDENCY_NOT_READY": "当前岗位正在等待上游成果采用。",
    "MATERIAL_HANDOFF_ORDER": "请先完成上游岗位的成果，再继续本轮制作。",
    "MATERIAL_HANDOFF_SOURCE_REQUIRED": "当前岗位缺少已采用的上游成果，请核对引用。",
    "MATERIAL_STORYBOARD_REQUIRED": "请先制作或选择已采用的分镜。",
    "MATERIAL_VIDEO_NOT_READY": "请核对首尾帧并确认生成视频。",
    "VIDEO_FRAMES_UNCONFIRMED": "请先确认本段分镜图片。",
    "VIDEO_FRAME_NOT_READY": "本段仍有分镜图片在制作中。",
    "STORYBOARD_EXISTING_FRAME_CHANGED": "复用的图片版本已有更新，请重新确认。",
}


def public_material_error(value):
    if not value:
        return None
    code = value["code"]
    return {"code": code, "message": value["message"] if code == "MATERIAL_REFERENCE_CAPACITY_EXCEEDED" else
        PUBLIC_MATERIAL_ERRORS.get(code, "制作已暂停，请查看任务状态后继续。")}


def material_generation_ids(session, project_id, owner_id):
    receipt = func.json_each(AgentStep.tool_results_json).table_valued("value", joins_implicitly=True)
    generation_id = func.json_extract(receipt.c.value, "$.result.generation.id")
    return set(session.scalars(select(generation_id).select_from(AgentStep).join(AgentRun, AgentRun.id == AgentStep.run_id)
        .where(AgentRun.project_id == project_id, AgentRun.owner_id == owner_id,
            AgentRun.limits_json["workspace"].as_string() == "materials",
            func.json_extract(receipt.c.value, "$.tool") == "submit_generation", generation_id.is_not(None))))


def is_material_generation(value, generation_ids):
    source = value["input"]
    return bool((source.get("request") or {}).get("videoArtifactNodeId") or
        (source.get("boundaryFrames") or {}).get("runId") or value["id"] in generation_ids)


def public_material_generation(value):
    source = value["input"]
    visible_input = {key: source[key] for key in ("model", "duration", "resolution", "ratio", "count", "audio") if key in source}
    if source.get("boundaryFrames"):
        boundaries = source["boundaryFrames"]
        visible_input["boundaryFrames"] = {"shotKey": boundaries["shotKey"],
            "frames": [{"type": frame["type"], "assetId": frame["assetId"]} for frame in boundaries["frames"]]}
    return {**{key: value[key] for key in ("id", "projectId", "sceneId", "sourceNodeId", "outputNodeId", "clipId",
        "projectRevision", "status", "confirmationHash", "assetId", "retryCount", "createdAt", "updatedAt", "startedAt", "finishedAt")},
        "material": True, "input": visible_input, "canResume": bool(value["remoteTaskId"]),
        "retryOf": value.get("retryOf"),
        "canRetry": bool(value["status"] == "submission_unknown" and not value["remoteTaskId"] and not value.get("retryOf")
            and source.get("provider") == "aliyun-wan" and (source.get("request") or {}).get("videoArtifactNodeId")),
        "error": public_material_error(value["error"])}


def public_node(node, role, content_hash, jobs):
    metadata = node.get("metadata", {})
    artifact = metadata.get("creativeArtifact")
    if artifact:
        role = artifact["role"]
        content = writer_body(artifact) if role == "writer" else artifact["productionBrief"]["objective"] if role == "director" else ""
        delivery = {"kind": "script" if role == "writer" else "images" if role in {"character_designer", "scene_designer", "storyboarder"} else "video" if role == "media_producer" else "audio",
            "text": content, "images": jobs, "reusedFrames": metadata.get("reusedBoundaryFrames", []),
            "segments": [{"key": shot["key"], "firstFrameKey": shot["firstFrameKey"], "lastFrameKey": shot["lastFrameKey"], "durationSeconds": shot["durationSeconds"]} for shot in artifact.get("shots", [])] if role == "storyboarder" and artifact.get("schemaVersion") in {2, 3} else []}
        safe_metadata = {"content": content}
    else:
        role = role or ("character_designer" if jobs else None)
        safe_metadata = {key: metadata[key] for key in ("content", "src", "assetId", "storageKey") if key in metadata}
        delivery = {"kind": "images", "text": "", "images": jobs, "segments": []} if jobs else None
    return {"id": node["id"], "type": node["type"], "title": node.get("title", "素材"),
        "contentHash": content_hash, "producerRole": role, "metadata": safe_metadata, "delivery": delivery}


def public_run(value):
    brief = public_director_brief(value.get("result"))
    return {**{key: value[key] for key in ("id", "projectId", "sceneId", "role", "executor", "status", "projectRevision", "currentRevision", "targetIds", "canResume", "retryableUnknownCallId")},
        "canExtendLimit": value.get("canExtendLimit", False),
        "workspace": "materials", "goal": brief["objective"] if brief else "素材制作", "limits": {"maxSteps": value["limits"]["maxSteps"]},
        "plan": [{"role": item["role"], "title": public_step_title(item), "instruction": ""} for item in value["plan"]],
        "steps": [{"id": item["id"], "role": item["role"], "title": public_step_title(item), "status": item["status"],
            "toolResults": [{"tool": "submit_generation", "result": {"generation": {"id": receipt["result"]["generation"]["id"]}}} for receipt in item["toolResults"] if receipt["tool"] == "submit_generation"]} for item in value["steps"]],
        "usage": {}, "error": public_material_error(value["error"])}
