import base64
import copy
import hashlib
import json
import sqlite3
import subprocess
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings, sqlite_database_path
from app.media_models import MediaSequence
from app.services.video_boundaries import media_payload
from conftest import PRODUCT_ROOT
from test_api import create_project, error_code, registered_client, scene_id
from test_media_tools import media_service
from test_story_workspace import add_clip, workspace


def upload(client, project_id, path, mime="video/mp4"):
    with path.open("rb") as stream:
        response = client.post(f"/projects/{project_id}/assets", files={"file": (path.name, stream, mime)})
    assert response.status_code == 201, response.text
    return response.json()


def current(client, project_id):
    return client.get(f"/projects/{project_id}").json()


def save_project(client, envelope, project):
    response = client.put(f"/projects/{project['id']}", json={"operationId": str(uuid.uuid4()), "expectedRevision": envelope["revision"], "project": project})
    assert response.status_code == 200, response.text
    return response.json()


def target_clip(envelope, target_scene, clip_id):
    scene = next(node for node in envelope["project"]["graphData"]["nodes"] if node["id"] == target_scene)
    return next(clip for track in scene["data"]["timeline"]["tracks"] for clip in track["clips"] if clip["id"] == clip_id)


def check(client, project_id, target_scene, asset_id, clip_id, revision):
    return client.post(f"/projects/{project_id}/media-adoptions/check", json={"operationId": str(uuid.uuid4()), "expectedRevision": revision,
        "sceneId": target_scene, "assetId": asset_id, "clipId": clip_id, "mode": "replace"})


def confirm(client, value, operation=None):
    return client.post(f"/projects/{value['projectId']}/media-adoptions/{value['id']}/apply", json={"operationId": operation or str(uuid.uuid4()),
        "expectedRevision": value["projectRevision"], "confirmationHash": value["confirmationHash"]})


def test_replacement_preserves_trim_speed_qte_shared_assets_and_is_idempotent(workspace, sample_video):
    client, project_id = workspace["client"], workspace["projectId"]
    original = add_clip(workspace)
    interaction = workspace["execute"]("edit_interaction", {"sceneId": workspace["first"], "label": "立即选择", "startTime": 0.2,
        "duration": 0.5, "mode": "qte", "targetId": workspace["second"], "timeoutTargetId": workspace["second"]})
    assert interaction.status_code == 200, interaction.text
    envelope = current(client, project_id)
    shared = client.post(f"/projects/{project_id}/timeline-assets", json={"operationId": str(uuid.uuid4()), "expectedRevision": envelope["revision"],
        "sceneId": workspace["second"], "assetId": workspace["asset"]["id"], "duration": 1})
    assert shared.status_code == 200, shared.text
    candidate = upload(client, project_id, sample_video)
    snapshot = copy.deepcopy(candidate["project"])
    clip = target_clip(candidate, workspace["first"], original["clipId"])
    clip.update(startTime=1.25, duration=1.2, sourceStart=0.4, sourceDuration=1.8, playbackRate=1.5, volume=0.35, fit="cover")
    first = next(node for node in snapshot["graphData"]["nodes"] if node["id"] == workspace["first"])
    actual = next(item for track in first["data"]["timeline"]["tracks"] for item in track["clips"] if item["id"] == original["clipId"])
    actual.update(clip)
    first["data"]["timeline"]["duration"] = 3
    before = save_project(client, candidate, snapshot)
    response = check(client, project_id, workspace["first"], candidate["asset"]["id"], original["clipId"], before["revision"])
    assert response.status_code == 201, response.text
    value = response.json()["adoption"]
    assert value["status"] == "ready"
    assert value["requiredSeconds"] == pytest.approx(2.2)
    operation = str(uuid.uuid4())
    adopted = confirm(client, value, operation)
    assert adopted.status_code == 200, adopted.text
    assert confirm(client, value, operation).json() == adopted.json()
    after = adopted.json()["project"]
    expected = copy.deepcopy(before["project"]["graphData"])
    expected_clip = next(item for node in expected["nodes"] if node["id"] == workspace["first"] for track in node["data"]["timeline"]["tracks"] for item in track["clips"] if item["id"] == original["clipId"])
    expected_clip.update(type="video", assetId=candidate["asset"]["id"], src=candidate["asset"]["url"], poster=candidate["asset"]["posterUrl"])
    assert after["project"]["graphData"] == expected
    assert target_clip(after, workspace["second"], next(item["id"] for node in before["project"]["graphData"]["nodes"] if node["id"] == workspace["second"] for track in node["data"]["timeline"]["tracks"] for item in track["clips"]))["assetId"] == workspace["asset"]["id"]
    listing = client.get(f"/projects/{project_id}/scenes/{workspace['first']}/media-candidates").json()
    assert any(item["assetId"] == candidate["asset"]["id"] and item["adopted"] for item in listing["candidates"])


def test_short_candidate_conflict_persists_until_keep_or_recheck(workspace, sample_video, tmp_path):
    client, project_id = workspace["client"], workspace["projectId"]
    original = add_clip(workspace)
    short = tmp_path / "short.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(sample_video), "-t", "0.4", "-c:v", "libx264", "-an", str(short)], check=True, capture_output=True)
    candidate = upload(client, project_id, short)
    before = current(client, project_id)
    response = check(client, project_id, workspace["first"], candidate["asset"]["id"], original["clipId"], before["revision"])
    assert response.status_code == 201, response.text
    conflict = response.json()["adoption"]
    assert conflict["status"] == "duration_conflict" and conflict["availableSeconds"] < conflict["requiredSeconds"]
    assert error_code(confirm(client, conflict), 409) == "MEDIA_DURATION_CONFLICT"
    assert current(client, project_id) == before
    listing_path = f"/projects/{project_id}/scenes/{workspace['first']}/media-candidates"
    assert client.get(listing_path).json()["publishAllowed"] is False
    adjusted = copy.deepcopy(before["project"])
    clip = target_clip({"project": adjusted}, workspace["first"], original["clipId"])
    clip.update(duration=0.2, sourceStart=0, sourceDuration=0.2)
    changed = save_project(client, before, adjusted)
    checked = check(client, project_id, workspace["first"], candidate["asset"]["id"], original["clipId"], changed["revision"])
    assert checked.status_code == 201, checked.text
    value = checked.json()["adoption"]
    assert value["status"] == "ready"
    applied = confirm(client, value)
    assert applied.status_code == 200, applied.text
    listing = client.get(listing_path).json()
    assert listing["publishAllowed"] is True
    assert next(item for item in listing["adoptions"] if item["id"] == conflict["id"])["status"] == "superseded"


def test_stale_confirmation_and_foreign_owner_never_change_media(workspace, sample_video, live_service):
    client, project_id = workspace["client"], workspace["projectId"]
    original = add_clip(workspace)
    candidate = upload(client, project_id, sample_video)
    value = check(client, project_id, workspace["first"], candidate["asset"]["id"], original["clipId"], candidate["revision"]).json()["adoption"]
    snapshot = copy.deepcopy(candidate["project"])
    snapshot["title"] = "保留人工编辑"
    changed = save_project(client, candidate, snapshot)
    assert error_code(confirm(client, value), 409) == "PROJECT_REVISION_CONFLICT"
    assert current(client, project_id) == changed
    outsider, _ = registered_client(live_service)
    assert error_code(outsider.get(f"/projects/{project_id}/scenes/{workspace['first']}/media-candidates"), 404) == "PROJECT_NOT_FOUND"
    assert error_code(confirm(outsider, value), 404) == "PROJECT_NOT_FOUND"
    outsider.close()
    kept = client.post(f"/projects/{project_id}/media-adoptions/{value['id']}/keep", json={"operationId": str(uuid.uuid4())})
    assert kept.status_code == 200 and kept.json()["adoption"]["status"] == "kept_current"


def actual_frame_paths():
    with sqlite3.connect(f"{sqlite_database_path(PRODUCT_ROOT / 'data').resolve().as_uri()}?mode=ro", uri=True) as database:
        rows = database.execute("SELECT a.relpath FROM image_jobs j JOIN assets a ON j.asset_id=a.id WHERE j.status='ready' ORDER BY j.created_at").fetchall()
    assert len(rows) >= 4
    return [PRODUCT_ROOT / "data" / row[0] for row in rows[:4]]


def create_test_sequence(client):
    project = create_project(client, "真实图片连续首尾帧")
    project_id, target_scene = project["project"]["id"], scene_id(project)
    identifiers = []
    for path in actual_frame_paths():
        project = upload(client, project_id, path, "image/png")
        identifiers.append(project["asset"]["id"])
    body = {"operationId": str(uuid.uuid4()), "expectedRevision": project["revision"], "title": "纸艺连续变化",
        "frameAssetIds": identifiers, "segments": [{"title": f"片段{index + 1}", "prompt": "固定机位，桌面上的纸艺平滑变化，保持木桌、背景与光线一致，不出现文字。", "duration": 2, "resolution": "720P"} for index in range(3)]}
    response = client.post(f"/projects/{project_id}/scenes/{target_scene}/media-sequences", json=body)
    assert response.status_code == 201, response.text
    assert client.post(f"/projects/{project_id}/scenes/{target_scene}/media-sequences", json=body).json() == response.json()
    return response.json(), identifiers


def test_four_real_images_bind_three_requests_without_model_submission(live_service):
    client, _ = registered_client(live_service)
    value, identifiers = create_test_sequence(client)
    sequence, envelope = value["sequence"], value["project"]
    project_id, target_scene = sequence["projectId"], sequence["sceneId"]
    frozen = []
    for index, segment in enumerate(sequence["segments"]):
        path = f"/projects/{project_id}/media-sequences/{sequence['id']}/segments/{segment['key']}/quote"
        body = {"operationId": str(uuid.uuid4()), "expectedRevision": envelope["revision"]}
        response = client.post(path, json=body)
        assert response.status_code == 201, response.text
        assert client.post(path, json=body).json() == response.json()
        generation = response.json()["generation"]
        assert generation["status"] == "awaiting_confirmation" and generation["remoteTaskId"] is None
        assert generation["clipId"] == segment["clipId"]
        frames = generation["input"]["boundaryFrames"]["frames"]
        assert [frame["assetId"] for frame in frames] == identifiers[index:index + 2]
        frozen.append(generation["input"]["boundaryFrames"])
    assert frozen[0]["frames"][1] == {**frozen[1]["frames"][0], "type": "last_frame"}
    engine = create_engine(f"sqlite:///{live_service.data_dir / 'sceneturn.sqlite3'}")
    with Session(engine) as session:
        stored = session.scalar(select(MediaSequence).where(MediaSequence.id == sequence["id"]))
        actual = media_payload(session, stored.owner_id, frozen[0], Settings(data_dir=live_service.data_dir))
        assert [item["type"] for item in actual] == ["first_frame", "last_frame"]
        for item, frame in zip(actual, frozen[0]["frames"], strict=True):
            assert hashlib.sha256(base64.b64decode(item["url"].split(",", 1)[1], validate=True)).hexdigest() == frame["sha256"]
    engine.dispose()
    listing = client.get(f"/projects/{project_id}/scenes/{target_scene}/media-candidates").json()
    assert len(listing["sequences"]) == 1 and len(listing["candidates"]) == 3
    assert "平滑变化" not in json.dumps(listing, ensure_ascii=False)
    assert all(len(item["frames"]) == 2 for item in listing["candidates"])
    client.close()


def test_sequences_and_pending_adoption_survive_real_service_restart(media_service, sample_video):
    client, _ = registered_client(media_service)
    value, _ = create_test_sequence(client)
    sequence = value["sequence"]
    uploaded = upload(client, sequence["projectId"], sample_video)
    project_id = sequence["projectId"]
    adopted = client.post(f"/projects/{project_id}/timeline-assets", json={"operationId": str(uuid.uuid4()), "expectedRevision": uploaded["revision"], "sceneId": sequence["sceneId"], "assetId": uploaded["asset"]["id"], "duration": 1})
    assert adopted.status_code == 200, adopted.text
    clip = next(item for node in adopted.json()["project"]["graphData"]["nodes"] if node["id"] == sequence["sceneId"] for track in node["data"]["timeline"]["tracks"] for item in track["clips"])
    checked = check(client, project_id, sequence["sceneId"], uploaded["asset"]["id"], clip["id"], adopted.json()["revision"])
    assert checked.status_code == 201, checked.text
    path = f"/projects/{project_id}/scenes/{sequence['sceneId']}/media-candidates"
    before = client.get(path).json()
    media_service.stop()
    media_service.start()
    assert client.get(path).json() == before
    assert confirm(client, checked.json()["adoption"]).status_code == 200
    client.close()


def test_actual_missing_candidate_file_blocks_adoption_and_recovers(workspace, sample_video, live_service):
    client, project_id = workspace["client"], workspace["projectId"]
    original = add_clip(workspace)
    candidate = upload(client, project_id, sample_video)
    with sqlite3.connect(live_service.data_dir / "sceneturn.sqlite3") as database:
        relpath = database.execute("SELECT relpath FROM assets WHERE id=?", (candidate["asset"]["id"],)).fetchone()[0]
    media = live_service.data_dir / relpath
    unavailable = media.with_suffix(".unavailable")
    media.rename(unavailable)
    try:
        response = check(client, project_id, workspace["first"], candidate["asset"]["id"], original["clipId"], candidate["revision"])
        assert response.status_code == 201, response.text
        value = response.json()["adoption"]
        assert value["status"] == "blocked"
        assert error_code(confirm(client, value), 409) == "MEDIA_ADOPTION_BLOCKED"
        assert client.get(f"/projects/{project_id}/scenes/{workspace['first']}/media-candidates").json()["publishAllowed"] is False
    finally:
        unavailable.rename(media)
    restored = check(client, project_id, workspace["first"], candidate["asset"]["id"], original["clipId"], candidate["revision"])
    assert restored.json()["adoption"]["status"] == "ready"
    assert confirm(client, restored.json()["adoption"]).status_code == 200


def test_confirmation_digest_and_new_selection_control_exact_target(workspace, sample_video):
    client, project_id = workspace["client"], workspace["projectId"]
    original = add_clip(workspace)
    first = upload(client, project_id, sample_video)
    second = upload(client, project_id, sample_video)
    before = current(client, project_id)
    first_check = check(client, project_id, workspace["first"], first["asset"]["id"], original["clipId"], before["revision"]).json()["adoption"]
    tampered = {**first_check, "confirmationHash": "0" * 64}
    assert error_code(confirm(client, tampered), 409) == "MEDIA_ADOPTION_CONFIRMATION_INVALID"
    assert current(client, project_id) == before
    second_check = check(client, project_id, workspace["first"], second["asset"]["id"], original["clipId"], before["revision"]).json()["adoption"]
    assert error_code(confirm(client, first_check), 409) == "MEDIA_ADOPTION_BLOCKED"
    assert confirm(client, second_check).status_code == 200
    after = current(client, project_id)
    assert target_clip(after, workspace["first"], original["clipId"])["assetId"] == second["asset"]["id"]


def test_freeze_frame_checks_actual_held_frame_and_preserves_timeline_duration(workspace, sample_video, tmp_path):
    client, project_id = workspace["client"], workspace["projectId"]
    original = add_clip(workspace)
    shorter = tmp_path / "freeze-source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(sample_video), "-t", "0.4", "-c:v", "libx264", "-an", str(shorter)], check=True, capture_output=True)
    candidate = upload(client, project_id, shorter)
    snapshot = copy.deepcopy(candidate["project"])
    clip = target_clip({"project": snapshot}, workspace["first"], original["clipId"])
    clip.update(freezeFrameTime=0.2, sourceStart=0, sourceDuration=0, duration=1)
    before = save_project(client, candidate, snapshot)
    response = check(client, project_id, workspace["first"], candidate["asset"]["id"], original["clipId"], before["revision"])
    assert response.status_code == 201, response.text
    value = response.json()["adoption"]
    assert value["status"] == "ready" and value["requiredSeconds"] == 0.2
    applied = confirm(client, value)
    assert applied.status_code == 200, applied.text
    saved = target_clip(applied.json()["project"], workspace["first"], original["clipId"])
    assert saved["duration"] == 1 and saved["freezeFrameTime"] == 0.2 and saved["sourceDuration"] == 0
