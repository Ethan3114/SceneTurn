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
    "director": "核对制作成果",
    "writer": "编写脚本",
    "character_designer": "设计角色形象",
    "scene_designer": "设计场景",
    "storyboarder": "制作分镜图片",
    "media_producer": "制作镜头视频",
    "sound_designer": "设计声音",
}


def public_step_title(item):
    return "核对制作成果" if item.get("kind") == "verify" else PUBLIC_ROLE_TITLES[item["role"]]


def public_node(node, role, content_hash, jobs):
    metadata = node.get("metadata", {})
    artifact = metadata.get("creativeArtifact")
    if artifact:
        role = artifact["role"]
        content = writer_body(artifact) if role == "writer" else artifact["productionBrief"]["objective"] if role == "director" else ""
        delivery = {"kind": "script" if role == "writer" else "images" if role in {"character_designer", "scene_designer", "storyboarder"} else "video" if role == "media_producer" else "audio",
            "text": content, "images": jobs, "reusedFrames": metadata.get("reusedBoundaryFrames", []),
            "segments": [{"key": shot["key"], "firstFrameKey": shot["firstFrameKey"], "lastFrameKey": shot["lastFrameKey"], "durationSeconds": shot["durationSeconds"]} for shot in artifact.get("shots", [])] if artifact.get("schemaVersion") in {2, 3} else []}
        safe_metadata = {"content": content}
    else:
        role = role or ("character_designer" if jobs else None)
        safe_metadata = {key: metadata[key] for key in ("content", "src", "assetId", "storageKey") if key in metadata}
        delivery = {"kind": "images", "text": "", "images": jobs, "segments": []} if jobs else None
    return {"id": node["id"], "type": node["type"], "title": node.get("title", "素材"),
        "contentHash": content_hash, "producerRole": role, "metadata": safe_metadata, "delivery": delivery}


def public_run(value):
    return {**{key: value[key] for key in ("id", "projectId", "sceneId", "role", "executor", "goal", "status", "projectRevision", "currentRevision", "targetIds", "canResume", "retryableUnknownCallId")}, "limits": {"maxSteps": value["limits"]["maxSteps"]},
        "plan": [{"role": item["role"], "title": public_step_title(item), "instruction": ""} for item in value["plan"]],
        "steps": [{"id": item["id"], "role": item["role"], "title": public_step_title(item), "status": item["status"],
            "toolResults": [{"tool": "submit_generation", "result": {"generation": {"id": receipt["result"]["generation"]["id"]}}} for receipt in item["toolResults"] if receipt["tool"] == "submit_generation"]} for item in value["steps"]],
        "usage": {}, "error": {"code": value["error"]["code"],
            "message": value["error"]["message"] if value["error"]["code"] == "MATERIAL_REFERENCE_CAPACITY_EXCEEDED" else "制作已暂停，请查看任务状态后继续。"} if value["error"] else None}
