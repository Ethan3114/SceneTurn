import copy
import codecs
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from email.message import Message
from pathlib import Path

import httpx
import pytest


def error_code(response: httpx.Response, status: int) -> str:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert isinstance(body["error"]["code"], str)
    assert isinstance(body["error"]["message"], str)
    assert "Traceback" not in response.text
    return body["error"]["code"]


def registered_client(live_service):
    client = live_service.client()
    account = {"email": f"verify-{uuid.uuid4().hex}@example.com", "password": secrets.token_urlsafe(12)}
    response = client.post("/auth/register", json=account)
    assert response.status_code in (200, 201), response.text
    assert response.json()["user"]["email"] == account["email"]
    assert client.cookies.get("openfmv_session")
    return client, account


def create_project(client, title="真实接口验证项目"):
    response = client.post("/projects", json={"title": title})
    assert response.status_code in (200, 201), response.text
    envelope = response.json()
    assert envelope["project"]["title"] == title
    return envelope


def scene_id(envelope):
    return next(node["id"] for node in envelope["project"]["graphData"]["nodes"] if node["type"] == "scene")


def save_generation_node(client, envelope):
    target_scene = scene_id(envelope)
    node_id = str(uuid.uuid4())
    node = {
        "id": node_id,
        "type": "video",
        "title": "真实视频任务输入",
        "position": {"x": 120, "y": 80},
        "width": 360,
        "height": 260,
        "metadata": {"prompt": "一朵花在自然光下缓慢开放", "content": "真实任务输入"},
    }
    response = client.put(
        f"/projects/{envelope['project']['id']}/canvases/{target_scene}",
        json={
            "expectedRevision": envelope["revision"],
            "operationId": str(uuid.uuid4()),
            "nodes": [node],
            "connections": [],
            "viewport": {"x": 40, "y": 20, "k": 0.8},
        },
    )
    assert response.status_code == 200, response.text
    return target_scene, node_id, response.json()["revision"]


def quote_request(target_scene, node_id, duration=5, resolution="720P"):
    return {
        "sceneId": target_scene,
        "sourceNodeId": node_id,
        "prompt": "一朵花在自然光下缓慢开放",
        "duration": duration,
        "resolution": resolution,
        "operationId": str(uuid.uuid4()),
    }


def test_unauthenticated_requests_have_uniform_errors(live_service):
    with live_service.client() as client:
        error_code(client.get("/auth/me"), 401)
        error_code(client.get("/projects"), 401)
        error_code(client.get("/settings/wan"), 401)


def test_real_registration_login_logout_and_wrong_password(live_service):
    client, account = registered_client(live_service)
    with closing(client):
        me = client.get("/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["email"] == account["email"]
        response = client.post("/auth/logout")
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        error_code(client.get("/auth/me"), 401)
        error_code(client.post("/auth/login", json={**account, "password": "incorrect-input"}), 401)
        login = client.post("/auth/login", json=account)
        assert login.status_code == 200
        cookie = login.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=" in cookie
        assert client.get("/auth/me").status_code == 200


def test_registration_email_is_unique_and_input_is_validated(live_service):
    client, account = registered_client(live_service)
    with closing(client):
        error_code(client.post("/auth/register", json=account), 409)
        error_code(client.post("/auth/register", json={"email": "invalid-email", "password": "short"}), 422)


def test_project_creation_preserves_openfmv_schema(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        project = envelope["project"]
        assert project["schemaVersion"] == 1
        assert {node["type"] for node in project["graphData"]["nodes"]} == {"start", "scene", "end"}
        assert project["metadata"]["serverRevision"] == envelope["revision"]
        assert client.get(f"/projects/{project['id']}").json() == envelope


def test_project_visibility_is_isolated_between_real_accounts(live_service):
    owner, _ = registered_client(live_service)
    stranger, _ = registered_client(live_service)
    with closing(owner), closing(stranger):
        envelope = create_project(owner)
        project_id = envelope["project"]["id"]
        error_code(stranger.get(f"/projects/{project_id}"), 404)
        assert all(item["id"] != project_id for item in stranger.get("/projects").json()["projects"])
        error_code(stranger.get(f"/projects/{project_id}/canvases/{scene_id(envelope)}"), 404)


def test_project_save_and_stale_revision_conflict(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        original = create_project(client)
        project = copy.deepcopy(original["project"])
        project["title"] = "保存后的真实项目"
        project["metadata"]["description"] = "这条内容应通过实际SQLite保存"
        payload = {"expectedRevision": original["revision"], "operationId": str(uuid.uuid4()), "project": project}
        response = client.put(f"/projects/{project['id']}", json=payload)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["revision"] > original["revision"]
        assert saved["project"]["metadata"]["description"] == project["metadata"]["description"]
        stale = {**payload, "operationId": str(uuid.uuid4())}
        error_code(client.put(f"/projects/{project['id']}", json=stale), 409)
        assert client.get(f"/projects/{project['id']}").json()["project"]["title"] == project["title"]


def test_same_operation_returns_existing_saved_result(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        payload = {"title": "真实新场景", "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4())}
        path = f"/projects/{envelope['project']['id']}/scenes"
        first = client.post(path, json=payload)
        assert first.status_code == 201, first.text
        second = client.post(path, json=payload)
        assert second.status_code == 200, second.text
        assert second.json() == first.json()
        scenes = [node for node in second.json()["project"]["graphData"]["nodes"] if node["type"] == "scene"]
        assert len(scenes) == 2


def test_canvas_nodes_and_viewport_survive_real_read(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        target_scene, node_id, revision = save_generation_node(client, envelope)
        result = client.get(f"/projects/{envelope['project']['id']}/canvases/{target_scene}")
        assert result.status_code == 200
        body = result.json()
        assert body["revision"] == revision
        assert body["canvas"]["sceneId"] == target_scene
        assert body["canvas"]["nodes"][0]["id"] == node_id
        assert body["canvas"]["nodes"][0]["metadata"]["prompt"] == "一朵花在自然光下缓慢开放"
        assert body["canvas"]["viewport"] == {"x": 40, "y": 20, "k": 0.8}
        error_code(client.get(f"/projects/{envelope['project']['id']}/canvases/missing-scene"), 404)


def test_quote_records_frozen_input_without_submitting_generation(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        target_scene, node_id, _ = save_generation_node(client, envelope)
        response = client.post(f"/projects/{envelope['project']['id']}/generations/quote", json=quote_request(target_scene, node_id))
        assert response.status_code in (200, 201), response.text
        generation = response.json()["generation"]
        assert generation["status"] == "awaiting_confirmation"
        assert generation["quotedCents"] == 300
        assert generation["reservedCents"] == 0
        assert generation["remoteTaskId"] is None
        assert generation["assetId"] is None
        assert generation["confirmationHash"]


def test_missing_provider_configuration_does_not_submit(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        assert client.get("/settings/wan").json()["configured"] is False
        envelope = create_project(client)
        target_scene, node_id, revision = save_generation_node(client, envelope)
        generation = client.post(
            f"/projects/{envelope['project']['id']}/generations/quote", json=quote_request(target_scene, node_id)
        ).json()["generation"]
        error_code(client.post(f"/generations/{generation['id']}/confirm", json={"confirmationHash": generation["confirmationHash"], "expectedRevision": revision}), 503)
        persisted = client.get(f"/generations/{generation['id']}").json()["generation"]
        assert persisted["status"] == "awaiting_confirmation"
        assert persisted["remoteTaskId"] is None
        assert persisted["reservedCents"] == 0


def test_mismatched_confirmation_hash_is_rejected(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        target_scene, node_id, revision = save_generation_node(client, envelope)
        generation = client.post(
            f"/projects/{envelope['project']['id']}/generations/quote", json=quote_request(target_scene, node_id)
        ).json()["generation"]
        error_code(client.post(f"/generations/{generation['id']}/confirm", json={"confirmationHash": "0" * 64, "expectedRevision": revision}), 409)
        assert client.get(f"/generations/{generation['id']}").json()["generation"]["remoteTaskId"] is None


def test_quote_above_single_generation_budget_cannot_be_confirmed(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        target_scene, node_id, revision = save_generation_node(client, envelope)
        response = client.post(
            f"/projects/{envelope['project']['id']}/generations/quote", json=quote_request(target_scene, node_id, duration=15, resolution="1080P")
        )
        assert response.status_code == 201, response.text
        generation = response.json()["generation"]
        assert generation["quotedCents"] == 1800
        confirmation = client.post(
            f"/generations/{generation['id']}/confirm",
            json={"confirmationHash": generation["confirmationHash"], "expectedRevision": revision},
        )
        assert error_code(confirmation, 422) == "GENERATION_COST_LIMIT"
        persisted = client.get(f"/generations/{generation['id']}").json()["generation"]
        assert persisted["status"] == "awaiting_confirmation"
        assert persisted["reservedCents"] == 0
        assert persisted["remoteTaskId"] is None


def test_generation_and_settings_are_isolated(live_service):
    owner, _ = registered_client(live_service)
    stranger, _ = registered_client(live_service)
    with closing(owner), closing(stranger):
        envelope = create_project(owner)
        target_scene, node_id, _ = save_generation_node(owner, envelope)
        generation = owner.post(
            f"/projects/{envelope['project']['id']}/generations/quote", json=quote_request(target_scene, node_id)
        ).json()["generation"]
        error_code(stranger.get(f"/generations/{generation['id']}"), 404)
        error_code(stranger.post(f"/generations/{generation['id']}/resume", json={}), 404)
        assert "apiKey" not in owner.get("/settings/wan").json()
        assert "apiKey" not in stranger.get("/settings/wan").json()


def test_payment_interfaces_report_actual_unconfigured_state(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        assert client.get("/payments/capabilities").json() == {"configured": False, "provider": None}
        requests = [
            ("POST", "/payments/orders", {"amountCents": 100, "operationId": str(uuid.uuid4())}),
            ("GET", "/payments/orders/absent-order", None),
            ("POST", "/payments/notifications", {"provider": "not-configured", "eventId": str(uuid.uuid4()), "orderId": "absent-order", "payload": {}}),
            ("POST", "/payments/refunds", {"orderId": "absent-order", "amountCents": 100, "operationId": str(uuid.uuid4())}),
        ]
        for method, path, payload in requests:
            assert error_code(client.request(method, path, json=payload), 503) == "PAYMENT_NOT_CONFIGURED"
        error_code(client.post("/payments/orders", json={"amountCents": -1, "operationId": str(uuid.uuid4())}), 422)


def test_real_video_upload_range_and_account_isolation(live_service, sample_video):
    owner, _ = registered_client(live_service)
    stranger, _ = registered_client(live_service)
    with closing(owner), closing(stranger), sample_video.open("rb") as media:
        envelope = create_project(owner)
        response = owner.post(f"/projects/{envelope['project']['id']}/assets", files={"file": ("flower.mp4", media, "video/mp4")})
        assert response.status_code in (200, 201), response.text
        asset = response.json()["asset"]
        content = owner.get(f"/assets/{asset['id']}/content")
        assert content.status_code == 200
        assert content.content == sample_video.read_bytes()
        partial = owner.get(f"/assets/{asset['id']}/content", headers={"Range": "bytes=0-15"})
        assert partial.status_code == 206
        assert partial.content == sample_video.read_bytes()[:16]
        assert partial.headers["content-range"].startswith("bytes 0-15/")
        error_code(stranger.get(f"/assets/{asset['id']}/content"), 404)


@pytest.mark.parametrize("suffix,mime_type", [(".txt", "text/plain"), (".md", "text/markdown")])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "gb18030"])
def test_real_text_upload_preserves_encoding_content_and_original_bytes(live_service, suffix, mime_type, encoding):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        content = "# 真实剧情素材\n\n角色走进车站，抬头看见月亮。\n提示：保留标点、换行和扩展文字𠀀。\n"
        original = content.encode(encoding)
        response = client.post(
            f"/projects/{envelope['project']['id']}/assets",
            files={"file": (f"scene{suffix}", original, f"{mime_type}; charset={encoding}")},
        )
        assert response.status_code == 201, response.text
        asset = response.json()["asset"]
        assert asset["mimeType"] == mime_type
        assert asset["bytes"] == len(original)
        assert asset["sha256"] == hashlib.sha256(original).hexdigest()
        project = client.get(f"/projects/{envelope['project']['id']}").json()["project"]
        saved = next(item for item in project["assets"] if item["id"] == asset["id"])
        assert saved["type"] == "text"
        assert saved["metadata"]["content"] == content
        assert codecs.lookup(saved["metadata"]["encoding"]).name == codecs.lookup(encoding).name
        downloaded = client.get(f"/assets/{asset['id']}/content")
        assert downloaded.status_code == 200
        assert downloaded.content == original
        assert downloaded.text == content
        header = Message()
        header["content-type"] = downloaded.headers["content-type"]
        assert header.get_content_type() == mime_type
        assert codecs.lookup(header.get_content_charset()).name == codecs.lookup(encoding).name
        assert downloaded.headers["x-content-type-options"] == "nosniff"


def test_text_upload_rejects_wrong_mime_without_creating_asset(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        response = client.post(
            f"/projects/{envelope['project']['id']}/assets",
            files={"file": ("notes.txt", "真实文本内容。".encode("utf-8"), "video/mp4")},
        )
        assert error_code(response, 422) == "TEXT_MIME_INVALID"
        assert client.get(f"/projects/{envelope['project']['id']}").json() == envelope


def test_text_upload_rejects_incorrect_declared_encoding(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        response = client.post(
            f"/projects/{envelope['project']['id']}/assets",
            files={"file": ("notes.md", "中文剧情和扩展字符𠀀。".encode("gb18030"), "text/markdown; charset=utf-8")},
        )
        assert error_code(response, 422) == "TEXT_ENCODING_INVALID"
        assert client.get(f"/projects/{envelope['project']['id']}").json() == envelope


def test_real_video_disguised_as_text_is_rejected(live_service, sample_video):
    client, _ = registered_client(live_service)
    with closing(client), sample_video.open("rb") as media:
        envelope = create_project(client)
        response = client.post(
            f"/projects/{envelope['project']['id']}/assets",
            files={"file": ("video.txt", media, "text/plain")},
        )
        assert error_code(response, 422) == "TEXT_CONTENT_INVALID"
        assert client.get(f"/projects/{envelope['project']['id']}").json() == envelope


def test_text_upload_above_ten_mib_is_rejected(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        original = ("a" * (10 * 1024 * 1024 + 1)).encode("ascii")
        response = client.post(
            f"/projects/{envelope['project']['id']}/assets",
            files={"file": ("large.txt", original, "text/plain; charset=ascii")},
        )
        assert error_code(response, 413) == "MEDIA_TOO_LARGE"
        assert client.get(f"/projects/{envelope['project']['id']}").json() == envelope


def test_real_sqlite_stores_password_hash_session_hash_and_project_history(live_service):
    client, account = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        project = copy.deepcopy(envelope["project"])
        project["title"] = "数据库历史实际检查"
        response = client.put(
            f"/projects/{project['id']}",
            json={"expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()), "project": project},
        )
        assert response.status_code == 200
        database_path = live_service.data_dir / "openfmv.sqlite3"
        assert database_path.is_file()
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as database:
            account_row = database.execute("SELECT id, password_hash FROM users WHERE email = ?", (account["email"],)).fetchone()
            assert account_row[1].startswith("$argon2")
            assert account_row[1] != account["password"]
            session_hash = database.execute("SELECT token_hash FROM sessions WHERE user_id = ?", (account_row[0],)).fetchone()[0]
            assert session_hash == hashlib.sha256(client.cookies["openfmv_session"].encode()).hexdigest()
            history = database.execute("SELECT data_json FROM project_versions WHERE project_id = ? ORDER BY revision", (project["id"],)).fetchall()
            assert len(history) >= 2
            assert json.loads(history[0][0])["title"] == envelope["project"]["title"]
            assert json.loads(history[-1][0])["title"] == project["title"]


def test_mutating_requests_reject_unapproved_origin(live_service):
    with live_service.client() as client:
        response = client.post(
            "/auth/register",
            headers={"Origin": "https://unapproved.example"},
            json={"email": f"origin-{uuid.uuid4().hex}@example.com", "password": secrets.token_urlsafe(12)},
        )
        error_code(response, 403)


def test_canvas_viewport_rejects_unsupported_zoom_field(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        response = client.put(
            f"/projects/{envelope['project']['id']}/canvases/{scene_id(envelope)}",
            json={
                "expectedRevision": envelope["revision"],
                "operationId": str(uuid.uuid4()),
                "nodes": [],
                "connections": [],
                "viewport": {"x": 0, "y": 0, "zoom": 1},
            },
        )
        assert error_code(response, 422) == "VALIDATION_ERROR"
        assert client.get(f"/projects/{envelope['project']['id']}").json()["revision"] == envelope["revision"]


def test_canvas_operation_identifier_cannot_be_reused_for_another_scene(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        response = client.post(
            f"/projects/{envelope['project']['id']}/scenes",
            json={"title": "第二幕", "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4())},
        )
        assert response.status_code == 201
        current = response.json()
        scenes = [node["id"] for node in current["project"]["graphData"]["nodes"] if node["type"] == "scene"]
        payload = {
            "expectedRevision": current["revision"],
            "operationId": str(uuid.uuid4()),
            "nodes": [],
            "connections": [],
            "viewport": {"x": 50, "y": 30, "k": 0.75},
        }
        first = client.put(f"/projects/{current['project']['id']}/canvases/{scenes[0]}", json=payload)
        assert first.status_code == 200
        conflicting = client.put(f"/projects/{current['project']['id']}/canvases/{scenes[1]}", json=payload)
        error_code(conflicting, 409)
        untouched = client.get(f"/projects/{current['project']['id']}/canvases/{scenes[1]}").json()["canvas"]
        assert untouched["viewport"] == {"x": 0, "y": 0, "k": 1}


def test_read_only_smoke_rejects_missing_task_without_changing_project(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        product_root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        environment["TMPDIR"] = str(live_service.data_dir.parent / "tmp")
        result = subprocess.run(
            [sys.executable, str(product_root / "scripts" / "smoke.py"), "--task-id", str(uuid.uuid4()), "--data-dir", str(live_service.data_dir)],
            cwd=product_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert "不存在指定的真实任务" in result.stderr
        assert client.get(f"/projects/{envelope['project']['id']}").json() == envelope
