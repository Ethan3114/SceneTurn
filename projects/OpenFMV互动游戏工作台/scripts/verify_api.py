import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path


PRODUCT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="运行真实HTTP、SQLite、媒体和可选浏览器检查")
    parser.add_argument("--browser", action="store_true", help="额外检查已启动的3210网页和8310服务")
    arguments = parser.parse_args()
    temporary = PRODUCT_ROOT / ".intermediate" / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    (PRODUCT_ROOT / ".intermediate" / "pytest").mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(TMPDIR=str(temporary), XDG_CACHE_HOME=str(PRODUCT_ROOT / ".intermediate" / "cache"))
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests", f"--basetemp={PRODUCT_ROOT / '.intermediate' / 'pytest' / uuid.uuid4().hex}"],
        cwd=PRODUCT_ROOT / "backend",
        env=environment,
        check=True,
    )
    if arguments.browser:
        subprocess.run(["node", "scripts/check-browser.mjs"], cwd=PRODUCT_ROOT, env=environment, check=True)


if __name__ == "__main__":
    main()
