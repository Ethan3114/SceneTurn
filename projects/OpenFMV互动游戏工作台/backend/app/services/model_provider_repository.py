from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import encode_secret
from app.models import new_id
from app.provider_schemas import ModelProviderUpdate, ProviderId
from app.schemas import UserView
from app.services.cloudbase_database import call_cloudbase_rpc
from app.services.providers import (
    PROVIDER_IDS,
    model_registry_response,
    normalize_model_defaults,
    prepare_provider_configuration,
    validate_provider_fields,
)


class ProviderRegistryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    config: dict
    configured: bool


class ProviderRegistrySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connections: dict[ProviderId, ProviderRegistryRecord]
    wan_workspace: str | None = Field(alias="wanWorkspace")
    codex_image: bool = Field(alias="codexImage")
    project_defaults: dict | None = Field(alias="projectDefaults")
    project_revision: int | None = Field(alias="projectRevision", ge=1)


class CloudBaseModelProviderRepository:
    def __init__(self, settings: Settings):
        self.settings = settings

    def read(self, owner_id: str, project_id: str | None = None) -> ProviderRegistrySnapshot:
        if project_id is not None:
            try:
                project_id = str(UUID(project_id))
            except ValueError:
                raise AppError(404, "PROJECT_NOT_FOUND", "项目不存在") from None
        raw = call_cloudbase_rpc(self.settings, "sceneturn_model_registry", {"p_owner_id": owner_id, "p_project_id": project_id})
        try:
            snapshot = ProviderRegistrySnapshot.model_validate(raw)
        except ValidationError:
            raise AppError(502, "MODEL_REGISTRY_INVALID", "供应商配置返回内容无效") from None
        if set(snapshot.connections) != set(PROVIDER_IDS):
            raise AppError(502, "MODEL_REGISTRY_INVALID", "供应商配置返回内容不完整")
        return snapshot

    def registry(self, user: UserView, project_id: str | None = None) -> dict:
        value = self.read(user.id, project_id)
        return model_registry_response(user, self.settings, {identifier: record.model_dump() for identifier, record in value.connections.items()},
            wan_workspace=value.wan_workspace, codex_image=value.codex_image, defaults=value.project_defaults, revision=value.project_revision)

    def project_defaults(self, user: UserView, project_id: str) -> dict:
        snapshot = self.read(user.id, project_id)
        return {"defaults": normalize_model_defaults(snapshot.project_defaults), "revision": snapshot.project_revision}

    def update(self, user: UserView, provider_id: ProviderId, body: ModelProviderUpdate) -> dict:
        validate_provider_fields(user, provider_id, body, self.settings)
        if provider_id not in {"aliyun-wan", "volcengine-ark"}:
            raise AppError(503, "MODEL_PROVIDER_GATEWAY_UNAVAILABLE", "当前供应商的配置服务尚未连接")
        snapshot = self.read(user.id)
        previous = snapshot.connections[provider_id]
        if body.expected_revision != previous.revision:
            raise AppError(409, "MODEL_CONFIGURATION_CONFLICT", "供应商配置已有新版本，请重新读取")
        config, key = prepare_provider_configuration(provider_id, body, previous.config)
        call_cloudbase_rpc(self.settings, "sceneturn_model_owner_update", {
            "p_owner_id": user.id,
            "p_record_id": new_id(),
            "p_provider_id": provider_id,
            "p_expected_revision": body.expected_revision,
            "p_config": config,
            "p_encrypted_key": encode_secret(key, self.settings) if key else None,
            "p_update_wan_workspace": body.workspace_id is not None,
        })
        return self.registry(user)
