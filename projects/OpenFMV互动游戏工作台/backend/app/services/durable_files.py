from pathlib import Path

from app.core.config import Settings
from app.services.object_storage import private_storage


def persist_file(settings: Settings, path: Path, content_type: str) -> None:
    storage = private_storage(settings)
    if storage is not None:
        key = path.resolve().relative_to(settings.data_dir.resolve()).as_posix()
        storage.put(key, path, content_type)


def materialize_file(settings: Settings, path: Path, *, max_bytes: int, sha256: str | None = None) -> Path:
    storage = private_storage(settings)
    if storage is not None and not path.is_file():
        key = path.resolve().relative_to(settings.data_dir.resolve()).as_posix()
        storage.materialize(key, path, max_bytes=max_bytes, sha256=sha256)
    return path
