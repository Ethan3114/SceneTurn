from pathlib import Path
from shutil import copy2
from tempfile import mkdtemp


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
ROOT_CONTEXT_FILES = {
    "backend/Dockerfile": "Dockerfile",
    "backend/Dockerfile.dockerignore": ".dockerignore",
}
RUNTIME_FILES = (
    *ROOT_CONTEXT_FILES,
    "backend/alembic.ini",
    "backend/requirements.txt",
    "scripts/render_game.cjs",
    "frontend/electron/exporter.js",
    "frontend/shared/assetPaths.js",
    "frontend/shared/graphRuntimeCore.mjs",
    "frontend/shared/runtimeCore.mjs",
)
PYTHON_DIRECTORIES = ("backend/alembic", "backend/app")


def stage_context() -> Path:
    staging_root = PRODUCT_ROOT / ".intermediate" / "stage4" / "backend-image-contexts"
    if staging_root.resolve() != staging_root:
        raise ValueError("后端构建目录必须位于本项目内且不经过符号链接")
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = Path(mkdtemp(prefix="api-", dir=staging_root))

    sources = [PRODUCT_ROOT / file for file in RUNTIME_FILES]
    for directory in PYTHON_DIRECTORIES:
        source_directory = PRODUCT_ROOT / directory
        if source_directory.resolve(strict=True) != source_directory or not source_directory.is_dir():
            raise ValueError(f"后端源码目录不可用于打包：{directory}")
        sources.extend(
            file for file in sorted(source_directory.rglob("*.py"))
            if all(not part.startswith(".") and part not in {"data", "secrets", "__pycache__"}
                   for part in file.relative_to(source_directory).parts)
        )

    for source in sources:
        relative_path = source.relative_to(PRODUCT_ROOT)
        if source.resolve(strict=True) != source or not source.is_file():
            raise ValueError(f"后端构建输入必须为普通文件且不经过符号链接：{relative_path}")
        target = destination / ROOT_CONTEXT_FILES.get(relative_path.as_posix(), relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        copy2(source, target)
    return destination


if __name__ == "__main__":
    print(stage_context())
