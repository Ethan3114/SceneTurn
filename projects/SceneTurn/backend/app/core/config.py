from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, EnvSettingsSource, PydanticBaseSettingsSource, SettingsConfigDict


PRODUCT_DIR = Path(__file__).resolve().parents[3]
SETTINGS_PREFIX = "SCENETURN_"
LEGACY_SETTINGS_PREFIX = "OPENFMV_"
DATABASE_FILENAME = "sceneturn.sqlite3"
LEGACY_DATABASE_FILENAME = "openfmv.sqlite3"


def sqlite_database_path(data_dir: Path) -> Path:
    current = data_dir / DATABASE_FILENAME
    legacy = data_dir / LEGACY_DATABASE_FILENAME
    if legacy.exists():
        if current.exists():
            raise RuntimeError("数据目录同时包含两个 SQLite 数据库，请确认应使用的文件")
        return legacy
    return current


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=SETTINGS_PREFIX, env_file=PRODUCT_DIR / "backend" / ".env", extra="forbid", hide_input_in_errors=True
    )

    data_dir: Path = PRODUCT_DIR / "data"
    tmp_dir: Path = PRODUCT_DIR / ".intermediate" / "media"
    frontend_origin: str = "http://127.0.0.1:3210"
    proxy_shared_secret: SecretStr | None = Field(default=None, min_length=16, max_length=4096,
        validation_alias=AliasChoices("SCENETURN_PROXY_SHARED_SECRET", "PROXY_SHARED_SECRET",
                                      LEGACY_SETTINGS_PREFIX + "PROXY_SHARED_SECRET", "proxy_shared_secret"))
    proxy_trusted_edge: Literal["", "cloudbase"] = ""
    master_key: SecretStr | None = None
    postgres_url: SecretStr | None = None
    cloudbase_env_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    cloudbase_storage_bucket: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")
    cloudbase_api_key: SecretStr | None = Field(default=None, min_length=20)
    cloudbase_gateway_scope_token: SecretStr | None = Field(default=None, min_length=43, max_length=128)
    cloudbase_gateway_enabled: bool = False
    transfer_queue_bucket: str | None = None
    transfer_queue_region: str = "ap-shanghai"
    transfer_queue_secret_id: SecretStr | None = None
    transfer_queue_secret_key: SecretStr | None = None
    transfer_queue_session_token: SecretStr | None = None
    transfer_queue_write_token: SecretStr | None = Field(default=None, min_length=32, max_length=256)
    scheduler_token: SecretStr | None = Field(default=None, min_length=32, max_length=256)
    deployment: Literal["local", "cloudbase", "standalone"] = "local"
    worker_enabled: bool = True
    generation_model: str = "wan3.0-video"
    generation_region: str = "cn-beijing"
    poll_interval: float = Field(default=10, ge=1, le=60)
    request_timeout: float = Field(default=30, ge=1, le=120)
    wan_submit_timeout: float = Field(default=90, ge=30, le=180)
    download_timeout: float = Field(default=180, ge=1, le=600)
    max_generation_cents: int = Field(default=1000, ge=1, le=1000)
    budget_cents: int = Field(default=30000, ge=1, le=30000)
    max_upload_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)
    max_download_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    session_seconds: int = Field(default=604800, ge=300, le=2592000)
    cookie_secure: bool = False
    text_timeout: float = Field(default=600, ge=5, le=600)
    text_max_output_tokens: int = Field(default=32768, ge=64, le=32768)
    text_admin_email: str = ""
    comfy_trusted_audio_nodes: str = ""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        for source in (env_settings, dotenv_settings):
            assert isinstance(source, EnvSettingsSource)
            prefix = SETTINGS_PREFIX if source.case_sensitive else SETTINGS_PREFIX.lower()
            legacy_prefix = LEGACY_SETTINGS_PREFIX if source.case_sensitive else LEGACY_SETTINGS_PREFIX.lower()
            values = dict(source.env_vars)
            for name in tuple(values):
                if name.startswith(legacy_prefix):
                    legacy_value = values.pop(name)
                    values.setdefault(prefix + name.removeprefix(legacy_prefix), legacy_value)
            source.env_vars = values
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @model_validator(mode="after")
    def validate_deployment(self):
        storage_fields = (self.cloudbase_env_id, self.cloudbase_storage_bucket, self.cloudbase_api_key)
        if any(value is not None for value in storage_fields) and any(value is None for value in storage_fields):
            raise ValueError("CloudBase 存储需要环境、私有桶和服务端密钥")
        if self.cloudbase_gateway_enabled:
            raise ValueError("启用数据库网关前，需要完成所有业务路由和后台任务的持久化切换")
        if self.deployment == "cloudbase" and (
            self.postgres_url is None or self.cloudbase_api_key is None or self.master_key is None
            or self.proxy_shared_secret is None or not self.cookie_secure
        ):
            raise ValueError("生产部署需要持久数据库、私有存储、主密钥和安全会话配置")
        if self.deployment == "standalone":
            origin = urlsplit(self.frontend_origin)
            if (origin.scheme != "https" or not origin.hostname or origin.username is not None
                    or origin.password is not None or origin.path or origin.query or origin.fragment
                    or self.frontend_origin != origin.geturl() or self.frontend_origin != self.frontend_origin.strip()
                    or origin.netloc != origin.netloc.lower() or not origin.netloc.isascii() or origin.port in (0, 443)):
                raise ValueError("独立部署需要准确的 HTTPS 网页来源地址")
            if self.proxy_shared_secret is None or not self.cookie_secure or self.proxy_trusted_edge:
                raise ValueError("独立部署需要专用代理密钥及安全的登录 Cookie")
        direct_queue_fields = (self.transfer_queue_bucket, self.transfer_queue_secret_id, self.transfer_queue_secret_key)
        if self.transfer_queue_write_token is not None:
            if (any(value is not None for value in (*direct_queue_fields, self.transfer_queue_session_token))
                    or self.scheduler_token is None or self.cloudbase_storage_bucket is None):
                raise ValueError("云函数队列需要私有素材存储及唤醒密钥，并使用独立队列写入凭据")
        elif any(value is not None for value in (*direct_queue_fields, self.transfer_queue_session_token, self.scheduler_token)) and (
            any(value is None for value in (*direct_queue_fields, self.scheduler_token)) or self.cloudbase_storage_bucket is None
        ):
            raise ValueError("传输调度需要私有素材存储、COS 队列访问配置和唤醒密钥")
        return self

    @property
    def database_url(self) -> str:
        return f"sqlite:///{sqlite_database_path(self.data_dir.resolve())}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
