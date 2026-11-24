import json
from contextlib import closing
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.conversation_schemas import ConversationCreate, ConversationList, ConversationSend
from app.core.security import new_token
from app.services.cloudbase_conversations import ConversationDelivery
from app.services.conversations import editing_run_limits
from app.services.projects import payload_digest
from conftest import PRODUCT_ROOT
from test_api import create_project, registered_client, scene_id
from test_postgresql_persistence import postgres_runtime
from test_project_conversations import claim


def test_real_gateway_project_conversation_round_trip_with_personal_bridge(postgres_runtime, request):
    admin, _, start = postgres_runtime
    scope = new_token()
    roles = ("anon", "authenticated", "service_role")
    sql_dir = PRODUCT_ROOT / "backend" / "sql" / "cloudbase"
    with admin.begin() as db:
        for role in roles:
            db.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        for filename in ("access.sql", "projects.sql", "conversations.sql"):
            db.exec_driver_sql((sql_dir / filename).read_text(encoding="utf-8"), execution_options={"no_parameters": True})
        db.execute(text("""
            INSERT INTO sceneturn.gateway_credentials (token_sha256) VALUES (sha256(convert_to(:scope, 'UTF8')))
        """), {"scope": scope})

    def cleanup():
        with admin.begin() as db:
            db.exec_driver_sql("""
                DROP FUNCTION public.sceneturn_conversation_require_project(text, text),
                    public.sceneturn_conversation_list(text, text), public.sceneturn_conversation_read(text, text, text),
                    public.sceneturn_conversation_create(text, text, text, text, text, text),
                    public.sceneturn_conversation_send(text, text, text, text, text, integer, text, jsonb),
                    public.sceneturn_project_list(text), public.sceneturn_project_get(text, text),
                    public.sceneturn_project_create(text, text, jsonb, jsonb),
                    public.sceneturn_project_save(text, text, integer, text, text, jsonb, jsonb),
                    public.sceneturn_schema_revision();
            """)
            for role in roles:
                db.exec_driver_sql(f"DROP ROLE {role}")

    request.addfinalizer(cleanup)

    def invoke(statement, parameters, *, scoped=True):
        with admin.begin() as db:
            db.execute(text("SELECT set_config('request.jwt.claims', :claims, true)"),
                {"claims": json.dumps({"role": "service_role"})})
            db.execute(text("SELECT set_config('request.headers', :headers, true)"),
                {"headers": json.dumps({"x-sceneturn-database-scope": scope} if scoped else {})})
            db.exec_driver_sql("SET LOCAL ROLE service_role")
            return db.execute(text(statement), parameters).scalar_one()

    def delivery(statement, parameters):
        return ConversationDelivery.model_validate(invoke(statement, parameters), strict=True).public_envelope().conversation

    def denied(statement, parameters, sqlstate, message):
        with pytest.raises(DBAPIError) as failure:
            invoke(statement, parameters)
        assert failure.value.orig.sqlstate == sqlstate and failure.value.orig.diag.message_primary == message

    capabilities = "SELECT public.sceneturn_conversation_require_project(:owner, :project)"
    listing = "SELECT public.sceneturn_conversation_list(:owner, :project)"
    create = "SELECT public.sceneturn_conversation_create(:owner, :project, :conversation, :operation, :digest, :title)"
    read = "SELECT public.sceneturn_conversation_read(:owner, :project, :conversation)"
    send = """
        SELECT public.sceneturn_conversation_send(:owner, :project, :conversation, :operation, :digest,
          :revision, :content, CAST(:limits AS jsonb))
    """

    with start("conversation") as service, closing(registered_client(service)[0]) as owner, closing(registered_client(service)[0]) as other:
        project = create_project(owner)
        project_id, current_scene = project["project"]["id"], scene_id(project)
        owner_id = owner.get("/auth/me").json()["user"]["id"]
        other_id = other.get("/auth/me").json()["user"]["id"]
        scoped = {"owner": owner_id, "project": project_id, "conversation": str(uuid4())}
        initial = ConversationCreate(operation_id=str(uuid4()))
        create_parameters = {**scoped, "operation": initial.operation_id, "title": initial.title,
                             "digest": payload_digest(initial.model_dump(by_alias=True))}

        with pytest.raises(DBAPIError) as forbidden:
            invoke(capabilities, scoped, scoped=False)
        assert forbidden.value.orig.sqlstate == "42501"
        assert invoke(capabilities, scoped) is True
        assert ConversationList.model_validate(invoke(listing, scoped), strict=True).conversations == []
        created = delivery(create, create_parameters)
        assert created.id == scoped["conversation"] and created.messages == [] and created.run is None
        assert delivery(create, {**create_parameters, "conversation": str(uuid4())}) == created
        denied(create, {**create_parameters, "digest": "a" * 64}, "PT409", "OPERATION_CONFLICT")
        denied(read, {**scoped, "owner": other_id}, "PT404", "PROJECT_NOT_FOUND")

        def message(content, revision=1, **context):
            body = ConversationSend(operation_id=str(uuid4()), expected_revision=revision, content=content, context=context)
            payload = {"conversationId": created.id, **body.model_dump(by_alias=True)}
            return {**scoped, "operation": body.operation_id, "digest": payload_digest(payload),
                    "revision": revision, "content": body.content,
                    "limits": json.dumps(editing_run_limits(created.id, payload["context"]))}

        denied(send, message("使用不存在的选中对象", selected_clip_ids=[str(uuid4())]), "PT422", "CHAT_CONTEXT_INVALID")
        denied(send, message("使用不存在的场景", scene_id=str(uuid4())), "PT422", "SCENE_NOT_FOUND")
        denied(send, message("旧版项目", revision=2), "PT409", "REVISION_CONFLICT")
        parameters = message("将当前场景移动到指定位置", selected_node_ids=[current_scene])
        received = delivery(send, parameters)
        assert received.run.status == "waiting_connection" and received.run.executor == "codex"
        assert received.run.scene_id == current_scene and received.run.target_ids == [current_scene]
        assert received.run.calls == [] and received.run.usage.cost_micros is None
        assert [item.content for item in received.messages] == [parameters["content"]]
        assert delivery(send, parameters) == received
        denied(send, message("正在处理时提交第二条"), "PT409", "CONVERSATION_BUSY")

        bridge, leased = claim(service, owner, project)
        with closing(bridge):
            assert leased["run"]["id"] == received.run.id
            move = bridge.post("/connections/tools", json={"runId": received.run.id, "toolName": "arrange_story",
                "operationId": str(uuid4()), "expectedRevision": 1, "arguments": {"operations": [
                    {"action": "move", "nodeId": current_scene, "x": 720, "y": 260}]}})
            assert move.status_code == 200, move.text
            thread_id = str(uuid4())
            reply = "当前场景已移动到 x=720，y=260。"
            completion = bridge.post("/connections/complete", json={"runId": received.run.id, "threadId": thread_id,
                "turnId": str(uuid4()), "result": {"finalText": reply}})
            assert completion.status_code == 200, completion.text

        direct = owner.get(f"/projects/{project_id}/conversations/{created.id}")
        restored = delivery(read, scoped)
        assert direct.status_code == 200
        assert restored.model_dump(by_alias=True) == direct.json()["conversation"]
        assert [item.role for item in restored.messages] == ["user", "assistant"]
        assert restored.messages[-1].content == reply and restored.run.status == "completed"
        assert restored.run.steps[0].tool_results[0].tool == "arrange_story"
        assert restored.run.calls[0].provider == "personal-codex"
        assert restored.run.current_revision == 2 and restored.run.usage.cost_micros is None
        next_turn = delivery(send, message("继续核对剧情排布", revision=2))
        assert next_turn.run.id != restored.run.id and next_turn.run.thread_id == thread_id
        assert [item.content for item in next_turn.messages] == [parameters["content"], reply, "继续核对剧情排布"]
