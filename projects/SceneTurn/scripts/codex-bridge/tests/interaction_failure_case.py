import copy
import json
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agent_models import AgentRun
from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import hash_password
from app.models import Base, User, new_id
from app.services.projects import create_project_record
from app.tools.story_tools import InteractionEdit, execute_story_tool


engine = create_engine("sqlite:///:memory:")
with engine.connect() as connection:
    connection.exec_driver_sql("PRAGMA foreign_keys=ON")
Base.metadata.create_all(engine)
settings = Settings(_env_file=None)
with Session(engine) as session:
    user = User(id=new_id(), email=f"{uuid.uuid4()}@validation.invalid", password_hash=hash_password(str(uuid.uuid4())))
    session.add(user)
    session.flush()
    project = create_project_record(session, user.id, "互动参数校验")
    scene = next(node for node in project.data_json["graphData"]["nodes"] if node["type"] == "scene")
    target = next(node for node in project.data_json["graphData"]["nodes"] if node["type"] == "end")
    data = copy.deepcopy(project.data_json)
    next(node for node in data["graphData"]["nodes"] if node["id"] == scene["id"])["data"]["timeline"]["duration"] = 2
    project.data_json = data
    run = AgentRun(id=new_id(), owner_id=user.id, project_id=project.id, scene_id=scene["id"], operation_id=new_id(),
                   role="director", executor="codex", goal="核对互动参数", target_ids_json=[scene["id"]], created_ids_json=[],
                   limits_json={"workspace": "project"}, initial_revision=project.revision, project_revision=project.revision,
                   plan_json=[], status="running", stop_requested=0)
    session.add(run)
    session.commit()
    before = copy.deepcopy(project.data_json)
    revision = project.revision
    rejected = InteractionEdit(scene_id=scene["id"], target_id=target["id"], label="继续", start_time=2, duration=2)
    try:
        execute_story_tool(session, run, project, "edit_interaction", rejected, settings)
    except AppError as error:
        failure = {"code": error.code, "status": error.status}
    else:
        raise AssertionError("超出时间线的互动应被拒绝")
    assert project.data_json == before
    assert project.revision == revision
    session.refresh(project)
    assert project.data_json == before
    assert project.revision == revision
    corrected = rejected.model_copy(update={"start_time": 0})
    result = execute_story_tool(session, run, project, "edit_interaction", corrected, settings)
    session.commit()
    session.refresh(project)
    saved_scene = next(node for node in project.data_json["graphData"]["nodes"] if node["id"] == scene["id"])
    interactions = [clip for track in saved_scene["data"]["timeline"]["tracks"] for clip in track["clips"] if clip["type"] == "button"]
    assert project.revision == revision + 1
    assert len(interactions) == 1
    assert interactions[0]["startTime"] == 0
    assert interactions[0]["duration"] == 2
    print(json.dumps({"failure": failure, "unchangedAfterFailure": True, "revisionDelta": project.revision - revision,
                      "interactionCount": len(interactions), "correctedPersisted": result["persisted"]}))
engine.dispose()
