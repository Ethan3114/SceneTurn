import json
import sqlite3
import sys
import uuid
from contextlib import closing
from pathlib import Path

import pytest

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
