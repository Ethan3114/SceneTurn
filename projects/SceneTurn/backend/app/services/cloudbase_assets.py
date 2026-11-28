from dataclasses import asdict

from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.models import new_id
from app.schemas import WireModel
from app.services.cloudbase_database import call_cloudbase_rpc
from app.services.cloudbase_projects import ProjectDelivery
from app.services.media import StoredMedia, asset_view


class CloudBaseAssetRecord(WireModel):
    id: str
    project_id: str
    generation_id: str | None
    name: str
    relpath: str
    poster_relpath: str | None
    mime_type: str
    bytes: int
    sha256: str
    duration_ms: int | None
    width: int | None
    height: int | None
    metadata_json: dict[str, str]
    created_at: float


class AssetRegistration(ProjectDelivery):
    asset: CloudBaseAssetRecord


class CloudBaseAssetRepository:
    def __init__(self, settings: Settings):
        self.settings = settings

    def check_upload_target(self, owner_id: str, project_id: str) -> None:
        allowed = call_cloudbase_rpc(self.settings, "sceneturn_asset_upload_target",
            {"p_owner_id": owner_id, "p_project_id": project_id})
        if allowed is not True:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "素材上传目标暂时无法核实")

    def register(self, owner_id: str, project_id: str, media: StoredMedia) -> dict:
        parameters = {f"p_{name}": value for name, value in asdict(media).items()}
        parameters.update(p_owner_id=owner_id, p_project_id=project_id, p_asset_id=new_id())
        value = call_cloudbase_rpc(self.settings, "sceneturn_asset_register", parameters)
        try:
            registration = AssetRegistration.model_validate(value, strict=True)
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "素材登记结果暂时无法读取") from None
        return {"asset": asset_view(registration.asset), **registration.public_envelope()}

    def get(self, owner_id: str, asset_id: str) -> CloudBaseAssetRecord:
        value = call_cloudbase_rpc(self.settings, "sceneturn_asset_get", {"p_owner_id": owner_id, "p_asset_id": asset_id})
        try:
            return CloudBaseAssetRecord.model_validate(value, strict=True)
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "素材数据暂时无法读取") from None
