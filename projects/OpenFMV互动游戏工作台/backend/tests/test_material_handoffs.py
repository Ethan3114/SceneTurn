import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_models import AgentCall, AgentRun, AgentStep
from app.core.errors import AppError
from app.material_models import MaterialConversation
from app.material_schemas import MaterialAnswer
from app.models import BudgetReservation, Generation, Operation, Project, new_id
from app.services.agent import _complete_platform_step, require_step_delivery, resume_run
from app.services.agent_provider import MATERIAL_NATIVE_AGENT_PROTOCOL, agent_request_body, agent_tools
from app.services.material_handoffs import material_plan, prepare_material_handoff, read_material_handoff, validate_material_handoff
from app.services.material_review import answer_review, pause_for_clarification, public_review
from app.services.projects import payload_digest
from app.tools.project_tools import execute_tool, require_canvas
from test_material_reference_assets import reference_image, reference_workspace, save_writer
from test_storyboard_existing_frames import approve, reusable_storyboard, save_storyboard


def plan_step(role):
    return {"role": role, "title": "制作当前素材", "instruction": "依据当前交付要求制作，保留来源", "kind": "produce", "dependsOn": []}


def test_material_planning_schema_is_available_in_real_request_configuration(reference_workspace):
    _, settings, _, _, _, _ = reference_workspace
    tools = agent_tools("director", True, protocol=MATERIAL_NATIVE_AGENT_PROTOCOL, catalog_version="R8")
    body = agent_request_body([{"role": "user", "content": "给后山相遇制作八秒素材"}], 4096, tools, True,
        settings, protocol=MATERIAL_NATIVE_AGENT_PROTOCOL, catalog_version="R8")
    [tool] = body["tools"]
    assert tool["function"]["strict"]
    assert set(tool["function"]["parameters"]["required"]) == {"directorBrief", "questions", "plan"}
    assert body["model"] == "deepseek-flash"


def test_clarification_survives_a_new_session_and_cannot_be_skipped(reference_workspace):
    session, settings, project, user, _, create_run = reference_workspace
    run, _, conversation = create_run()
    pause_for_clarification(run, [{"question": "采用哪一种画风？", "suggestedAnswer": "写实动画"}])
    session.commit()
    with Session(session.bind, expire_on_commit=False) as resumed:
        stored = resumed.get(AgentRun, run.id)
        value = resumed.get(MaterialConversation, conversation.id)
        gate = public_review(resumed, stored)
        assert gate["kind"] == "clarification" and gate["nodeId"] is None
        with pytest.raises(AppError) as rejection:
            resume_run(resumed, project.id, run.id, user.id, new_id(), project.revision, settings)
        assert rejection.value.code == "MATERIAL_CONFIRMATION_REQUIRED"
        resumed.rollback()
        body = MaterialAnswer.model_validate({"reviewId": gate["id"], "expectedRevision": project.revision, "operationId": new_id(),
            "answers": {gate["questions"][0]["id"]: "冷色动画"}})
        answer_review(resumed, value, body, settings)
        resumed.commit()
        answer_review(resumed, value, body, settings)
        assert public_review(resumed, stored) is None
        assert stored.status == "queued"
        assert stored.limits_json["materialContext"]["clarificationAnswers"] == [{"question": "采用哪一种画风？", "answer": "冷色动画"}]


def test_design_role_uses_adopted_writer_version_and_real_read_receipt(reference_workspace):
    session, settings, project, _, _, create_run = reference_workspace
    run, writer, conversation = create_run()
    canvas = require_canvas(session, run, project)
    roles = ["writer", "character_designer", "scene_designer", "storyboarder", "media_producer"]
    plan = material_plan(session, run, canvas, [plan_step(role) for role in roles])
    assert [item["dependsOn"] for item in plan] == [[], [0], [0], [0, 1, 2], [3]]
    with pytest.raises(AppError) as rejection:
        material_plan(session, run, canvas, [plan_step("media_producer")])
    assert rejection.value.code == "MATERIAL_STORYBOARD_REQUIRED"
    run.plan_json = plan[:2]
    role = plan[1]
    design = AgentStep(id=new_id(), run_id=run.id, index=1, role=role["role"], title=role["title"],
        instruction=role["instruction"], status="queued", tool_results_json=[])
    session.add(design)
    session.commit()
    node = save_writer(session, settings, project, run, writer)
    with pytest.raises(AppError) as rejection:
        prepare_material_handoff(session, run, design, canvas)
    assert rejection.value.code == "AGENT_DEPENDENCY_NOT_READY"
    review = public_review(session, run)
    body = MaterialAnswer.model_validate({"reviewId": review["id"], "operationId": new_id(), "expectedRevision": project.revision,
        "answers": {question["id"]: question["suggestedAnswer"] for question in review["questions"]}})
    answer_review(session, conversation, body, settings)
    verification = require_step_delivery(session, run, writer, project, settings)
    _complete_platform_step(session, run, writer, "剧本已采用", verification)
    design.status = "running"
    run.status = "running"
    session.commit()
    [source] = prepare_material_handoff(session, run, design, canvas)
    assert source["artifactHash"] == payload_digest(node["metadata"]["creativeArtifact"])
    assert source["nodeId"] == node["id"]
    downstream = {"sources": [{"kind": "artifact", "referenceId": node["id"]}]}
    with pytest.raises(AppError) as rejection:
        validate_material_handoff(session, run, design, canvas, downstream)
    assert rejection.value.code == "MATERIAL_HANDOFF_READ_REQUIRED"
    session.commit()
    execute_tool(session, run, design.role, "read_artifact", {"targetId": node["id"]}, new_id(), project.revision, settings, design)
    validate_material_handoff(session, run, design, canvas, downstream)


def test_runtime_handoff_persists_one_verified_read_with_real_video_frames(reference_workspace):
    session, settings, project, _, add_image, create_run = reference_workspace
    first, last = add_image("首帧.png"), add_image("尾帧.png")
    author_run, storyboarder, conversation = create_run("storyboarder", [first, last], catalog_version="R7")
    artifact = reusable_storyboard([first.id, last.id])
    node_id = save_storyboard(session, settings, project, author_run, storyboarder, artifact)["sourceNodeId"]
    video_run, video_step, _ = create_run("media_producer")
    video_run.target_ids_json = [node_id]
    session.commit()
    with pytest.raises(AppError) as unapproved:
        prepare_material_handoff(session, video_run, video_step, require_canvas(session, video_run, project))
    assert unapproved.value.code == "AGENT_DEPENDENCY_NOT_READY"
    approve(session, settings, project, author_run, conversation)

    with Session(session.bind, autoflush=False) as worker:
        run = worker.get(AgentRun, video_run.id)
        step = worker.get(AgentStep, video_step.id)
        same_project = worker.get(Project, project.id)
        revision = same_project.revision
        canvas = require_canvas(worker, run, same_project)
        inputs = prepare_material_handoff(worker, run, step, canvas)
        [context] = read_material_handoff(worker, run, step, same_project, inputs, settings)
        assert context["nodeId"] == node_id and context["artifact"] == artifact
        assert context["approvedVideoSegments"] == [{"storyboardNodeId": node_id, "shotKey": "shot-1",
            "firstFrameAssetId": first.id, "lastFrameAssetId": last.id, "durationSeconds": 2}]
        validate_material_handoff(worker, run, step, canvas, {"sources": [{"kind": "artifact", "referenceId": node_id}]})
        [receipt] = step.tool_results_json
        assert receipt["tool"] == "read_artifact"
        operation = worker.scalar(select(Operation).where(Operation.operation_id == receipt["operationId"]))
        assert operation.response_json["result"]["artifactSha256"] == payload_digest(artifact)
        assert same_project.revision == revision
        worker.commit()

    with Session(session.bind, autoflush=False) as worker:
        run = worker.get(AgentRun, video_run.id)
        step = worker.get(AgentStep, video_step.id)
        same_project = worker.get(Project, project.id)
        inputs = prepare_material_handoff(worker, run, step, require_canvas(worker, run, same_project))
        assert read_material_handoff(worker, run, step, same_project, inputs, settings) == [context]
        assert step.tool_results_json == [receipt] and same_project.revision == revision
        assert not worker.scalars(select(AgentCall)).all()
        assert not worker.scalars(select(BudgetReservation)).all()
        assert not worker.scalars(select(Generation)).all()
