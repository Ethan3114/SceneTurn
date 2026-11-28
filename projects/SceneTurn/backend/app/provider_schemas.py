from typing import Literal

from pydantic import Field, SecretStr, model_validator

from app.schemas import WireModel


ProviderId = Literal["deepseek", "aliyun-qwen", "sensetime", "volcengine-ark", "aliyun-wan"]


class ModelPrice(WireModel):
    version: str = Field(min_length=1, max_length=100)
    verified: Literal[True]
    currency: Literal["CNY"] = "CNY"
    cache_hit_micros_per_million: int | None = Field(default=None, ge=0, le=1000000000)
    cache_miss_micros_per_million: int | None = Field(default=None, ge=0, le=1000000000)
    output_micros_per_million: int | None = Field(default=None, ge=0, le=1000000000)
    micros_per_image: int | None = Field(default=None, gt=0, le=10000000)
    cents_per_second: dict[Literal["480P", "720P", "1080P"], int] | None = None

    @model_validator(mode="after")
    def validate_rates(self):
        if self.cents_per_second is not None and (not self.cents_per_second or any(type(value) is not int or value <= 0 or value > 100000 for value in self.cents_per_second.values())):
            raise ValueError("视频价表需要明确的正整数分每秒价格")
        return self


class ModelProviderUpdate(WireModel):
    expected_revision: int = Field(default=0, ge=0)
    api_key: SecretStr | None = None
    api_keys: list[SecretStr] | None = Field(default=None, min_length=3, max_length=3)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=63, pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
    region: Literal["cn-beijing"] = "cn-beijing"
    base_url: str | None = Field(default=None, max_length=250)
    entitled_model_ids: list[str] | None = Field(default=None, max_length=20)
    prices: dict[str, ModelPrice] | None = None


class ProjectModelDefaults(WireModel):
    text: str = Field(default="deepseek-flash", min_length=1, max_length=100)
    image: str = Field(default="sensetime-fast", min_length=1, max_length=100)
    video: str = Field(default="wan-3-0", min_length=1, max_length=100)


class ProjectModelDefaultsUpdate(ProjectModelDefaults):
    expected_revision: int = Field(ge=1)
    operation_id: str = Field(min_length=1, max_length=100)


class ModelSelection(WireModel):
    text: str | None = Field(default=None, min_length=1, max_length=100)
    image: str | None = Field(default=None, min_length=1, max_length=100)
    video: str | None = Field(default=None, min_length=1, max_length=100)


class ModelProfileView(WireModel):
    id: str
    provider_id: str
    label: str
    kind: Literal["text", "image", "video"]
    model: str
    capabilities: dict
    configured: bool
    available: bool
    unavailable_reason: str | None
    requires_entitlement: bool


class ModelProviderConnectionView(WireModel):
    provider_id: str
    configured: bool
    revision: int
    can_manage: bool
    region: str
    workspace_id: str | None
    entitled_model_ids: list[str]
    prices: dict[str, ModelPrice]


class ModelProviderRegistryView(WireModel):
    models: list[ModelProfileView]
    connections: list[ModelProviderConnectionView]
    defaults: ProjectModelDefaults
    revision: int | None


class ProjectModelDefaultsView(WireModel):
    defaults: ProjectModelDefaults
    revision: int
