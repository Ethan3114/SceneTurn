import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from app.image_models import ImageAccount, ImageJob
from app.models import Asset
from app.services.image_queue import claim_job, finalize_response
from app.services.material_review import public_review
from app.services.sensetime import decode_image
from app.services.video_boundaries import freeze_boundaries, validate_frozen_boundaries
from conftest import sample_video
from test_material_reference_assets import disabled_accounts, reference_image, reference_workspace
from test_storyboard_existing_frames import approve, reusable_storyboard, save_storyboard


def test_saved_actual_response_registers_new_frame_while_first_frame_keeps_original_asset(reference_workspace):
    response_value = os.environ.get("OPENFMV_STORYBOARD_IMAGE_RESPONSE")
    png_value = os.environ.get("OPENFMV_STORYBOARD_IMAGE_PNG")
    if not response_value or not png_value:
        pytest.fail("请显式提供OPENFMV_STORYBOARD_IMAGE_RESPONSE和OPENFMV_STORYBOARD_IMAGE_PNG，仅回放授权的真实图片响应。")
    response_path, png_path = Path(response_value).resolve(), Path(png_value).resolve()
    if not response_path.is_file() or not png_path.is_file():
        pytest.fail("指定的真实图片响应或PNG文件不存在。")
    response_raw, png_raw = response_path.read_bytes(), png_path.read_bytes()
    assert decode_image(json.loads(response_raw), 2048, 1152) == png_raw
    digest = hashlib.sha256(png_raw).hexdigest()
    session, settings, project, user, add_image, create_run = reference_workspace
    existing = add_image("已保存的真实夜景边界.png")
    shutil.copyfile(png_path, settings.data_dir / existing.relpath)
    existing.sha256, existing.bytes, existing.width, existing.height = digest, len(png_raw), 2048, 1152
    session.commit()
    run, step, conversation = create_run("storyboarder", [existing], catalog_version="R7")
    disabled_accounts(session, user)
    for account in session.scalars(select(ImageAccount)):
        account.status = "available"
    session.commit()
    result = save_storyboard(session, settings, project, run, step, reusable_storyboard([existing.id, None]))
    jobs = session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()
    assert len(jobs) == 1 and jobs[0].frame_key == "frame-1"
    job = jobs[0]
    approve(session, settings, project, run, conversation)
    assert claim_job(session) == job.id
    destination = settings.data_dir / "image-responses" / f"{job.id}.json"
    destination.parent.mkdir()
    shutil.copyfile(response_path, destination)
    job.status, job.response_relpath = "response_saved", destination.relative_to(settings.data_dir).as_posix()
    session.commit()
    finalize_response(session, job, settings)
    assert job.status == "ready" and job.attempt_count == 0 and job.submitted_at is None
    actual = session.get(Asset, job.asset_id)
    assert (settings.data_dir / actual.relpath).read_bytes() == png_raw and actual.sha256 == digest
    assert public_review(session, run)["status"] == "review_images"
    approve(session, settings, project, run, conversation)
    frozen = freeze_boundaries(session, run, result["sourceNodeId"], "shot-1", settings)
    assert frozen["frames"][0] == {"type": "first_frame", "frameKey": "frame-0", "assetId": existing.id, "sha256": digest}
    assert frozen["frames"][1]["assetId"] == actual.id and actual.id != existing.id
    validate_frozen_boundaries(session, user.id, frozen, settings)
    assert len(session.scalars(select(ImageJob).where(ImageJob.run_id == run.id)).all()) == 1
    assert all(account.active_job_id is None for account in session.scalars(select(ImageAccount)))
