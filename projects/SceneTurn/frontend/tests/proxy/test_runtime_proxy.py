import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path

import httpx
import pytest


PRODUCT = Path(__file__).resolve().parents[3]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for_service(process, origin):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        assert process.poll() is None, "隔离服务启动失败，请检查本次目录的私有日志"
        try:
            with httpx.Client(base_url=origin, timeout=1, trust_env=False) as client:
                response = client.get("/api/v1/health")
            return response
        except httpx.TransportError:
            time.sleep(0.1)
    raise AssertionError("隔离服务启动超时")


@pytest.fixture(scope="module")
def runtime_proxy(tmp_path_factory):
    package = Path(os.environ["SCENETURN_PROXY_TEST_PACKAGE"]).resolve()
    node = os.environ.get("SCENETURN_PROXY_TEST_NODE") or shutil.which("node")
    assert node and (package / "server.js").is_file(), "请先准备真实Next standalone产物"
    directory = tmp_path_factory.mktemp("runtime-proxy").resolve()
    assert directory.is_relative_to(PRODUCT / ".intermediate")
    shared_secret = secrets.token_urlsafe(48)
    ports = {key: free_port() for key in ("api_a", "api_b", "front_a", "front_b", "local", "missing", "preview")}
    origins = {key: f"http://127.0.0.1:{port}" for key, port in ports.items()}
    clean_environment = {key: value for key, value in os.environ.items()
        if not key.startswith(("SCENETURN_", "OPENFMV_")) and key not in {"BACKEND_URL", "PROXY_SHARED_SECRET"}}
    clean_environment.update(TMPDIR=str(directory), XDG_CACHE_HOME=str(directory / "cache"), PYTHONDONTWRITEBYTECODE="1")
    children, logs, packages = [], [], {}
    setup_complete = False

    def launch(arguments, cwd, environment, name):
        log = (directory / f"{name}.log").open("wb")
        logs.append(log)
        process = subprocess.Popen(arguments, cwd=cwd, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        children.append(process)
        return process

    try:
        for suffix in ("a", "b"):
            launch([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(ports[f"api_{suffix}"]), "--no-access-log", "--no-proxy-headers"],
                PRODUCT / "backend", {**clean_environment, "SCENETURN_DATA_DIR": str(directory / f"api-{suffix}" / "data"),
                    "SCENETURN_TMP_DIR": str(directory / f"api-{suffix}" / "media"), "SCENETURN_WORKER_ENABLED": "false",
                    "SCENETURN_FRONTEND_ORIGIN": origins[f"front_{suffix}"], "PROXY_SHARED_SECRET": shared_secret}, f"api-{suffix}")
            assert wait_for_service(children[-1], origins[f"api_{suffix}"]).status_code == 200
        original_sha = hashlib.sha256((package / "server.js").read_bytes()).hexdigest()
        for key in ("front_a", "front_b", "local", "missing", "preview"):
            target = directory / key
            target.mkdir()
            subprocess.run(["rsync", "-a", str(package) + "/", str(target) + "/"], check=True, capture_output=True)
            packages[key] = target
            assert hashlib.sha256((target / "server.js").read_bytes()).hexdigest() == original_sha
            environment = {**clean_environment, "HOSTNAME": "127.0.0.1", "PORT": str(ports[key]), "NODE_ENV": "production"}
            if key in {"front_a", "front_b"}:
                suffix = key[-1]
                environment.update(BACKEND_URL=origins[f"api_{suffix}"], PROXY_SHARED_SECRET=shared_secret)
            elif key == "local":
                environment["SCENETURN_API_ORIGIN"] = origins["api_a"]
            elif key == "preview":
                environment.update(BACKEND_URL=origins["api_a"], PROXY_SHARED_SECRET=shared_secret,
                    SCENETURN_PUBLIC_PREVIEW="1", SCENETURN_PREMIERE_PUBLICATION_SLUG="preview-regression")
            else:
                environment["BACKEND_URL"] = origins["api_a"]
            launch([str(node), "server.js"], target, environment, key)
            response = wait_for_service(children[-1], origins[key])
            assert response.status_code == {"preview": 404, "missing": 503}.get(key, 200)
        manifest = {"origins": origins, "pids": [child.pid for child in children], "packageSha256": original_sha,
            "packages": {key: str(value) for key, value in packages.items()}}
        manifest_path = directory / "services.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        manifest_path.chmod(0o600)
        setup_complete = True
        yield origins, directory
        assert hashlib.sha256((package / "server.js").read_bytes()).hexdigest() == original_sha
    finally:
        if not setup_complete or os.environ.get("SCENETURN_PROXY_TEST_KEEP_SERVICES") != "1":
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=15)
        for log in logs:
            log.close()


def register(client):
    credentials = {"email": f"proxy-{uuid.uuid4().hex}@example.com", "password": secrets.token_urlsafe(24)}
    response = client.post("/api/v1/auth/register", json=credentials)
    assert response.status_code == 201
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1 and "HttpOnly" in cookies[0] and "SameSite=strict" in cookies[0]
    assert client.cookies.get("sceneturn_session")
    return credentials


def test_public_preview_exposes_only_landing_legal_and_trial_assets(runtime_proxy):
    origins, _ = runtime_proxy
    messages = {
        "zh-CN": "公开预览提供产品介绍和互动试玩。",
        "en": "The public preview includes the product tour and interactive demo.",
        "ja": "公開プレビューでは、製品紹介とインタラクティブデモをお試しいただけます。",
        "ko": "공개 미리 보기에서는 제품 소개와 인터랙티브 데모를 이용할 수 있습니다.",
    }
    with httpx.Client(base_url=origins["preview"], follow_redirects=True, trust_env=False) as preview:
        assert preview.get("/").status_code == 200
        for locale, message in messages.items():
            homepage = preview.get(f"/{locale}")
            assert homepage.status_code == 200 and message in homepage.text
            for path in ("projects", "works", "settings/models"):
                assert f'href="/{locale}/{path}"' not in homepage.text
            assert "preview-regression" not in homepage.text
            assert preview.get(f"/{locale}/privacy").status_code == 200
            assert preview.get(f"/{locale}/terms").status_code == 200
            blocked = preview.get(f"/{locale}/editor")
            assert blocked.status_code == 404 and message in blocked.text

        for path in ("/sceneturn/tide.jpg", "/sceneturn/signal.jpg", "/sceneturn/port.jpg",
            "/sceneturn/icon.svg", "/sceneturn/home/player.webp"):
            assert preview.head(path).status_code == 200
        for path in ("/works", "/account", "/internal/worker/wake", "/sceneturn/canvas/character-one.png",
            "/_next/image?url=%2Fsceneturn%2Fcanvas%2Fcharacter-one.png&w=384&q=75"):
            assert preview.get(path).status_code == 404

        for method, path in (("GET", "/api/v1/health"), ("POST", "/api/v1/auth/register"),
            ("GET", "/api/v1/play/preview-regression"), ("GET", "/api/local-asset"),
            ("GET", "/api/ai-settings/agents"), ("POST", "/api/ai-settings/agents/example/test")):
            blocked = preview.request(method, path)
            assert blocked.status_code == 404 and blocked.headers["cache-control"] == "no-store"
            assert blocked.json()["error"]["code"] == "PUBLIC_PREVIEW_UNAVAILABLE"
        assert preview.post("/en").status_code == 405

    with httpx.Client(base_url=origins["front_a"], trust_env=False) as authoring:
        assert authoring.get("/api/v1/health").status_code == 200
        assert 'href="/en/projects"' in authoring.get("/en").text


def test_one_production_package_uses_two_runtime_backends(runtime_proxy):
    origins, _ = runtime_proxy
    with httpx.Client(base_url=origins["front_a"], headers={"Origin": origins["front_a"]}, trust_env=False) as first:
        register(first)
        created = first.post("/api/v1/projects", json={"title": "运行时后端A"})
        assert created.status_code == 201
        project_id = created.json()["project"]["id"]
        with httpx.Client(base_url=origins["front_b"], headers={"Origin": origins["front_b"]}, trust_env=False) as second:
            second.cookies.update(first.cookies)
            assert second.get("/api/v1/auth/me").status_code == 401
            second.cookies.clear()
            register(second)
            assert second.get(f"/api/v1/projects/{project_id}").status_code == 404
            assert second.post("/api/v1/projects", json={"title": "运行时后端B"}).status_code == 201
        assert first.get(f"/api/v1/projects/{project_id}").status_code == 200


def test_proxy_upload_range_head_ownership_and_logout(runtime_proxy):
    origins, _ = runtime_proxy
    video = PRODUCT / ".intermediate" / "test-media" / "flower.mp4"
    assert video.is_file(), "需要本产品已验证的真实CC0媒体"
    raw = video.read_bytes()
    with httpx.Client(base_url=origins["front_a"], headers={"Origin": origins["front_a"]}, timeout=30, trust_env=False) as owner:
        credentials = register(owner)
        created = owner.post("/api/v1/projects", json={"title": "代理媒体与会话验证"})
        assert created.status_code == 201
        project_id = created.json()["project"]["id"]
        with video.open("rb") as stream:
            uploaded = owner.post(f"/api/v1/projects/{project_id}/assets", files={"file": (video.name, stream, "video/mp4")})
        assert uploaded.status_code == 201
        asset = uploaded.json()["asset"]
        content = owner.get(asset["url"])
        assert content.status_code == 200 and content.content == raw
        ranged = owner.get(asset["url"], headers={"Range": "bytes=0-15"})
        assert ranged.status_code == 206 and ranged.content == raw[:16]
        assert ranged.headers["content-range"] == f"bytes 0-15/{len(raw)}"
        assert ranged.headers["accept-ranges"] == "bytes"
        if "content-length" in ranged.headers:
            assert ranged.headers["content-length"] == "16"
        else:
            assert ranged.headers["transfer-encoding"] == "chunked"
        head = owner.head(asset["url"])
        with httpx.Client(base_url=origins["api_a"], cookies=owner.cookies, trust_env=False) as upstream:
            direct_head = upstream.head(asset["url"])
        assert head.status_code == direct_head.status_code and head.content == b""
        assert head.headers.get("allow") == direct_head.headers.get("allow")
        with httpx.Client(base_url=origins["front_a"], headers={"Origin": origins["front_a"]}, trust_env=False) as stranger:
            register(stranger)
            assert stranger.get(asset["url"]).status_code == 404
            assert stranger.get(f"/api/v1/projects/{project_id}").status_code == 404
        logged_out = owner.post("/api/v1/auth/logout")
        assert logged_out.status_code == 200 and "Max-Age=0" in logged_out.headers["set-cookie"]
        assert owner.get("/api/v1/auth/me").status_code == 401
        assert owner.post("/api/v1/auth/login", json=credentials).status_code == 200
        assert owner.get(f"/api/v1/projects/{project_id}").status_code == 200


def test_authorization_origin_and_proxy_identity_remain_separate(runtime_proxy):
    origins, _ = runtime_proxy
    with httpx.Client(base_url=origins["front_a"], headers={"Origin": origins["front_a"]}, trust_env=False) as owner:
        register(owner)
        created = owner.post("/api/v1/projects", json={"title": "真实连接授权透传"})
        assert created.status_code == 201
        project_id = created.json()["project"]["id"]
        pairing = owner.post(f"/api/v1/projects/{project_id}/connections/pairings", json={
            "operationId": str(uuid.uuid4()), "tools": ["read_project"], "expiresInSeconds": 3600})
        assert pairing.status_code == 201
        result = pairing.json()
        claimed = owner.post(f"/api/v1/connections/pairings/{result['pairingId']}/claim", json={
            "pairingCode": result["pairingCode"], "clientVersion": "0.168.0"})
        assert claimed.status_code == 200
        connection_token = claimed.json()["token"]
        with httpx.Client(base_url=origins["front_a"], headers={"Origin": origins["front_a"]}, trust_env=False) as bridge:
            assert bridge.post("/api/v1/connections/heartbeat", headers={"Authorization": f"Bearer {connection_token}"}).status_code == 200
            assert bridge.get(f"/api/v1/projects/{project_id}").status_code == 401
            assert bridge.get(f"/api/v1/projects/{project_id}", headers={"x-client-ip": "192.0.2.1", "x-proxy-secret": secrets.token_urlsafe(32),
                "x-envoy-external-address": "192.0.2.1", "x-forwarded-for": "192.0.2.1"}).status_code == 401
        malicious = owner.post("/api/v1/projects", headers={"Origin": "https://untrusted.example"}, json={"title": "被拒绝的来源"})
        assert malicious.status_code == 403 and malicious.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"


def test_local_target_and_missing_deployment_secret(runtime_proxy):
    origins, _ = runtime_proxy
    with httpx.Client(trust_env=False) as client:
        assert client.get(origins["local"] + "/api/v1/health").status_code == 200
        missing = client.get(origins["missing"] + "/api/v1/health")
        assert missing.status_code == 503 and missing.json() == {"error": {"code": "BACKEND_NOT_CONFIGURED", "message": "服务连接尚未配置，请稍后再试"}}
        fixed = client.get(origins["front_a"] + "/api/v1/health", params={"url": origins["front_b"], "host": "untrusted.example"})
        assert fixed.status_code == 200 and "x-proxy-secret" not in fixed.headers
