import base64
import json
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Generation
from app.services.canvas_video_inputs import media_payload
from app.services.wan import video_request
from test_api import create_project, error_code, registered_client, scene_id

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from smoke import ComposedInput, GenerationBinding, validate_canvas_binding


def node(identifier, kind, title, **metadata):
    return {
        "id": identifier, "type": kind, "title": title,
        "position": {"x": 80, "y": 80}, "width": 300, "height": 200,
        "metadata": metadata,
    }


def connection(source, target, identifier=None):
    return {"id": identifier or str(uuid.uuid4()), "fromNodeId": source, "toNodeId": target}


def save_canvas(client, envelope, nodes, connections=None, target_scene=None):
    target_scene = target_scene or scene_id(envelope)
    response = client.put(
        f"/projects/{envelope['project']['id']}/canvases/{target_scene}",
        json={
            "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()),
            "nodes": nodes, "connections": connections or [], "viewport": {"x": 30, "y": 40, "k": 0.8},
        },
    )
    assert response.status_code == 200, response.text
    loaded = client.get(f"/projects/{envelope['project']['id']}")
    assert loaded.status_code == 200, loaded.text
    return loaded.json()


def quote(client, envelope, source, prompt, output=None):
    payload = {
        "sceneId": scene_id(envelope), "sourceNodeId": source, "prompt": prompt,
        "duration": 5, "resolution": "720P", "operationId": str(uuid.uuid4()),
    }
    if output is not None:
        payload["outputNodeId"] = output
    return client.post(f"/projects/{envelope['project']['id']}/generations/quote", json=payload)


def upload_input(client, envelope, source):
    media_file, mime_type = source
    with media_file.open("rb") as media:
        response = client.post(f"/projects/{envelope['project']['id']}/assets", files={"file": (media_file.name, media, mime_type)})
    assert response.status_code == 201, response.text
    loaded = client.get(f"/projects/{envelope['project']['id']}")
    assert loaded.status_code == 200, loaded.text
    return loaded.json(), response.json()["asset"]


def accepted_quote(response, live_service):
    assert response.status_code == 201, response.text
    generation = response.json()["generation"]
    assert generation["status"] == "awaiting_confirmation"
    assert generation["reservedCents"] == 0
    assert generation["remoteTaskId"] is None
    assert generation["assetId"] is None
    assert generation["confirmationHash"]
    assert generation["input"]["inputCompositionVersion"] == 1
    with sqlite3.connect(live_service.data_dir / "openfmv.sqlite3") as database:
        persisted = database.execute(
            "SELECT status,reserved_cents,remote_task_id,asset_id FROM generations WHERE id = ?",
            (generation["id"],),
        ).fetchone()
    assert persisted == ("awaiting_confirmation", 0, None, None)
    return generation


def rejected_quote(client, envelope, response, code):
    assert error_code(response, 422) == code
    listed = client.get(f"/projects/{envelope['project']['id']}/generations")
    assert listed.status_code == 200, listed.text
    assert listed.json()["generations"] == []


def test_plain_source_preserves_original_prompt_and_optional_output(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [node("source", "video", "原视频分镜", prompt="固定镜头，清晨车站。")])
        generation = accepted_quote(quote(client, envelope, "source", "固定镜头，清晨车站。"), live_service)
        assert generation["outputNodeId"] is None
        assert "outputNodeId" not in generation["input"]
        assert generation["input"]["sourcePrompt"] == "固定镜头，清晨车站。"
        assert generation["input"]["prompt"] == "固定镜头，清晨车站。"
        assert generation["input"]["inputs"] == []
        assert generation["input"]["request"]["prompt"] == "固定镜头，清晨车站。"


def test_empty_config_uses_connected_text_without_prompt_fallback(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [
            node("source", "config", "视频生成配置", generationMode="video", composerContent="", prompt="旧提示词内容"),
            node("text-a", "text", "剧情文字", content="清晨的车站，乘客走进大厅。"),
        ], [connection("text-a", "source")])
        generation = accepted_quote(quote(client, envelope, "source", ""), live_service)
        assert generation["input"]["sourcePrompt"] == ""
        assert generation["input"]["prompt"] == "【文本1】\n清晨的车站，乘客走进大厅。"
        assert generation["input"]["inputs"] == [{"nodeId": "text-a", "type": "text", "title": "剧情文字", "text": "清晨的车站，乘客走进大厅。", "label": "文本1"}]


def test_film_guidance_preserves_script_and_source_prompt_in_real_quote(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        source = node("source", "config", "视频节点", generationMode="video", composerContent="镜头推进。", generateAudio="false",
            filmGuidance={"palette": "月光蓝色调", "camera": "Auto", "focal": "35mm"})
        envelope = save_canvas(client, create_project(client), [source, node("text", "text", "镜头剧本", content="角色走入画面。")], [connection("text", "source")])
        generated = accepted_quote(quote(client, envelope, "source", "镜头推进。"), live_service)["input"]
        assert generated["sourcePrompt"] == "镜头推进。"
        assert generated["prompt"] == "镜头推进。\n\n【文本1】\n角色走入画面。\n\n配色：月光蓝色调\n焦距：35mm"
        assert generated["audio"] is False
        assert generated["inputs"][0]["nodeId"] == "text"


def test_invalid_film_guidance_rejected_before_creating_task(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        source = node("source", "config", "视频节点", generationMode="video", composerContent="镜头推进。", filmGuidance={"lens": ["55mm"]})
        envelope = save_canvas(client, create_project(client), [source])
        rejected_quote(client, envelope, quote(client, envelope, "source", "镜头推进。"), "FILM_GUIDANCE_INVALID")


def test_canvas_quotes_fixed_frames_in_saved_order(live_service, sample_input_media):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope, image = upload_input(client, create_project(client), sample_input_media["image"])
        prompt = "固定摄像机，路人缓慢走向画面边缘。"
        nodes = [node("source", "config", "图生视频", generationMode="video", composerContent=prompt),
            node("first", "image", "开头", storageKey=image["id"]), node("last", "image", "结尾", storageKey=image["id"]),
            node("out", "video", "生成结果")]
        edges = [connection("last", "source"), connection("first", "source"), connection("source", "out")]
        envelope = save_canvas(client, envelope, nodes, edges)
        path = f"/projects/{envelope['project']['id']}/generations/quote"
        body = {"sceneId": scene_id(envelope), "sourceNodeId": "source", "outputNodeId": "out", "prompt": prompt,
            "duration": 5, "resolution": "720P", "ratio": "9:16", "operationId": str(uuid.uuid4())}
        first = accepted_quote(client.post(path, json={**body, "firstFrameNodeId": "first"}), live_service)
        assert first["input"]["ratio"] == "9:16" and first["quotedCents"] == 300
        assert first["input"]["canvasMedia"] == [{"nodeId": "first", "type": "first_frame", "assetId": image["id"]}]
        both = accepted_quote(client.post(path, json={**body, "operationId": str(uuid.uuid4()),
            "firstFrameNodeId": "first", "lastFrameNodeId": "last"}), live_service)
        assert [item["type"] for item in both["input"]["canvasMedia"]] == ["first_frame", "last_frame"]
        assert {item["title"] for item in both["input"]["inputs"]} == {"开头", "结尾"}
        assert all("url" not in item for item in both["input"]["inputs"])
        engine = create_engine(f"sqlite+pysqlite:///file:{live_service.data_dir / 'openfmv.sqlite3'}?mode=ro&uri=true")
        with Session(engine) as session:
            generation = session.get(Generation, both["id"])
            media = media_payload(session, generation, Settings(data_dir=live_service.data_dir))
            request = video_request({**generation.input_json, "media": media})
        engine.dispose()
        assert request["parameters"]["ratio"] == "9:16"
        assert [part["type"] for part in request["input"]["media"]] == ["first_frame", "last_frame"]
        assert all(base64.b64decode(part["url"].partition(",")[2], validate=True) == sample_input_media["image"][0].read_bytes() for part in media)
        assert error_code(client.post(path, json={**body, "operationId": str(uuid.uuid4()), "lastFrameNodeId": "last"}), 422) == "VIDEO_FIRST_FRAME_REQUIRED"
        assert error_code(client.post(path, json={**body, "operationId": str(uuid.uuid4()), "firstFrameNodeId": "not-connected"}), 422) == "VIDEO_MEDIA_INPUT_NOT_READY"


def test_video_uses_the_saved_result_of_its_linked_image_generator(live_service, sample_input_media):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope, image = upload_input(client, create_project(client), sample_input_media["image"])
        prompt = "根据首帧缓慢向前推进。"
        nodes = [
            node("image-generator", "config", "图片节点", generationMode="image", outputNodeId="image-result"),
            node("image-result", "image", "图片结果", storageKey=image["id"]),
            node("video-generator", "config", "视频节点", generationMode="video", composerContent=prompt, firstFrameNodeId="image-generator"),
            node("video-result", "video", "视频结果"),
        ]
        links = [
            connection("image-generator", "video-generator"),
            connection("video-generator", "video-result"),
        ]
        envelope = save_canvas(client, envelope, nodes, links)
        path = f"/projects/{envelope['project']['id']}/generations/quote"
        body = {"sceneId": scene_id(envelope), "sourceNodeId": "video-generator", "outputNodeId": "video-result",
            "firstFrameNodeId": "image-generator", "prompt": prompt, "duration": 5, "resolution": "720P"}
        rejected_quote(client, envelope, client.post(path, json={**body, "operationId": str(uuid.uuid4())}), "VIDEO_MEDIA_INPUT_NOT_READY")

        envelope = save_canvas(client, envelope, nodes, [*links, connection("image-generator", "image-result")])
        generation = accepted_quote(client.post(path, json={**body, "operationId": str(uuid.uuid4())}), live_service)
        assert generation["input"]["canvasMedia"] == [{"nodeId": "image-generator", "type": "first_frame", "assetId": image["id"]}]
        assert generation["input"]["inputs"][0]["title"] == "图片节点"


@pytest.mark.parametrize("text_type", ["config", "text"])
def test_generation_cards_keep_previous_finished_inputs_while_new_results_are_pending(live_service, sample_input_media, text_type):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope, image = upload_input(client, create_project(client), sample_input_media["image"])
        envelope, video = upload_input(client, envelope, sample_input_media["video"])
        prompt = "参考@[node:shot]和@[node:look]，遵循@[node:script]。"
        nodes = [
            node("script", text_type, "文本卡片", generationMode="text", outputNodeId="script-pending", composerContent="交给模型的内部说明"),
            node("script-ready", "text", "文本结果", inlineResultOf="script", content="雾散后，列车驶出站台。"),
            node("script-pending", "text", "文本结果", inlineResultOf="script", pendingResult=True),
            node("look", "config", "图片卡片", generationMode="image", outputNodeId="look-ready"),
            node("look-ready", "image", "图片结果", inlineResultOf="look", storageKey=image["id"]),
            node("shot", "config", "视频卡片", generationMode="video", outputNodeId="shot-pending"),
            node("shot-ready", "video", "视频结果", inlineResultOf="shot", storageKey=video["id"]),
            node("shot-pending", "video", "视频结果", inlineResultOf="shot", pendingResult=True),
            node("lookalike", "video", "无关视频", storageKey=video["id"]),
            node("target", "config", "后续镜头", generationMode="video", composerContent=prompt),
        ]
        links = [
            connection("script", "script-ready"), connection("script", "script-pending"), connection("script", "target"),
            connection("look", "look-ready"), connection("look", "target"),
            connection("shot", "shot-ready"), connection("shot", "shot-pending"), connection("shot", "lookalike"), connection("shot", "target"),
        ]
        envelope = save_canvas(client, envelope, nodes, links)
        path = f"/projects/{envelope['project']['id']}/generations/quote"
        body = {"sceneId": scene_id(envelope), "sourceNodeId": "target", "prompt": prompt, "duration": 4, "resolution": "720P",
                "referenceVideoNodeId": "shot", "referenceImageNodeIds": ["look"], "videoInputMode": "edit"}
        generation = accepted_quote(client.post(path, json={**body, "operationId": str(uuid.uuid4())}), live_service)
        assert generation["input"]["canvasMedia"] == [
            {"type": "reference_video", "nodeId": "shot", "assetId": video["id"]},
            {"type": "reference_image", "nodeId": "look", "assetId": image["id"]},
        ]
        assert generation["input"]["inputs"][-1]["nodeId"] == "script"
        assert generation["input"]["prompt"] == "参考【视频1】和【图1】，遵循【文本1】。\n\n【文本1】\n雾散后，列车驶出站台。"

        save_canvas(client, envelope, nodes, [link for link in links if link["toNodeId"] != "shot-ready"])
        # 没有已完成的对应结果时，禁止把普通下游视频错当成卡片产物。
        assert error_code(client.post(path, json={**body, "operationId": str(uuid.uuid4())}), 422) == "GENERATION_REFERENCE_MISSING"


def test_wan_freezes_project_video_and_reference_image_without_using_external_node_url(live_service, sample_input_media):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope, video = upload_input(client, create_project(client), sample_input_media["video"])
        envelope, image = upload_input(client, envelope, sample_input_media["image"])
        prompt = "沿用@[node:reference]中的动作，参考@[node:flower]中的花瓣。"
        nodes = [node("source", "config", "视频生成", generationMode="video", composerContent=prompt),
            node("reference", "video", "导入视频", storageKey=video["id"], content="https://example.com/video.mp4"),
            node("untrusted", "video", "外部链接", content="https://example.com/video.mp4"),
            node("flower", "image", "花瓣", storageKey=image["id"]),
            node("script", "text", "分镜剧本", content="沿着花瓣从右向左运动。")]
        envelope = save_canvas(client, envelope, nodes, [connection(name, "source") for name in ("reference", "untrusted", "flower", "script")])
        path = f"/projects/{envelope['project']['id']}/generations/quote"
        body = {"sceneId": scene_id(envelope), "sourceNodeId": "source", "prompt": prompt, "duration": 4,
            "resolution": "720P", "referenceVideoNodeId": "reference", "referenceImageNodeIds": ["flower"], "videoInputMode": "extend"}
        assert error_code(client.post(path, json={**body, "operationId": str(uuid.uuid4()), "ratio": "16:9"}), 422) == "MODEL_VIDEO_RATIO_ADAPTIVE_REQUIRED"
        assert error_code(client.post(path, json={**body, "operationId": str(uuid.uuid4()), "duration": 26}), 422) == "VIDEO_REFERENCE_DURATION_INVALID"
        quote = accepted_quote(client.post(path, json={**body, "operationId": str(uuid.uuid4())}), live_service)
        assert quote["quotedCents"] == (4 + (video["durationMs"] + 999) // 1000) * 60
        assert quote["input"]["ratio"] == "adaptive"
        assert quote["input"]["prompt"] == "沿用【视频1】中的动作，参考【图1】中的花瓣。\n\n【文本1】\n沿着花瓣从右向左运动。"
        assert quote["input"]["canvasMedia"] == [
            {"nodeId": "reference", "type": "reference_video", "assetId": video["id"]},
            {"nodeId": "flower", "type": "reference_image", "assetId": image["id"]},
        ]
        assert all("url" not in item for item in quote["input"]["inputs"])
        edit = accepted_quote(client.post(path, json={**body, "operationId": str(uuid.uuid4()), "videoInputMode": "edit", "ratio": "16:9"}), live_service)
        assert edit["input"]["canvasMedia"] == quote["input"]["canvasMedia"]
        engine = create_engine(f"sqlite+pysqlite:///file:{live_service.data_dir / 'openfmv.sqlite3'}?mode=ro&uri=true")
        with Session(engine) as session:
            generation = session.get(Generation, quote["id"])
            media = media_payload(session, generation, Settings(data_dir=live_service.data_dir))
        engine.dispose()
        assert media[0]["type"] == "reference_video" and "url" not in media[0]
        assert media[0]["path"].is_relative_to(live_service.data_dir / "media")
        assert media[0]["path"].read_bytes() == sample_input_media["video"][0].read_bytes()
        assert media[1]["type"] == "reference_image" and "path" not in media[1]
        assert base64.b64decode(media[1]["url"].partition(",")[2], validate=True) == sample_input_media["image"][0].read_bytes()

        original_path = media[0]["path"]
        away = original_path.rename(original_path.with_suffix(".moved"))
        try:
            missing = client.post(path, json={**body, "operationId": str(uuid.uuid4())})
            assert error_code(missing, 409) == "VIDEO_REFERENCE_CHANGED"
            assert "重新导入或选择其他视频" in missing.json()["error"]["message"]
        finally:
            away.rename(original_path)

        nodes[0]["metadata"]["composerContent"] = "采用@[node:untrusted]。"
        envelope = save_canvas(client, envelope, nodes, [connection(name, "source") for name in ("reference", "untrusted", "flower", "script")])
        assert error_code(client.post(path, json={**body, "operationId": str(uuid.uuid4()), "prompt": "采用@[node:untrusted]。",
            "referenceVideoNodeId": "untrusted"}), 422) == "VIDEO_MEDIA_INPUT_NOT_READY"


def test_wan_uses_connected_image_references_in_selected_order(live_service, sample_input_media):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope, image = upload_input(client, create_project(client), sample_input_media["image"])
        prompt = "保留@[node:wide]的构图，让@[node:close]中的花进入画面。@[node:script]"
        nodes = [node("source", "config", "参考生视频", generationMode="video", composerContent=prompt),
            node("wide", "image", "构图", storageKey=image["id"]), node("close", "image", "花朵", storageKey=image["id"]),
            node("script", "text", "本段剧本", content="露水顺着花瓣滑下。"),
            node("notes", "text", "导演笔记", content="风声比鸟鸣更近。")]
        envelope = save_canvas(client, envelope, nodes, [connection(key, "source") for key in ("close", "notes", "script", "wide")])
        path = f"/projects/{envelope['project']['id']}/generations/quote"
        body = {"sceneId": scene_id(envelope), "sourceNodeId": "source", "prompt": prompt, "duration": 2,
            "resolution": "720P", "videoInputMode": "reference", "referenceImageNodeIds": ["wide", "close"], "operationId": str(uuid.uuid4())}
        quote = accepted_quote(client.post(path, json=body), live_service)
        assert quote["quotedCents"] == 120
        assert quote["input"]["prompt"] == "保留【图1】的构图，让【图2】中的花进入画面。【文本1】\n\n【文本1】\n露水顺着花瓣滑下。\n\n【文本2】\n风声比鸟鸣更近。"
        assert quote["input"]["canvasMedia"] == [{"nodeId": key, "type": "reference_image", "assetId": image["id"]} for key in ("wide", "close")]
        assert [(item["nodeId"], item["label"]) for item in quote["input"]["inputs"]] == [("wide", "图1"), ("close", "图2"), ("script", "文本1"), ("notes", "文本2")]
        assert all("url" not in item for item in quote["input"]["inputs"])
        assert accepted_quote(client.post(path, json=body), live_service)["id"] == quote["id"]
        invalid_inputs = [({"firstFrameNodeId": "wide"}, "VIDEO_MEDIA_COMBINATION_INVALID"),
            ({"videoInputMode": "edit"}, "VIDEO_REFERENCE_VIDEO_REQUIRED"),
            ({"videoInputMode": "extend"}, "VIDEO_REFERENCE_VIDEO_REQUIRED"),
            ({"referenceImageNodeIds": []}, "VIDEO_REFERENCE_MEDIA_REQUIRED"),
            ({"referenceImageNodeIds": ["wide", "wide"]}, "VIDEO_REFERENCE_IMAGES_INVALID"),
            ({"referenceImageNodeIds": ["wide", "close", "missing"]}, "VIDEO_MEDIA_INPUT_NOT_READY")]
        for settings, failure in invalid_inputs:
            assert error_code(client.post(path, json={**body, **settings, "operationId": str(uuid.uuid4())}), 422) == failure

        engine = create_engine(f"sqlite+pysqlite:///file:{live_service.data_dir / 'openfmv.sqlite3'}?mode=ro&uri=true")
        with Session(engine) as session:
            generation = session.get(Generation, quote["id"])
            media = media_payload(session, generation, Settings(data_dir=live_service.data_dir))
            frozen_input = dict(generation.input_json)
        engine.dispose()
        assert video_request({**frozen_input, "media": media})["input"]["media"] == media
        assert all(base64.b64decode(part["url"].partition(",")[2], validate=True) == sample_input_media["image"][0].read_bytes() for part in media)

        original = {"type": "reference_video", "url": "https://help-static-aliyun-doc.aliyuncs.com/file-manage-files/zh-CN/20260402/ldnfdf/wan2.7-videoedit-style-change.mp4"}
        for mode in ("reference", "edit", "extend"):
            body = video_request({**frozen_input, "media": [original, *media], "videoInputMode": mode, "ratio": "adaptive"})
            assert body["input"]["media"] == [original, *media]
            assert "视频1" in body["input"]["prompt"]
            assert "【图1】" in body["input"]["prompt"]
            assert "露水顺着花瓣滑下。" in body["input"]["prompt"]


def test_explicit_text_video_mode_keeps_unassigned_media_connected(live_service, sample_input_media):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope, image = upload_input(client, create_project(client), sample_input_media["image"])
        nodes = [node("source", "config", "文字生成视频", generationMode="video", composerContent="转换为慢镜头。"),
            node("picture", "image", "参考画面", storageKey=image["id"]),
            node("script", "text", "镜头安排", content="露水顺着花瓣滑下。")]
        edges = [connection("picture", "source"), connection("script", "source")]
        envelope = save_canvas(client, envelope, nodes, edges)
        path = f"/projects/{envelope['project']['id']}/generations/quote"
        body = {"sceneId": scene_id(envelope), "sourceNodeId": "source", "prompt": "转换为慢镜头。", "duration": 2,
            "resolution": "720P", "videoInputMode": "text", "operationId": str(uuid.uuid4())}
        quote = accepted_quote(client.post(path, json=body), live_service)
        assert quote["input"]["prompt"] == "转换为慢镜头。\n\n【文本1】\n露水顺着花瓣滑下。"
        assert [item["nodeId"] for item in quote["input"]["inputs"]] == ["script"]
        assert "canvasMedia" not in quote["input"]
        assert error_code(client.post(path, json={**body, "firstFrameNodeId": "picture", "operationId": str(uuid.uuid4())}), 422) == "VIDEO_MEDIA_COMBINATION_INVALID"
        nodes[0]["metadata"]["composerContent"] = "转换为慢镜头。@[node:picture]"
        save_canvas(client, envelope, nodes, edges)
        response = client.post(path, json={**body, "prompt": "转换为慢镜头。@[node:picture]", "operationId": str(uuid.uuid4())})
        assert error_code(response, 422) == "GENERATION_MEDIA_INPUT_UNSUPPORTED"


def test_connected_text_keeps_connection_array_order_and_deduplicates(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [
            node("text-a", "text", "甲节点", content="甲内容"),
            node("source", "config", "生成配置", generationMode="video", composerContent="指令"),
            node("text-b", "text", "乙节点", content="乙内容"),
        ], [connection("text-b", "source", "z-first"), connection("text-a", "source", "a-second"), connection("text-b", "source", "duplicate")])
        generation = accepted_quote(quote(client, envelope, "source", "指令"), live_service)
        assert generation["input"]["prompt"] == "指令\n\n【文本1】\n乙内容\n\n【文本2】\n甲内容"
        assert generation["input"]["inputs"] == [
            {"nodeId": "text-b", "type": "text", "title": "乙节点", "text": "乙内容", "label": "文本1"},
            {"nodeId": "text-a", "type": "text", "title": "甲节点", "text": "甲内容", "label": "文本2"},
        ]


def test_group_expands_direct_members_in_canvas_order_without_nested_groups(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [
            node("text-b", "text", "乙节点", content="乙内容", groupId="group"),
            node("nested", "group", "嵌套分组", groupId="group"),
            node("text-a", "text", "甲节点", content="甲内容", groupId="group"),
            node("nested-text", "text", "嵌套文字", content="嵌套内容", groupId="nested"),
            node("group", "group", "素材分组"),
            node("source", "config", "生成配置", generationMode="video", composerContent="指令"),
        ], [connection("group", "source"), connection("text-a", "source")])
        generation = accepted_quote(quote(client, envelope, "source", "指令"), live_service)
        assert generation["input"]["prompt"] == "指令\n\n【文本1】\n乙内容\n\n【文本2】\n甲内容"
        assert [item["nodeId"] for item in generation["input"]["inputs"]] == ["text-b", "text-a"]


def test_explicit_references_choose_order_reuse_labels_and_ignore_unselected_media(live_service, sample_input_media):
    client, _ = registered_client(live_service)
    with closing(client):
        prompt = "先看@[node:text-b]，再看@[node:group]，重复@[node:text-b]。"
        envelope, image = upload_input(client, create_project(client), sample_input_media["image"])
        envelope = save_canvas(client, envelope, [
            node("text-c", "text", "丙节点", content="丙内容", groupId="group"),
            node("text-a", "text", "甲节点", content="甲内容", groupId="group"),
            node("text-b", "text", "乙节点", content="乙内容"),
            node("unused-text", "text", "未采用文字", content="未采用内容"),
            node("unused-image", "image", "未采用图片", content=image["url"], storageKey=image["id"]),
            node("group", "group", "素材分组"),
            node("source", "config", "生成配置", generationMode="video", composerContent=prompt),
        ], [connection("unused-image", "source"), connection("unused-text", "source"), connection("group", "source"), connection("text-b", "source")])
        generation = accepted_quote(quote(client, envelope, "source", prompt), live_service)
        assert generation["input"]["sourcePrompt"] == prompt
        assert generation["input"]["request"]["prompt"] == prompt
        assert generation["input"]["prompt"] == "先看【文本1】，再看【文本2】、【文本3】，重复【文本1】。\n\n【文本1】\n乙内容\n\n【文本2】\n丙内容\n\n【文本3】\n甲内容"
        assert [item["nodeId"] for item in generation["input"]["inputs"]] == ["text-b", "text-c", "text-a"]


@pytest.mark.parametrize("reference", ["missing-node", "unconnected"])
def test_explicit_reference_must_exist_and_be_connected(live_service, reference):
    client, _ = registered_client(live_service)
    with closing(client):
        prompt = f"参考@[node:{reference}]"
        envelope = save_canvas(client, create_project(client), [
            node("unconnected", "text", "独立文字", content="真实文字"),
            node("source", "config", "生成配置", generationMode="video", composerContent=prompt),
        ])
        rejected_quote(client, envelope, quote(client, envelope, "source", prompt), "GENERATION_REFERENCE_MISSING")


@pytest.mark.parametrize("kind", ["image", "video", "audio"])
@pytest.mark.parametrize("include_content", [True, False])
def test_adopted_media_input_is_rejected_without_generation(live_service, kind, include_content, sample_input_media):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope, media = upload_input(client, create_project(client), sample_input_media[kind])
        metadata = {"storageKey": media["id"]}
        if include_content:
            metadata["content"] = media["url"]
        envelope = save_canvas(client, envelope, [
            node("media", kind, "媒体输入", **metadata),
            node("source", "config", "生成配置", generationMode="video", composerContent="指令"),
        ], [connection("media", "source")])
        rejected_quote(client, envelope, quote(client, envelope, "source", "指令"), "GENERATION_MEDIA_INPUT_UNSUPPORTED")


def test_explicitly_adopted_media_input_is_rejected(live_service, sample_input_media):
    client, _ = registered_client(live_service)
    with closing(client):
        prompt = "参考@[node:image]"
        envelope, image = upload_input(client, create_project(client), sample_input_media["image"])
        envelope = save_canvas(client, envelope, [
            node("image", "image", "图片输入", content=image["url"], storageKey=image["id"]),
            node("source", "config", "生成配置", generationMode="video", composerContent=prompt),
        ], [connection("image", "source")])
        rejected_quote(client, envelope, quote(client, envelope, "source", prompt), "GENERATION_MEDIA_INPUT_UNSUPPORTED")


def test_group_with_adopted_media_is_rejected(live_service, sample_input_media):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope, image = upload_input(client, create_project(client), sample_input_media["image"])
        envelope = save_canvas(client, envelope, [
            node("text", "text", "文字成员", content="剧情", groupId="group"),
            node("image", "image", "图片成员", groupId="group", content=image["url"], storageKey=image["id"]),
            node("group", "group", "素材分组"),
            node("source", "config", "生成配置", generationMode="video", composerContent="指令"),
        ], [connection("group", "source")])
        rejected_quote(client, envelope, quote(client, envelope, "source", "指令"), "GENERATION_MEDIA_INPUT_UNSUPPORTED")


@pytest.mark.parametrize("metadata", [{"composerContent": ""}, {"composerContent": "  \n  "}])
def test_empty_composed_input_is_rejected(live_service, metadata):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [node("source", "config", "空生成配置", generationMode="video", **metadata)])
        rejected_quote(client, envelope, quote(client, envelope, "source", metadata["composerContent"]), "GENERATION_INPUT_EMPTY")


@pytest.mark.parametrize("text_length,accepted", [(19990, True), (19991, False)])
def test_final_composed_prompt_enforces_twenty_thousand_character_limit(live_service, text_length, accepted):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [
            node("text", "text", "长文字", content="甲" * text_length),
            node("source", "config", "生成配置", generationMode="video", composerContent="指令"),
        ], [connection("text", "source")])
        response = quote(client, envelope, "source", "指令")
        if accepted:
            generation = accepted_quote(response, live_service)
            assert generation["input"]["prompt"] == "指令\n\n【文本1】\n" + "甲" * text_length
            assert len(generation["input"]["prompt"]) == 20000
        else:
            rejected_quote(client, envelope, response, "GENERATION_INPUT_TOO_LONG")


@pytest.mark.parametrize("mode", ["text", "image", "audio"])
def test_config_non_video_generation_mode_is_rejected(live_service, mode):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [node("source", "config", "生成配置", generationMode=mode, composerContent="指令")])
        rejected_quote(client, envelope, quote(client, envelope, "source", "指令"), "GENERATION_MODE_UNSUPPORTED")


@pytest.mark.parametrize("metadata", [{"prompt": "参考@[node:text]"}, {"composerContent": "参考@[node:text]"}])
def test_non_config_sources_keep_reference_syntax_as_original_text(live_service, metadata):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [
            node("text", "text", "文字输入", content="甲内容"),
            node("source", "video", "视频分镜", **metadata),
        ], [connection("text", "source")])
        generation = accepted_quote(quote(client, envelope, "source", "参考@[node:text]"), live_service)
        assert generation["input"]["prompt"] == "参考@[node:text]\n\n【文本1】\n甲内容"


def test_config_prompt_without_composer_does_not_activate_explicit_mode(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [
            node("text-a", "text", "甲输入", content="甲内容"),
            node("text-b", "text", "乙输入", content="乙内容"),
            node("source", "config", "生成配置", generationMode="video", prompt="参考@[node:text-b]"),
        ], [connection("text-a", "source"), connection("text-b", "source")])
        generation = accepted_quote(quote(client, envelope, "source", "参考@[node:text-b]"), live_service)
        assert generation["input"]["prompt"] == "参考@[node:text-b]\n\n【文本1】\n甲内容\n\n【文本2】\n乙内容"


def test_quote_binds_saved_distinct_video_output_node(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [
            node("source", "config", "生成配置", generationMode="video", composerContent="指令"),
            node("output", "video", "视频结果"),
        ], [connection("source", "output")])
        generation = accepted_quote(quote(client, envelope, "source", "指令", "output"), live_service)
        assert generation["outputNodeId"] == "output"
        assert generation["input"]["outputNodeId"] == "output"
        assert generation["input"]["request"]["outputNodeId"] == "output"
        assert generation["input"]["inputs"] == []
        loaded = client.get(f"/generations/{generation['id']}")
        assert loaded.status_code == 200, loaded.text
        assert loaded.json()["generation"] == generation


@pytest.mark.parametrize("output", ["missing", "text", "source"])
def test_output_must_be_existing_different_video(live_service, output):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [
            node("source", "video", "视频输入", prompt="指令"),
            node("text", "text", "文字节点", content="文字内容"),
        ])
        rejected_quote(client, envelope, quote(client, envelope, "source", "指令", output), "GENERATION_OUTPUT_INVALID")


def test_output_in_another_scene_is_rejected(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = create_project(client)
        original_scene = scene_id(envelope)
        created = client.post(f"/projects/{envelope['project']['id']}/scenes", json={
            "title": "第二个场景", "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()),
        })
        assert created.status_code == 201, created.text
        envelope = created.json()
        other_scene = next(item["id"] for item in envelope["project"]["graphData"]["nodes"] if item["type"] == "scene" and item["id"] != original_scene)
        envelope = save_canvas(client, envelope, [node("other-output", "video", "其他场景结果")], target_scene=other_scene)
        envelope = save_canvas(client, envelope, [node("source", "config", "生成配置", generationMode="video", composerContent="指令")], target_scene=original_scene)
        rejected_quote(client, envelope, quote(client, envelope, "source", "指令", "other-output"), "GENERATION_OUTPUT_INVALID")


def test_source_uses_first_connected_config_inputs_before_its_direct_inputs(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [
            node("source", "text", "来源文字", content="源指令"),
            node("direct", "text", "直接入边文字", content="直接入边内容"),
            node("selected", "text", "首个配置文字", content="首个配置内容"),
            node("unselected", "text", "其他配置文字", content="其他配置内容"),
            node("config-a", "config", "其他生成配置", generationMode="video"),
            node("config-b", "config", "首个生成配置", generationMode="video"),
        ], [
            connection("source", "config-b", "z-first"), connection("source", "config-a", "a-second"),
            connection("direct", "source"), connection("selected", "config-b"), connection("unselected", "config-a"),
        ])
        generation = accepted_quote(quote(client, envelope, "source", "源指令"), live_service)
        assert generation["input"]["prompt"] == "源指令\n\n【文本1】\n首个配置内容"
        assert [item["nodeId"] for item in generation["input"]["inputs"]] == ["selected"]


def test_source_uses_direct_inputs_when_connected_config_has_no_other_resources(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [
            node("source", "text", "来源文字", content="源指令"),
            node("direct", "text", "直接文字", content="直接内容"),
            node("config", "config", "生成配置", generationMode="video"),
        ], [connection("source", "config"), connection("direct", "source")])
        generation = accepted_quote(quote(client, envelope, "source", "源指令"), live_service)
        assert generation["input"]["prompt"] == "源指令\n\n【文本1】\n直接内容"
        assert [item["nodeId"] for item in generation["input"]["inputs"]] == ["direct"]


def test_explicit_group_child_requires_its_own_top_level_connection(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        prompt = "参考@[node:child]"
        envelope = save_canvas(client, create_project(client), [
            node("child", "text", "分组文字", content="剧情", groupId="group"),
            node("group", "group", "素材分组"),
            node("source", "config", "生成配置", generationMode="video", composerContent=prompt),
        ], [connection("group", "source")])
        rejected_quote(client, envelope, quote(client, envelope, "source", prompt), "GENERATION_REFERENCE_MISSING")


def test_optional_null_output_does_not_duplicate_or_change_quote(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        envelope = save_canvas(client, create_project(client), [node("source", "video", "视频输入", prompt="指令")])
        payload = {
            "sceneId": scene_id(envelope), "sourceNodeId": "source", "prompt": "指令", "duration": 5,
            "resolution": "720P", "operationId": str(uuid.uuid4()),
        }
        endpoint = f"/projects/{envelope['project']['id']}/generations/quote"
        original = accepted_quote(client.post(endpoint, json=payload), live_service)
        repeated = client.post(endpoint, json={**payload, "outputNodeId": None})
        assert repeated.status_code in (200, 201), repeated.text
        assert repeated.json()["generation"] == original
        records = client.get(f"/projects/{envelope['project']['id']}/generations").json()["generations"]
        assert len(records) == 1


def test_smoke_binding_uses_actual_quote_history_when_current_text_changes(live_service):
    client, _ = registered_client(live_service)
    with closing(client):
        nodes = [
            node("text", "text", "文字输入", content="报价时的真实文字"),
            node("source", "config", "生成配置", generationMode="video", composerContent="参考@[node:text]"),
            node("output", "video", "视频结果"),
        ]
        links = [connection("text", "source"), connection("source", "output")]
        envelope = save_canvas(client, create_project(client), nodes, links)
        generation = accepted_quote(quote(client, envelope, "source", "参考@[node:text]", "output"), live_service)
        binding = GenerationBinding.model_validate(generation)
        composed = ComposedInput.model_validate(generation["input"])
        assert composed.inputs[0].text == "报价时的真实文字"
        nodes[0]["metadata"]["content"] = "生成期间继续编辑后的真实文字"
        changed = save_canvas(client, envelope, nodes, links)
        assert changed["revision"] > generation["projectRevision"]
        with sqlite3.connect(live_service.data_dir / "openfmv.sqlite3") as database:
            row = database.execute(
                "SELECT canvases_json FROM project_versions WHERE project_id = ? AND revision = ?",
                (generation["projectId"], generation["projectRevision"]),
            ).fetchone()
        assert row is not None
        history = json.loads(row[0])
        validate_canvas_binding(history, binding)
        with pytest.raises(ValueError, match="真实历史节点"):
            validate_canvas_binding(changed["canvases"], binding)
        persisted = client.get(f"/generations/{generation['id']}").json()["generation"]
        assert persisted == generation
