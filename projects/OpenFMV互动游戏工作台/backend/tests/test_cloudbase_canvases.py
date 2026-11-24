import copy
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.agent_models import AgentRun
from app.core.security import new_token
from app.models import Canvas, new_id
from app.schemas import CanvasSave, CanvasSnapshot
from app.services.cloudbase_canvases import CanvasDelivery, CanvasHistory, CanvasPreparation
from app.services.projects import payload_digest
from conftest import PRODUCT_ROOT
from test_api import create_project, registered_client, scene_id
from test_generation_canvas import connection, node
from test_material_reference_assets import writer_input
from test_postgresql_persistence import postgres_runtime


def test_real_canvas_gateway_preserves_private_results_and_version_history(postgres_runtime, request):
    admin, runtime, start = postgres_runtime
    scope = new_token()
    roles = ("anon", "authenticated", "service_role")
    sql_dir = PRODUCT_ROOT / "backend" / "sql" / "cloudbase"
    with admin.begin() as db:
        for role in roles:
            db.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        for filename in ("access.sql", "projects.sql", "canvases.sql"):
            db.exec_driver_sql((sql_dir / filename).read_text(encoding="utf-8"), execution_options={"no_parameters": True})
        db.execute(text("""
            INSERT INTO sceneturn.gateway_credentials (token_sha256) VALUES (sha256(convert_to(:scope, 'UTF8')))
        """), {"scope": scope})

    def cleanup():
        with admin.begin() as db:
            db.exec_driver_sql("""
                DROP FUNCTION public.sceneturn_canvas_read(text, text, text),
                    public.sceneturn_canvas_prepare(text, text, text, integer, text, text),
                    public.sceneturn_canvas_save(text, text, text, integer, text, text, text, jsonb),
                    public.sceneturn_canvas_versions(text, text, text, text, integer),
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

    def denied(statement, parameters, sqlstate, message):
        with pytest.raises(DBAPIError) as failure:
            invoke(statement, parameters)
        assert failure.value.orig.sqlstate == sqlstate and failure.value.orig.diag.message_primary == message

    read = "SELECT public.sceneturn_canvas_read(:owner, :project, :scene)"
    prepare = "SELECT public.sceneturn_canvas_prepare(:owner, :project, :scene, :revision, :operation, :digest)"
    save = """
        SELECT public.sceneturn_canvas_save(:owner, :project, :scene, :revision, :operation, :digest,
          :base, CAST(:canvas AS jsonb))
    """
    history = "SELECT public.sceneturn_canvas_versions(:owner, :project, :scene, :node, :before)"

    with start("canvas") as backend, closing(registered_client(backend)[0]) as owner, closing(registered_client(backend)[0]) as other:
        initial = create_project(owner)
        owner_id = owner.get("/auth/me").json()["user"]["id"]
        project_id, current_scene = initial["project"]["id"], scene_id(initial)
        scope_parameters = {"owner": owner_id, "project": project_id, "scene": current_scene}
        other_owner = other.get("/auth/me").json()["user"]["id"]
        authored = node("writer", "text", "审定剧本", creativeArtifact=writer_input(), content="私有写作依据")
        internal = node("internal", "config", "内部视频输入", materialVideoInput=True, content="内部动作提示词")
        output = node("output", "video", "镜头视频")
        private_edge = connection("internal", "output", "internal-edge")
        with Session(runtime) as db:
            canvas = db.scalar(select(Canvas).where(Canvas.project_id == project_id, Canvas.scene_id == current_scene))
            canvas.data_json = {**canvas.data_json, "nodes": [authored, internal, output], "connections": [private_edge]}
            run = AgentRun(owner_id=owner_id, project_id=project_id, scene_id=current_scene,
                operation_id=new_id(), role="writer", executor="platform", goal="保留由用户确认的画布记录",
                created_ids_json=[authored["id"]], limits_json={"workspace": "materials"},
                initial_revision=1, project_revision=1, status="ready")
            db.add(run)
            db.commit()
            run_id = run.id

        with pytest.raises(DBAPIError) as forbidden:
            invoke(read, scope_parameters, scoped=False)
        assert forbidden.value.orig.sqlstate == "42501"
        denied(read, {**scope_parameters, "owner": other_owner}, "PT404", "PROJECT_NOT_FOUND")
        denied(read, {**scope_parameters, "scene": str(uuid4())}, "PT404", "SCENE_NOT_FOUND")
        delivery = CanvasDelivery.model_validate(invoke(read, scope_parameters), strict=True)
        public = delivery.public_result()
        assert public["revision"] == 1 and public["canvas"]["connections"] == []
        assert [item["id"] for item in public["canvas"]["nodes"]] == ["writer", "output"]
        assert public["canvas"]["nodes"][0]["metadata"]["publicMaterialRole"] == "writer"
        assert "私有写作依据" not in json.dumps(public, ensure_ascii=False)

        def edit(current, label, x):
            body = copy.deepcopy(current["canvas"])
            body.pop("sceneId")
            body.update(expectedRevision=current["revision"], operationId=label)
            body["nodes"][0]["position"]["x"] = x
            body["nodes"][0]["metadata"].update(content="替换剧本", creativeArtifact={"role": "director"})
            body["nodes"].append(node("user-note", "text", "用户笔记", content="实际输入", materialArtifact=True))
            body["connections"].append(connection("writer", "output", "internal-edge"))
            model = CanvasSave.model_validate(body)
            submission = CanvasSnapshot(scene_id=current_scene, nodes=model.nodes, connections=model.connections, viewport=model.viewport)
            payload = {"sceneId": current_scene, **model.model_dump(by_alias=True, exclude_unset=True)}
            return {**scope_parameters, "revision": model.expected_revision, "operation": model.operation_id,
                    "digest": payload_digest(payload)}, submission

        def ready(parameters, submission):
            response = CanvasPreparation.model_validate(invoke(prepare, parameters), strict=True)
            raw = response.restore(submission).model_dump(by_alias=True, exclude_unset=True)
            return {**parameters, "base": response.base_token, "canvas": json.dumps(raw, ensure_ascii=False)}, raw

        parameters, submission = edit(public, "首次画布保存", 120)
        old, _ = ready(parameters, submission)
        with Session(runtime) as db:
            stored_run = db.get(AgentRun, run_id)
            stored_run.created_ids_json = [authored["id"], output["id"]]
            db.commit()
        denied(save, old, "PT409", "REVISION_CONFLICT")
        accepted, raw = ready(parameters, submission)
        protected = next(item for item in raw["nodes"] if item["id"] == "writer")
        assert protected["metadata"] == authored["metadata"] and protected["position"]["x"] == 120
        assert raw["connections"] == [private_edge]
        assert raw["nodes"][2]["metadata"] == {"content": "实际输入"}
        invalid = {**raw, "connections": [connection("writer", "missing", "dangling")]}
        denied(save, {**accepted, "canvas": json.dumps(invalid)}, "PT422", "CANVAS_REQUEST_INVALID")
        written = CanvasDelivery.model_validate(invoke(save, accepted), strict=True).public_result()
        assert written["revision"] == 2
        replay = CanvasPreparation.model_validate(invoke(prepare, parameters), strict=True)
        assert replay.base_token is None and replay.public_result() == written
        denied(prepare, {**parameters, "digest": "a" * 64}, "PT409", "OPERATION_CONFLICT")

        written["canvas"]["nodes"].pop()
        edit_one, _ = ready(*edit(written, "同时修改一", 200))
        edit_two, _ = ready(*edit(written, "同时修改二", 300))

        def commit(candidate):
            try:
                return invoke(save, candidate)["revision"]
            except DBAPIError as failure:
                assert failure.orig.sqlstate == "PT409" and failure.orig.diag.message_primary == "REVISION_CONFLICT"
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            revisions = list(pool.map(commit, (edit_one, edit_two)))
        assert revisions.count(3) == 1 and revisions.count(None) == 1
        assert CanvasDelivery.model_validate(invoke(save, accepted), strict=True).revision == 2

        query = {**scope_parameters, "node": "writer", "before": None}
        versions = CanvasHistory.model_validate(invoke(history, query), strict=True).public_result()
        assert [item["revision"] for item in versions["versions"]] == [3, 2]
        assert ["creativeArtifact" in item["node"]["metadata"] for item in versions["versions"]] == [False, False]
        assert [item["revision"] for item in CanvasHistory.model_validate(
            invoke(history, {**query, "before": 3}), strict=True).public_result()["versions"]] == [2]
        hidden = CanvasHistory.model_validate(invoke(history, {**query, "node": "internal"}), strict=True).public_result()
        assert hidden == {"versions": [], "before": None}
        denied(history, {**query, "owner": other_owner}, "PT404", "PROJECT_NOT_FOUND")
