def writer_body(artifact):
    paragraphs = [artifact["title"], artifact["plot"]]
    for scene in artifact["scenes"]:
        paragraphs.extend([scene["heading"], scene["action"]])
        for line in scene["dialogues"]:
            paragraphs.append(f"{line['speaker']}：{line['line']}")
    return "\n\n".join(paragraphs)


def public_node(node, role, content_hash, jobs):
    metadata = node.get("metadata", {})
    artifact = metadata.get("creativeArtifact")
    if artifact:
        role = artifact["role"]
        content = writer_body(artifact) if role == "writer" else artifact["productionBrief"]["objective"] if role == "director" else ""
        delivery = {"kind": "script" if role == "writer" else "images" if role in {"character_designer", "scene_designer", "storyboarder"} else "video" if role == "media_producer" else "audio",
            "text": content, "images": jobs, "segments": [{"key": shot["key"], "firstFrameKey": shot["firstFrameKey"], "lastFrameKey": shot["lastFrameKey"], "durationSeconds": shot["durationSeconds"]} for shot in artifact.get("shots", [])] if artifact.get("schemaVersion") == 2 else []}
        safe_metadata = {"content": content}
    else:
        role = role or ("character_designer" if jobs else None)
        safe_metadata = {key: metadata[key] for key in ("content", "src", "assetId", "storageKey") if key in metadata}
        delivery = {"kind": "images", "text": "", "images": jobs, "segments": []} if jobs else None
    return {"id": node["id"], "type": node["type"], "title": node.get("title", "素材"),
        "contentHash": content_hash, "producerRole": role, "metadata": safe_metadata, "delivery": delivery}


def public_run(value):
    return {**{key: value[key] for key in ("id", "projectId", "sceneId", "role", "executor", "goal", "status", "projectRevision", "currentRevision", "targetIds", "canResume", "retryableUnknownCallId")}, "limits": {"maxSteps": value["limits"]["maxSteps"]},
        "plan": [{"role": item["role"], "title": item["title"], "instruction": ""} for item in value["plan"]],
        "steps": [{"id": item["id"], "role": item["role"], "title": item["title"], "status": item["status"],
            "toolResults": [{"tool": "submit_generation", "result": {"generation": {"id": receipt["result"]["generation"]["id"]}}} for receipt in item["toolResults"] if receipt["tool"] == "submit_generation"]} for item in value["steps"]],
        "usage": {}, "error": {"code": value["error"]["code"], "message": "制作已暂停，请查看任务状态后继续。"} if value["error"] else None}
