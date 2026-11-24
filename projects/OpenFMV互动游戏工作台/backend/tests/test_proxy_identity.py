import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager

import httpx
import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.core.config import Settings
from app.core.proxy import resolve_client_ip
from app.core.security import token_hash
from conftest import FRONTEND_ORIGIN, PRODUCT_ROOT, LiveService, stop_process
from test_api import create_project, error_code, registered_client


def incoming(headers=None, peer="127.0.0.1"):
    return Request({"type": "http", "method": "GET", "path": "/api/v1/auth/me", "scheme": "http",
        "headers": [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()],
        "client": (peer, 42000), "server": ("127.0.0.1", 8310), "query_string": b""})


def configured(secret):
    return Settings(_env_file=None, PROXY_SHARED_SECRET=secret)


def test_shared_secret_aliases_are_private_and_reject_short_configuration():
    value = secrets.token_urlsafe(32)
    for key in ("PROXY_SHARED_SECRET", "OPENFMV_PROXY_SHARED_SECRET", "proxy_shared_secret"):
        settings = Settings(_env_file=None, **{key: value})
        assert settings.proxy_shared_secret.get_secret_value() == value
        assert value not in repr(settings) and value not in settings.model_dump_json()
    with pytest.raises(ValidationError):
        configured("short")


@pytest.mark.parametrize("forwarded", ["203.0.113.7", "2001:db8::42", "::1"])
def test_only_matching_shared_secret_accepts_a_single_valid_ip(forwarded):
    value = secrets.token_urlsafe(32)
    request = incoming({"x-proxy-secret": value, "x-client-ip": forwarded})
    assert resolve_client_ip(request, configured(value)) == forwarded
    assert request.client.host == "127.0.0.1"
    assert "openfmv_session" not in request.cookies


@pytest.mark.parametrize("forwarded", ["", "hostname.invalid", "203.0.113.7, 192.0.2.4", "203.0.113.7:443", "fe80::1%lo0", "999.1.1.1"])
def test_invalid_forwarded_addresses_keep_the_tcp_peer(forwarded):
    value = secrets.token_urlsafe(32)
    request = incoming({"x-proxy-secret": value, "x-client-ip": forwarded})
    assert resolve_client_ip(request, configured(value)) == "127.0.0.1"


def test_missing_or_incorrect_secret_ignores_all_forwarding_headers():
    value = secrets.token_urlsafe(32)
    headers = {"x-client-ip": "203.0.113.7", "x-forwarded-for": "203.0.113.8",
        "x-real-ip": "203.0.113.9", "x-envoy-external-address": "203.0.113.10"}
    assert resolve_client_ip(incoming(headers), configured(value)) == "127.0.0.1"
    assert resolve_client_ip(incoming({**headers, "x-proxy-secret": secrets.token_urlsafe(32)}), configured(value)) == "127.0.0.1"
    assert resolve_client_ip(incoming({**headers, "x-proxy-secret": value}), configured(None)) == "127.0.0.1"


@pytest.mark.parametrize("duplicate", ["x-client-ip", "x-proxy-secret"])
def test_duplicate_proxy_headers_keep_the_tcp_peer(duplicate):
    value = secrets.token_urlsafe(32)
    request = incoming({"x-proxy-secret": value, "x-client-ip": "203.0.113.7"})
    request.scope["headers"].append((duplicate.encode(), value.encode() if duplicate == "x-proxy-secret" else b"203.0.113.8"))
    assert resolve_client_ip(request, configured(value)) == "127.0.0.1"


@contextmanager
def running_proxy_backend(directory, secret, trusted_edge=""):
    assert directory.is_relative_to(PRODUCT_ROOT / ".intermediate")
    temp = directory / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {**os.environ, "PROXY_SHARED_SECRET": secret, "OPENFMV_PROXY_TRUSTED_EDGE": trusted_edge,
        "OPENFMV_DATA_DIR": str(directory / "data"), "OPENFMV_TMP_DIR": str(directory / "media"),
        "OPENFMV_WORKER_ENABLED": "false", "OPENFMV_FRONTEND_ORIGIN": FRONTEND_ORIGIN,
        "TMPDIR": str(temp), "XDG_CACHE_HOME": str(directory / "cache"), "PYTHONDONTWRITEBYTECODE": "1"}
    with (directory / "backend.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
             "--no-access-log", "--no-proxy-headers"],
            cwd=PRODUCT_ROOT / "backend", env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 45
            while True:
                assert process.poll() is None, f"隔离代理后端启动失败：{directory / 'backend.log'}"
                with socket.socket() as connection:
                    connection.settimeout(0.2)
                    if connection.connect_ex(("127.0.0.1", port)) == 0:
                        break
                assert time.monotonic() < deadline, "隔离代理后端启动超时"
                time.sleep(0.1)
            service = LiveService(f"http://127.0.0.1:{port}/api/v1", directory / "data")
            with service.client() as client:
                assert client.get("/health").status_code == 200
            yield service
        finally:
            stop_process(process)


@pytest.fixture(scope="module")
def proxy_backend(tmp_path_factory):
    directory = tmp_path_factory.mktemp("proxy-backend").resolve()
    secret = secrets.token_urlsafe(32)
    with running_proxy_backend(directory, secret) as service:
        yield service, secret


def test_real_http_proxy_source_preserves_cookie_identity_origin_and_project_isolation(proxy_backend):
    service, value = proxy_backend
    owner, account = registered_client(service)
    stranger, _ = registered_client(service)
    headers = {"x-proxy-secret": value, "x-client-ip": "203.0.113.7"}
    with closing(owner), closing(stranger), service.client() as anonymous:
        project = create_project(owner)
        project_id = project["project"]["id"]
        assert owner.get("/auth/me", headers=headers).json()["user"]["email"] == account["email"]
        assert owner.get(f"/projects/{project_id}", headers=headers).status_code == 200
        assert error_code(stranger.get(f"/projects/{project_id}", headers=headers), 404) == "PROJECT_NOT_FOUND"
        assert error_code(anonymous.get("/auth/me", headers=headers), 401) == "AUTH_REQUIRED"
        assert error_code(owner.post("/projects", headers={**headers, "Origin": "https://untrusted.invalid"}, json={"title": "未经允许的来源"}), 403) == "ORIGIN_NOT_ALLOWED"
        assert anonymous.post("/connections/heartbeat", headers={**headers, "Authorization": "Bearer not-a-registered-connection"}, json={}).status_code == 401
        assert owner.post("/auth/logout", headers=headers).status_code == 200
        assert error_code(owner.get("/auth/me", headers=headers), 401) == "AUTH_REQUIRED"


def test_login_parallel_attempts_ignore_untrusted_headers_and_persist_across_restart(tmp_path):
    secret = secrets.token_urlsafe(32)
    wrong_secret = secrets.token_urlsafe(32)
    directory = tmp_path.resolve()
    with running_proxy_backend(directory, secret) as service:
        owner, account = registered_client(service)
        start = time.time()

        def fail(index):
            address = f"203.0.113.{index}"
            headers = {"X-Forwarded-For": address, "X-Real-IP": address,
                       "X-Envoy-External-Address": address, "X-Client-IP": address, "X-Proxy-Secret": wrong_secret}
            with service.client() as visitor:
                return visitor.post("/auth/login", headers=headers, json={**account, "password": "incorrect-password"})

        with closing(owner), ThreadPoolExecutor(max_workers=8) as workers:
            responses = list(workers.map(fail, range(1, 9)))
            assert sorted(response.status_code for response in responses) == [401] * 4 + [429] * 4
            for response in responses:
                status = response.status_code
                assert error_code(response, status) == ("INVALID_CREDENTIALS" if status == 401 else "LOGIN_RATE_LIMITED")
                assert "set-cookie" not in response.headers
                if status == 429:
                    assert 1 <= int(response.headers["retry-after"]) <= 900
            assert owner.get("/auth/me").status_code == 200

        with closing(sqlite3.connect(service.data_dir / "openfmv.sqlite3")) as database:
            rows = database.execute("SELECT source_hash, failure_count, expires_at FROM login_failures").fetchall()
        [(source, count, expires_at)] = rows
        assert (source, count) == (token_hash("127.0.0.1"), 5)
        assert start + 900 <= expires_at <= time.time() + 900

    with running_proxy_backend(directory, secret) as restarted, restarted.client() as visitor:
        assert error_code(visitor.post("/auth/login", json=account), 429) == "LOGIN_RATE_LIMITED"
        headers = {"x-client-ip": "2001:db8::1", "x-proxy-secret": secret}
        assert visitor.post("/auth/login", json=account, headers=headers).status_code == 200
        with closing(sqlite3.connect(restarted.data_dir / "openfmv.sqlite3")) as database, database:
            database.execute("UPDATE login_failures SET expires_at = ? WHERE source_hash = ?", (time.time() - 1, source))
        assert visitor.post("/auth/login", json=account).status_code == 200
        with closing(sqlite3.connect(restarted.data_dir / "openfmv.sqlite3")) as database:
            assert database.execute("SELECT COUNT(*) FROM login_failures").fetchone() == (0,)


def test_login_cloudbase_edge_and_authenticated_proxy_share_lock_with_distinct_sources(tmp_path):
    secret = secrets.token_urlsafe(32)
    with running_proxy_backend(tmp_path.resolve(), secret, "cloudbase") as service:
        owner, account = registered_client(service)
        wrong = {**account, "password": "incorrect-password"}
        direct = {"x-envoy-external-address": "203.0.113.36", "x-forwarded-for": "192.0.2.18"}
        proxy = {"x-client-ip": "::ffff:203.0.113.36", "x-proxy-secret": secret, "x-envoy-external-address": "192.0.2.18"}
        with closing(owner), service.client() as visitor:
            for _ in range(4):
                assert error_code(visitor.post("/auth/login", headers=direct, json=wrong), 401) == "INVALID_CREDENTIALS"
            assert visitor.post("/auth/login", headers=proxy, json=account).status_code == 200
            for _ in range(4):
                assert error_code(visitor.post("/auth/login", headers=direct, json=wrong), 401) == "INVALID_CREDENTIALS"
            assert error_code(visitor.post("/auth/login", headers=proxy, json=wrong), 429) == "LOGIN_RATE_LIMITED"
            assert error_code(visitor.post("/auth/login", headers=direct, json=account), 429) == "LOGIN_RATE_LIMITED"
            assert visitor.post("/auth/login", headers={"x-envoy-external-address": "192.0.2.18"},
                                json=account).status_code == 200
