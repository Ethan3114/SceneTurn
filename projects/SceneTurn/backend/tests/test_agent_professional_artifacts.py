import copy
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent_models import AgentRun, AgentStep
from app.agent_schemas import RunLimits
from app.core.config import Settings
from app.core.errors import AppError
from app.creative_artifacts import ARTIFACT_CONTRACT_VERSION, ARTIFACT_MODELS, artifact_arguments_schema, image_prompt_items, render_artifact, validate_artifact
from app.services.projects import payload_digest
from app.services.agent import _role_messages, _tool_observation, require_step_delivery
from app.models import Canvas, Project
from app.services.context import source_scope
from app.tools.project_tools import artifact_is_current
from app.tools.project_tools import tool_catalog
from test_agent_runtime import bridge_client, claim_run, create_run, tool
from test_api import create_project, error_code, registered_client, scene_id
from test_generation_canvas import node, save_canvas
from test_context_memory import branch_project


# 以下内容是人工填写的应用输入，用于实际 HTTP 保存和权限验证。
IMAGE = {
    "subject": "林岚是一名三十岁左右的女性修表师，黑色齐肩短发，深灰工作服，胸前佩戴黄铜工牌，右手持有银色怀表。",
    "action": "林岚低头注视打开的怀表，右手托住表壳，左手停在表冠旁，眉头轻皱，身体微微前倾靠近工作台。",
    "scene": "深夜旧钟表店，木质工作台位于画面前景，左侧窗外是细雨街道，后景有整齐的钟表柜和半开的木门。",
    "composition": "采用平视中近景，镜头位于工作台对面，林岚占画面右侧三分之二，怀表位于视觉中心，左侧保留窗景。",
    "style": "写实电影概念美术，克制的材质纹理，人物肤质自然，旧木和金属表面保留细微使用痕迹。",
    "lighting": "左侧窗户透入冷蓝色夜光，右上方台灯提供柔和暖黄主光，面部明暗层次清晰，怀表边缘出现细小反光。",
    "details": "银色表壳内侧有一道斜向划痕，表链垂落到右手手腕，工牌保持黄铜材质，黑色短发与深灰工作服保持一致。",
    "negativePrompt": ["避免出现六根手指、额外手臂或扭曲的手腕", "避免将银色怀表替换为金色手表或电子设备", "避免出现任何标题、字幕、商标或水印文字"],
    "outputSpec": {"purpose": "角色中近景参考图", "aspectRatio": "16:9", "width": 1024, "height": 576, "imageCount": 1, "fileFormat": "png", "background": "保留完整钟表店环境", "textPolicy": "禁止标题、字幕、商标和水印文字", "deliveryNotes": "人物与怀表同时清晰可见"},
}


def manual_artifact(role, project_id):
    source = {"kind": "project", "referenceId": project_id, "description": "当前项目的钟表店互动故事制作资料"}
    base = {"role": role, "title": "林岚与银色怀表制作资料", "sources": [source], "assumptions": []}
    asset = {"key": "watch", "kind": "video", "availability": "to_generate", "assetId": None, "source": None, "description": "五秒打开怀表的中近景，媒体制作需要本人确认"}
    bodies = {
        "director": {"productionBrief": {"objective": "通过怀表异常引导玩家调查", "audience": "喜欢悬疑叙事的玩家", "storyScope": "发现怀表并决定是否进入密室", "deliveryFormat": "横屏互动短片", "constraints": ["收费媒体需本人确认"], "completionCriteria": ["人物与怀表保持一致"]}, "visualBible": {"visualConcept": "克制的冷暖光悬疑氛围", "characterRules": ["深灰工服与黑色短发"], "environmentRules": ["窗户在工作台左侧"], "compositionRules": ["怀表位于视觉中心"], "lightingPalette": "冷蓝窗光与暖黄台灯", "prohibitedChanges": ["银色怀表不更换外形"]}, "assignments": [{"key": "script", "role": "writer", "inputRequirements": ["读取已确认设定"], "dependsOn": [], "deliverables": ["完整分场剧本与结局"], "acceptance": ["人物与道具连续"]}], "productionOrder": ["剧本、角色场景、分镜图片、视频"], "approvalGates": ["提交媒体前确认费用"], "acceptanceChecklist": ["交付来源与内容可核查"]},
        "writer": {"premise": "修表师发现逆向行走的怀表", "plot": "林岚打开怀表，听到后门钟声，选择进入密室后找到师父的信。", "characters": [{"key": "lin", "name": "林岚", "identity": "旧钟表店修表师", "motivation": "追查师父失踪留下的线索", "arc": "从犹豫转为主动调查", "visualAnchors": ["黑色短发与深灰工作服"]}], "scenes": [{"key": "shop", "heading": "内景，钟表店，深夜", "dramaticGoal": "建立怀表与后门的联系", "characters": ["lin"], "action": "林岚打开怀表，秒针逆转，后门响起敲击。她抬头等待玩家选择。", "dialogueDirection": "压低声音，留出雨声停顿", "dialogues": [{"speaker": "lin", "line": "师父留下了什么？", "performance": "气声，望向后门"}], "endingState": "怀表打开，后门透光"}], "branchingDesign": "玩家进入后门取得信件", "branches": [{"key": "enter", "fromScene": "shop", "trigger": "选择进入后门", "destination": "letter", "consequence": "取得师父的信"}], "endings": [{"key": "letter", "condition": "进入后门", "resolution": "林岚在暗室钟架下找到信件，怀表恢复正向行走。", "finalState": "线索已取得，怀表恢复"}], "continuityRules": ["保留银色表壳与划痕"], "productionHandoff": ["准备角色与怀表参考图"]},
        "character_designer": {"designDirection": "用职业服装与持表动作建立人物身份", "characters": [{"key": "lin", "name": "林岚", "identity": "三十岁女性修表师，谨慎专注", "identityAnchors": ["黑色齐肩短发与银色怀表"], "costumeMaterials": "深灰棉布工作服，黄铜工牌，袖口磨损", "expressionsMovement": "看表时轻微皱眉，左手靠近表冠", "continuityRules": ["工牌与发型保持一致"], "imagePrompts": [IMAGE]}], "referenceRequirements": [], "acceptanceChecklist": ["手指、表壳、工服与身份锚点一致"]},
        "scene_designer": {"environmentDirection": "旧钟表店承载工作空间与悬疑线索", "scenes": [{"key": "shop", "name": "深夜钟表店", "geography": "入口在前，工作台左侧为窗，后方为密室门", "layout": [{"name": "木质工作台", "position": "房间中央靠窗", "appearance": "旧木表面与黄铜台灯", "storyFunction": "供人物检查怀表"}], "atmosphere": "窗外细雨，室内静谧", "cameraZones": "工作台正面与右侧可设置中近景", "spatialContinuity": ["窗户在左，后门在后景"], "imagePrompts": [IMAGE]}], "referenceRequirements": [], "acceptanceChecklist": ["道具位置与光线方向一致"]},
        "storyboarder": {"sequenceIntent": "静态检查怀表后推进异常信息", "shots": [{"key": "s01", "sceneKey": "shop", "durationSeconds": 5, "dramaticPurpose": "看见怀表和人物反应", "framing": "平视中近景，怀表居中", "cameraPosition": "工作台对面，眼睛高度", "cameraMovement": "五秒内缓慢向前移动", "action": "低头打开怀表，眉头逐渐收紧", "dialogueSound": "台钟与雨声，无对白", "transitionIn": "从全景切入，窗户在左", "transitionOut": "沿视线切至后门", "imagePrompt": IMAGE, "motionRequirements": ["手指动作缓慢"], "continuityChecks": ["左右手保持一致"]}], "continuityRules": ["主光从右上方照入"], "productionHandoff": ["先制作静态图片，再准备视频报价"]},
        "media_producer": {"productionStrategy": "分镜图片与身份确认后准备视频", "shots": [{"shotKey": "s01", "sourceBasis": [source], "references": [], "imagePrompt": IMAGE, "videoPrompt": {"subjectAndScene": IMAGE["subject"] + IMAGE["scene"], "actionTimeline": "零至两秒低头，两至四秒开盖，最后一秒皱眉", "cameraMovement": "平视中近景缓慢向前移动", "lightingStyle": IMAGE["lighting"], "continuityConstraints": ["银色表壳与发型一致"], "negativePrompt": ["避免手指和表链融合"], "durationSeconds": 5, "resolution": "1280×720，16:9", "frameRate": None, "audioRequirement": "静音交付"}, "modelRequirements": [{"capability": "参考图身份保持与五秒视频", "provider": None, "model": None, "verificationStatus": "unverified", "evidence": None}], "acceptance": ["人物与开表动作连续"]}], "confirmationPoints": ["核实模型能力并本人确认报价"], "deliveryChecks": ["真实文件可播放后采用"]},
        "editor": {"editingIntent": "保持发现怀表的动作完整", "cuts": [{"key": "cut01", "source": asset, "order": 1, "sourceInSeconds": 0, "sourceOutSeconds": 5, "playbackDurationSeconds": 5, "speedAndCrop": "原速保留横向画幅", "transition": "结尾等待玩家选择", "sound": "雨声持续，开盖时金属轻响", "purpose": "清晰展示异常信息"}], "totalDurationSeconds": 5, "audioPlan": "后门敲击前降低环境声", "exportSpec": "16:9、720P、H.264，帧率待核实", "executionRequirements": ["准备真实视频并核对时长"], "acceptanceChecklist": ["没有黑帧与动作裁断"]},
        "interaction_designer": {"interactionIntent": "决定是否调查后门", "variables": [{"name": "entered", "valueType": "boolean", "initialValue": False, "meaning": "是否进入密室"}], "controls": [{"key": "enter", "kind": "button", "sceneKey": "shop", "label": "推开后门", "displayCondition": [], "startSeconds": 4, "durationSeconds": 5, "inputSequence": ["click"], "success": {"destination": "letter", "conditions": [], "variableChanges": [{"variable": "entered", "operation": "set", "value": True}], "narrativeResult": "推门进入暗室取得信件"}, "failure": {"destination": "wait", "conditions": [], "variableChanges": [], "narrativeResult": "收起怀表等待天亮"}, "feedback": "点击隐藏按钮，超时进入等待分支"}], "continuityRules": ["后续场次保留已选状态"], "implementationRequirements": ["在真实场景存在后绑定代号"], "acceptanceChecklist": ["选择与超时出口可达"]},
        "sound_designer": {"sonicDirection": "雨声、钟声与静默建立悬疑", "dialogueDirection": "女性中低声线，疑问后保留停顿", "musicDirection": "缓慢持续的低音弦乐", "effectsDirection": "近距金属开盖声与后方敲门声", "cues": [{"key": "rain", "kind": "ambience", "sceneOrShotKey": "s01", "content": "窗外细雨，水滴击打窗框", "voiceOrTimbre": "自然雨滴与室内共鸣", "startSeconds": 0, "durationSeconds": 5, "entry": "半秒渐入", "exit": "敲门前降低音量", "mix": "偏左声像，低于对白", "asset": {**asset, "key": "rain", "kind": "audio", "availability": "to_record", "description": "录制五秒以上自然雨声"}}], "mixDelivery": "48kHz、24bit立体声，成片后实测响度", "productionRequirements": ["取得真实雨声录音"], "acceptanceChecklist": ["没有爆音与断点"]},
    }
    return copy.deepcopy({**base, **bodies[role]})


@pytest.mark.parametrize("role", ARTIFACT_MODELS)
def test_real_http_professional_artifact_has_complete_delivery_history_and_replay(live_service, role):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("target", "text", "授权交付", content="本人预留的专业资料节点")])
        project_id = envelope["project"]["id"]
        response, _ = create_run(client, envelope, role=role, targets=["target"], delivery_mode="professional")
        assert response.status_code == 202
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            schema = next(item for item in tool_catalog(role) if item["name"] == "write_artifact")["inputSchema"]
            assert schema == artifact_arguments_schema(role)
            assert all(model.__name__ not in schema["$defs"] for name, model in ARTIFACT_MODELS.items() if name != role)
            artifact = manual_artifact(role, project_id)
            operation = str(uuid.uuid4())
            arguments = {"targetId": "target", "artifact": artifact}
            written = tool(bridge, run, "write_artifact", arguments, operation)
            assert written.status_code == 200, written.text
            receipt = written.json()["result"]
            result = receipt["result"]
            normalized = validate_artifact(role, artifact)
            text = render_artifact(normalized)
            prompts = image_prompt_items(normalized)
            loaded = client.get(f"/projects/{project_id}").json()
            target = next(item for item in loaded["canvases"][0]["nodes"] if item["id"] == "target")
            assert target["metadata"] == {"content": text, "creativeArtifact": normalized, "artifactContractVersion": ARTIFACT_CONTRACT_VERSION,
                "generationPrompts": prompts, "artifactContentSha256": hashlib.sha256(text.encode()).hexdigest(), "artifactSources": result["sourceBindings"]}
            assert result["artifact"] == normalized and result["artifactSha256"] == payload_digest(normalized)
            assert result["artifactContractVersion"] == ARTIFACT_CONTRACT_VERSION and result["promptItems"] == prompts
            assert result["sourceNodeId"] == "target" and result["sourceRevision"] == receipt["revision"] == loaded["revision"]
            assert result["savedTexts"][0]["text"] == text and result["nodeSnapshots"][0]["snapshot"] == target
            assert tool(bridge, run, "write_artifact", arguments, operation).json() == written.json()
            run = {**run, "projectRevision": loaded["revision"]}
            checked = tool(bridge, run, "validate_artifacts", {})
            assert checked.status_code == 200, checked.text
            assert {item["kind"] for item in checked.json()["result"]["result"]["artifacts"]} == {"text", "creative_artifact"}
            current = tool(bridge, run, "read_project", {})
            assert current.json()["result"]["result"]["canvas"]["nodes"][0]["artifactCurrent"] is True
            with sqlite3.connect(f"file:{live_service.data_dir / 'sceneturn.sqlite3'}?mode=ro", uri=True) as database:
                version = database.execute("SELECT canvases_json FROM project_versions WHERE project_id=? AND revision=?", (project_id, loaded["revision"])).fetchone()
                original = next(item for item in json.loads(version[0]) if item["sceneId"] == scene_id(envelope))
                assert original["nodes"][0] == target
                assert database.execute("SELECT count(*) FROM agent_calls WHERE run_id=?", (run["id"],)).fetchone()[0] == 0
                assert database.execute("SELECT count(*) FROM generations WHERE project_id=?", (project_id,)).fetchone()[0] == 0


@pytest.mark.parametrize("changed", ["content", "creativeArtifact", "generationPrompts", "artifactContentSha256", "artifactContractVersion", "artifactSources"])
def test_professional_artifact_manual_changes_preserve_body_and_invalidate_saved_prompts(live_service, changed):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("target", "text", "交付", content="原文")])
        project_id = envelope["project"]["id"]
        response, _ = create_run(client, envelope, role="character_designer", targets=["target"], delivery_mode="professional")
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            written = tool(bridge, run, "write_artifact", {"targetId": "target", "artifact": manual_artifact("character_designer", project_id)})
            assert written.status_code == 200, written.text
            current = client.get(f"/projects/{project_id}").json()
            canvas = current["canvases"][0]
            metadata = canvas["nodes"][0]["metadata"]
            if changed == "content":
                metadata[changed] += "\n本人补充：工牌改为铜绿色。"
            elif changed == "creativeArtifact":
                metadata[changed]["characters"][0]["costumeMaterials"] = "本人改为深绿色亚麻工作服"
            elif changed == "generationPrompts":
                metadata[changed][0]["prompt"] += "\n本人修改输出要求"
            elif changed == "artifactSources":
                metadata[changed][0]["digest"] = "0" * 64
            else:
                metadata[changed] = "本人编辑"
            saved = save_canvas(client, current, canvas["nodes"], canvas["connections"])
            event = bridge.post("/connections/events", json={"runId": run["id"], "eventType": "disconnected"})
            assert event.status_code == 200
            resumed = client.post(f"/projects/{project_id}/agent-runs/{run['id']}/resume", json={"operationId": str(uuid.uuid4()), "expectedRevision": saved["revision"]})
            assert resumed.status_code == 200, resumed.text
            run = claim_run(bridge, run["id"])
            read = tool(bridge, run, "read_project", {})
            actual = read.json()["result"]["result"]["canvas"]["nodes"][0]
            assert actual["artifactCurrent"] is False and actual["metadata"] == metadata
            rejected = tool(bridge, run, "validate_artifacts", {})
            assert error_code(rejected, 409) == ("AGENT_ARTIFACT_SOURCE_CHANGED" if changed == "artifactSources" else "AGENT_ARTIFACT_CHANGED")
            assert client.get(f"/projects/{project_id}").json() == saved


def test_professional_limits_support_complete_delivery_and_old_explicit_limits():
    assert RunLimits().max_tokens == 32768
    assert RunLimits(maxTokens=32768).max_tokens == 32768
    assert RunLimits(maxTokens=8192).max_tokens == 8192
    assert RunLimits(maxTokens=512).max_tokens == 512
    assert RunLimits(maxTokens=1024).max_tokens == 1024
    assert RunLimits(maxTokens=2048).max_tokens == 2048


def test_professional_default_rejects_text_bypass_and_records_missing_artifact(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("target", "text", "授权正文", content="已有剧情")])
        project_id = envelope["project"]["id"]
        earlier, payload = create_run(client, envelope, targets=["target"], role="director")
        assert client.post(f"/projects/{project_id}/agent-runs/{earlier.json()['run']['id']}/stop", json={"operationId": str(uuid.uuid4())}).status_code == 200
        payload.pop("deliveryMode")
        payload["operationId"] = str(uuid.uuid4())
        response = client.post(f"/projects/{project_id}/agent-runs", json=payload)
        assert response.status_code == 202
        assert response.json()["run"]["deliveryMode"] == "professional"
        assert response.json()["run"]["artifactContractVersion"] == ARTIFACT_CONTRACT_VERSION
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            assert error_code(tool(bridge, run, "write_script", {"targetId": "target", "text": "局部正文"}), 403) == "AGENT_PROFESSIONAL_TOOL_REQUIRED"
            assert tool(bridge, run, "read_project", {}).status_code == 200
            engine = create_engine(f"sqlite:///{live_service.data_dir / 'sceneturn.sqlite3'}")
            with Session(engine) as session:
                actual = session.get(AgentRun, run["id"])
                step = session.scalar(select(AgentStep).where(AgentStep.run_id == actual.id))
                with pytest.raises(AppError) as failure:
                    require_step_delivery(session, actual, step, session.get(Project, project_id), Settings(data_dir=live_service.data_dir))
                assert failure.value.code == "AGENT_PROFESSIONAL_ARTIFACT_MISSING"
            engine.dispose()


@pytest.mark.parametrize("kind", ["project", "scene", "canvas_node", "asset", "memory", "artifact"])
def test_professional_sources_reject_nonexistent_and_foreign_references(live_service, kind):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("target", "text", "授权正文", content="原文")])
        other = create_project(client)
        project_id = envelope["project"]["id"]
        invalid_id = other["project"]["id"] if kind == "project" else next(item["id"] for item in envelope["project"]["graphData"]["nodes"] if item["type"] == "start") if kind == "scene" else str(uuid.uuid4())
        artifact = manual_artifact("character_designer", project_id)
        artifact["sources"] = [{"kind": kind, "referenceId": invalid_id, "description": "本人填写来源，服务端需要检查其真实归属"}]
        response, _ = create_run(client, envelope, role="character_designer", targets=["target"], delivery_mode="professional")
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            rejected = tool(bridge, run, "write_artifact", {"targetId": "target", "artifact": artifact})
            assert error_code(rejected, 422) == "AGENT_ARTIFACT_SOURCE_INVALID"
            assert client.get(f"/projects/{project_id}").json() == envelope


@pytest.mark.parametrize("source_kind", ["canvas_node", "artifact"])
def test_professional_same_target_uses_real_historical_input_snapshot(live_service, source_kind):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("target", "text", "已有角色设计", content="林岚穿深灰工作服，右手托住银色怀表，左手靠近表冠。")])
        project_id = envelope["project"]["id"]
        artifact = manual_artifact("character_designer", project_id)
        response, _ = create_run(client, envelope, role="character_designer", targets=["target"], delivery_mode="professional")
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            read = tool(bridge, run, "read_project", {})
            assert read.status_code == 200
            if source_kind == "artifact":
                earlier = tool(bridge, run, "write_artifact", {"targetId": "target", "artifact": artifact})
                assert earlier.status_code == 200, earlier.text
                run = {**run, "projectRevision": earlier.json()["result"]["revision"]}
                envelope = client.get(f"/projects/{project_id}").json()
            artifact["sources"] = [{"kind": source_kind, "referenceId": "target", "description": "根据已有角色文字扩展完整生图交付"}]
            written = tool(bridge, run, "write_artifact", {"targetId": "target", "artifact": artifact})
            assert written.status_code == 200, written.text
            binding = written.json()["result"]["result"]["sourceBindings"][0]
            assert binding["revision"] == envelope["revision"] and binding["bindingMode"] == "input_snapshot"
            assert binding["digest"] == payload_digest(envelope["canvases"][0]["nodes"][0])
            run = {**run, "projectRevision": written.json()["result"]["revision"]}
            checked = tool(bridge, run, "validate_artifacts", {})
            assert checked.status_code == 200, checked.text
            engine = create_engine(f"sqlite:///{live_service.data_dir / 'sceneturn.sqlite3'}")
            with Session(engine) as session:
                actual = session.get(AgentRun, run["id"])
                step = session.scalar(select(AgentStep).where(AgentStep.run_id == actual.id))
                assert require_step_delivery(session, actual, step, session.get(Project, project_id), Settings(data_dir=live_service.data_dir))["valid"] is True
            engine.dispose()


def test_professional_role_and_write_scope_are_checked_by_server(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("target", "text", "授权正文", content="原文"), node("other", "text", "人工正文", content="保留")])
        project_id = envelope["project"]["id"]
        response, _ = create_run(client, envelope, role="character_designer", targets=["target"], delivery_mode="professional")
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            wrong_role = tool(bridge, run, "write_artifact", {"targetId": "target", "artifact": manual_artifact("scene_designer", project_id)})
            assert error_code(wrong_role, 422) == "AGENT_ARTIFACT_INVALID"
            unauthorized = tool(bridge, run, "write_artifact", {"targetId": "other", "artifact": manual_artifact("character_designer", project_id)})
            assert error_code(unauthorized, 403) == "AGENT_TARGET_NOT_ALLOWED"
            assert client.get(f"/projects/{project_id}").json() == envelope


def test_text_edit_requires_real_authorized_text_and_capable_role_before_calls(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("target", "text", "授权正文", content="已有剧情")])
        unsupported, _ = create_run(client, envelope, role="media_producer", targets=["target"], delivery_mode="text_edit")
        assert error_code(unsupported, 422) == "AGENT_DELIVERY_ROLE_UNSUPPORTED"
        no_target, _ = create_run(client, envelope, delivery_mode="text_edit")
        assert error_code(no_target, 422) == "AGENT_TEXT_EDIT_TARGET_REQUIRED"
        response, _ = create_run(client, envelope, targets=["target"], delivery_mode="text_edit")
        project_id = envelope["project"]["id"]
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            assert error_code(tool(bridge, run, "write_script", {"text": "无目标正文"}), 403) == "AGENT_TEXT_EDIT_TARGET_REQUIRED"
            saved = tool(bridge, run, "write_script", {"targetId": "target", "text": "本人指定的局部正文修订。"})
            assert saved.status_code == 200
            engine = create_engine(f"sqlite:///{live_service.data_dir / 'sceneturn.sqlite3'}")
            with Session(engine) as session:
                actual = session.get(AgentRun, run["id"])
                step = session.scalar(select(AgentStep).where(AgentStep.run_id == actual.id))
                require_step_delivery(session, actual, step, session.get(Project, project_id), Settings(data_dir=live_service.data_dir))
            engine.dispose()


def test_memory_source_remains_allowed_when_long_canvas_exceeds_context_capacity(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("target", "text", "授权正文", content="原文")])
        project_id = envelope["project"]["id"]
        saved = client.post(f"/projects/{project_id}/memories", json={"operationId": str(uuid.uuid4()), "expectedRevision": envelope["revision"],
            "kind": "setting", "subject": "人物职业", "content": "林岚是一名旧钟表店修表师，穿深灰工作服，使用银色怀表。", "sources": [{"kind": "project", "id": project_id, "revision": envelope["revision"]}]})
        assert saved.status_code == 201
        memory_id = saved.json()["memory"]["id"]
        envelope = client.get(f"/projects/{project_id}").json()
        response, _ = create_run(client, envelope, role="character_designer", targets=["target"], delivery_mode="professional")
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            read = tool(bridge, run, "retrieve_context", {})
            assert read.status_code == 200 and memory_id in {item["id"] for item in read.json()["result"]["result"]["memories"]}
            long_background = "本人补充的钟表店背景资料，保留房间里的旧木柜和黄铜台灯。" * 700
            expanded = tool(bridge, run, "apply_canvas_ops", {"operations": [{"action": "add_text", "title": f"背景资料{index}", "text": long_background[:19000]} for index in range(4)]})
            assert expanded.status_code == 200
            run = {**run, "projectRevision": expanded.json()["result"]["revision"]}
            artifact = manual_artifact("character_designer", project_id)
            artifact["sources"] = [{"kind": "memory", "referenceId": memory_id, "description": "本次已实际检索的人物职业设定"}]
            written = tool(bridge, run, "write_artifact", {"targetId": "target", "artifact": artifact})
            assert written.status_code == 200, written.text
            run = {**run, "projectRevision": written.json()["result"]["revision"]}
            checked = tool(bridge, run, "validate_artifacts", {})
            assert checked.status_code == 200, checked.text
            engine = create_engine(f"sqlite:///{live_service.data_dir / 'sceneturn.sqlite3'}")
            with Session(engine) as session:
                actual = session.get(AgentRun, run["id"])
                scope = source_scope(session, project_id, actual.owner_id, actual.scene_id)
                assert memory_id in scope["memoryIds"]
            engine.dispose()


@pytest.mark.parametrize("dependency", ["canvas_node", "memory"])
def test_nested_artifact_rejects_changed_actual_sources(live_service, dependency):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("source", "text", "原始设定", content="深灰工作服与银色怀表"), node("first", "text", "专业资料", content="预留"), node("second", "text", "后续资料", content="预留")])
        project_id = envelope["project"]["id"]
        identifier = "source"
        if dependency == "memory":
            memory = client.post(f"/projects/{project_id}/memories", json={"operationId": str(uuid.uuid4()), "expectedRevision": envelope["revision"], "kind": "setting", "subject": "服装要求", "content": "林岚穿深灰工作服。", "sources": [{"kind": "project", "id": project_id, "revision": envelope["revision"]}]})
            assert memory.status_code == 201
            identifier = memory.json()["memory"]["id"]
            envelope = client.get(f"/projects/{project_id}").json()
        created, _ = create_run(client, envelope, role="character_designer", targets=["first"], delivery_mode="professional")
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, created.json()["run"]["id"])
            assert tool(bridge, run, "retrieve_context", {}).status_code == 200
            first = manual_artifact("character_designer", project_id)
            first["sources"] = [{"kind": dependency, "referenceId": identifier, "description": "来自实际保存的服装设定"}]
            written = tool(bridge, run, "write_artifact", {"targetId": "first", "artifact": first})
            assert written.status_code == 200, written.text
            assert client.post(f"/projects/{project_id}/agent-runs/{run['id']}/stop", json={"operationId": str(uuid.uuid4())}).status_code == 200
        current = client.get(f"/projects/{project_id}").json()
        if dependency == "canvas_node":
            snapshot = current["canvases"][0]
            next(item for item in snapshot["nodes"] if item["id"] == "source")["metadata"]["content"] = "本人将工作服改为深绿色"
            current = save_canvas(client, current, snapshot["nodes"], snapshot["connections"])
        else:
            replacement = client.post(f"/projects/{project_id}/memories", json={"operationId": str(uuid.uuid4()), "expectedRevision": current["revision"], "kind": "setting", "subject": "服装要求", "content": "本人将工作服改为深绿色。", "supersedesId": identifier, "sources": [{"kind": "project", "id": project_id, "revision": current["revision"]}]})
            assert replacement.status_code == 201
            current = client.get(f"/projects/{project_id}").json()
        response, _ = create_run(client, current, role="character_designer", targets=["second"], delivery_mode="professional")
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            read = tool(bridge, run, "read_project", {})
            first_node = next(item for item in read.json()["result"]["result"]["canvas"]["nodes"] if item["id"] == "first")
            assert first_node["artifactCurrent"] is False
            second = manual_artifact("character_designer", project_id)
            second["sources"] = [{"kind": "artifact", "referenceId": "first", "description": "继承前一份专业角色资料"}]
            rejected = tool(bridge, run, "write_artifact", {"targetId": "second", "artifact": second})
            assert rejected.status_code in {409, 422} and rejected.json()["error"]["code"] in {"AGENT_ARTIFACT_SOURCE_INVALID", "AGENT_ARTIFACT_SOURCE_CHANGED"}
            assert client.get(f"/projects/{project_id}").json() == current


def test_ancestor_artifact_input_snapshot_can_be_reused_without_rereading_indirect_sources(live_service):
    with closing(registered_client(live_service)[0]) as client:
        envelope = create_project(client)
        project_id = envelope["project"]["id"]
        engine = create_engine(f"sqlite:///{live_service.data_dir / 'sceneturn.sqlite3'}")
        with Session(engine) as session:
            _project, scenes, _edges = branch_project(session, project_id)
        shared = client.get(f"/projects/{project_id}/canvases/{scenes['shared']}").json()
        saved = client.put(f"/projects/{project_id}/canvases/{scenes['shared']}", json={"operationId": str(uuid.uuid4()), "expectedRevision": shared["revision"],
            "nodes": [node("ancestor", "text", "原始人物设定", content="林岚穿深灰工作服，托住银色怀表。")], "connections": [], "viewport": shared["canvas"]["viewport"]})
        assert saved.status_code == 200, saved.text
        envelope = client.get(f"/projects/{project_id}").json()
        response, _ = create_run(client, envelope, role="character_designer", targets=["ancestor"], delivery_mode="professional")
        assert response.status_code == 202
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            artifact = manual_artifact("character_designer", project_id)
            artifact["sources"] = [{"kind": "canvas_node", "referenceId": "ancestor", "description": "采用本节点之前保存的原始设定"}]
            written = tool(bridge, run, "write_artifact", {"targetId": "ancestor", "artifact": artifact})
            assert written.status_code == 200, written.text
            assert client.post(f"/projects/{project_id}/agent-runs/{run['id']}/stop", json={"operationId": str(uuid.uuid4())}).status_code == 200
        child = client.get(f"/projects/{project_id}/canvases/{scenes['left']}").json()
        saved = client.put(f"/projects/{project_id}/canvases/{scenes['left']}", json={"operationId": str(uuid.uuid4()), "expectedRevision": child["revision"],
            "nodes": [node("child", "text", "本场人物资料", content="预留")], "connections": [], "viewport": child["canvas"]["viewport"]})
        assert saved.status_code == 200
        response = client.post(f"/projects/{project_id}/agent-runs", json={"operationId": str(uuid.uuid4()), "expectedRevision": saved.json()["revision"],
            "sceneId": scenes["left"], "role": "character_designer", "executor": "codex", "deliveryMode": "professional", "targetIds": ["child"],
            "goal": "根据已保存祖先场景资料制作本场完整人物资料。"})
        assert response.status_code == 202, response.text
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            read = tool(bridge, run, "retrieve_context", {"targetIds": ["ancestor"]})
            assert read.status_code == 200, read.text
            assert {item["id"] for item in read.json()["result"]["result"]["canvasTexts"]} == {"ancestor"}
            automatic = client.post(f"/projects/{project_id}/context/retrieve", json={"sceneId": scenes["left"], "targetIds": ["child"], "maxChars": 16000})
            assert automatic.status_code == 200
            assert {item["id"] for item in automatic.json()["canvasTexts"]} == {"child"}
            with Session(engine) as session:
                actual = session.get(AgentRun, run["id"])
                step = session.scalar(select(AgentStep).where(AgentStep.run_id == actual.id))
                receipt = next(item for item in step.tool_results_json if item["tool"] == "retrieve_context")
                assert _tool_observation(receipt)["context"] == read.json()["result"]["result"]
                messages = _role_messages(actual, step, actual.role, automatic.json(), [], [])
                instruction = json.loads(messages[1]["content"])
                observed = next(item for item in instruction["completedOperations"] if item["tool"] == "retrieve_context")
                assert observed["context"]["canvasTexts"][0]["content"] == read.json()["result"]["result"]["canvasTexts"][0]["content"]
            artifact = manual_artifact("character_designer", project_id)
            artifact["sources"] = [{"kind": "scene", "referenceId": scenes["right"], "description": "服务端须拒绝互斥分支来源"}]
            assert error_code(tool(bridge, run, "write_artifact", {"targetId": "child", "artifact": artifact}), 422) == "AGENT_ARTIFACT_SOURCE_INVALID"
            artifact["sources"] = [{"kind": "artifact", "referenceId": "ancestor", "description": "继承已实际检索的祖先场景角色资料"}]
            written = tool(bridge, run, "write_artifact", {"targetId": "child", "artifact": artifact})
            assert written.status_code == 200, written.text
            run = {**run, "projectRevision": written.json()["result"]["revision"]}
            checked = tool(bridge, run, "validate_artifacts", {})
            assert checked.status_code == 200, checked.text
        engine.dispose()


def test_existing_media_requirement_uses_real_uploaded_project_asset(live_service, sample_video):
    with closing(registered_client(live_service)[0]) as client:
        envelope = save_canvas(client, create_project(client), [node("target", "text", "剪辑方案", content="预留")])
        project_id = envelope["project"]["id"]
        other = create_project(client)
        foreign = client.post(f"/projects/{other['project']['id']}/assets", files={"file": ("original.mp4", sample_video.read_bytes(), "video/mp4")})
        uploaded = client.post(f"/projects/{project_id}/assets", files={"file": ("original.mp4", sample_video.read_bytes(), "video/mp4")})
        assert foreign.status_code == uploaded.status_code == 201
        envelope = client.get(f"/projects/{project_id}").json()
        response, _ = create_run(client, envelope, role="editor", targets=["target"], delivery_mode="professional")
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            artifact = manual_artifact("editor", project_id)
            source = artifact["cuts"][0]["source"]
            source.update(availability="existing", assetId=foreign.json()["asset"]["id"], source={"kind": "asset", "referenceId": foreign.json()["asset"]["id"], "description": "需要检查项目归属的真实视频"})
            rejected = tool(bridge, run, "write_artifact", {"targetId": "target", "artifact": artifact})
            assert error_code(rejected, 422) == "AGENT_ARTIFACT_SOURCE_INVALID"
            identifier = uploaded.json()["asset"]["id"]
            source.update(assetId=identifier, source={"kind": "asset", "referenceId": identifier, "description": "本人已上传至当前项目的原视频"})
            artifact["cuts"][0].update(sourceOutSeconds=1, playbackDurationSeconds=1)
            artifact["totalDurationSeconds"] = 1
            written = tool(bridge, run, "write_artifact", {"targetId": "target", "artifact": artifact})
            assert written.status_code == 200, written.text
            run = {**run, "projectRevision": written.json()["result"]["revision"]}
            assert tool(bridge, run, "validate_artifacts", {}).status_code == 200
            read = tool(bridge, run, "read_project", {})
            assert read.status_code == 200
            engine = create_engine(f"sqlite:///{live_service.data_dir / 'sceneturn.sqlite3'}")
            with Session(engine) as session:
                step = session.scalar(select(AgentStep).where(AgentStep.run_id == run["id"]))
                receipt = next(item for item in reversed(step.tool_results_json) if item["tool"] == "read_project")
                observation = _tool_observation(receipt)
                assert observation["assets"] == read.json()["result"]["result"]["assets"]
                assert {item["id"] for item in observation["assets"]} == {identifier}
                assert observation["nodes"][0]["id"] == "target" and observation["nodes"][0]["artifactRole"] == "editor"
                assert observation["nodes"][0]["artifactCurrent"] is True
                assert "content" not in json.dumps(observation["nodes"], ensure_ascii=False)
                assert observation["capabilities"]["text"] is True
            engine.dispose()
            assert client.get(f"/assets/{identifier}/content").content == sample_video.read_bytes()


@pytest.mark.parametrize("write_tool", ["write_script", "write_artifact"])
def test_batch_text_write_preserves_authored_candidates_and_selects_new_actual_content(live_service, write_tool):
    with closing(registered_client(live_service)[0]) as client:
        old_candidates = [{"id": "authored-a", "content": "本人填写的第一份人物资料。", "status": "success", "note": "保留原注释"},
            {"id": "authored-b", "content": "本人填写的第二份人物资料，修表师穿深灰工作服。", "status": "success"}]
        target = node("target", "text", "本人批量文字", content="旧的元数据正文", texts=copy.deepcopy(old_candidates), primaryTextId="authored-b")
        envelope = save_canvas(client, create_project(client), [target])
        project_id = envelope["project"]["id"]
        mode = "professional" if write_tool == "write_artifact" else "text_edit"
        response, _ = create_run(client, envelope, role="character_designer", targets=["target"], delivery_mode=mode)
        assert response.status_code == 202
        with bridge_client(live_service, client, project_id)[0] as bridge:
            run = claim_run(bridge, response.json()["run"]["id"])
            arguments = {"targetId": "target", "artifact": manual_artifact("character_designer", project_id)} if write_tool == "write_artifact" else {"targetId": "target", "text": "本人要求保存的新正文：林岚打开银色怀表。"}
            operation = str(uuid.uuid4())
            written = tool(bridge, run, write_tool, arguments, operation)
            assert written.status_code == 200, written.text
            receipt = written.json()["result"]
            saved = client.get(f"/projects/{project_id}").json()
            metadata = saved["canvases"][0]["nodes"][0]["metadata"]
            assert metadata["texts"][:2] == old_candidates and len(metadata["texts"]) == 3
            selected = next(item for item in metadata["texts"] if item["id"] == metadata["primaryTextId"])
            assert selected["id"] not in {item["id"] for item in old_candidates}
            assert selected["status"] == "success" and selected["agentRunId"] == run["id"]
            assert selected["content"] == metadata["content"] == receipt["result"]["savedTexts"][0]["text"]
            if write_tool == "write_artifact":
                assert artifact_is_current(saved["canvases"][0]["nodes"][0]) is True
                alternate = copy.deepcopy(saved["canvases"][0]["nodes"][0])
                alternate["metadata"]["primaryTextId"] = "authored-a"
                assert artifact_is_current(alternate) is False
            assert str(uuid.UUID(selected["id"])) == selected["id"]
            assert tool(bridge, run, write_tool, arguments, operation).json() == written.json()
            assert client.get(f"/projects/{project_id}").json() == saved
            with sqlite3.connect(f"file:{live_service.data_dir / 'sceneturn.sqlite3'}?mode=ro", uri=True) as database:
                original = database.execute("SELECT canvases_json FROM project_versions WHERE project_id=? AND revision=?", (project_id, envelope["revision"])).fetchone()
                old = next(item for item in json.loads(original[0]) if item["sceneId"] == scene_id(envelope))["nodes"][0]["metadata"]
                assert old["texts"] == old_candidates and old["primaryTextId"] == "authored-b"
            run = {**run, "projectRevision": receipt["revision"]}
            assert tool(bridge, run, "validate_artifacts", {}).status_code == 200
