from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PRODUCT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPENFMV_", env_file=PRODUCT_DIR / "backend" / ".env", extra="forbid"
    )

    data_dir: Path = PRODUCT_DIR / "data"
    tmp_dir: Path = PRODUCT_DIR / ".intermediate" / "media"
    frontend_origin: str = "http://127.0.0.1:3210"
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

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir.resolve() / 'openfmv.sqlite3'}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
