import base64
import hashlib
import io
import json
import struct
import subprocess
import uuid
import wave
from contextlib import closing

from test_api import create_project, registered_client


def test_director_model_keeps_original_gltf_bytes_and_project_access(live_service):
    vertices = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    data = json.dumps({
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "buffers": [{"byteLength": len(vertices), "uri": "data:application/octet-stream;base64," + base64.b64encode(vertices).decode()}],
        "bufferViews": [{"buffer": 0, "byteLength": len(vertices), "target": 34962}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3", "min": [0, 0, 0], "max": [1, 1, 0]}],
    }).encode()
    expected = hashlib.sha256(data).hexdigest()
    with closing(registered_client(live_service)[0]) as owner, closing(registered_client(live_service)[0]) as other:
        project = create_project(owner)["project"]["id"]
        endpoint = f"/projects/{project}/media-tools/director-models"
        for _ in range(2):
            uploaded = owner.post(endpoint, files={"file": ("triangle.gltf", data, "model/gltf+json")})
            assert uploaded.status_code == 201
            assert uploaded.json() == {"id": expected}
        model_url = f"{endpoint}/{expected}"
        assert owner.get(model_url).content == data
        partial = owner.get(model_url, headers={"Range": "bytes=0-15"})
        assert partial.status_code == 206
        assert partial.content == data[:16]
        assert other.get(model_url).status_code == 404


def test_audio_export_captions_tracks_and_ownership(live_service, tmp_path):
    client, _ = registered_client(live_service)
    other, _ = registered_client(live_service)
    project = create_project(client)["project"]["id"]
    base = f"/projects/{project}/media-tools"
    voices = client.get("/media-tools/narrators").json()["voices"]
    if voices:
        cn = next(v for v in voices if v["locale"] == "zh_CN" and v["name"].startswith("Tingting"))
        narration = client.post(base + "/narrate", json={"text": "月色照进山谷。", "voice": cn["name"]})
        assert narration.status_code == 200, narration.text[:120]
        with wave.open(io.BytesIO(narration.content)) as audio:
            assert audio.getframerate() == 24000 and audio.getnframes() > 5000
    movie = tmp_path / "original.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=96x64:r=24:d=1", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(movie)], check=True)
    source = client.post(f"/projects/{project}/assets", files={"file": ("source.mp4", movie.read_bytes(), "video/mp4")}).json()["asset"]["id"]
    clip = {"assetId": source, "start": 0.1, "end": 0.8, "fadeIn": 0.1, "fadeOut": 0.2}
    body = {"videos": [clip, clip], "audio": [{**clip, "offset": 0.2}], "captions": [{"start": 0.1, "end": 0.7, "text": "分镜一"}], "resolution": 480, "fps": 24, "container": "mp4"}
    render = client.post(base + "/render", json=body)
    assert render.status_code == 200, render.text[:180]
    assert other.post(base + "/render", json=body).status_code == 404
    assert client.post(base + "/render", json={**body, "videos": [{**clip,"end":42}]}).status_code == 422
    final = tmp_path / "output.mp4"
    final.write_bytes(render.content)
    meta = json.loads(subprocess.check_output(["ffprobe","-v","error","-show_streams","-show_format","-of","json",str(final)]))
    assert {s["codec_type"] for s in meta["streams"]} == {"audio","video"}
    video = next(s for s in meta["streams"] if s["codec_type"] == "video")
    assert video["height"] == 480 and video["avg_frame_rate"] == "24/1"
    assert abs(float(meta["format"]["duration"]) - 1.4) < 0.1
    extracted = client.post(base + "/render",json={"audio":[clip],"container":"wav"})
    with wave.open(io.BytesIO(extracted.content)) as audio:
        assert abs(audio.getnframes()/audio.getframerate() - 0.7) < 0.01
    retained=client.post(base + "/render",json={"videos":[clip],"keepSourceFormat":True,"container":"webm"})
    assert retained.status_code == 200, retained.text[:200]
    kept=tmp_path / "kept.webm"
    kept.write_bytes(retained.content)
    geometry=json.loads(subprocess.check_output(["ffprobe","-v","error","-select_streams","v:0","-show_streams","-of","json",str(kept)]))["streams"][0]
    assert (geometry["width"],geometry["height"],geometry["avg_frame_rate"])==(96,64,"24/1")
    region = {"x": 0.25, "y": 0.25, "right": 0.75, "bottom": 0.75}
    cropped = client.post(base + "/render", json={"videos": [clip], "crop": region, "keepSourceFormat": True})
    assert cropped.status_code == 200, cropped.text[:180]
    crop_file = tmp_path / "crop.mp4"
    crop_file.write_bytes(cropped.content)
    streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(crop_file)]))["streams"]
    cropped_video = next(stream for stream in streams if stream["codec_type"] == "video")
    assert (cropped_video["width"], cropped_video["height"], cropped_video["avg_frame_rate"]) == (48, 32, "24/1")
    assert any(stream["codec_type"] == "audio" for stream in streams)
    assert client.post(base + "/render", json={"videos": [clip], "crop": {**region, "right": 0.2}, "keepSourceFormat": True}).status_code == 422
    assert client.post(base + "/director-models",files={"file":("wrong.gltf",b"not gltf","model/gltf+json")}).status_code == 422
    assert not list((live_service.data_dir.parent / "tmp").glob("canvas-edit-*"))


def test_local_audio_workflow_requires_configuration_and_is_account_scoped(live_service):
    client,_ = registered_client(live_service)
    other,_ = registered_client(live_service)
    project = create_project(client)["project"]["id"]
    jobs = f"/projects/{project}/media-tools/audio-jobs"
    assert client.get("/media-tools/audio-workflows").json() == {"models":{}}
    request = {"operationId":str(uuid.uuid4()),"mode":"effects","prompt":"夜雨落在屋瓦上"}
    assert client.post(jobs,json=request).json()["error"]["code"] == "AUDIO_WORKFLOW_REQUIRED"
    assert other.get(jobs+"/"+request["operationId"]).status_code == 404
    assert client.put("/media-tools/audio-workflows/effects",json={"name":"无效工作流","workflow":{"1":{"class_type":"ExternalAPI","inputs":{}}}}).json()["error"]["code"] == "AUDIO_WORKFLOW_NODE_INVALID"
