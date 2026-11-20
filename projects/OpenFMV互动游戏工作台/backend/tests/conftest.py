import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import connect
from tempfile import TemporaryDirectory

import httpx
import pytest


PRODUCT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ORIGIN = "http://127.0.0.1:3210"


@pytest.fixture
def writable_snapshot(tmp_path):
    with TemporaryDirectory(dir=tmp_path) as directory:
        destination = Path(directory) / "openfmv.sqlite3"

        def copy(source: Path) -> Path:
            with closing(connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as original:
                with closing(connect(destination)) as snapshot:
                    original.backup(snapshot)
            return destination

        yield copy


@dataclass(frozen=True)
class LiveService:
    base_url: str
    data_dir: Path

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"Origin": FRONTEND_ORIGIN},
            timeout=30,
            trust_env=False,
        )


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=15)


@pytest.fixture(scope="session")
def live_service(tmp_path_factory, request) -> LiveService:
    run_dir = tmp_path_factory.mktemp("live-service").resolve()
    assert run_dir.is_relative_to(PRODUCT_ROOT / ".intermediate")
    temp_dir = run_dir / "tmp"
    temp_dir.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = os.environ.copy()
    environment.update(
        OPENFMV_DATA_DIR=str(run_dir / "data"),
        OPENFMV_TMP_DIR=str(run_dir / "media"),
        OPENFMV_WORKER_ENABLED="false",
        OPENFMV_FRONTEND_ORIGIN=FRONTEND_ORIGIN,
        TMPDIR=str(temp_dir),
        XDG_CACHE_HOME=str(run_dir / "cache"),
        PYTHONPYCACHEPREFIX=str(run_dir / "pycache"),
    )
    log_file = (run_dir / "server.log").open("wb")
    request.addfinalizer(log_file.close)
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=PRODUCT_ROOT / "backend",
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    request.addfinalizer(lambda: stop_process(process))
    deadline = time.monotonic() + 45
    while True:
        assert process.poll() is None, f"真实服务启动失败，日志：{run_dir / 'server.log'}"
        with socket.socket() as connection:
            connection.settimeout(0.2)
            connected = connection.connect_ex(("127.0.0.1", port)) == 0
        if connected:
            break
        assert time.monotonic() < deadline, f"真实服务启动超时，日志：{run_dir / 'server.log'}"
        time.sleep(0.1)
    base_url = f"http://127.0.0.1:{port}/api/v1"
    with httpx.Client(base_url=base_url, trust_env=False) as client:
        assert client.get("/health").status_code == 200
    return LiveService(base_url=base_url, data_dir=run_dir / "data")


@pytest.fixture(scope="session")
def sample_video() -> Path:
    destination = PRODUCT_ROOT / ".intermediate" / "test-media" / "flower.mp4"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            response = client.get("https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4")
            response.raise_for_status()
            destination.write_bytes(response.content)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(destination), "-f", "null", "-"],
        check=True,
        capture_output=True,
    )
    return destination


@pytest.fixture(scope="session")
def sample_input_media(sample_video) -> dict[str, tuple[Path, str]]:
    image = sample_video.parent / "flower-frame.png"
    if not image.exists():
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(sample_video), "-frames:v", "1", str(image)],
            check=True, capture_output=True,
        )
    audio = sample_video.parent / "t-rex-roar.mp3"
    if not audio.exists():
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            response = client.get("https://interactive-examples.mdn.mozilla.net/media/cc0-audio/t-rex-roar.mp3")
            response.raise_for_status()
            audio.write_bytes(response.content)
    for source in (image, audio):
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-f", "null", "-"], check=True, capture_output=True)
    return {"image": (image, "image/png"), "video": (sample_video, "video/mp4"), "audio": (audio, "audio/mpeg")}
