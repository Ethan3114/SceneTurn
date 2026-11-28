from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.models import new_id
from app.schemas import ProjectEnvelope, ProjectList, ProjectSave, SceneCreate, WireModel
from app.services.canvas_delivery import public_canvas
from app.services.cloudbase_database import call_cloudbase_rpc
from app.services.projects import empty_canvas, initial_project, payload_digest, scene_node


class ProjectDelivery(WireModel):
    envelope: ProjectEnvelope
    material_node_ids: dict[str, list[str]]

    def public_envelope(self) -> dict:
        envelope = self.envelope.model_dump(by_alias=True, exclude_unset=True)
        envelope["canvases"] = [
            public_canvas(canvas, set(self.material_node_ids.get(canvas["sceneId"], ())))
            for canvas in envelope["canvases"]
        ]
        return envelope


class SceneDelivery(ProjectDelivery):
    replayed: bool


class CloudBaseProjectRepository:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _delivery(self, operation: str, parameters: dict) -> dict:
        value = call_cloudbase_rpc(self.settings, f"sceneturn_project_{operation}", parameters)
        try:
            delivery = ProjectDelivery.model_validate(value, strict=True)
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "项目数据暂时无法读取") from None
        return delivery.public_envelope()

    def list(self, owner_id: str) -> ProjectList:
        value = call_cloudbase_rpc(self.settings, "sceneturn_project_list", {"p_owner_id": owner_id})
        try:
            listing = ProjectList.model_validate(value, strict=True)
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "项目列表暂时无法读取") from None
        for project in listing.projects:
            if project.archived_at is not None and not project.archived_at.strip():
                project.archived_at = None
        return listing

    def create(self, owner_id: str, title: str) -> dict:
        project_id = new_id()
        snapshot, scene_id = initial_project(project_id, title)
        return self._delivery("create", {"p_owner_id": owner_id, "p_project_id": project_id,
            "p_snapshot": snapshot, "p_canvas": empty_canvas(scene_id)})

    def get(self, owner_id: str, project_id: str) -> dict:
        return self._delivery("get", {"p_owner_id": owner_id, "p_project_id": project_id})

    def create_scene(self, owner_id: str, project_id: str, body: SceneCreate) -> SceneDelivery:
        scene_id = new_id()
        value = call_cloudbase_rpc(self.settings, "sceneturn_scene_create", {
            "p_owner_id": owner_id, "p_project_id": project_id, "p_expected_revision": body.expected_revision,
            "p_operation_id": body.operation_id, "p_payload_hash": payload_digest(body.model_dump(by_alias=True)),
            "p_scene": scene_node(scene_id, body.title), "p_canvas": empty_canvas(scene_id),
        })
        try:
            return SceneDelivery.model_validate(value, strict=True)
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "场景数据暂时无法读取") from None

    def save(self, owner_id: str, project_id: str, body: ProjectSave) -> dict:
        payload = body.model_dump(by_alias=True, exclude_unset=True)
        snapshot = {"assets": [], "metadata": {}, **payload["project"]}
        template = empty_canvas("")
        del template["sceneId"]
        return self._delivery("save", {"p_owner_id": owner_id, "p_project_id": project_id,
            "p_expected_revision": body.expected_revision, "p_operation_id": body.operation_id,
            "p_payload_hash": payload_digest(payload), "p_snapshot": snapshot, "p_canvas_template": template})
