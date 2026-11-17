import asyncio
import hashlib
import io
import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.errors import AppError
from app.services.generation import _acquire_lease, _process_one, _worker_generation
from conftest import FRONTEND_ORIGIN, PRODUCT_ROOT, LiveService, stop_process
from test_api import create_project, error_code, registered_client, scene_id
from test_generation_canvas import connection, node, save_canvas, upload_input


class RestartableMediaService:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        assert self.directory.is_relative_to(PRODUCT_ROOT / ".intermediate")
        self.data_dir = self.directory / "data"
        self.temp_dir = self.directory / "tmp"
        self.temp_dir.mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}/api/v1"
        self.process = None
        self.log = None
        self.starts = 0

    def client(self):
        return LiveService(self.base_url, self.data_dir).client()

    def start(self, worker_enabled=False):
        assert self.process is None
        self.starts += 1
        environment = os.environ.copy()
        environment.update(
            OPENFMV_DATA_DIR=str(self.data_dir), OPENFMV_TMP_DIR=str(self.directory / "media"),
            OPENFMV_WORKER_ENABLED=str(worker_enabled).lower(), OPENFMV_POLL_INTERVAL="1",
            OPENFMV_FRONTEND_ORIGIN=FRONTEND_ORIGIN, TMPDIR=str(self.temp_dir),
            XDG_CACHE_HOME=str(self.directory / "cache"),
            PYTHONPYCACHEPREFIX=str(self.directory / "pycache"),
        )
        log_path = self.directory / f"server-{self.starts}.log"
        self.log = log_path.open("wb")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port), "--no-access-log"],
            cwd=PRODUCT_ROOT / "backend", env=environment, stdin=subprocess.DEVNULL,
            stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 45
        while True:
            assert self.process.poll() is None, f"真实测试服务启动失败：{log_path}"
            with socket.socket() as probe:
                probe.settimeout(0.2)
                ready = probe.connect_ex(("127.0.0.1", self.port)) == 0
            if ready:
                break
            assert time.monotonic() < deadline, f"真实测试服务启动超时：{log_path}"
            time.sleep(0.1)
        with self.client() as client:
            assert client.get("/health").status_code == 200

    def stop(self):
        if self.process is not None:
            stop_process(self.process)
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None


@pytest.fixture
def media_service(tmp_path, request):
    service = RestartableMediaService(tmp_path)
    request.addfinalizer(service.stop)
    service.start()
    return service


@pytest.fixture(scope="module")
def pixel_image(tmp_path_factory):
    directory = tmp_path_factory.mktemp("split-image")
    destination = directory / "pixel-boundaries.png"
    image = Image.new("RGBA", (37, 29))
    image.putdata([((x * 13) % 256, (y * 17) % 256, ((x + y) * 7) % 256, 90 if (x + y) % 5 == 0 else 255) for y in range(29) for x in range(37)])
    image.save(destination, format="PNG")
    return destination, "image/png"


@pytest.fixture(scope="module")
def joining_media(tmp_path_factory):
    directory = tmp_path_factory.mktemp("join-media")
    assert directory.resolve().is_relative_to(PRODUCT_ROOT / ".intermediate")
    media = {}
    for name, color, duration, frequency in (("red", "red", 1.0, 440), ("blue", "blue", 1.4, 880), ("silent", "green", 0.6, None)):
        destination = directory / f"{name}.mp4"
        size = "160x120" if name != "blue" else "96x64"
        command = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=30:d={duration}"]
        if frequency is not None:
            command.extend(["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={duration}", "-c:a", "aac"])
        command.extend(["-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination)])
        subprocess.run(command, check=True, capture_output=True)
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(destination), "-f", "null", "-"], check=True, capture_output=True)
        media[name] = destination, "video/mp4"
    return media


def envelope_after(client, envelope):
    response = client.get(f"/projects/{envelope['project']['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def scene_canvas(envelope):
    return next(canvas for canvas in envelope["canvases"] if canvas["sceneId"] == scene_id(envelope))


def split_setup(client, source):
    envelope, asset = upload_input(client, create_project(client), source)
    original_node = node("image-source", "image", "完整原图", content=asset["url"], storageKey=asset["id"])
    envelope = save_canvas(client, envelope, [original_node])
    return envelope, asset, original_node


def split_request(envelope, **patch):
    return {
        "sceneId": scene_id(envelope), "sourceNodeId": "image-source", "rows": 2, "columns": 3,
        "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()), **patch,
    }


def perform_split(client, envelope, payload):
    return client.post(f"/projects/{envelope['project']['id']}/media-tools/split", json=payload)


def asset_content(client, asset_id):
    response = client.get(f"/assets/{asset_id}/content")
    assert response.status_code == 200, response.text
    return response.content


def assert_split_pixels(client, before, result, asset, original_node, rows, columns):
    original_bytes = asset_content(client, asset["id"])
    original = Image.open(io.BytesIO(original_bytes)).convert("RGBA")
    rebuilt = Image.new("RGBA", original.size)
    registry = {entry["id"]: entry for entry in result["project"]["assets"]}
    canvas = scene_canvas(result)
    assert next(item for item in canvas["nodes"] if item["id"] == original_node["id"]) == original_node
    assert canvas["viewport"] == scene_canvas(before)["viewport"]
    assert result["revision"] == before["revision"] + 1
    assert len(result["pieces"]) == rows * columns
    assert len(canvas["nodes"]) == 1 + rows * columns
    assert len(canvas["connections"]) == rows * columns
    assert len(registry) == len(before["project"]["assets"]) + rows * columns
    assert {(entry["row"], entry["column"]) for entry in result["pieces"]} == {(row, column) for row in range(rows) for column in range(columns)}
    covered_pixels = 0
    boxes = []
    for piece in result["pieces"]:
        entry = registry[piece["assetId"]]
        metadata = entry["metadata"]
        box = metadata["cropBox"]
        image = Image.open(io.BytesIO(asset_content(client, piece["assetId"]))).convert("RGBA")
        expected = original.crop(box)
        assert image.size == expected.size == (metadata["width"], metadata["height"])
        assert image.tobytes() == expected.tobytes()
        assert metadata["sourceAssetId"] == asset["id"]
        assert metadata["sourceNodeId"] == original_node["id"]
        assert (metadata["row"], metadata["column"]) == (piece["row"], piece["column"])
        output = next(item for item in canvas["nodes"] if item["id"] == piece["nodeId"])
        assert output["metadata"]["storageKey"] == piece["assetId"]
        assert output["metadata"]["naturalWidth"] == image.width
        assert output["metadata"]["naturalHeight"] == image.height
        assert any(edge["fromNodeId"] == original_node["id"] and edge["toNodeId"] == piece["nodeId"] for edge in canvas["connections"])
        rebuilt.paste(image, (box[0], box[1]))
        covered_pixels += image.width * image.height
        boxes.append(box)
    assert covered_pixels == original.width * original.height
    assert rebuilt.tobytes() == original.tobytes()
    for index, first in enumerate(boxes):
        for second in boxes[index + 1:]:
            assert min(first[2], second[2]) <= max(first[0], second[0]) or min(first[3], second[3]) <= max(first[1], second[1])
    assert hashlib.sha256(original_bytes).hexdigest() == asset["sha256"]
    return boxes


@pytest.mark.parametrize("rows,columns,lines", [(2, 3, {}), (12, 12, {}), (3, 4, {"horizontalLines": [0.2, 0.65], "verticalLines": [0.05, 0.4, 0.95]})])
def test_split_exact_pixels_without_gaps_or_overlap(live_service, pixel_image, rows, columns, lines):
    client, _ = registered_client(live_service)
    with closing(client):
        before, asset, original = split_setup(client, pixel_image)
        previous_budget = budget(live_service)
        response = perform_split(client, before, split_request(before, rows=rows, columns=columns, **lines))
        assert response.status_code == 201, response.text
        result = response.json()
        boxes = assert_split_pixels(client, before, result, asset, original, rows, columns)
        assert envelope_after(client, before) == {key: value for key, value in result.items() if key != "pieces"}
        assert budget(live_service) == previous_budget
        if lines:
            assert sorted({box[0] for box in boxes} | {box[2] for box in boxes}) == [0, 2, 15, 35, 37]
            assert sorted({box[1] for box in boxes} | {box[3] for box in boxes}) == [0, 6, 19, 29]


@pytest.mark.parametrize("patch,code", [
    ({"rows": 0}, "VALIDATION_ERROR"), ({"rows": 13}, "VALIDATION_ERROR"),
    ({"columns": 13, "rows": 12}, "VALIDATION_ERROR"), ({"rows": True}, "VALIDATION_ERROR"),
    ({"horizontalLines": []}, "SPLIT_LINES_INVALID"), ({"horizontalLines": [0]}, "SPLIT_LINES_INVALID"),
    ({"horizontalLines": [1]}, "SPLIT_LINES_INVALID"), ({"verticalLines": [0.8, 0.2]}, "SPLIT_LINES_INVALID"),
    ({"verticalLines": [0.5, 0.5]}, "SPLIT_LINES_INVALID"), ({"horizontalLines": [0.001]}, "SPLIT_PIECE_EMPTY"),
])
def test_split_rejects_invalid_limits_and_cut_lines_without_changes(live_service, pixel_image, patch, code):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope, asset, _ = split_setup(client, pixel_image)
        assert error_code(perform_split(client, envelope, split_request(envelope, **patch)), 422) == code
        assert envelope_after(client, envelope) == envelope
        assert hashlib.sha256(asset_content(client, asset["id"])).hexdigest() == asset["sha256"]


def test_split_revision_idempotency_and_operation_conflicts(live_service, pixel_image):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope, _, _ = split_setup(client, pixel_image)
        stale = split_request(envelope, expectedRevision=envelope["revision"] - 1)
        assert error_code(perform_split(client, envelope, stale), 409) == "REVISION_CONFLICT"
        payload = split_request(envelope)
        response = perform_split(client, envelope, payload)
        assert response.status_code == 201, response.text
        repeated = perform_split(client, envelope, payload)
        assert repeated.status_code == 201, repeated.text
        assert repeated.json() == response.json()
        assert error_code(perform_split(client, envelope, {**payload, "rows": 3}), 409) == "OPERATION_CONFLICT"
        assert error_code(perform_split(client, envelope, split_request(envelope)), 409) == "REVISION_CONFLICT"
        assert envelope_after(client, envelope)["revision"] == envelope["revision"] + 1
        versions = sql_rows(live_service, "SELECT revision,canvases_json FROM project_versions WHERE project_id=? AND revision>=? ORDER BY revision", (envelope["project"]["id"], envelope["revision"]))
        assert [revision for revision, _ in versions] == [envelope["revision"], envelope["revision"] + 1]
        assert [len(json.loads(canvases)[0]["nodes"]) for _, canvases in versions] == [1, 7]


def test_split_requires_owner_project_and_authenticated_asset_access(live_service, pixel_image):
    owner, _ = registered_client(live_service)
    other, _ = registered_client(live_service)
    with closing(owner), closing(other), live_service.client() as anonymous:
        envelope, asset, _ = split_setup(owner, pixel_image)
        assert error_code(perform_split(other, envelope, split_request(envelope)), 404) == "PROJECT_NOT_FOUND"
        error_code(perform_split(anonymous, envelope, split_request(envelope)), 401)
        result = perform_split(owner, envelope, split_request(envelope)).json()
        for item in result["pieces"]:
            assert error_code(other.get(f"/assets/{item['assetId']}/content"), 404) == "ASSET_NOT_FOUND"
            error_code(anonymous.get(f"/assets/{item['assetId']}/content"), 401)
        other_project = create_project(other)
        other_project = save_canvas(other, other_project, [node("image-source", "image", "他人图片引用", storageKey=asset["id"])])
        assert error_code(perform_split(other, other_project, split_request(other_project)), 422) == "SPLIT_ASSET_INVALID"
        same_owner_project = create_project(owner)
        same_owner_project = save_canvas(owner, same_owner_project, [node("image-source", "image", "其他项目图片", storageKey=asset["id"])])
        assert error_code(perform_split(owner, same_owner_project, split_request(same_owner_project)), 422) == "SPLIT_ASSET_INVALID"


def test_split_data_and_session_survive_real_server_restart(media_service, pixel_image):
    client, _ = registered_client(media_service)
    with closing(client):
        envelope, asset, original = split_setup(client, pixel_image)
        response = perform_split(client, envelope, split_request(envelope))
        assert response.status_code == 201, response.text
        result = response.json()
        before = {item["assetId"]: asset_content(client, item["assetId"]) for item in result["pieces"]}
        media_service.stop()
        media_service.start()
        assert envelope_after(client, envelope) == {key: value for key, value in result.items() if key != "pieces"}
        assert_split_pixels(client, envelope, result, asset, original, 2, 3)
        assert {identifier: asset_content(client, identifier) for identifier in before} == before


def join_setup(client, joining_media, order=("red", "blue")):
    envelope = create_project(client)
    assets = []
    videos = []
    for index, name in enumerate(order):
        envelope, asset = upload_input(client, envelope, joining_media[name])
        assets.append(asset)
        videos.append(node(f"clip-{index}", "video", name, content=asset["url"], storageKey=asset["id"]))
    source = node("join-source", "config", "拼接成片", generationMode="video", dramaTask="join")
    output = node("join-output", "video", "拼接结果")
    envelope = save_canvas(client, envelope, [*videos, source, output], [*(connection(video["id"], source["id"]) for video in videos), connection(source["id"], output["id"])])
    return envelope, assets


def join_request(envelope, **patch):
    return {"sceneId": scene_id(envelope), "sourceNodeId": "join-source", "outputNodeId": "join-output", "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()), **patch}


def perform_join(client, envelope, payload):
    return client.post(f"/projects/{envelope['project']['id']}/generations/join", json=payload)


def zero_cost(generation):
    assert generation["quotedCents"] == generation["reservedCents"] == generation["actualCents"] == 0
    assert generation["remoteTaskId"] is None and generation["remoteRequestId"] is None
    assert generation["input"]["provider"] == "local-ffmpeg"
    assert generation["usage"]["settled"] is True and generation["usage"]["localProcessing"] is True
    assert "localInputs" not in generation["input"]
    assert all("relpath" not in item for item in generation["input"]["inputs"])


def sql_rows(service, query, parameters=()):
    with sqlite3.connect(f"file:{service.data_dir / 'openfmv.sqlite3'}?mode=ro", uri=True) as database:
        return database.execute(query, parameters).fetchall()


def budget(service):
    return sql_rows(service, "SELECT spent_cents,reserved_cents FROM budgets WHERE id='global'")[0]


def wait_ready(client, generation_id):
    deadline = time.monotonic() + 60
    while True:
        response = client.get(f"/generations/{generation_id}")
        assert response.status_code == 200, response.text
        generation = response.json()["generation"]
        assert generation["status"] != "failed", generation
        if generation["status"] == "ready":
            return generation
        assert time.monotonic() < deadline, generation
        time.sleep(0.1)


def ffprobe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], check=True, capture_output=True)
    return json.loads(result.stdout)


def frame_color(path, seconds):
    result = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], check=True, capture_output=True)
    image = Image.open(io.BytesIO(result.stdout)).convert("RGB")
    return image.getpixel((image.width // 2, image.height // 2))


def audio_signal(path, seconds):
    result = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path), "-t", "0.3", "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", "-f", "wav", "-"], check=True, capture_output=True)
    with wave.open(io.BytesIO(result.stdout), "rb") as audio:
        assert audio.getsampwidth() == 2 and audio.getnchannels() == 1
        samples = array("h")
        samples.frombytes(audio.readframes(audio.getnframes()))
        rate = audio.getframerate()
    if sys.byteorder != "little":
        samples.byteswap()
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    crossings = sum(first <= 0 < second for first, second in zip(samples, samples[1:]))
    return rms, crossings * rate / len(samples)


@pytest.mark.parametrize("order", [("red", "blue"), ("blue", "red"), ("red", "silent"), ("silent", "silent")])
def test_join_real_worker_preserves_order_duration_audio_and_restart(media_service, joining_media, tmp_path, order):
    client, _ = registered_client(media_service)
    with closing(client):
        envelope, assets = join_setup(client, joining_media, order)
        previous_budget = budget(media_service)
        payload = join_request(envelope)
        response = perform_join(client, envelope, payload)
        assert response.status_code == 201, response.text
        generation = response.json()["generation"]
        assert generation["status"] == "queued"
        zero_cost(generation)
        assert [item["assetId"] for item in generation["input"]["inputs"]] == [asset["id"] for asset in assets]
        assert budget(media_service) == previous_budget
        media_service.stop()
        media_service.start(worker_enabled=True)
        ready = wait_ready(client, generation["id"])
        zero_cost(ready)
        lease_owner, lease_expires_at = sql_rows(media_service, "SELECT lease_owner,lease_expires_at FROM generations WHERE id=?", (ready["id"],))[0]
        assert lease_owner and lease_expires_at is not None
        output_bytes = asset_content(client, ready["assetId"])
        output = tmp_path / "actual-joined.mp4"
        output.write_bytes(output_bytes)
        probe = ffprobe(output)
        duration = sum(asset["durationMs"] for asset in assets) / 1000
        assert float(probe["format"]["duration"]) == pytest.approx(duration, abs=0.12)
        video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
        assert video["width"] == assets[0]["width"] and video["height"] == assets[0]["height"]
        assert any(stream["codec_type"] == "audio" for stream in probe["streams"])
        times = [0.2, assets[0]["durationMs"] / 1000 + 0.2]
        for name, seconds in zip(order, times, strict=True):
            color = frame_color(output, seconds)
            dominant = {"red": 0, "blue": 2, "silent": 1}[name]
            assert color[dominant] > max(component for index, component in enumerate(color) if index != dominant) + 60
            rms, frequency = audio_signal(output, seconds)
            if name == "silent":
                assert rms < 4
            else:
                assert rms > 500
                assert frequency == pytest.approx({"red": 440, "blue": 880}[name], abs=25)
        assert budget(media_service) == previous_budget
        assert sql_rows(media_service, "SELECT COUNT(*) FROM assets WHERE generation_id=?", (ready["id"],)) == [(1,)]
        assert sql_rows(media_service, "SELECT COUNT(*) FROM provider_settings") == [(0,)]
        adopted = client.post(f"/generations/{ready['id']}/adopt", json={"expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4())})
        assert adopted.status_code == 200, adopted.text
        saved = adopted.json()
        clips = [clip for graph_node in saved["project"]["graphData"]["nodes"] if graph_node["id"] == scene_id(envelope) for track in graph_node["data"]["timeline"]["tracks"] for clip in track["clips"]]
        assert len(clips) == 1 and clips[0]["assetId"] == ready["assetId"]
        assert clips[0]["duration"] == pytest.approx(duration, abs=0.12)
        media_service.stop()
        media_service.start()
        restored = client.get(f"/generations/{ready['id']}")
        assert restored.status_code == 200 and restored.json()["generation"] == ready
        assert envelope_after(client, saved) == saved
        assert asset_content(client, ready["assetId"]) == output_bytes
        for asset in assets:
            assert hashlib.sha256(asset_content(client, asset["id"])).hexdigest() == asset["sha256"]
        assert budget(media_service) == previous_budget


def test_join_parallel_repeated_requests_and_worker_lease_use_one_result(media_service, joining_media):
    client, _ = registered_client(media_service)
    with closing(client):
        envelope, _ = join_setup(client, joining_media)
        payload = join_request(envelope)
        with ThreadPoolExecutor(max_workers=6) as executor:
            responses = list(executor.map(lambda _: perform_join(client, envelope, payload), range(6)))
        assert all(response.status_code == 201 for response in responses), [response.text for response in responses]
        generation_ids = {response.json()["generation"]["id"] for response in responses}
        assert len(generation_ids) == 1
        generation_id = generation_ids.pop()
        assert sql_rows(media_service, "SELECT COUNT(*) FROM generations WHERE project_id=?", (envelope["project"]["id"],)) == [(1,)]
        engine = create_engine(f"sqlite:///{media_service.data_dir / 'openfmv.sqlite3'}")
        with ExitStack() as cleanup:
            cleanup.callback(engine.dispose)
            factory = sessionmaker(engine, expire_on_commit=False)
            worker_a, worker_b = str(uuid.uuid4()), str(uuid.uuid4())
            assert _acquire_lease(factory, worker_a) is True
            assert _acquire_lease(factory, worker_b) is False
            lease = sql_rows(media_service, "SELECT lease_owner,lease_expires_at FROM budgets WHERE id='global'")[0]
            assert lease[0] == worker_a and lease[1] > time.time()
            settings = Settings(data_dir=media_service.data_dir, tmp_dir=media_service.directory / "worker-media", worker_enabled=False)
            asyncio.run(_process_one(settings, factory, worker_a))
            with factory() as session:
                assert _worker_generation(session, generation_id, worker_a).status == "ready"
                with pytest.raises(AppError) as failure:
                    _worker_generation(session, generation_id, worker_b)
                assert failure.value.code == "WORKER_LEASE_LOST"
        ready = client.get(f"/generations/{generation_id}").json()["generation"]
        assert ready["status"] == "ready"
        zero_cost(ready)
        repeated = perform_join(client, envelope, payload)
        assert repeated.status_code == 201 and repeated.json()["generation"] == ready
        assert sql_rows(media_service, "SELECT COUNT(*) FROM assets WHERE generation_id=?", (generation_id,)) == [(1,)]
        assert budget(media_service) == (0, 0)


def test_join_owner_access_revision_and_idempotency_conflicts(live_service, joining_media):
    owner, _ = registered_client(live_service)
    other, _ = registered_client(live_service)
    with closing(owner), closing(other), live_service.client() as anonymous:
        envelope, assets = join_setup(owner, joining_media)
        payload = join_request(envelope)
        assert error_code(perform_join(other, envelope, payload), 404) == "PROJECT_NOT_FOUND"
        error_code(perform_join(anonymous, envelope, payload), 401)
        assert error_code(perform_join(owner, envelope, {**payload, "expectedRevision": envelope["revision"] - 1}), 409) == "PROJECT_REVISION_CONFLICT"
        response = perform_join(owner, envelope, payload)
        assert response.status_code == 201, response.text
        generation = response.json()["generation"]
        assert error_code(other.get(f"/generations/{generation['id']}"), 404) == "GENERATION_NOT_FOUND"
        assert error_code(other.get(f"/assets/{assets[0]['id']}/content"), 404) == "ASSET_NOT_FOUND"
        assert error_code(perform_join(owner, envelope, {**payload, "expectedRevision": envelope["revision"] + 1}), 409) == "OPERATION_CONFLICT"
        assert envelope_after(owner, envelope) == envelope
        zero_cost(generation)


@pytest.mark.parametrize("count,accepted", [(1, False), (100, True), (101, False)])
def test_join_enforces_real_video_input_count(live_service, joining_media, count, accepted):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope, assets = join_setup(client, joining_media)
        videos = [node(f"part-{index}", "video", f"真实片段{index}", storageKey=assets[index % 2]["id"]) for index in range(count)]
        source = node("join-source", "config", "拼接成片", generationMode="video", dramaTask="join")
        envelope = save_canvas(client, envelope, [*videos, source, node("join-output", "video", "独立结果")], [connection(video["id"], source["id"]) for video in videos])
        response = perform_join(client, envelope, join_request(envelope))
        if accepted:
            assert response.status_code == 201, response.text
            generation = response.json()["generation"]
            assert generation["status"] == "queued" and len(generation["input"]["inputs"]) == count
            zero_cost(generation)
        else:
            assert error_code(response, 422) == "VIDEO_JOIN_INPUT_COUNT"
        assert envelope_after(client, envelope) == envelope


@pytest.mark.parametrize("kind", ["other-account", "other-project", "text", "missing-asset"])
def test_join_rejects_unowned_or_non_video_inputs(live_service, joining_media, kind):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope, assets = join_setup(client, joining_media)
        target = envelope
        if kind == "other-account":
            borrower, _ = registered_client(live_service)
        else:
            borrower = client
        with ExitStack() as cleanup:
            if borrower is not client:
                cleanup.callback(borrower.close)
            if kind in {"other-account", "other-project"}:
                target = create_project(borrower)
            inputs = [node("clip-a", "text" if kind == "text" else "video", "来源甲", storageKey=assets[0]["id"], content="文字输入" if kind == "text" else ""), node("clip-b", "video", "来源乙", storageKey=assets[1]["id"])]
            if kind == "missing-asset":
                inputs[0]["metadata"].pop("storageKey")
            target = save_canvas(borrower, target, [*inputs, node("join-source", "config", "拼接成片", generationMode="video", dramaTask="join"), node("join-output", "video", "结果")], [connection(item["id"], "join-source") for item in inputs])
            response = perform_join(borrower, target, join_request(target))
            assert error_code(response, 422) == ("VIDEO_JOIN_INPUT_INVALID" if kind == "text" else "VIDEO_JOIN_INPUT_NOT_READY")
            listed = borrower.get(f"/projects/{target['project']['id']}/generations")
            assert listed.status_code == 200 and listed.json()["generations"] == []
