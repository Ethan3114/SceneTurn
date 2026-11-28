from uuid import UUID

from app.core.config import Settings
from app.core.errors import AppError
from app.models import Generation
from app.services.cloudbase_database import call_cloudbase_rpc
from app.services.generation import generation_view
from app.services.material_delivery import public_material_generation
from app.services.projects import payload_digest


def _identifier(value: str, *, project: bool = False) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        if project:
            raise AppError(404, "PROJECT_NOT_FOUND", "项目不存在") from None
        raise AppError(404, "GENERATION_NOT_FOUND", "生成任务不存在") from None


def video_delivery(record: dict) -> dict:
    value = generation_view(Generation(**record["job"]))
    if not record["material"]:
        return value
    artifacts = None
    if artifact := record.get("artifact"):
        node_id = value["input"]["request"]["videoArtifactNodeId"]
        artifacts = {(value["sceneId"], node_id): (payload_digest(artifact), artifact)}
    return public_material_generation(value, artifacts)


class CloudBaseVideoRepository:
    def __init__(self, settings: Settings):
        self.settings = settings

    def read(self, owner_id: str, generation_id: str) -> dict:
        record = call_cloudbase_rpc(self.settings, "sceneturn_video_read", {"p_owner_id": owner_id,
            "p_job_id": _identifier(generation_id)})
        return video_delivery(record)

    def list(self, owner_id: str, project_id: str) -> list[dict]:
        records = call_cloudbase_rpc(self.settings, "sceneturn_video_list", {"p_owner_id": owner_id,
            "p_project_id": _identifier(project_id, project=True)})
        return [video_delivery(record) for record in records]
