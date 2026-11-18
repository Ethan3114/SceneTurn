import copy
import hashlib
import json

import pytest
from jsonschema import Draft202012Validator
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import select

from app.core.errors import AppError
from app.creative_artifacts import image_prompt_items, render_artifact, validate_artifact
from app.image_models import ImageAccount, ImageJob
from app.material_schemas import MaterialAnswer
from app.models import Canvas, new_id
from app.services.agent_provider import MATERIAL_NATIVE_AGENT_PROTOCOL, agent_tools, decode_platform_message
from app.services.material_review import answer_review, public_review
from app.services.materials import view
from app.services.projects import create_project_record, payload_digest
from app.services.video_boundaries import freeze_boundaries, validate_frozen_boundaries
from app.tools.project_tools import execute_tool, tool_catalog
from test_material_reference_assets import disabled_accounts, image_prompt, reference_image, reference_workspace, storyboard_input


def reusable_storyboard(identifiers):
    artifact = storyboard_input()
    template = artifact["shots"][0]
    artifact["schemaVersion"] = 3
    artifact["shots"] = []
    artifact["keyframes"] = []
    for index, identifier in enumerate(identifiers):
        artifact["keyframes"].append({"key": f"frame-{index}", "title": f"第{index + 1}张边界图片",
            "existingAssetId": identifier, "imagePrompt": None if identifier else image_prompt()})
        if index:
            shot = copy.deepcopy(template)
            shot.update(key=f"shot-{index}", firstFrameKey=f"frame-{index - 1}", lastFrameKey=f"frame-{index}")
            artifact["shots"].append(shot)
    return validate_artifact("storyboarder", artifact)


def save_storyboard(session, settings, project, run, step, artifact):
    return execute_tool(session, run, step.role, "write_artifact", {"artifact": artifact}, new_id(), project.revision, settings, step)["result"]


def approve(session, settings, project, run, conversation):
    review = public_review(session, run)
    answer_review(session, conversation, MaterialAnswer(operation_id=new_id(), expected_revision=project.revision,
        review_id=review["id"], answers={item["id"]: item["suggestedAnswer"] for item in review["questions"]}), settings)
    session.commit()


def test_r7_native_schema_and_actual_decoding_keep_r6_unchanged():
    old = storyboard_input()
    assert validate_artifact("storyboarder", old) == old
    original_digest, original_text = payload_digest(old), render_artifact(old)
    legacy = next(tool for tool in tool_catalog("storyboarder", "R6") if tool["name"] == "write_artifact")["inputSchema"]
    assert "existingAssetId" not in json.dumps(legacy)
    assert legacy["$defs"]["BoundaryStoryboardArtifact"]["properties"]["schemaVersion"]["const"] == 2
    tools = agent_tools("storyboarder", False, MATERIAL_NATIVE_AGENT_PROTOCOL, catalog_version="R7")
    schema = next(tool["function"]["parameters"] for tool in tools if tool["function"]["name"] == "write_artifact")
    artifact = reusable_storyboard(["explicitly-selected-asset", None])
    arguments = {"targetId": None, "title": None, "artifact": artifact}
    Draft202012Validator(schema).validate(arguments)
    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "actual-protocol-decoding",
        "type": "function", "function": {"name": "write_artifact", "arguments": json.dumps(arguments)}}]}
    decision, identifier = decode_platform_message(message, False, "storyboarder", MATERIAL_NATIVE_AGENT_PROTOCOL, "R7")
    assert identifier == "actual-protocol-decoding" and decision["arguments"]["artifact"] == artifact
    assert payload_digest(validate_artifact("storyboarder", old)) == original_digest
    assert render_artifact(old) == original_text
    assert next(tool for tool in tool_catalog("storyboarder", "R6") if tool["name"] == "write_artifact")["inputSchema"] == legacy


@pytest.mark.parametrize("existing,prompt", [(None, None), ("selected", image_prompt())])
def test_boundary_requires_exactly_one_real_source(existing, prompt):
    artifact = reusable_storyboard([None, None])
    artifact["keyframes"][0].update(existingAssetId=existing, imagePrompt=prompt)
    with pytest.raises(ValidationError):
        validate_artifact("storyboarder", artifact)


def test_mixed_existing_first_frame_creates_only_new_jobs_and_preserves_confirmation(reference_workspace):
    session, settings, project, user, add_image, create_run = reference_workspace
    existing = add_image("上一段真实尾帧.png")
    run, step, conversation = create_run("storyboarder", [existing], catalog_version="R7")
    assert run.limits_json["toolCatalogVersion"] == "R7"
    disabled_accounts(session, user)
    artifact = reusable_storyboard([existing.id, None, None])
    result = save_storyboard(session, settings, project, run, step, artifact)
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id).order_by(ImageJob.created_at)).all()
    assert [job.frame_key for job in jobs] == ["frame-1", "frame-2"]
    assert jobs[0].dependency_ids == [] and jobs[1].dependency_ids == [jobs[0].id]
    assert all(job.reference_asset_ids == [existing.id] for job in jobs)
    assert all(job.status == "awaiting_confirmation" and job.submitted_at is None and job.account_id is None for job in jobs)
    assert len(image_prompt_items(artifact)) == 2
    expected = [{"frameKey": "frame-0", "label": "第1张边界图片", "status": "reused", "assetId": existing.id,
        "sha256": existing.sha256, "width": 960, "height": 540}]
    assert run.result_json["materialReview"]["reusedFrames"] == expected
    current = view(session, conversation)
    assert current["pendingReview"]["imageCount"] == 2 and current["pendingReview"]["reusedImages"] == expected
    node = next(node for node in current["nodes"] if node["id"] == result["sourceNodeId"])
    assert node["delivery"]["reusedFrames"] == expected and len(node["delivery"]["segments"]) == 2
    with pytest.raises(AppError) as invalid:
        freeze_boundaries(session, run, result["sourceNodeId"], "shot-1", settings)
    assert invalid.value.code == "VIDEO_FRAMES_UNCONFIRMED"
    approve(session, settings, project, run, conversation)
    assert all(job.status == "queued" and job.submitted_at is None for job in jobs)
    assert run.status == "waiting_confirmation"
    with pytest.raises(AppError) as invalid:
        freeze_boundaries(session, run, result["sourceNodeId"], "shot-1", settings)
    assert invalid.value.code == "VIDEO_FRAMES_UNCONFIRMED"


def test_internal_reused_frame_uses_correct_keys_and_breaks_only_unneeded_dependency(reference_workspace):
    session, settings, project, user, add_image, create_run = reference_workspace
    existing = add_image()
    run, step, _ = create_run("storyboarder", [existing], catalog_version="R7")
    disabled_accounts(session, user)
    save_storyboard(session, settings, project, run, step, reusable_storyboard([None, existing.id, None, None]))
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id).order_by(ImageJob.created_at)).all()
    assert [job.frame_key for job in jobs] == ["frame-0", "frame-2", "frame-3"]
    assert [job.dependency_ids for job in jobs] == [[], [], [jobs[1].id]]
    assert all(existing.id in job.reference_asset_ids for job in jobs)


def test_two_independent_branches_bind_the_same_existing_image_without_image_jobs(reference_workspace):
    session, settings, project, _, add_image, create_run = reference_workspace
    common, ending_a, ending_b = add_image("F05.png"), add_image("FA01.png"), add_image("FB01.png")
    frozen_branches = []
    for ending in (ending_a, ending_b):
        run, step, conversation = create_run("storyboarder", [common, ending], catalog_version="R7")
        result = save_storyboard(session, settings, project, run, step, reusable_storyboard([common.id, ending.id]))
        assert not session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()
        assert public_review(session, run)["imageCount"] == 0
        with pytest.raises(AppError) as invalid:
            freeze_boundaries(session, run, result["sourceNodeId"], "shot-1", settings)
        assert invalid.value.code == "VIDEO_FRAMES_UNCONFIRMED"
        approve(session, settings, project, run, conversation)
        assert run.result_json["materialReview"]["status"] == "approved"
        frozen = freeze_boundaries(session, run, result["sourceNodeId"], "shot-1", settings)
        validate_frozen_boundaries(session, run.owner_id, frozen, settings)
        frozen_branches.append(frozen)
    assert frozen_branches[0]["frames"][0] == frozen_branches[1]["frames"][0]
    assert frozen_branches[0]["frames"][0]["assetId"] == common.id
    assert frozen_branches[0]["frames"][0]["sha256"] == common.sha256
    assert not session.scalars(select(ImageAccount)).all()


@pytest.mark.parametrize("kind", ["unselected", "foreign", "wrong_mime", "changed_hash", "missing_file", "portrait"])
def test_invalid_existing_frame_is_rejected_before_project_or_job_write(reference_workspace, kind):
    session, settings, project, _, add_image, create_run = reference_workspace
    existing = add_image()
    selected = [] if kind == "unselected" else [existing]
    run, step, _ = create_run("storyboarder", selected, catalog_version="R7")
    if kind == "foreign":
        existing.project_id = create_project_record(session, project.owner_id, "其他项目").id
    elif kind == "wrong_mime":
        existing.mime_type = "video/mp4"
    elif kind == "changed_hash":
        path = settings.data_dir / existing.relpath
        path.write_bytes(path.read_bytes() + b"changed")
    elif kind == "missing_file":
        (settings.data_dir / existing.relpath).unlink()
    elif kind == "portrait":
        path = settings.data_dir / existing.relpath
        with Image.open(path) as image:
            image.transpose(Image.Transpose.ROTATE_90).save(path, "PNG")
        existing.bytes = path.stat().st_size
        existing.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        run.limits_json = {**run.limits_json, "materialContext": {**run.limits_json["materialContext"],
            "referenceAssets": [{"assetId": existing.id, "name": existing.name, "sha256": existing.sha256}]}}
    session.commit()
    revision = project.revision
    canvas = session.scalar(select(Canvas).where(Canvas.project_id == project.id, Canvas.scene_id == run.scene_id))
    before = copy.deepcopy(canvas.data_json)
    with pytest.raises(AppError) as invalid:
        save_storyboard(session, settings, project, run, step, reusable_storyboard([existing.id, None]))
    assert invalid.value.code in {"STORYBOARD_EXISTING_FRAME_NOT_SELECTED", "MATERIAL_REFERENCE_ASSET_INVALID",
        "MATERIAL_REFERENCE_IMAGE_REQUIRED", "MATERIAL_REFERENCE_ASSET_CHANGED", "ASSET_FILE_NOT_FOUND", "FRAME_ASPECT_INVALID"}
    assert project.revision == revision and canvas.data_json == before
    assert not session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()


def test_reused_image_file_is_checked_again_on_confirmation_and_video_binding(reference_workspace):
    session, settings, project, _, add_image, create_run = reference_workspace
    first, last = add_image(), add_image()
    run, step, conversation = create_run("storyboarder", [first, last], catalog_version="R7")
    result = save_storyboard(session, settings, project, run, step, reusable_storyboard([first.id, last.id]))
    path = settings.data_dir / first.relpath
    raw = path.read_bytes()
    path.write_bytes(raw + b"changed")
    with pytest.raises(AppError) as invalid:
        approve(session, settings, project, run, conversation)
    assert invalid.value.code == "MATERIAL_REFERENCE_ASSET_CHANGED"
    path.write_bytes(raw)
    approve(session, settings, project, run, conversation)
    frozen = freeze_boundaries(session, run, result["sourceNodeId"], "shot-1", settings)
    path.write_bytes(raw + b"changed")
    with pytest.raises(AppError) as invalid:
        validate_frozen_boundaries(session, run.owner_id, frozen, settings)
    assert invalid.value.code == "MATERIAL_REFERENCE_ASSET_CHANGED"


def test_five_selected_references_allow_known_previous_frame_and_reject_sixth_slot(reference_workspace):
    session, settings, project, user, add_image, create_run = reference_workspace
    selected = [add_image(f"参考{index}.png") for index in range(5)]
    run, step, _ = create_run("storyboarder", selected, catalog_version="R7")
    disabled_accounts(session, user)
    save_storyboard(session, settings, project, run, step, reusable_storyboard([selected[0].id, None]))
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()
    assert len(jobs) == 1 and jobs[0].reference_asset_ids == [asset.id for asset in selected]
    other, other_step, _ = create_run("storyboarder", selected, catalog_version="R7")
    with pytest.raises(AppError) as invalid:
        save_storyboard(session, settings, project, other, other_step, reusable_storyboard([selected[0].id, None, None]))
    assert invalid.value.code == "MATERIAL_REFERENCE_CAPACITY_EXCEEDED"
    assert not [item for item in session.new if isinstance(item, ImageJob)]


def test_revision_cancels_only_new_unsubmitted_jobs_and_keeps_reused_resource(reference_workspace):
    session, settings, project, user, add_image, create_run = reference_workspace
    existing = add_image()
    run, step, conversation = create_run("storyboarder", [existing], catalog_version="R7")
    disabled_accounts(session, user)
    save_storyboard(session, settings, project, run, step, reusable_storyboard([existing.id, None]))
    review = public_review(session, run)
    answer_review(session, conversation, MaterialAnswer(operation_id=new_id(), expected_revision=project.revision,
        review_id=review["id"], requested_changes="保留首帧真实图片，修改尾帧镜头构图。"), settings)
    session.commit()
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()
    assert len(jobs) == 1 and jobs[0].status == "cancelled" and jobs[0].submitted_at is None
    assert (settings.data_dir / existing.relpath).is_file()
    assert run.result_json["materialReview"]["status"] == "revision_requested"
    assert run.result_json["materialReview"]["reusedFrames"][0]["assetId"] == existing.id


def test_legacy_catalog_cannot_enable_reuse_by_submitting_a_new_schema(reference_workspace):
    session, settings, project, _, add_image, create_run = reference_workspace
    existing = add_image()
    run, step, _ = create_run("storyboarder", [existing], catalog_version="R5")
    revision = project.revision
    with pytest.raises(AppError) as invalid:
        save_storyboard(session, settings, project, run, step, reusable_storyboard([existing.id, None]))
    assert invalid.value.code == "STORYBOARD_REUSABLE_CATALOG_REQUIRED"
    assert project.revision == revision
    assert not session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()
