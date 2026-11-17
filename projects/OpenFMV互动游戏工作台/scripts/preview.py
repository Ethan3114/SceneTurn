import argparse
import fcntl
import json
import math
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
INTERMEDIATE = PRODUCT_ROOT / ".intermediate"
RUN_DIRECTORY = INTERMEDIATE / "run"
TEMP_DIRECTORY = INTERMEDIATE / "tmp"
CACHE_DIRECTORY = INTERMEDIATE / "cache"
LAUNCHCTL = "/bin/launchctl"
DOMAIN = f"gui/{os.getuid()}"
MANAGER = str(Path(__file__).resolve())


@dataclass(frozen=True)
class Service:
    name: str
    port: int
    url: str

    @property
    def label(self) -> str:
        return f"com.openfmv.workbench.preview-{self.name}"

    @property
    def target(self) -> str:
        return f"{DOMAIN}/{self.label}"

    @property
    def plist_path(self) -> Path:
        return RUN_DIRECTORY / f"{self.label}.plist"


API = Service("api", 8310, "http://127.0.0.1:8310/api/v1/health")
WEB = Service("web", 3210, "http://127.0.0.1:3210/zh-CN/account")
SERVICES = (API, WEB)


def launchctl(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run([LAUNCHCTL, *arguments], capture_output=True, text=True, check=False)


def require_success(result: subprocess.CompletedProcess) -> None:
    if result.returncode != 0:
        print(result.stdout, end="", file=sys.stderr)
        print(result.stderr, end="", file=sys.stderr)
        result.check_returncode()


def loaded(service: Service) -> bool:
    result = launchctl("print", service.target)
    if result.returncode == 113:
        return False
    require_success(result)
    return True


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.2)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def executable(name: str) -> Path:
    found = shutil.which(name)
    if found is None:
        raise RuntimeError(f"缺少{name}，请先安装该程序")
    path = Path(found).absolute()
    if not path.resolve(strict=True).is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"程序无法执行：{path}")
    return path


def web_directory(value: Path) -> Path:
    path = value.resolve(strict=True)
    intermediate = INTERMEDIATE.resolve(strict=True)
    if path == intermediate or not path.is_relative_to(intermediate) or not path.is_dir():
        raise ValueError("--web-root必须指向本产品.intermediate内的独立构建目录")
    server = (path / "server.js").resolve(strict=True)
    if not server.is_file() or not server.is_relative_to(path):
        raise ValueError("独立构建目录必须包含本目录内的server.js")
    return path


def configurations(web_root: Path) -> dict[str, dict]:
    node = executable("node")
    ffmpeg = executable("ffmpeg")
    executable("ffprobe")
    python = PRODUCT_ROOT / "backend" / ".venv" / "bin" / "python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError(f"后端Python环境无法执行：{python}")
    paths = dict.fromkeys([
        str(node.parent), str(ffmpeg.parent), str(python.parent),
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ])
    environment = {
        "PATH": os.pathsep.join(paths),
        "TMPDIR": str(TEMP_DIRECTORY),
        "XDG_CACHE_HOME": str(CACHE_DIRECTORY),
        "TZ": "Asia/Shanghai",
        "OPENFMV_PREVIEW_MANAGED_BY": MANAGER,
    }
    result = {}
    for service in SERVICES:
        result[service.name] = {
            "Label": service.label,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "Umask": 0o077,
            "StandardOutPath": str(RUN_DIRECTORY / f"{service.name}.stdout.log"),
            "StandardErrorPath": str(RUN_DIRECTORY / f"{service.name}.stderr.log"),
            "EnvironmentVariables": dict(environment),
        }
    result["api"].update(
        ProgramArguments=[str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                          "--port", str(API.port), "--no-access-log"],
        WorkingDirectory=str(PRODUCT_ROOT / "backend"),
    )
    result["api"]["EnvironmentVariables"].update(
        OPENFMV_DATA_DIR=str(PRODUCT_ROOT / "data"),
        OPENFMV_TMP_DIR=str(TEMP_DIRECTORY / "media"),
        OPENFMV_FRONTEND_ORIGIN="http://127.0.0.1:3210",
        OPENFMV_WORKER_ENABLED="true",
        OPENFMV_TEXT_TIMEOUT="600",
        PYTHONUNBUFFERED="1",
        PYTHONPYCACHEPREFIX=str(CACHE_DIRECTORY / "python"),
    )
    operator_path = RUN_DIRECTORY / "operator.json"
    if operator_path.exists():
        operator = json.loads(operator_path.read_text(encoding="utf-8"))
        admin_email = operator["textAdminEmail"]
        if not isinstance(admin_email, str) or "@" not in admin_email:
            raise ValueError("本机operator.json需要填写有效的textAdminEmail")
        result["api"]["EnvironmentVariables"]["OPENFMV_TEXT_ADMIN_EMAIL"] = admin_email
    result["web"].update(
        ProgramArguments=[str(node), str(web_root / "server.js")],
        WorkingDirectory=str(web_root),
    )
    result["web"]["EnvironmentVariables"].update(
        HOSTNAME="127.0.0.1", PORT=str(WEB.port), NODE_ENV="production",
    )
    return result


def stored_configuration(service: Service) -> dict:
    if not service.plist_path.is_file():
        raise RuntimeError(f"目标job已加载但缺少本产品配置，停止操作：{service.target}")
    with service.plist_path.open("rb") as source:
        configuration = plistlib.load(source)
    environment = configuration.get("EnvironmentVariables", {})
    if configuration.get("Label") != service.label or environment.get("OPENFMV_PREVIEW_MANAGED_BY") != MANAGER:
        raise RuntimeError(f"目标job的配置归属无法确认，停止操作：{service.target}")
    return configuration


def preflight(desired: dict[str, dict]) -> dict[str, bool]:
    existing = {}
    for service in SERVICES:
        existing[service.name] = loaded(service)
        if existing[service.name]:
            if stored_configuration(service) != desired[service.name]:
                raise RuntimeError(f"{service.target}配置不同；请先运行stop，再使用所需配置start。当前服务保持运行")
        elif port_open(service.port):
            raise RuntimeError(f"端口{service.port}已被非本管理job的进程占用；请核实并手动停止该进程。没有终止任何进程")
    return existing


def wait_healthy(service: Service, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not port_open(service.port):
        if not loaded(service):
            raise RuntimeError(f"目标job未加载：{service.target}；日志目录：{RUN_DIRECTORY}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{service.target}在{timeout:g}秒内未监听{service.port}；请检查{RUN_DIRECTORY}中的日志及launchctl print {service.target}")
        time.sleep(0.2)
    with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
        response = client.get(service.url)
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError(f"{service.url}必须返回200，实际为{response.status_code}")


def start(web_root: Path, timeout: float) -> None:
    desired = configurations(web_directory(web_root))
    preflight(desired)
    for directory in (RUN_DIRECTORY, TEMP_DIRECTORY, CACHE_DIRECTORY, TEMP_DIRECTORY / "media", CACHE_DIRECTORY / "python"):
        directory.mkdir(parents=True, exist_ok=True)
    RUN_DIRECTORY.chmod(0o700)
    with (RUN_DIRECTORY / "preview.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        existing = preflight(desired)
        for service in SERVICES:
            if not existing[service.name]:
                with service.plist_path.open("wb") as destination:
                    plistlib.dump(desired[service.name], destination, fmt=plistlib.FMT_XML, sort_keys=True)
                service.plist_path.chmod(0o600)
                require_success(launchctl("bootstrap", DOMAIN, str(service.plist_path)))
            wait_healthy(service, timeout)
            print(f"{service.label}已加载，正式GET检查200：{service.url}", flush=True)


def stop() -> None:
    for service in reversed(SERVICES):
        if not loaded(service):
            print(f"{service.label}未加载，无需停止", flush=True)
            continue
        stored_configuration(service)
        result = launchctl("bootout", service.target)
        if result.returncode != 113:
            require_success(result)
        deadline = time.monotonic() + 30
        while loaded(service) or port_open(service.port):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{service.label}停止后端口{service.port}仍未释放；请核查进程。没有终止其他进程")
            time.sleep(0.2)
        print(f"{service.label}已停止", flush=True)


def status() -> None:
    results = []
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
        for service in SERVICES:
            is_loaded = loaded(service)
            response_status = None
            if is_loaded:
                stored_configuration(service)
                if port_open(service.port):
                    response_status = client.get(service.url).status_code
            results.append({
                "label": service.label, "target": service.target,
                "loaded": is_loaded, "port": service.port,
                "httpStatus": response_status, "healthy": response_status == 200,
                "configuration": str(service.plist_path),
            })
    print(json.dumps({"services": results, "logs": str(RUN_DIRECTORY)}, ensure_ascii=False, indent=2))
    if not all(result["loaded"] and result["healthy"] for result in results):
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="使用macOS launchd管理本产品3210前端与8310后端预览")
    actions = parser.add_subparsers(dest="action", required=True)
    start_parser = actions.add_parser("start", help="启动正式预览；已有相同配置可重复执行")
    start_parser.add_argument("--web-root", type=Path, required=True, help="本产品.intermediate内已构建的独立Next.js目录")
    start_parser.add_argument("--timeout", type=float, default=45, help="每个服务等待端口的秒数，默认45")
    actions.add_parser("stop", help="仅停止本产品两个固定launchd job")
    actions.add_parser("status", help="只读检查本产品job及正式HTTP响应")
    arguments = parser.parse_args()
    if sys.platform != "darwin" or os.geteuid() == 0:
        raise RuntimeError("请在macOS本人登录会话中运行，不使用sudo")
    if arguments.action == "start":
        if not math.isfinite(arguments.timeout) or arguments.timeout <= 0:
            parser.error("--timeout必须是大于0的有限数值")
        start(arguments.web_root, arguments.timeout)
    elif arguments.action == "stop":
        stop()
    else:
        status()


if __name__ == "__main__":
    main()
