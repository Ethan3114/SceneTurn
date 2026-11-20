import copy
import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from sqlalchemy import select

from app.api.generations import GenerationResult, visible_generation
from app.core.config import Settings
from app.core.errors import AppError
from app.creative_artifacts import image_prompt_items, render_artifact, validate_artifact
from app.image_models import ImageJob
from app.material_schemas import MaterialAnswer
from app.models import Canvas, Generation, new_id
from app.services.agent_provider import MATERIAL_JSON_AGENT_PROTOCOL, MATERIAL_NATIVE_AGENT_PROTOCOL, agent_request_body, agent_response_schema, agent_tools, decode_platform_message, provider_endpoint
from app.services.material_handoffs import prepare_material_handoff
from app.services.material_review import answer_review, public_review
from app.services.video_boundaries import approved_video_segments, freeze_adopted_boundaries, validate_frozen_boundaries
from app.services.text_provider import ENDPOINT
from app.tools.project_tools import MATERIAL_SOP_TOOL_CATALOG_VERSION, _read_artifact, allowed_tools, execute_tool, require_canvas, tool_catalog
from test_agent_professional_artifacts import manual_artifact
from test_material_reference_assets import disabled_accounts, reference_image, reference_workspace
from test_storyboard_existing_frames import approve, reusable_storyboard, save_storyboard


def test_material_sop_tools_keep_story_editing_and_video_tasks_out_of_other_roles():
    version = MATERIAL_SOP_TOOL_CATALOG_VERSION
    director = allowed_tools("director", version)
    assert director == {"read_project", "retrieve_context", "read_artifact", "validate_artifacts", "write_artifact", "patch_artifact"}
    for role in ("writer", "character_designer", "scene_designer", "storyboarder", "sound_designer"):
        assert allowed_tools(role, version) == director
    assert allowed_tools("media_producer", version) == director | {"submit_generation", "inspect_generation"}
    assert not allowed_tools("editor", version)
    assert "edit_timeline" in allowed_tools("director", "R4")
    assert "apply_canvas_ops" in allowed_tools("director", "R7")


def test_material_video_motion_schema_preserves_legacy_video_history():
    legacy = manual_artifact("media_producer", "authored-project")
    legacy["schemaVersion"] = 1
    assert validate_artifact("media_producer", legacy) == legacy
    assert image_prompt_items(legacy)
    previous = next(item for item in tool_catalog("media_producer", "R7") if item["name"] == "write_artifact")
    assert previous["inputSchema"]["$defs"]["MediaShot"]["properties"]["imagePrompt"]

    current = next(item for item in tool_catalog("media_producer", MATERIAL_SOP_TOOL_CATALOG_VERSION) if item["name"] == "write_artifact")
    artifact = copy.deepcopy(legacy)
    artifact["schemaVersion"] = 2
    for index, shot in enumerate(artifact["shots"]):
        artifact["shots"][index] = {"storyboardNodeId": "storyboard-node", "shotKey": shot["shotKey"],
            "firstFrameAssetId": "first-approved-frame", "lastFrameAssetId": "last-approved-frame",
            "videoPrompt": {"actionTimeline": "风拂过钟表店白色窗帘。", "cameraMovement": "机位固定。", "continuityConstraints": ["两张边界图片保持同一窗户与机位。"],
                "negativePrompt": ["窗框变形"], "durationSeconds": 2, "resolution": "720P", "audioRequirement": "保留店内台钟声。"},
            "acceptance": ["首尾帧与采用分镜一致"]}
    Draft202012Validator(current["inputSchema"]).validate({"targetId": None, "title": None, "artifact": artifact})
    assert validate_artifact("media_producer", artifact) == artifact
    assert image_prompt_items(artifact) == []
    assert "风拂过钟表店白色窗帘" in render_artifact(artifact)
    result, identifier = decode_platform_message({"role": "assistant", "content": json.dumps({"action": "tool", "tool": "write_artifact",
        "arguments": {"targetId": None, "title": None, "artifact": artifact}})}, False, "media_producer", MATERIAL_JSON_AGENT_PROTOCOL)
    assert result["arguments"]["artifact"] == artifact and identifier is None
    artifact["shots"][0]["imagePrompt"] = legacy["shots"][0]["imagePrompt"]
    with pytest.raises(ValidationError):
        validate_artifact("media_producer", artifact)


def test_material_json_output_requires_sop_catalog_and_validates_planning_gate():
    protocol = MATERIAL_JSON_AGENT_PROTOCOL
    assert provider_endpoint(protocol) == ENDPOINT
    tools = agent_tools("director", True, protocol=protocol)
    body = agent_request_body([{"role": "system", "content": "输出一份 JSON 制作简报。"}], 32768, tools, True, Settings(_env_file=None), protocol=protocol)
    assert tools == [] and "tools" not in body and "temperature" not in body
    assert body["response_format"] == {"type": "json_object"} and body["tool_choice"] == "none"
    assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "high"
    with pytest.raises(AppError) as old_catalog:
        agent_tools("director", True, protocol=protocol, catalog_version="R7")
    assert old_catalog.value.code == "AGENT_TOOL_CATALOG_PROTOCOL_INVALID"

    question = {"action": "plan", "directorBrief": {"objective": "制作八秒的庭院片段", "confirmedRequirements": ["自然光"]},
        "questions": [{"question": "画面需要有人物吗？", "suggestedAnswer": "只拍庭院"}], "plan": []}
    Draft202012Validator(agent_response_schema(True, protocol=protocol)).validate(question)
    result, identifier = decode_platform_message({"role": "assistant", "content": json.dumps(question)}, True, "director", protocol)
    assert result["directorBrief"] == question["directorBrief"] and result["questions"] == question["questions"]
    assert result["plan"] == [] and identifier is None
    question["plan"] = [{"role": "writer", "title": "编写庭院剧本", "kind": "produce", "instruction": "写明庭院里发生的动作。", "dependsOn": []}]
    with pytest.raises(AppError) as both:
        decode_platform_message({"role": "assistant", "content": json.dumps(question)}, True, "director", protocol)
    assert both.value.code == "AGENT_JSON_ARGUMENTS_INVALID"
    question["questions"] = []
    result, identifier = decode_platform_message({"role": "assistant", "content": json.dumps(question)}, True, "director", protocol)
    assert result["plan"] == question["plan"] and identifier is None


def test_scene_json_can_return_direct_artifact_only_in_material_sop():
    artifact = {**manual_artifact("scene_designer", "authored-project"), "schemaVersion": 1}
    response = {"role": "assistant", "content": json.dumps(artifact)}
    result, identifier = decode_platform_message(response, False, "scene_designer", MATERIAL_JSON_AGENT_PROTOCOL, scene_artifact_only=True)
    assert result["tool"] == "write_artifact" and result["arguments"]["artifact"] == artifact and identifier is None
    with pytest.raises(AppError) as legacy:
        decode_platform_message(response, False, "scene_designer", scene_artifact_only=True)
    assert legacy.value.code == "AGENT_JSON_FUNCTION_FORBIDDEN"
    with pytest.raises(AppError) as other:
        decode_platform_message(response, False, "character_designer", MATERIAL_JSON_AGENT_PROTOCOL, scene_artifact_only=True)
    assert other.value.code == "AGENT_JSON_FUNCTION_FORBIDDEN"


def test_video_handoff_freezes_actual_images_from_an_adopted_prior_run(reference_workspace):
    session, settings, project, _, add_image, create_run = reference_workspace
    first, last = add_image("上轮实际首帧.png"), add_image("上轮实际尾帧.png")
    origin, storyboarder, conversation = create_run("storyboarder", [first, last], catalog_version="R7")
    artifact = reusable_storyboard([first.id, last.id])
    node = save_storyboard(session, settings, project, origin, storyboarder, artifact)["sourceNodeId"]
    video, _, _ = create_run("media_producer")
    assert approved_video_segments(session, video, node, artifact) == []
    with pytest.raises(AppError) as pending:
        freeze_adopted_boundaries(session, video, node, "shot-1", settings)
    assert pending.value.code == "VIDEO_FRAMES_UNCONFIRMED"
    approve(session, settings, project, origin, conversation)
    assert approved_video_segments(session, video, node, artifact) == [{"storyboardNodeId": node, "shotKey": "shot-1",
        "firstFrameAssetId": first.id, "lastFrameAssetId": last.id, "durationSeconds": 2}]
    frozen = freeze_adopted_boundaries(session, video, node, "shot-1", settings)
    assert frozen["runId"] == origin.id and [item["sha256"] for item in frozen["frames"]] == [first.sha256, last.sha256]
    validate_frozen_boundaries(session, video.owner_id, frozen, settings)


def test_director_cannot_prepare_a_video_through_transport(reference_workspace):
    session, settings, project, _, _, create_run = reference_workspace
    director, step, _ = create_run("director")
    director.limits_json = {**director.limits_json, "toolCatalogVersion": MATERIAL_SOP_TOOL_CATALOG_VERSION}
    session.commit()
    with pytest.raises(AppError) as forbidden:
        execute_tool(session, director, step.role, "submit_generation", {"videoArtifactNodeId": new_id(), "shotKey": "one"},
            new_id(), project.revision, settings, step)
    assert forbidden.value.code == "TOOL_NOT_ALLOWED"


def test_video_step_prepares_only_an_unconfirmed_quote_from_its_saved_motion(reference_workspace):
    session, settings, project, _, add_image, create_run = reference_workspace
    first, last = add_image("本段首帧.png"), add_image("本段尾帧.png")
    origin, storyboarder, conversation = create_run("storyboarder", [first, last], catalog_version="R7")
    node = save_storyboard(session, settings, project, origin, storyboarder, reusable_storyboard([first.id, last.id]))["sourceNodeId"]
    approve(session, settings, project, origin, conversation)
    video, step, _ = create_run("media_producer")
    video.target_ids_json = [node]
    video.limits_json = {**video.limits_json, "toolCatalogVersion": MATERIAL_SOP_TOOL_CATALOG_VERSION}
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.scene_id == video.scene_id))
    prepare_material_handoff(session, video, step, canvas)
    session.commit()
    scene_content = copy.deepcopy(project.data_json)
    approved = execute_tool(session, video, "media_producer", "read_artifact", {"targetId": node}, new_id(), project.revision, settings, step)["result"]["approvedVideoSegments"][0]
    duration = int(approved.pop("durationSeconds"))
    artifact = {"schemaVersion": 2, "role": "media_producer", "title": "花卉镜头的视频制作安排",
        "sources": [{"kind": "artifact", "referenceId": node, "description": "使用确认后的连续首尾图"}], "assumptions": [],
        "productionStrategy": "固定首尾画面。", "confirmationPoints": ["核对用户看到的首尾画面和时长。"], "deliveryChecks": ["可播放"],
        "shots": [{**approved, "videoPrompt": {"actionTimeline": "镜头期间花瓣随风轻动。", "cameraMovement": "保持固定机位。",
            "continuityConstraints": ["同一花朵和背景"], "negativePrompt": ["植物扭曲"],
            "durationSeconds": duration, "resolution": "720P", "audioRequirement": "保留自然庭院风声。"}, "acceptance": ["两张实际画面保持一致"]}]}
    saved = execute_tool(session, video, "media_producer", "write_artifact", {"artifact": artifact}, new_id(), project.revision, settings, step)["result"]
    operation = new_id()
    arguments = {"videoArtifactNodeId": saved["sourceNodeId"], "shotKey": approved["shotKey"]}
    revision = project.revision
    prepared = execute_tool(session, video, "media_producer", "submit_generation", arguments, operation, revision, settings, step)
    assert prepared["result"]["confirmationRequired"] is True and video.status == "waiting_confirmation"
    task = session.get(Generation, prepared["result"]["generation"]["id"])
    assert task.status == "awaiting_confirmation" and task.reserved_cents == 0 and task.remote_task_id is None
    assert [frame["assetId"] for frame in task.input_json["boundaryFrames"]["frames"]] == [first.id, last.id]
    assert "镜头期间花瓣随风轻动" in task.input_json["prompt"]
    assert task.input_json["boundaryFrames"]["runId"] == origin.id
    visible = visible_generation(session, task)
    GenerationResult.model_validate({"generation": visible})
    assert visible["material"] and not visible["canResume"]
    assert {"quotedCents", "reservedCents", "actualCents", "usage", "remoteTaskId"}.isdisjoint(visible)
    assert {"request", "prompt", "sourcePrompt", "workspaceId"}.isdisjoint(visible["input"])
    assert [frame["assetId"] for frame in visible["input"]["boundaryFrames"]["frames"]] == [first.id, last.id]
    source = next(item for item in canvas.data_json["nodes"] if item["id"] == task.source_node_id)
    assert source["metadata"]["materialVideoInput"] is True
    assert project.data_json == scene_content
    replay = execute_tool(session, video, "media_producer", "submit_generation", arguments, operation, revision, settings, step)
    assert replay == prepared and len(session.scalars(select(Generation)).all()) == 1


@pytest.mark.parametrize(("role", "collection"), [("character_designer", "characters"), ("scene_designer", "scenes")])
def test_visual_revision_can_reduce_existing_images_after_a_review_bound_read(reference_workspace, role, collection):
    session, settings, project, user, _, create_run = reference_workspace
    run, step, conversation = create_run(role)
    disabled_accounts(session, user)
    artifact = manual_artifact(role, project.id)
    original = artifact[collection][0]["imagePrompts"][0]
    artifact[collection][0]["imagePrompts"].extend([
        {**original, "outputSpec": {**original["outputSpec"], "purpose": purpose}}
        for purpose in ("环境中景", "局部细节")
    ])
    write = execute_tool(session, run, role, "write_artifact", {"artifact": artifact}, new_id(), project.revision, settings, step)["result"]
    target = write["sourceNodeId"]
    path = f"/{collection}/0/imagePrompts"
    paths = [f"{path}/{index}" for index in range(3)]
    current = _read_artifact(session, run, project, require_canvas(session, run, project), target, settings, step)
    assert not any(item.get("removeImagePrompt") for item in current["patchablePaths"])
    first, _, third = current["artifact"][collection][0]["imagePrompts"]
    review = public_review(session, run)
    answer_review(session, conversation, MaterialAnswer(review_id=review["id"], operation_id=new_id(),
        expected_revision=project.revision, requested_changes="请只保留第一张与第三张。"), settings)
    session.commit()

    def remove(*selected, additional=()):
        arguments = {"targetId": target, "expectedRevision": current["revision"], "baseArtifactSha256": current["baseArtifactSha256"],
                     "patches": [{"op": "remove", "path": item} for item in selected] + list(additional)}
        decision, identifier = decode_platform_message({"role": "assistant", "content": json.dumps({"action": "tool", "tool": "patch_artifact", "arguments": arguments})},
            False, role, MATERIAL_JSON_AGENT_PROTOCOL, MATERIAL_SOP_TOOL_CATALOG_VERSION)
        assert identifier is None and decision["arguments"] == arguments
        return execute_tool(session, run, role, "patch_artifact", arguments, new_id(), project.revision, settings, step)["result"]

    with pytest.raises(AppError) as blocked:
        remove(paths[1])
    assert blocked.value.code == "AGENT_ARTIFACT_READ_REQUIRED"
    session.rollback()
    read = execute_tool(session, run, role, "read_artifact", {"targetId": target}, new_id(), project.revision, settings, step)["result"]
    assert read["imagePromptRemovalReviewId"] == review["id"]
    assert [item["path"] for item in read["patchablePaths"] if item.get("removeImagePrompt")] == paths
    for selected in ((paths[0], paths[1]), (paths[1], paths[1]), tuple(reversed(paths)), (f"{path}/3",)):
        with pytest.raises(AppError) as blocked:
            remove(*selected)
        assert blocked.value.code == "AGENT_IMAGE_PROMPT_SELECTION_INVALID"
        session.rollback()
    with pytest.raises(AppError) as blocked:
        remove(paths[1], additional=[{"op": "replace", "path": paths[2] + "/subject", "value": first["subject"]}])
    assert blocked.value.code == "AGENT_IMAGE_PROMPT_SELECTION_INVALID"
    session.rollback()
    revised = remove(paths[1])
    assert revised["changedPaths"] == [path]
    expected = copy.deepcopy(current["artifact"])
    expected[collection][0]["imagePrompts"] = [first, third]
    assert revised["artifact"] == expected
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()
    assert len(jobs) == 5 and sum(job.status == "cancelled" for job in jobs) == 3
    assert sum(job.status == "awaiting_confirmation" for job in jobs) == 2
    assert all(job.submitted_at is None for job in jobs)
    assert run.status == step.status == "waiting_confirmation"


def test_sop_keeps_the_material_native_patch_wire_schema():
    protocol = MATERIAL_NATIVE_AGENT_PROTOCOL
    for role in ("character_designer", "scene_designer"):
        old = agent_tools(role, False, protocol=protocol, catalog_version="R7")
        new = agent_tools(role, False, protocol=protocol, catalog_version=MATERIAL_SOP_TOOL_CATALOG_VERSION)
        assert next(item for item in new if item["function"]["name"] == "patch_artifact") == next(
            item for item in old if item["function"]["name"] == "patch_artifact")
