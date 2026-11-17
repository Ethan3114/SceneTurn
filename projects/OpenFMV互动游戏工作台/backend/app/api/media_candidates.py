from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user, get_db
from app.agent_models import AgentStep
from app.api.generations import GenerationResult
from app.core.config import get_settings
from app.media_models import MediaAdoption, MediaSequence
from app.media_schemas import AdoptionApplied, AdoptionCheck, AdoptionConfirm, AdoptionEnvelope, AdoptionKeep, CandidateList, MediaOperation, SequenceCreate, SequenceEnvelope
from app.models import Asset, Canvas, Generation, User
from app.services.generation import generation_view
from app.services.media_adoptions import adoption_view, apply_adoption, check_adoption, conflict_issues, keep_current
from app.services.media_sequences import create_sequence, quote_segment, sequence_view
from app.services.projects import project_envelope, require_project, require_scene


router = APIRouter(tags=["media-candidates"])


@router.post("/projects/{project_id}/scenes/{scene_id}/media-sequences", response_model=SequenceEnvelope, response_model_exclude_unset=True, status_code=201)
def create(project_id: str, scene_id: str, body: SequenceCreate, user: User = Depends(current_user), session: Session = Depends(get_db)):
    sequence = create_sequence(session, project_id, scene_id, user.id, body, get_settings())
    return {"sequence": sequence_view(session, sequence), "project": project_envelope(session, require_project(session, project_id, user.id))}


@router.post("/projects/{project_id}/media-sequences/{sequence_id}/segments/{shot_key}/quote", response_model=GenerationResult, status_code=201)
def quote(project_id: str, sequence_id: str, shot_key: str, body: MediaOperation, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"generation": generation_view(quote_segment(session, project_id, sequence_id, shot_key, user.id, body, get_settings()))}


@router.post("/projects/{project_id}/media-adoptions/check", response_model=AdoptionEnvelope, status_code=201)
def check(project_id: str, body: AdoptionCheck, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"adoption": adoption_view(check_adoption(session, project_id, user.id, body, get_settings()))}


@router.post("/projects/{project_id}/media-adoptions/{adoption_id}/apply", response_model=AdoptionApplied, response_model_exclude_unset=True)
def apply(project_id: str, adoption_id: str, body: AdoptionConfirm, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return apply_adoption(session, project_id, adoption_id, user.id, body, get_settings())


@router.post("/projects/{project_id}/media-adoptions/{adoption_id}/keep", response_model=AdoptionEnvelope)
def keep(project_id: str, adoption_id: str, body: AdoptionKeep, user: User = Depends(current_user), session: Session = Depends(get_db)):
    return {"adoption": adoption_view(keep_current(session, project_id, adoption_id, user.id, body))}


@router.get("/projects/{project_id}/scenes/{scene_id}/media-candidates", response_model=CandidateList)
def candidates(project_id: str, scene_id: str, conversation_id: str | None = None, user: User = Depends(current_user), session: Session = Depends(get_db)):
    project = require_project(session, project_id, user.id)
    scene = require_scene(project, scene_id)
    clips = [clip for track in scene["data"]["timeline"]["tracks"] for clip in track["clips"] if clip["type"] in {"video", "image"}]
    by_clip = {clip["id"]: clip for clip in clips}
    assets = session.scalars(select(Asset).where(Asset.owner_id == user.id, Asset.project_id == project_id)).all()
    by_asset = {asset.id: asset for asset in assets}
    canvas = session.scalar(select(Canvas).where(Canvas.owner_id == user.id, Canvas.project_id == project_id, Canvas.scene_id == scene_id))
    titles = {node["id"]: node.get("title", "视频候选") for node in canvas.data_json["nodes"]} if canvas else {}
    generations = session.scalars(select(Generation).where(Generation.owner_id == user.id,
        Generation.project_id == project_id, Generation.scene_id == scene_id).order_by(Generation.created_at.desc())).all()
    sequences = session.scalars(select(MediaSequence).where(MediaSequence.owner_id == user.id,
        MediaSequence.project_id == project_id, MediaSequence.scene_id == scene_id).order_by(MediaSequence.created_at.desc())).all()
    if conversation_id:
        from app.services.materials import require_material
        conversation = require_material(session, project_id, scene_id, conversation_id, user.id)
        sequences = [sequence for sequence in sequences if sequence.input_json.get("conversationId") == conversation.id and sequence.input_json.get("runId") == conversation.active_run_id]
        sequence_ids = {sequence.id for sequence in sequences}
        receipts = session.scalars(select(AgentStep.tool_results_json).where(AgentStep.run_id == conversation.active_run_id)).all() if conversation.active_run_id else []
        generation_ids = {receipt["result"]["generation"]["id"] for values in receipts for receipt in values if receipt["tool"] == "submit_generation"}
        generations = [value for value in generations if value.id in generation_ids or value.input_json.get("boundaryFrames", {}).get("sequenceId") in sequence_ids]
    values = []
    for generation in generations:
        asset = by_asset.get(generation.asset_id)
        values.append({"id": generation.id, "generationId": generation.id, "assetId": generation.asset_id,
            "clipId": generation.clip_id, "title": titles.get(generation.source_node_id, "视频候选"), "status": generation.status,
            "adopted": bool(asset and by_clip.get(generation.clip_id, {}).get("assetId") == asset.id),
            "durationSeconds": asset.duration_ms / 1000 if asset and asset.duration_ms else None,
            "resolution": generation.input_json.get("resolution"), "confirmationHash": generation.input_hash,
            "projectRevision": generation.project_revision,
            "frames": [{"type": item["type"], "assetId": item["assetId"]} for item in generation.input_json.get("boundaryFrames", {}).get("frames", [])]})
    adoptions = session.scalars(select(MediaAdoption).where(MediaAdoption.owner_id == user.id,
        MediaAdoption.project_id == project_id, MediaAdoption.scene_id == scene_id).order_by(MediaAdoption.created_at.desc())).all()
    if conversation_id:
        active_clips = {value.clip_id for value in generations} | {segment["clipId"] for sequence in sequences for segment in sequence.input_json["segments"]}
        adoptions = [value for value in adoptions if value.clip_id in active_clips]
    known = {(value["assetId"], value["clipId"]) for value in values}
    for adoption in adoptions:
        if (adoption.asset_id, adoption.clip_id) in known:
            continue
        known.add((adoption.asset_id, adoption.clip_id))
        asset = by_asset[adoption.asset_id]
        values.append({"id": f"asset:{asset.id}:{adoption.clip_id}", "generationId": None, "assetId": asset.id,
            "clipId": adoption.clip_id, "title": asset.name, "status": "ready",
            "adopted": by_clip.get(adoption.clip_id, {}).get("assetId") == asset.id,
            "durationSeconds": asset.duration_ms / 1000, "resolution": f"{asset.height}P", "confirmationHash": None,
            "projectRevision": None, "frames": []})
    return {"projectId": project_id, "sceneId": scene_id, "revision": project.revision,
        "sequences": [sequence_view(session, sequence) for sequence in sequences], "candidates": values,
        "adoptions": [adoption_view(adoption, project.revision) for adoption in adoptions],
        "clips": [{key: clip[key] for key in ("id", "name", "assetId", "startTime", "duration", "sourceStart", "sourceDuration", "playbackRate") if key in clip} for clip in clips],
        "images": [{"assetId": asset.id, "name": asset.name, "sha256": asset.sha256, "width": asset.width, "height": asset.height}
            for asset in assets if asset.mime_type.startswith("image/") and asset.width and asset.height],
        "publishAllowed": not conflict_issues(session, project_id, user.id)}
