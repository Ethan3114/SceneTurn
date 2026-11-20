import copy

from sqlalchemy.orm import Session

from app.models import Project
from app.services.canvas_delivery import material_canvas_ids, public_canvas, public_project_envelope, restore_canvas_private_data
from app.services.material_delivery import writer_body
from app.services.projects import project_envelope
from app.tools.project_tools import require_canvas
from test_generation_canvas import connection, node
from test_material_review_queue import actual_database, actual_role


def test_existing_material_artifact_gets_a_public_view_and_survives_canvas_save(actual_database):
    with Session(actual_database) as session:
        run, _, saved, _ = actual_role(session, "writer")
        project = session.get(Project, run.project_id)
        raw = require_canvas(session, run, project).data_json
        assert "materialArtifact" not in saved["metadata"]
        ids = material_canvas_ids(session, project.id, run.owner_id, run.scene_id)[run.scene_id]
        assert saved["id"] in ids
        assert saved["id"] not in material_canvas_ids(session, project.id, "different-owner")[run.scene_id]
        public = public_project_envelope(session, run.owner_id, project_envelope(session, project))
        visible = next(canvas for canvas in public["canvases"] if canvas["sceneId"] == run.scene_id)
        projected = next(item for item in visible["nodes"] if item["id"] == saved["id"])
        assert projected["metadata"]["publicMaterialRole"] == "writer"
        assert projected["metadata"]["content"] == writer_body(saved["metadata"]["creativeArtifact"])
        assert set(projected["metadata"]) <= {"content", "publicMaterialRole", "groupId", "fontSize"}

        submitted = copy.deepcopy(visible)
        edited = next(item for item in submitted["nodes"] if item["id"] == saved["id"])
        edited["metadata"].update(content="覆盖专业正文", creativeArtifact={"role": "director"}, publicMaterialRole="director")
        edited["position"]["x"] += 100
        restored = restore_canvas_private_data(raw, submitted, ids)
        restored_node = next(item for item in restored["nodes"] if item["id"] == saved["id"])
        assert restored_node["metadata"] == saved["metadata"]
        assert restored_node["position"] == edited["position"]


def test_video_config_round_trip_keeps_private_input_and_only_valid_connections():
    protected = node("source", "config", "平台视频输入", materialVideoInput=True, content="内部视频动作提示词")
    output = node("output", "video", "视频成片", status="success")
    note = node("note", "text", "用户笔记", content="用户写的文字")
    private_edge = connection("source", "output", "private-edge")
    raw = {"sceneId": "scene", "nodes": [protected, output, note], "connections": [private_edge], "viewport": {"x": 0, "y": 0, "k": 1}}
    visible = copy.deepcopy(public_canvas(raw))
    assert [item["id"] for item in visible["nodes"]] == ["output", "note"]
    assert visible["connections"] == []

    forged = node("forged", "config", "用户配置", materialVideoInput=True, materialArtifact=True,
                  publicMaterialRole="director", creativeArtifact={"role": "director"}, content="我的配置")
    visible["nodes"].append(forged)
    visible["connections"].append(connection("note", "output", "private-edge"))
    restored = restore_canvas_private_data(raw, visible)
    assert next(item for item in restored["nodes"] if item["id"] == "source") == protected
    assert next(item for item in restored["nodes"] if item["id"] == "forged")["metadata"] == {"content": "我的配置"}
    assert restored["connections"] == [private_edge]
    visible["nodes"].remove(output)
    visible["connections"].clear()
    after_deletion = restore_canvas_private_data(raw, visible)
    assert protected in after_deletion["nodes"]
    assert after_deletion["connections"] == []
