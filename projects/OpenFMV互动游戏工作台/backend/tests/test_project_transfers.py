import copy
import base64
import io
import json
import shutil
import stat
import subprocess
import time
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.services.transfer_worker import claim
from test_api import create_project, error_code, registered_client, scene_id
from test_media_tools import media_service
from conftest import PRODUCT_ROOT


@pytest.fixture
def transfer_service(media_service):
    media_service.stop()
    media_service.start(worker_enabled=True)
    return media_service


def wait_job(client, identifier, statuses=("ready", "awaiting_confirmation", "blocked", "failed"), timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"/transfers/{identifier}")
        assert response.status_code == 200, response.text
        value = response.json()["transfer"]
        if value["status"] in statuses:
            return value
        assert time.monotonic() < deadline, value
        time.sleep(0.1)


def export(client, project_id, revision, kind="backup"):
    response = client.post(f"/projects/{project_id}/exports", json={"expectedRevision": revision, "operationId": str(uuid.uuid4()), "kind": kind})
    assert response.status_code == 201, response.text
    return response.json()["transfer"]


def imported(client, raw, operation=None):
    response = client.post("/transfers/imports", files={"file": ("完整项目.zip", raw, "application/zip")}, data={"operationId": operation or str(uuid.uuid4())})
    assert response.status_code == 201, response.text
    return response.json()["transfer"]


def confirm(client, job):
    response = client.post(f"/transfers/{job['id']}/confirm", json={"confirmationHash": job["confirmationHash"]})
    assert response.status_code == 200, response.text
    return wait_job(client, job["id"], ("ready", "failed"))


def resource(client, url):
    assert url.startswith("/api/v1/")
    return client.get(url.removeprefix("/api/v1"))


def actual_project(client, sample_video):
    project = create_project(client, "完整项目与游戏包验证")
    identifier, scene = project["project"]["id"], scene_id(project)
    with sample_video.open("rb") as source:
        upload = client.post(f"/projects/{identifier}/assets", files={"file": ("真实视频.mp4", source, "video/mp4")})
    assert upload.status_code == 201, upload.text
    envelope = upload.json()
    result = client.post(f"/projects/{identifier}/timeline-assets", json={"sceneId": scene, "assetId": envelope["asset"]["id"],
        "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()), "duration": 1.5, "sourceStart": 0.2})
    assert result.status_code == 200, result.text
    envelope = result.json()
    snapshot = copy.deepcopy(envelope["project"])
    target = next(node for node in snapshot["graphData"]["nodes"] if node["id"] == scene)
    end = next(node["id"] for node in snapshot["graphData"]["nodes"] if node["type"] == "end")
    button = str(uuid.uuid4())
    target["data"]["timeline"]["tracks"][1]["clips"].append({"id": button, "type": "button", "mode": "qte", "label": "完成选择", "enabled": True,
        "startTime": 0.3, "duration": 0.5, "rect": {"x": 0.2, "y": 0.7, "width": 0.6, "height": 0.2}, "qte": {"input": "click", "clickCount": 1, "prompt": "立即选择"}})
    for event in ("click", "timeout"):
        snapshot["graphData"]["edges"].append({"id": str(uuid.uuid4()), "source": scene, "target": end, "sourceHandle": f"button:{button}:{event}"})
    target["data"]["internalPrompt"] = "这份创作依据只保留在完整工程中"
    target["data"]["creativeNotes"] = {"secret": "人物保留的秘密", "token": "古铜代币"}
    saved = client.put(f"/projects/{identifier}", json={"operationId": str(uuid.uuid4()), "expectedRevision": envelope["revision"], "project": snapshot})
    assert saved.status_code == 200, saved.text
    return saved.json(), envelope["project"]["assets"][0]


def test_actual_backup_import_mapping_duplicate_and_restart(transfer_service, sample_video):
    with closing(registered_client(transfer_service)[0]) as client:
        source, asset = actual_project(client, sample_video)
        identifier = source["project"]["id"]
        job = wait_job(client, export(client, identifier, source["revision"])["id"])
        assert job["status"] == "ready", job
        downloaded = resource(client, job["downloadUrl"])
        assert downloaded.status_code == 200
        with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
            assert archive.testzip() is None
            assert {"project.json", "canvases.json", "manifest.json", "history.json"} <= set(archive.namelist())
            assert "project_versions" in json.loads(archive.read("history.json"))
        preview = wait_job(client, imported(client, downloaded.content)["id"])
        assert preview["status"] == "awaiting_confirmation", preview
        assert len(client.get("/projects").json()["projects"]) == 1
        assert preview["projects"][0]["assetCount"] == 1
        transfer_service.stop()
        transfer_service.start(worker_enabled=True)
        assert client.get(f"/transfers/{preview['id']}").json()["transfer"] == preview
        result = confirm(client, preview)
        assert result["status"] == "ready", result
        assert result["projectId"] != identifier
        restored = client.get(f"/projects/{result['projectId']}").json()
        assert [node["id"] for node in restored["project"]["graphData"]["nodes"]] == [node["id"] for node in source["project"]["graphData"]["nodes"]]
        assert restored["project"]["graphData"]["edges"] == source["project"]["graphData"]["edges"]
        original_scene = next(node for node in source["project"]["graphData"]["nodes"] if node["type"] == "scene")
        restored_scene = next(node for node in restored["project"]["graphData"]["nodes"] if node["id"] == original_scene["id"])
        assert restored_scene["data"]["creativeNotes"] == original_scene["data"]["creativeNotes"]
        mapped = next(item["targetId"] for item in result["mappings"] if item["kind"] == "asset" and item["sourceId"] == asset["id"])
        assert client.get(f"/assets/{mapped}/content").content == sample_video.read_bytes()
        repeated = imported(client, downloaded.content)
        assert repeated["id"] == result["id"]
        assert len(client.get("/projects").json()["projects"]) == 2
        repacked = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(downloaded.content)) as original, zipfile.ZipFile(repacked, "w") as output:
            for name in reversed(original.namelist()):
                output.writestr(name, original.read(name))
        another = wait_job(client, imported(client, repacked.getvalue())["id"])
        assert another["status"] == "awaiting_confirmation", another
        adopted = confirm(client, another)
        assert adopted["projectId"] == result["projectId"]
        assert adopted["mappings"] == result["mappings"]
        assert len(client.get("/projects").json()["projects"]) == 2
        transfer_service.stop()
        transfer_service.start(worker_enabled=True)
        assert client.get(f"/transfers/{result['id']}").json()["transfer"] == result
        history = client.get(f"/transfers/{result['id']}/source-history")
        assert history.status_code == 200 and "project_versions" in history.json()["history"]


def test_actual_game_package_frozen_revision_private_and_missing_file(transfer_service, sample_video, sample_input_media):
    with closing(registered_client(transfer_service)[0]) as client, closing(registered_client(transfer_service)[0]) as other:
        source, _ = actual_project(client, sample_video)
        identifier = source["project"]["id"]
        pending = export(client, identifier, source["revision"], "game")
        assert pending["status"] == "awaiting_confirmation", pending
        assert error_code(client.post(f"/transfers/{pending['id']}/confirm", json={"confirmationHash": "0" * 64}), 409) == "TRANSFER_CONFIRMATION_CHANGED"
        result = confirm(client, pending)
        assert result["status"] == "ready", result
        assert error_code(other.get(f"/transfers/{result['id']}"), 404) == "TRANSFER_NOT_FOUND"
        assert resource(other, result["downloadUrl"]).status_code == 404
        raw = resource(client, result["downloadUrl"])
        assert raw.status_code == 200
        with zipfile.ZipFile(io.BytesIO(raw.content)) as archive:
            assert archive.testzip() is None
            assert {name for name in archive.namelist() if not name.startswith("assets/")} == {"index.html", "game.json"}
            game = json.loads(archive.read("game.json"))
            assert game["metadata"]["sourceRevision"] == source["revision"]
            assert "internalPrompt" not in json.dumps(game)
            assert "这份创作依据只保留在完整工程中" not in archive.read("index.html").decode()
            assert "/api/v1/" not in archive.read("game.json").decode()
            assert "OpenFMVRuntimeCore" in archive.read("index.html").decode()
        assert resource(client, result["previewUrl"]).status_code == 200
        assert resource(other, result["previewUrl"]).status_code == 404
        archive_path = next((transfer_service.data_dir / "transfers" / result["id"]).glob("*.zip"))
        media_file = next((archive_path.with_suffix("") / "assets").glob("*.mp4"))
        original_file = media_file.with_suffix(".original")
        media_file.rename(original_file)
        try:
            shutil.copyfile(sample_input_media["image"][0], media_file)
            preview_path = result["previewUrl"].removesuffix("index.html") + f"assets/{media_file.name}"
            assert error_code(resource(client, preview_path), 409) == "GAME_FILE_CHANGED"
        finally:
            original_file.replace(media_file)
        changed = copy.deepcopy(source["project"])
        changed["title"] = "项目后续修改"
        saved = client.put(f"/projects/{identifier}", json={"operationId": str(uuid.uuid4()), "expectedRevision": source["revision"], "project": changed})
        assert saved.status_code == 200
        assert resource(client, result["downloadUrl"]).content == raw.content
        archive = next((transfer_service.data_dir / "transfers" / result["id"]).glob("*.zip"))
        missing = archive.with_suffix(".unavailable")
        archive.rename(missing)
        try:
            assert error_code(resource(client, result["downloadUrl"]), 404) == "TRANSFER_FILE_MISSING"
        finally:
            missing.rename(archive)
        assert resource(client, result["downloadUrl"]).content == raw.content
        archive.rename(missing)
        try:
            restarted = client.post(f"/transfers/{result['id']}/retry")
            assert restarted.status_code == 200, restarted.text
            regenerated = wait_job(client, result["id"])
            assert regenerated["status"] == "ready", regenerated
            with zipfile.ZipFile(io.BytesIO(resource(client, regenerated["downloadUrl"]).content)) as rebuilt:
                assert json.loads(rebuilt.read("game.json")) == game
        finally:
            missing.rename(archive)


def test_import_missing_media_is_reported_before_confirmation(transfer_service, sample_video):
    with closing(registered_client(transfer_service)[0]) as client:
        source, _ = actual_project(client, sample_video)
        backup = wait_job(client, export(client, source["project"]["id"], source["revision"])["id"])
        original = zipfile.ZipFile(io.BytesIO(resource(client, backup["downloadUrl"]).content))
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as output:
            for name in original.namelist():
                if not name.startswith("assets/"):
                    output.writestr(name, original.read(name))
        result = wait_job(client, imported(client, data.getvalue())["id"])
        assert result["status"] == "blocked", result
        assert any(item["code"] == "IMPORT_MEDIA_MISSING" for item in result["issues"])
        assert result["confirmationHash"] is None
        assert len(client.get("/projects").json()["projects"]) == 1


def test_publication_only_exposes_confirmed_fixed_version_and_selected_media(transfer_service, sample_video):
    with (closing(registered_client(transfer_service)[0]) as owner,
          closing(registered_client(transfer_service)[0]) as other,
          transfer_service.client() as guest):
        source, asset = actual_project(owner, sample_video)
        project_id = source["project"]["id"]
        version = confirm(owner, export(owner, project_id, source["revision"], "game"))
        settings = f"/transfers/{version['id']}/publication"
        choice = {"published": True, "title": "公开试玩", "description": "获准发布的固定版本", "trailerAssetId": asset["id"]}
        assert guest.get("/publications").json() == {"publications": []}
        assert other.put(settings, json=choice).status_code == 404
        assert owner.put(settings, json={**choice, "posterAssetId": asset["id"]}).status_code == 404
        published = owner.put(settings, json=choice)
        assert published.status_code == 200, published.text
        current = published.json()["publication"]
        detail = guest.get(f"/publications/{current['slug']}").json()["publication"]
        assert guest.get("/publications").json()["publications"] == [detail]
        assert "trailerAssetId" not in detail
        assert resource(guest, detail["trailerUrl"]).content == sample_video.read_bytes()
        assert guest.get(detail["trailerUrl"].removeprefix("/api/v1"), headers={"Range": "bytes=0-19"}).status_code == 206
        player = resource(guest, detail["playUrl"])
        assert player.status_code == 200
        assert "sandbox allow-scripts" in player.headers["content-security-policy"]
        assert "这份创作依据只保留在完整工程中" not in player.text
        assert resource(guest, version["downloadUrl"]).status_code == 401
        assert guest.get(f"/assets/{asset['id']}/content").status_code == 401
        transfer_service.stop()
        transfer_service.start(worker_enabled=True)
        assert guest.get(f"/publications/{current['slug']}").json()["publication"] == detail
        assert owner.put(settings, json={"published": False}).status_code == 200
        assert resource(guest, detail["playUrl"]).status_code == 404
        assert resource(guest, detail["trailerUrl"]).status_code == 404
        assert guest.get("/publications").json() == {"publications": []}
        assert resource(owner, version["previewUrl"]).status_code == 200


@pytest.mark.parametrize("unsafe", ["parent", "symlink", "manifest"])
def test_invalid_archives_fail_with_persistent_errors(transfer_service, unsafe):
    with closing(registered_client(transfer_service)[0]) as client:
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            if unsafe == "parent":
                archive.writestr("../outside.txt", "路径边界检查")
            elif unsafe == "symlink":
                info = zipfile.ZipInfo("link")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, "../../outside")
            else:
                archive.writestr("project.json", "无法解析的项目清单")
        value = wait_job(client, imported(client, data.getvalue())["id"])
        assert value["status"] == "failed", value
        assert value["errorCode"] is not None
        assert len(client.get("/projects").json()["projects"]) == 0


def test_infinite_canvas_v3_actual_image_and_source_history(transfer_service):
    proof = json.loads((PRODUCT_ROOT / ".intermediate/sensetime-queue-20261115/proof.json").read_bytes())
    record = next(item for item in proof["records"] if item["status"] == "ready")
    image = (PRODUCT_ROOT / "data/media/images" / f"{record['jobId']}.png").read_bytes()
    timestamp, source_id = datetime.now(timezone.utc).isoformat(), str(uuid.uuid4())
    text_id, image_id, key = str(uuid.uuid4()), str(uuid.uuid4()), f"image:{uuid.uuid4()}"
    inline_id, text_key = str(uuid.uuid4()), f"file:{uuid.uuid4()}"
    text = "保留完整文字来源"
    text_path = f"projects/{source_id}/files/script.txt"
    filename = f"projects/{source_id}/files/boundary.png"
    source = {"id": source_id, "title": "原格式真实图片迁移验证", "workflow": "drama", "createdAt": timestamp, "updatedAt": timestamp,
        "nodes": [{"id": text_id, "type": "text", "position": {"x": 0, "y": 0}, "metadata": {"content": text, "storageKey": text_key}},
            {"id": image_id, "type": "image", "position": {"x": 400, "y": 0}, "metadata": {"storageKey": key}},
            {"id": inline_id, "type": "image", "position": {"x": 800, "y": 0}, "metadata": {"content": "data:image/png;base64," + base64.b64encode(image).decode()}}],
        "connections": [{"id": str(uuid.uuid4()), "fromNodeId": text_id, "toNodeId": image_id}], "chatSessions": [], "activeChatId": None,
        "backgroundMode": "dark", "showImageInfo": True, "viewport": {"x": 20, "y": 30, "k": 0.8}}
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("projects.json", json.dumps({"app": "infinite-canvas", "version": 3, "exportedAt": timestamp,
            "projects": [{"project": source, "files": [{"storageKey": key, "path": filename, "mimeType": "image/png", "bytes": len(image)},
                {"storageKey": text_key, "path": text_path, "mimeType": "text/plain", "bytes": len(text.encode())}]}]}))
        archive.writestr(filename, image)
        archive.writestr(text_path, text.encode())
    with closing(registered_client(transfer_service)[0]) as client:
        preview = wait_job(client, imported(client, data.getvalue())["id"])
        assert preview["status"] == "awaiting_confirmation", preview
        assert preview["sourceFormat"] == "infinite-canvas-v3"
        ready = confirm(client, preview)
        assert ready["status"] == "ready", ready
        restored = client.get(f"/projects/{ready['projectId']}").json()
        canvas = restored["canvases"][0]
        assert [node["id"] for node in canvas["nodes"]] == [text_id, image_id, inline_id]
        assert canvas["connections"] == source["connections"]
        assert canvas["viewport"] == source["viewport"]
        restored_image = next(node for node in canvas["nodes"] if node["id"] == image_id)
        assert client.get(restored_image["metadata"]["content"].removeprefix("/api/v1")).content == image
        restored_inline = next(node for node in canvas["nodes"] if node["id"] == inline_id)
        assert client.get(restored_inline["metadata"]["content"].removeprefix("/api/v1")).content == image
        assert next(node for node in canvas["nodes"] if node["id"] == text_id)["metadata"]["content"] == text
        history = client.get(f"/transfers/{ready['id']}/source-history").json()["history"]
        assert history["sourceProjects"][0] == source
        unsupported = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data.getvalue())) as original, zipfile.ZipFile(unsupported, "w") as output:
            manifest = json.loads(original.read("projects.json"))
            manifest["projects"][0]["project"]["nodes"][1]["type"] = "unavailable:asset"
            for name in original.namelist():
                output.writestr(name, json.dumps(manifest).encode() if name == "projects.json" else original.read(name))
        blocked = wait_job(client, imported(client, unsupported.getvalue())["id"])
        assert blocked["status"] == "blocked", blocked
        assert any(item["code"] == "IMPORT_NODE_UNSUPPORTED" for item in blocked["issues"])
        assert len(client.get("/projects").json()["projects"]) == 1


def test_game_confirmation_requires_current_revision(transfer_service, sample_video):
    with closing(registered_client(transfer_service)[0]) as client:
        source, _ = actual_project(client, sample_video)
        identifier = source["project"]["id"]
        job = export(client, identifier, source["revision"], "game")
        changed = copy.deepcopy(source["project"])
        changed["title"] = "确认前修改标题"
        response = client.put(f"/projects/{identifier}", json={"operationId": str(uuid.uuid4()), "expectedRevision": source["revision"], "project": changed})
        assert response.status_code == 200, response.text
        assert error_code(client.post(f"/transfers/{job['id']}/confirm", json={"confirmationHash": job["confirmationHash"]}), 409) == "REVISION_CONFLICT"
        assert client.get(f"/transfers/{job['id']}").json()["transfer"]["status"] == "awaiting_confirmation"


def test_original_openfmv_project_zip_preserves_graph_and_actual_media(transfer_service, sample_video, sample_input_media):
    with closing(registered_client(transfer_service)[0]) as client:
        source, _ = actual_project(client, sample_video)
        directory = transfer_service.data_dir / "native-source"
        directory.mkdir()
        snapshot = copy.deepcopy(source["project"])
        for asset in snapshot["assets"]:
            asset["path"] = str(sample_video.resolve())
            asset["relativePath"] = str(sample_video.resolve())
        for node in snapshot["graphData"]["nodes"]:
            for track in node["data"].get("timeline", {}).get("tracks", []):
                for clip in track["clips"]:
                    if clip["type"] == "video":
                        clip["src"] = str(sample_video.resolve())
                        clip["poster"] = str(sample_input_media["image"][0].resolve())
        input_path = directory / "input.json"
        input_path.write_text(json.dumps(snapshot), encoding="utf-8")
        native = directory / "saved"
        subprocess.run(["node", str(PRODUCT_ROOT / "scripts/export_native_sample.cjs"),
            str(PRODUCT_ROOT / "frontend/electron/exporter.js"), str(input_path), str(native)], check=True, capture_output=True)
        assert (native / "project.openfmv.json").is_file()
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as output:
            for path in native.rglob("*"):
                if path.is_file():
                    output.write(path, path.relative_to(native).as_posix())
        value = wait_job(client, imported(client, data.getvalue())["id"])
        assert value["sourceFormat"] == "openfmv-project"
        assert value["status"] == "awaiting_confirmation", value
        value = confirm(client, value)
        assert value["status"] == "ready", value
        restored = client.get(f"/projects/{value['projectId']}").json()
        assert restored["project"]["graphData"]["edges"] == source["project"]["graphData"]["edges"]
        assert len(restored["project"]["assets"]) == 2
        assert len(restored["canvases"]) == 1
        video = next(asset for asset in restored["project"]["assets"] if asset["type"] == "video")
        assert client.get(f"/assets/{video['id']}/content").content == sample_video.read_bytes()


def test_unwritable_export_directory_stops_then_explicit_retry_recovers(media_service, sample_video):
    with closing(registered_client(media_service)[0]) as client:
        source, _ = actual_project(client, sample_video)
        value = export(client, source["project"]["id"], source["revision"])
        directory = media_service.data_dir / "transfers"
        directory.mkdir(mode=0o500)
        media_service.stop()
        media_service.start(worker_enabled=True)
        try:
            failure = wait_job(client, value["id"], ("failed",))
            assert failure["errorCode"] == "TRANSFER_STORAGE_FAILED"
            assert failure["downloadUrl"] is None
        finally:
            directory.chmod(0o700)
        response = client.post(f"/transfers/{value['id']}/retry")
        assert response.status_code == 200, response.text
        completed = wait_job(client, value["id"])
        assert completed["status"] == "ready", completed
        assert resource(client, completed["downloadUrl"]).status_code == 200


def test_claimed_real_export_recovers_after_actual_service_restart(media_service, sample_video):
    with closing(registered_client(media_service)[0]) as client:
        source, _ = actual_project(client, sample_video)
        value = export(client, source["project"]["id"], source["revision"])
        engine = create_engine(f"sqlite:///{media_service.data_dir / 'openfmv.sqlite3'}")
        try:
            assert claim(sessionmaker(bind=engine, expire_on_commit=False), str(uuid.uuid4())) == value["id"]
        finally:
            engine.dispose()
        processing = client.get(f"/transfers/{value['id']}").json()["transfer"]
        assert processing["status"] == "processing"
        media_service.stop()
        media_service.start(worker_enabled=True)
        result = wait_job(client, value["id"], ("ready", "failed"), timeout=45)
        assert result["id"] == value["id"] and result["status"] == "ready", result
        assert resource(client, result["downloadUrl"]).status_code == 200


def test_button_background_image_mapping_survives_import_and_game_package(transfer_service, sample_video, sample_input_media):
    with closing(registered_client(transfer_service)[0]) as client:
        source, _ = actual_project(client, sample_video)
        project_id = source["project"]["id"]
        image_path, image_mime = sample_input_media["image"]
        with image_path.open("rb") as stream:
            uploaded = client.post(f"/projects/{project_id}/assets", files={"file": (image_path.name, stream, image_mime)})
        assert uploaded.status_code == 201, uploaded.text
        value = uploaded.json()
        snapshot = copy.deepcopy(value["project"])
        scene = next(node for node in snapshot["graphData"]["nodes"] if node["type"] == "scene")
        button = next(clip for track in scene["data"]["timeline"]["tracks"] for clip in track["clips"] if clip["type"] == "button")
        button["style"] = {"backgroundImageAssetId": value["asset"]["id"], "backgroundImageSrc": value["asset"]["url"]}
        saved = client.put(f"/projects/{project_id}", json={"operationId": str(uuid.uuid4()), "expectedRevision": value["revision"], "project": snapshot})
        assert saved.status_code == 200, saved.text
        backup = wait_job(client, export(client, project_id, saved.json()["revision"])["id"])
        preview = wait_job(client, imported(client, resource(client, backup["downloadUrl"]).content)["id"])
        result = confirm(client, preview)
        assert result["status"] == "ready", result
        restored = client.get(f"/projects/{result['projectId']}").json()
        mapped = next(item["targetId"] for item in result["mappings"] if item["kind"] == "asset" and item["sourceId"] == value["asset"]["id"])
        new_scene = next(node for node in restored["project"]["graphData"]["nodes"] if node["id"] == scene["id"])
        new_button = next(clip for track in new_scene["data"]["timeline"]["tracks"] for clip in track["clips"] if clip["id"] == button["id"])
        assert new_button["style"] == {"backgroundImageAssetId": mapped, "backgroundImageSrc": f"/api/v1/assets/{mapped}/content"}
        game = confirm(client, export(client, result["projectId"], restored["revision"], "game"))
        assert game["status"] == "ready", game
        with zipfile.ZipFile(io.BytesIO(resource(client, game["downloadUrl"]).content)) as archive:
            images = [name for name in archive.namelist() if name.endswith(".png")]
            assert len(images) == 1
            assert archive.read(images[0]) == image_path.read_bytes()
