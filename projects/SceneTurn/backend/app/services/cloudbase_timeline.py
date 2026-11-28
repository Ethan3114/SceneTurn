from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.schemas import ProjectSnapshot, WireModel
from app.services.cloudbase_assets import CloudBaseAssetRecord
from app.services.cloudbase_database import call_cloudbase_rpc
from app.services.cloudbase_projects import ProjectDelivery
from app.services.object_storage import private_storage
from app.services.projects import payload_digest
from app.tools.story_tools import TimelineAssetInput, attach_timeline_asset


class TimelineSource(WireModel):
    snapshot: ProjectSnapshot
    scene_index: int
    asset: CloudBaseAssetRecord


class CloudBaseTimelineRepository:
    def __init__(self, settings: Settings):
        self.settings = settings

    def attach(self, owner_id: str, project_id: str, body: TimelineAssetInput) -> dict:
        parameters = {
            "p_owner_id": owner_id, "p_project_id": project_id, "p_expected_revision": body.expected_revision,
            "p_operation_id": body.operation_id, "p_scene_id": body.scene_id, "p_asset_id": body.asset_id,
            "p_payload_hash": payload_digest(body.model_dump(by_alias=True)),
        }
        raw = call_cloudbase_rpc(self.settings, "sceneturn_timeline_prepare", parameters)
        try:
            if isinstance(raw, dict) and set(raw) == {"replay"}:
                return ProjectDelivery.model_validate(raw["replay"], strict=True).public_envelope()
            source = TimelineSource.model_validate(raw, strict=True)
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "时间线素材暂时无法读取") from None

        storage = private_storage(self.settings)
        if storage is None:
            raise AppError(503, "STORAGE_REQUEST_FAILED", "私有素材存储暂时不可用")
        if storage.object_size(source.asset.relpath) != source.asset.bytes:
            raise AppError(422, "STORAGE_CONTENT_INVALID", "素材文件大小与项目记录不一致")
        snapshot = source.snapshot.model_dump(by_alias=True, exclude_unset=True)
        nodes = snapshot["graphData"]["nodes"]
        if not 0 <= source.scene_index < len(nodes) or nodes[source.scene_index]["id"] != body.scene_id:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "时间线场景暂时无法核实")
        scene = nodes[source.scene_index]
        attach_timeline_asset(snapshot, scene, body, source.asset)
        validated = ProjectSnapshot.model_validate(snapshot).model_dump(by_alias=True, exclude_unset=True)
        result = call_cloudbase_rpc(self.settings, "sceneturn_timeline_save", {
            **parameters, "p_timeline": validated["graphData"]["nodes"][source.scene_index]["data"]["timeline"],
            "p_assets": validated["assets"],
        })
        try:
            return ProjectDelivery.model_validate(result, strict=True).public_envelope()
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "时间线保存结果暂时无法读取") from None
