import argparse
import asyncio
import copy
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings
from app.db import SessionLocal
from app.image_models import ImageAccount
from app.media_models import MediaAdoption
from app.media_schemas import AdoptionCheck, AdoptionConfirm, AdoptionKeep
from app.models import Asset, Canvas, Generation, Project, new_id
from app.schemas import ProjectSnapshot
from app.services.generation import adopt_generation, generation_view
from app.services.media import _validate_and_store, asset_view, resolve_asset_path
from app.services.media_adoptions import apply_adoption, check_adoption, conflict_issues, find_clip, keep_current
from app.services.projects import iso_timestamp, lock_write, payload_digest, project_envelope, record_project_version, require_scene, scene_node
from app.services.video_join import join_videos
from agent_smoke_evidence import write_private_json
from smoke import validate_media, verify_local_data
from smoke_registry import EvidenceStore


def request_data(directory):
    path = directory / "confirmation-request.json"
    return json.loads((path if path.exists() else directory / "request.json").read_bytes())


def ready_values(session, request):
    values = [session.get(Generation, item["id"]) for item in request["generations"]]
    if len(values) != 3 or any(value is None or value.project_id != request["projectId"] or value.scene_id != request["sceneId"] or value.status != "ready" or not value.asset_id for value in values):
        raise ValueError("三个首尾帧视频尚未全部完成真实生成")
    for index, value in enumerate(values):
        frames = value.input_json["boundaryFrames"]["frames"]
        if frames != request["generations"][index]["frames"] or value.input_json["model"] != "wan3.0-video" or value.input_json["duration"] != 2:
            raise ValueError("真实视频与已确认的首尾图片或模型参数不一致")
        if index and values[index - 1].input_json["boundaryFrames"]["frames"][1]["assetId"] != frames[0]["assetId"]:
            raise ValueError("相邻视频没有复用同一张边界图片")
    return values


def adopt(directory):
    request = request_data(directory)
    with SessionLocal() as session:
        values = ready_values(session, request)
        for value in values:
            project = session.get(Project, value.project_id)
            adopt_generation(session, value.id, value.owner_id, project.revision, f"sequence-adopt-{value.id}", get_settings())
        write_private_json(directory / "adopted.json", {"projectId": request["projectId"], "generationIds": [value.id for value in values], "allThreeAdopted": True})
    print("三个实际视频已采用到各自固定片段。")


def register_media(session, project, media, name, source_ids):
    lock_write(session)
    session.refresh(project)
    asset = Asset(id=new_id(), owner_id=project.owner_id, project_id=project.id, generation_id=None,
        name=name, relpath=media.relpath, poster_relpath=media.poster_relpath, mime_type=media.mime_type,
        bytes=media.bytes, sha256=media.sha256, duration_ms=media.duration_ms, width=media.width, height=media.height,
        metadata_json={**media.metadata_json, "provider": "local-ffmpeg", "sourceAssetIds": source_ids, "purpose": "candidate-validation"})
    session.add(asset)
    session.flush()
    view = asset_view(asset)
    snapshot = copy.deepcopy(project.data_json)
    snapshot["assets"].append({"id": asset.id, "type": "video", "name": name, "path": view["url"], "relativePath": view["url"],
        "importedAt": iso_timestamp(asset.created_at), "metadata": {"duration": asset.duration_ms / 1000, "width": asset.width, "height": asset.height}})
    project.data_json, project.revision = snapshot, project.revision + 1
    record_project_version(session, project)
    session.commit()
    return asset


def apply_candidate(session, project, scene_id, clip_id, asset):
    value = check_adoption(session, project.id, project.owner_id, AdoptionCheck(operation_id=str(uuid.uuid4()),
        expected_revision=project.revision, scene_id=scene_id, clip_id=clip_id, asset_id=asset.id, mode="replace"), get_settings())
    if value.status != "ready":
        raise ValueError("验证用长候选没有通过真实裁切范围检查")
    return apply_adoption(session, project.id, value.id, project.owner_id,
        AdoptionConfirm(operation_id=str(uuid.uuid4()), expected_revision=value.project_revision, confirmation_hash=value.confirmation_hash), get_settings())


def exercise(directory):
    if (directory / "candidate-exercise.json").exists():
        raise ValueError("候选验证已经执行，不得重复修改验证项目")
    request, settings = request_data(directory), get_settings()
    with SessionLocal() as session:
        values = ready_values(session, request)
        project = session.get(Project, request["projectId"])
        assets = [session.get(Asset, value.asset_id) for value in values]
        lock_write(session)
        snapshot = copy.deepcopy(project.data_json)
        scene = next(node for node in snapshot["graphData"]["nodes"] if node["id"] == request["sceneId"])
        clip = find_clip(scene, values[0].clip_id)
        if clip is None:
            raise ValueError("请先采用三个真实视频")
        clip.update(sourceStart=0.1, sourceDuration=1.5, duration=1.5, playbackRate=1, volume=0.5)
        shared = scene_node(new_id(), "共享素材验证", 2)
        shared["data"]["timeline"]["duration"] = 1.5
        shared["data"]["timeline"]["tracks"][0]["clips"].append({**copy.deepcopy(clip), "id": new_id(), "startTime": 0})
        snapshot["graphData"]["nodes"].append(shared)
        session.add(Canvas(owner_id=project.owner_id, project_id=project.id, scene_id=shared["id"], data_json={"sceneId": shared["id"], "nodes": [], "connections": [], "viewport": {"x": 0, "y": 0, "k": 1}}))
        button_id = new_id()
        scene["data"]["timeline"]["tracks"][1]["clips"].append({"id": button_id, "type": "button", "enabled": True,
            "rect": {"x": 0.3, "y": 0.75, "width": 0.4, "height": 0.12}, "label": "保留互动检查", "startTime": 0.2, "duration": 0.5,
            "mode": "qte", "pauseOnShow": False, "qte": {"input": "click", "prompt": "保留互动检查", "clickCount": 1, "showCountdown": True}})
        for event in ("click", "timeout"):
            snapshot["graphData"]["edges"].append({"id": new_id(), "source": scene["id"], "target": shared["id"], "sourceHandle": f"button:{button_id}:{event}", "targetHandle": None})
        project.data_json = ProjectSnapshot.model_validate(snapshot).model_dump(by_alias=True, exclude_unset=True)
        project.revision += 1
        record_project_version(session, project)
        session.commit()
        before = copy.deepcopy(project.data_json["graphData"])
        inputs = [{"relpath": asset.relpath, "bytes": asset.bytes, "sha256": asset.sha256,
            "width": asset.width, "height": asset.height, "durationMs": asset.duration_ms} for asset in assets[:2]]
        session.commit()
        joined = asyncio.run(join_videos(inputs, settings))
        long_asset = register_media(session, project, joined, "较长视频候选.mp4", [asset.id for asset in assets[:2]])
        applied = apply_candidate(session, project, request["sceneId"], values[0].clip_id, long_asset)
        expected = copy.deepcopy(before)
        target = find_clip(next(node for node in expected["nodes"] if node["id"] == request["sceneId"]), values[0].clip_id)
        media = asset_view(long_asset)
        target.update(type="video", assetId=long_asset.id, src=media["url"], poster=media["posterUrl"])
        if applied["project"]["project"]["graphData"] != expected:
            raise ValueError("指定片段替换改变了其他编辑、共享引用或互动")
        short_path = directory / "short-candidate.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(resolve_asset_path(settings, assets[0].relpath)), "-t", "0.4", "-c:v", "libx264", "-an", str(short_path)], check=True, capture_output=True)
        short_media = _validate_and_store(short_path, settings, "较短视频候选.mp4", True)
        short_asset = register_media(session, project, short_media, "较短视频候选.mp4", [assets[0].id])
        conflict = check_adoption(session, project.id, project.owner_id, AdoptionCheck(operation_id=str(uuid.uuid4()), expected_revision=project.revision,
            scene_id=request["sceneId"], clip_id=values[0].clip_id, asset_id=short_asset.id, mode="replace"), settings)
        if conflict.status != "duration_conflict" or not conflict_issues(session, project.id, project.owner_id):
            raise ValueError("较短真实视频没有形成持久冲突与发布阻止状态")
        write_private_json(directory / "candidate-exercise.json", {"originalAssetId": assets[0].id, "longAssetId": long_asset.id,
            "shortAssetId": short_asset.id, "conflictId": conflict.id, "clipId": values[0].clip_id, "sharedSceneId": shared["id"],
            "trimInteractionAndSharedReferencesPreserved": True, "shortCandidateBlocked": True})
    print("实际长候选已局部采用；裁切、互动和共享引用保持一致，短候选冲突已保存。")


def snapshot(directory):
    request = request_data(directory)
    with SessionLocal() as session:
        values = ready_values(session, request)
        project = session.get(Project, request["projectId"])
        adoptions = session.scalars(select(MediaAdoption).where(MediaAdoption.project_id == project.id).order_by(MediaAdoption.id)).all()
        return {"project": project_envelope(session, project), "generations": [generation_view(value) for value in values],
            "adoptions": [{"id": value.id, "status": value.status, "proposal": value.proposal_json, "hash": value.confirmation_hash} for value in adoptions],
            "imageAccounts": [{"slot": value.slot, "status": value.status, "activeJobId": value.active_job_id}
                for value in session.scalars(select(ImageAccount).where(ImageAccount.owner_id == project.owner_id).order_by(ImageAccount.slot))]}


def record(directory):
    after = json.loads((directory / "after-restart.json").read_bytes())
    if after.get("restartVerified") is not True:
        raise ValueError("真实服务重启验证尚未完成")
    request, settings = request_data(directory), get_settings()
    exercise_result = json.loads((directory / "candidate-exercise.json").read_bytes())
    with SessionLocal() as session:
        project = session.get(Project, request["projectId"])
        conflict = session.get(MediaAdoption, exercise_result["conflictId"])
        if conflict.status == "duration_conflict":
            keep_current(session, project.id, conflict.id, project.owner_id, AdoptionKeep(operation_id=f"verification-keep-{conflict.id}"))
        original = session.get(Asset, exercise_result["originalAssetId"])
        if find_clip(require_scene(project, request["sceneId"]), exercise_result["clipId"])["assetId"] != original.id:
            apply_candidate(session, project, request["sceneId"], exercise_result["clipId"], original)
        if conflict_issues(session, project.id, project.owner_id):
            raise ValueError("验证项目仍有未处理的素材冲突")
    store = EvidenceStore(ROOT, stage="阶段2-3")
    browser_media = []
    verified_records = []
    for item in request["generations"]:
        generation, media_path = verify_local_data(ROOT / "data", item["id"])
        media = validate_media(media_path)
        stream = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration", "-of", "json", str(media_path)], check=True, capture_output=True).stdout)["streams"][0]
        if abs(float(stream["duration"]) - generation.input["duration"]) > 0.001:
            raise ValueError("真实视频画面时长与确认参数不一致")
        browser_media.append({"generationId": generation.id, "assetId": generation.assetId,
            "path": str(media_path.relative_to(ROOT)), "sha256": hashlib.sha256(media_path.read_bytes()).hexdigest()})
        verified = {"verifiedAt": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
            "generationId": generation.id, "remoteTaskId": generation.remoteTaskId, "remoteRequestId": generation.remoteRequestId,
            "projectId": generation.projectId, "sceneId": generation.sceneId, "clipId": generation.clipId, "assetId": generation.assetId,
            "model": generation.input["model"], "usage": generation.usage, "retryCount": generation.retryCount,
            "startedAt": generation.startedAt.isoformat(), "finishedAt": generation.finishedAt.isoformat(),
            "elapsedSeconds": (generation.finishedAt - generation.startedAt).total_seconds(), "media": media,
            "firstAndLastFrames": generation.input["boundaryFrames"]["frames"], "restartVerified": True,
            "candidateExerciseSha256": hashlib.sha256((directory / "candidate-exercise.json").read_bytes()).hexdigest()}
        store.register(verified)
        verified_records.append(verified)
    write_private_json(directory / "browser-media.json", {"videos": browser_media})
    playback_path = directory / "browser-playback.json"
    if playback_path.exists():
        playback = json.loads(playback_path.read_bytes())
        if playback.get("reloadVerified") is not True or {item["generationId"] for item in playback["results"]} != {item["generationId"] for item in browser_media}:
            raise ValueError("浏览器播放记录没有覆盖三个真实视频")
        for result in playback["results"]:
            source = next(item for item in browser_media if item["generationId"] == result["generationId"])
            if result["sha256"] != source["sha256"] or result["playback"]["error"] is not None or result["playback"]["currentTime"] <= 0.1:
                raise ValueError("浏览器播放记录与真实媒体不一致")
        store.revalidate(verified_records, {"candidateExercise": exercise_result,
            "restartProofSha256": hashlib.sha256((directory / "after-restart.json").read_bytes()).hexdigest(),
            "browserPlaybackSha256": hashlib.sha256(playback_path.read_bytes()).hexdigest()})
    write_private_json(directory / "proof.json", {"threeRealVideos": True, "sharedBoundaryAssets": True, "restartVerified": True,
        "candidateExercise": exercise_result, "generationIds": [item["id"] for item in request["generations"]]})
    print("三段真实首尾帧视频、候选、裁切、互动、共享引用和重启恢复验证通过。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["adopt", "exercise", "before", "after", "record"])
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if not directory.is_relative_to(ROOT / ".intermediate"):
        raise ValueError("验证目录必须位于本产品.intermediate内")
    if args.action == "adopt":
        adopt(directory)
    elif args.action == "exercise":
        exercise(directory)
    elif args.action == "before":
        write_private_json(directory / "before-restart.json", snapshot(directory))
        print("已保存真实候选和冲突的重启前状态。")
    elif args.action == "after":
        value = snapshot(directory)
        if value != json.loads((directory / "before-restart.json").read_bytes()):
            raise ValueError("真实任务、媒体、候选或冲突在服务重启后发生变化")
        write_private_json(directory / "after-restart.json", {"restartVerified": True, "snapshotSha256": payload_digest(value)})
        print("真实候选、冲突、视频及账号占用在服务重启后保持一致。")
    else:
        record(directory)


if __name__ == "__main__":
    main()
