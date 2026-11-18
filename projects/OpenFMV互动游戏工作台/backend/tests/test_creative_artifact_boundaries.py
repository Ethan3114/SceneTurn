import copy
import hashlib
import json
import sqlite3

import pytest
from pydantic import ValidationError

from app.creative_artifacts import render_artifact, validate_artifact
from app.services.projects import payload_digest
from app.services.prompts.agent import PROMPT_VERSION, native_agent_prompt
from conftest import PRODUCT_ROOT
from test_agent_professional_artifacts import manual_artifact


@pytest.mark.parametrize("role,field,key", [
    ("character_designer", "characters", "key"), ("scene_designer", "scenes", "key"),
    ("storyboarder", "shots", "key"), ("media_producer", "shots", "shotKey"), ("sound_designer", "cues", "key"),
])
def test_local_role_keys_reject_ambiguous_duplicates_and_accept_distinct_entries(role, field, key):
    authored = manual_artifact(role, "authored-project")
    authored[field].append(copy.deepcopy(authored[field][0]))
    with pytest.raises(ValidationError) as failure:
        validate_artifact(role, authored)
    assert failure.value.errors()[0]["type"] == "value_error"
    authored[field][-1][key] += "-second"
    result = validate_artifact(role, authored)
    assert len(result[field]) == 2 and result[field][0][key] != result[field][1][key]


def test_unoccupied_scene_keeps_environment_action_sound_and_global_cast():
    authored = manual_artifact("writer", "authored-project")
    scene = authored["scenes"][0]
    scene.update(characters=[], dialogues=[], action="空无一人的旧钟表店里，窗帘随风轻动，银色怀表静置于工作台上。",
        dialogueDirection="无对白，保留台钟走动声、风声与窗外雨声。", endingState="室内无人，怀表与工具保持原位置。")
    result = validate_artifact("writer", authored)
    assert result["scenes"][0]["characters"] == [] and result["scenes"][0]["dialogues"] == []
    assert result["characters"] == authored["characters"]
    scene["dialogues"] = [{"speaker": "lin", "line": "这场环境镜头中没有人物", "performance": "台词引用检查"}]
    with pytest.raises(ValidationError):
        validate_artifact("writer", authored)


def test_writer_branch_reference_errors_identify_fields_and_legal_keys_without_story_body():
    authored = manual_artifact("writer", "authored-project")
    authored["branches"][0].update(fromScene="undefined-source", destination="undefined-ending")
    with pytest.raises(ValidationError) as failure:
        validate_artifact("writer", authored)
    errors = failure.value.errors(include_input=False, include_context=False, include_url=False)
    assert {error["loc"] for error in errors} == {("branches", 0, "fromScene"), ("branches", 0, "destination")}
    assert all("enter" in error["msg"] and "shop" in error["msg"] for error in errors)
    assert "undefined-source" in errors[0]["msg"]
    assert "undefined-ending" in errors[1]["msg"] and "letter" in errors[1]["msg"]
    assert authored["plot"] not in str(failure.value)
    assert all("input" not in error and "ctx" not in error for error in errors)


def test_editor_net_timeline_duration_includes_explicit_overlap_and_speed_allocation():
    authored = manual_artifact("editor", "authored-project")
    first = authored["cuts"][0]
    second = copy.deepcopy(first)
    second.update(key="cut02", order=2, playbackDurationSeconds=4,
        speedAndCrop="五秒源素材以1.25倍速度播放，计入最终时间线四秒。", transition="无新增时长，直接结束。")
    first.update(playbackDurationSeconds=3, transition="与后片段重叠两秒，重叠时间计入后片段，本片段净计入三秒。")
    authored.update(cuts=[first, second], totalDurationSeconds=7)
    assert validate_artifact("editor", authored)["totalDurationSeconds"] == 7
    authored["totalDurationSeconds"] = 7.0000001
    assert validate_artifact("editor", authored)["totalDurationSeconds"] == 7.0000001
    authored["totalDurationSeconds"] = 500
    with pytest.raises(ValidationError):
        validate_artifact("editor", authored)


def test_editor_float_sum_keeps_valid_decimal_inputs_and_rejects_visible_difference():
    authored = manual_artifact("editor", "authored-project")
    first = authored["cuts"][0]
    second = copy.deepcopy(first)
    first.update(playbackDurationSeconds=0.1, speedAndCrop="明确计入最终时间线0.1秒。")
    second.update(key="cut02", order=2, playbackDurationSeconds=0.2, speedAndCrop="明确计入最终时间线0.2秒。")
    authored.update(cuts=[first, second], totalDurationSeconds=0.3)
    assert validate_artifact("editor", authored)["totalDurationSeconds"] == 0.3
    authored["totalDurationSeconds"] = 0.31
    with pytest.raises(ValidationError):
        validate_artifact("editor", authored)


def test_professional_prompts_explain_unique_keys_unoccupied_scene_and_net_duration():
    assert PROMPT_VERSION.endswith("r19")
    for role in ("character_designer", "scene_designer", "storyboarder", "media_producer", "sound_designer"):
        assert "每项代号必须唯一" in native_agent_prompt(role, False)
    assert "characters=[]、dialogues=[]" in native_agent_prompt("writer", False)
    assert "净时长之和" in native_agent_prompt("editor", False)


def test_all_existing_successful_artifacts_keep_canonical_content_and_saved_text_hashes():
    databases = [PRODUCT_ROOT / "data" / "openfmv.sqlite3"]
    for folder in ("agent-smoke", "codex-smoke"):
        databases.extend((PRODUCT_ROOT / ".intermediate" / folder).glob("*/data/openfmv.sqlite3"))
    checked = set()
    for database in databases:
        if not database.exists():
            continue
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "agent_steps" not in tables:
                continue
            for row in connection.execute("SELECT tool_results_json FROM agent_steps"):
                for receipt in json.loads(row[0]):
                    if receipt["tool"] != "write_artifact":
                        continue
                    result = receipt["result"]
                    original = result["artifact"]
                    digest = payload_digest(original)
                    if digest in checked:
                        continue
                    current = validate_artifact(original["role"], original)
                    assert payload_digest(current) == digest == result["artifactSha256"]
                    text = render_artifact(current)
                    assert all(saved["text"] == text and saved["sha256"] == hashlib.sha256(text.encode()).hexdigest() for saved in result["savedTexts"])
                    checked.add(digest)
    assert checked, "需要本产品已有真实成功专业产物记录"
