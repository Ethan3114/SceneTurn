import copy

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent_models import AgentStep
from app.api.generations import GenerationResult, visible_generation
from app.core.config import Settings
from app.core.errors import AppError
from app.models import Budget, Canvas, Generation, Project
from app.services.generation import confirm_generation, retry_generation_quote
from conftest import PRODUCT_ROOT


ORIGINAL = "40422254-e0bb-40fa-8f30-e118afdc6c32"
STEP = "dce1af33-0faf-4f6e-935e-5c4facfe1bcb"


def test_actual_interrupted_material_video_retry_keeps_the_original_and_requires_confirmation(writable_snapshot, request):
    settings = Settings(_env_file=None, data_dir=PRODUCT_ROOT / "data", worker_enabled=False)
    database = writable_snapshot(settings.data_dir / "openfmv.sqlite3")
    engine = create_engine(f"sqlite:///{database}")
    request.addfinalizer(engine.dispose)

    with Session(engine) as session:
        original = session.get(Generation, ORIGINAL)
        revision = session.get(Project, original.project_id).revision
        old_input = copy.deepcopy(original.input_json)
        old_reservation = original.reserved_cents
        old_budget = session.get(Budget, "global").reserved_cents
        first = retry_generation_quote(session, original.id, original.owner_id, revision, settings)
        second = retry_generation_quote(session, original.id, original.owner_id, revision, settings)
        assert first.id == second.id != original.id
        assert first.source_node_id == original.source_node_id and first.clip_id == original.clip_id
        assert first.input_json["outputNodeId"] == old_input["outputNodeId"]
        assert first.input_json["boundaryFrames"] == old_input["boundaryFrames"] and first.input_json["prompt"] == old_input["prompt"]
        assert original.status == "submission_unknown" and original.remote_task_id is None
        assert original.input_json == old_input and original.reserved_cents == old_reservation == 480
        assert session.get(Budget, "global").reserved_cents == old_budget
        generation_ids = [item["result"]["generation"]["id"] for item in session.get(AgentStep, STEP).tool_results_json
            if item["tool"] == "submit_generation"]
        assert generation_ids.count(original.id) == generation_ids.count(first.id) == 1
        response = GenerationResult.model_validate({"generation": visible_generation(session, first)}).model_dump(by_alias=True)["generation"]
        assert response["retryOf"] == original.id and response["material"] and not response["canRetry"]
        assert "prompt" not in response["input"] and "request" not in response["input"] and "reservedCents" not in response
        canvas = session.scalars(select(Canvas).where(Canvas.project_id == original.project_id, Canvas.scene_id == original.scene_id)).one()
        node = next(node for node in canvas.data_json["nodes"] if node["id"] == old_input["request"]["videoArtifactNodeId"])
        artifact = node["metadata"]["creativeArtifact"]
        shot_key = old_input["boundaryFrames"]["shotKey"]
        prompt = next(shot["videoPrompt"] for shot in artifact["shots"] if shot["shotKey"] == shot_key)
        assert response["input"]["shotSummary"] == {"title": f"{artifact['title']} · {shot_key}"[:200],
            "action": prompt["actionTimeline"], "camera": prompt["cameraMovement"], "sound": prompt["audioRequirement"]}
        assert session.scalars(select(Generation).where(
            Generation.project_id == original.project_id,
            Generation.input_json["request"]["retryOf"].as_string() == original.id,
        )).one().id == first.id

        if first.status == "awaiting_confirmation":
            assert first.reserved_cents == 0 and first.started_at is None
            queued = confirm_generation(session, first.id, first.owner_id, first.input_hash, revision, settings)
            assert queued.status == "queued" and queued.reserved_cents == 480
            assert confirm_generation(session, first.id, first.owner_id, first.input_hash, revision, settings).id == queued.id
            assert session.get(Budget, "global").reserved_cents == old_budget + 480
            assert original.status == "submission_unknown" and original.reserved_cents == old_reservation
            session.rollback()

        with pytest.raises(AppError) as denied:
            retry_generation_quote(session, first.id, first.owner_id, revision, settings)
        assert denied.value.code == "VIDEO_RETRY_UNAVAILABLE"
