from typing import Annotated

from pydantic import Field, ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.schemas import CanvasNode, CanvasResult, CanvasSave, CanvasSnapshot, WireModel
from app.services.canvas_delivery import public_canvas, restore_canvas_private_data
from app.services.cloudbase_database import call_cloudbase_rpc
from app.services.projects import payload_digest


class CanvasDelivery(CanvasResult):
    material_node_ids: list[str]

    def public_result(self) -> dict:
        snapshot = self.canvas.model_dump(by_alias=True, exclude_unset=True)
        return {"canvas": public_canvas(snapshot, set(self.material_node_ids)), "revision": self.revision}


class CanvasPreparation(CanvasDelivery):
    base_token: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None

    def restore(self, submitted: CanvasSnapshot) -> CanvasSnapshot:
        current = self.canvas.model_dump(by_alias=True, exclude_unset=True)
        restored = restore_canvas_private_data(current, submitted.model_dump(by_alias=True, exclude_unset=True),
                                              set(self.material_node_ids))
        try:
            return CanvasSnapshot.model_validate(restored, strict=True)
        except ValidationError:
            raise AppError(422, "CANVAS_REQUEST_INVALID", "画布内容无效，请检查节点及连线") from None


class CanvasVersion(WireModel):
    revision: int
    node: CanvasNode


class CanvasHistory(WireModel):
    versions: list[CanvasVersion] = Field(max_length=13)
    material_node_ids: list[str]

    def public_result(self) -> dict:
        allowed = set(self.material_node_ids)
        versions = []
        for item in self.versions[:12]:
            node = item.node.model_dump(by_alias=True, exclude_unset=True)
            projected = public_canvas({"nodes": [node], "connections": []}, allowed)
            if projected["nodes"]:
                versions.append({"revision": item.revision, "node": projected["nodes"][0]})
        return {"versions": versions, "before": self.versions[11].revision if len(self.versions) > 12 else None}


class CloudBaseCanvasRepository:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _request[T: WireModel](self, name: str, result_type: type[T], **parameters) -> T:
        value = call_cloudbase_rpc(self.settings, f"sceneturn_canvas_{name}", parameters)
        try:
            return result_type.model_validate(value, strict=True)
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "画布暂时无法读取") from None

    def read(self, owner_id: str, project_id: str, scene_id: str) -> dict:
        return self._request("read", CanvasDelivery, p_owner_id=owner_id, p_project_id=project_id,
                             p_scene_id=scene_id).public_result()

    def versions(self, owner_id: str, project_id: str, scene_id: str, node_id: str, before: int | None) -> dict:
        return self._request("versions", CanvasHistory, p_owner_id=owner_id, p_project_id=project_id,
                             p_scene_id=scene_id, p_node_id=node_id, p_before=before).public_result()

    def save(self, owner_id: str, project_id: str, scene_id: str, body: CanvasSave) -> dict:
        payload = {"sceneId": scene_id, **body.model_dump(by_alias=True, exclude_unset=True)}
        parameters = {"p_owner_id": owner_id, "p_project_id": project_id, "p_scene_id": scene_id,
                      "p_expected_revision": body.expected_revision, "p_operation_id": body.operation_id,
                      "p_payload_hash": payload_digest(payload)}
        preparation = self._request("prepare", CanvasPreparation, **parameters)
        if preparation.base_token is None:
            return preparation.public_result()
        submitted = CanvasSnapshot(scene_id=scene_id, nodes=body.nodes, connections=body.connections, viewport=body.viewport)
        restored = preparation.restore(submitted)
        return self._request("save", CanvasDelivery, **parameters, p_base_token=preparation.base_token,
                             p_canvas=restored.model_dump(by_alias=True, exclude_unset=True)).public_result()
