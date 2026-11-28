import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path

import httpx


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = PRODUCT_ROOT / "backend" / ".venv" / "bin" / "python"
FRONTEND_ORIGIN = "http://127.0.0.1:3210"


def run_directory() -> Path:
    directory = Path(os.environ["RESTART_CHECK_DIR"]).resolve()
    if not directory.is_relative_to(PRODUCT_ROOT / ".intermediate"):
        raise ValueError("重启验证目录必须位于本产品.intermediate内")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_json(path: Path, value) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        json.dump(value, destination, ensure_ascii=False)


def endpoint(directory: Path) -> dict:
    return json.loads((directory / "endpoint.json").read_text(encoding="utf-8"))


def client(directory: Path, authenticated: bool = False) -> httpx.Client:
    cookies = {}
    if authenticated:
        cookies = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    return httpx.Client(
        base_url=f"http://127.0.0.1:{endpoint(directory)['port']}/api/v1",
        headers={"Origin": FRONTEND_ORIGIN},
        cookies=cookies,
        timeout=15,
        trust_env=False,
    )


def start(directory: Path) -> None:
    endpoint_file = directory / "endpoint.json"
    if not endpoint_file.exists():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        save_json(endpoint_file, {"port": port})
    port = endpoint(directory)["port"]
    with socket.socket() as listener:
        if listener.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"重启验证端口{port}已被占用")
    temp_dir = directory / "tmp"
    temp_dir.mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        SCENETURN_DATA_DIR=str(directory / "data"),
        SCENETURN_TMP_DIR=str(directory / "media"),
        SCENETURN_WORKER_ENABLED="false",
        SCENETURN_FRONTEND_ORIGIN=FRONTEND_ORIGIN,
        TMPDIR=str(temp_dir),
        XDG_CACHE_HOME=str(directory / "cache"),
        PYTHONPYCACHEPREFIX=str(directory / "pycache"),
        MODEL_API_KEY="",
        DASHSCOPE_API_KEY="",
    )
    with (directory / "server.log").open("ab") as output:
        process = subprocess.Popen(
            [str(PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=PRODUCT_ROOT / "backend",
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    processes_file = directory / "processes.json"
    processes = json.loads(processes_file.read_text()) if processes_file.exists() else []
    processes.append(process.pid)
    save_json(processes_file, processes)
    deadline = time.monotonic() + 45
    while True:
        if process.poll() is not None:
            raise RuntimeError(f"重启验证服务未能启动，日志位于{directory / 'server.log'}")
        with socket.socket() as connection:
            connection.settimeout(0.2)
            connected = connection.connect_ex(("127.0.0.1", port)) == 0
        if connected:
            break
        if time.monotonic() >= deadline:
            process.terminate()
            raise TimeoutError("重启验证服务启动超时")
        time.sleep(0.1)
    with client(directory) as service:
        service.get("/health").raise_for_status()
    print(process.pid)


def write(directory: Path, marker: str) -> None:
    with client(directory) as service:
        registration = service.post(
            "/auth/register",
            json={"email": f"restart-{uuid.uuid4().hex}@example.com", "password": secrets.token_urlsafe(12)},
        )
        registration.raise_for_status()
        project = service.post("/projects", json={"title": marker})
        project.raise_for_status()
        save_json(directory / "session.json", {"sceneturn_session": service.cookies["sceneturn_session"]})
        project_id = project.json()["project"]["id"]
        save_json(directory / "written.json", {"projectId": project_id})
    print(project_id)


def read(directory: Path, project_id: str) -> None:
    with client(directory, authenticated=True) as service:
        response = service.get(f"/projects/{project_id}")
        response.raise_for_status()
        print(response.json()["project"]["title"])


def stop(directory: Path) -> None:
    processes_file = directory / "processes.json"
    if processes_file.exists():
        port = endpoint(directory)["port"]
        for process_id in json.loads(processes_file.read_text()):
            command = subprocess.run(["ps", "-p", str(process_id), "-o", "command="], capture_output=True, text=True)
            if command.returncode == 0 and str(PYTHON) in command.stdout and f"--port {port}" in command.stdout:
                os.kill(process_id, signal.SIGTERM)
    print("已终止本次重启验证服务")


def main() -> None:
    parser = argparse.ArgumentParser(description="通过真实HTTP接口验证服务重启后的数据与会话恢复")
    parser.add_argument("action", choices=["start", "write", "read", "stop"])
    parser.add_argument("value", nargs="?")
    arguments = parser.parse_args()
    directory = run_directory()
    if arguments.action == "start":
        start(directory)
    elif arguments.action == "write":
        if arguments.value is None:
            parser.error("write需要随机标记")
        write(directory, arguments.value)
    elif arguments.action == "read":
        if arguments.value is None:
            parser.error("read需要项目ID")
        read(directory, arguments.value)
    else:
        stop(directory)


if __name__ == "__main__":
    main()
