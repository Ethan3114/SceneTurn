import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun
from app.api.media_candidates import candidates
from app.core.errors import AppError
from app.material_models import MaterialConversation, MaterialMessage
from app.material_schemas import MaterialSend, MaterialView
from app.models import Generation, TextProviderSettings, new_id
from app.services.material_review import pause_for_clarification
from app.services.materials import material_followups, send, view
from test_material_reference_assets import reference_image, reference_workspace


def request(project, content, intent=None):
    return MaterialSend(operation_id=new_id(), expected_revision=project.revision, content=content, intent=intent)


def test_followup_keeps_unknown_video_when_legacy_continue_displaced_its_run(reference_workspace):
    session, settings, project, user, _, create_run = reference_workspace
    original, step, conversation = create_run("media_producer")
    original.status = "stopped"
    original.limits_json = {**original.limits_json, "materialConversationId": conversation.id}
    generation = Generation(owner_id=user.id, project_id=project.id, scene_id=original.scene_id,
        source_node_id=new_id(), clip_id=new_id(), operation_id=new_id(), project_revision=project.revision,
        input_json={"model": "wan3.0-video"}, input_hash="0" * 64, source_digest="0" * 64,
        quoted_cents=480, reserved_cents=480, status="submission_unknown")
    session.add(generation)
    session.flush()
    step.tool_results_json = [{"tool": "submit_generation", "result": {"generation": {"id": generation.id}}}]
    session.commit()

    displaced, displaced_step, other_conversation = create_run("director")
    session.delete(displaced_step)
    displaced.plan_json = []
    pause_for_clarification(displaced, [{"question": "要制作什么内容？", "suggestedAnswer": "新的素材"}])
    prior_message = session.scalar(select(MaterialMessage).where(MaterialMessage.conversation_id == other_conversation.id))
    prior_message.conversation_id, prior_message.sequence, prior_message.content = conversation.id, 2, "继续"
    conversation.active_run_id = displaced.id
    session.commit()

    before = view(session, conversation)
    assert before["activeRunId"] == original.id and not before["canStartProduction"]
    assert before["messages"][-1]["kind"] == "followup"
    assert before["messages"][-1]["run"]["steps"][0]["toolResults"][0]["result"]["generation"]["id"] == generation.id
    [candidate] = candidates(project.id, original.scene_id, conversation.id, user, session)["candidates"]
    assert candidate["generationId"] == generation.id and candidate["status"] == "submission_unknown"
    body = request(project, "刚才的视频到哪一步了？")
    send(session, project.id, original.scene_id, conversation.id, user, body, settings)
    send(session, project.id, original.scene_id, conversation.id, user, body, settings)
    session.rollback()
    fresh = request(project, "继续当前任务", "followup")
    send(session, project.id, original.scene_id, conversation.id, user, fresh, settings)

    with Session(session.bind) as reopened:
        existing = reopened.get(MaterialConversation, conversation.id)
        result = MaterialView.model_validate(view(reopened, existing)).model_dump(by_alias=True)
        assert result["activeRunId"] == original.id == existing.active_run_id
        assert len(result["messages"]) == 4
        assert all(message["run"]["id"] == original.id for message in result["messages"])
        assert "平台尚未收到百炼任务编号" in result["messages"][-1]["reply"]
        assert reopened.get(AgentRun, original.id).status == "stopped"
        assert reopened.get(AgentRun, displaced.id).result_json["materialClarification"]["status"] == "pending"
        assert reopened.get(Generation, generation.id).reserved_cents == 480
        assert reopened.scalar(select(func.count(AgentRun.id))) == 2
        assert [item["content"] for item in material_followups(reopened, reopened.get(AgentRun, original.id))] == [body.content, fresh.content]

    with pytest.raises(AppError) as rejection:
        send(session, project.id, original.scene_id, conversation.id, user, request(project, "另做一段", "start"), settings)
    assert rejection.value.code == "MATERIAL_CONVERSATION_BUSY"
    session.rollback()


def test_continue_language_resumes_safe_existing_run_and_preserves_earlier_note(reference_workspace):
    session, settings, project, user, _, create_run = reference_workspace
    original, step, conversation = create_run()
    original.status, step.status = "stopped", "stopped"
    original.limits_json = {**original.limits_json, "materialConversationId": conversation.id}
    session.commit()

    note = request(project, "结尾保留原来的静音和灯光。", "followup")
    send(session, project.id, original.scene_id, conversation.id, user, note, settings)
    continuation = request(project, "继续当前任务。", "followup")
    sent = send(session, project.id, original.scene_id, conversation.id, user, continuation, settings)
    assert sent.active_run_id == original.id
    assert view(session, sent)["messages"][-1]["reply"].startswith("已恢复当前任务")
    assert (original.status, step.status) == ("queued", "queued")
    assert material_followups(session, original)[0]["content"] == note.content
    assert session.scalar(select(func.count(AgentRun.id))) == 1


def test_explicit_next_round_carries_the_previous_goal_and_followups(reference_workspace):
    session, settings, project, user, add_image, create_run = reference_workspace
    image = add_image()
    original, step, conversation = create_run(selected=[image])
    original.status, step.status = "completed", "completed"
    session.add(TextProviderSettings(id="platform", admin_owner_id=user.id, api_key_encrypted="disabled-test-worker"))
    session.commit()
    authorized = settings.model_copy(update={"text_admin_email": user.email})
    note = request(project, "补充一下，画面仍保持冷色灯光。")
    send(session, project.id, original.scene_id, conversation.id, user, note, authorized)
    follow = request(project, "在保持同一场景下再制作一个尾镜头。", "start")
    sent = send(session, project.id, original.scene_id, conversation.id, user, follow, authorized)

    assert sent.active_run_id != original.id
    current = session.get(AgentRun, sent.active_run_id)
    context = current.limits_json["materialContext"]
    assert [item["content"] for item in context["history"]] == [original.goal, note.content]
    assert [item["assetId"] for item in context["referenceAssets"]] == [image.id]
    result = view(session, sent)
    assert [message["kind"] for message in result["messages"]] == ["production", "followup", "production"]
    assert result["activeRunId"] == current.id and not result["canStartProduction"]
