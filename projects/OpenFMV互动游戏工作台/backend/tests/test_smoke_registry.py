import copy
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from conftest import PRODUCT_ROOT


sys.path.insert(0, str(PRODUCT_ROOT / "scripts"))

from smoke_record_text import PROMPT_VERSION, verify_text_data, verify_text_prompt_contract
from smoke_registry import EvidenceStore, render_evidence, write_private_json


TEXT_TASK = "ff7557df-a61f-436e-be17-9181369ad52e"


@pytest.fixture
def real_store():
    store = EvidenceStore(PRODUCT_ROOT)
    if not store.registry_path.is_file():
        pytest.skip("本产品尚无真实调用索引，本用例需要实际模型验收数据")
    store.load()
    return store


@pytest.fixture
def copied_store(real_store, tmp_path):
    copied = EvidenceStore(tmp_path)
    copied.registry_path.parent.mkdir(parents=True)
    copied.evidence_path.parent.mkdir(parents=True)
    registry = real_store.load()
    copied.evidence_path.write_bytes(real_store.evidence_path.read_bytes())
    write_private_json(copied.registry_path, registry)
    for record in registry["records"]:
        copied.directory(record).mkdir()
        write_private_json(copied.directory(record) / "verification.json", record)
    copied.load()
    return copied


def evidence_files(store):
    paths = [store.registry_path, store.evidence_path]
    paths.extend(store.directory(record) / "verification.json" for record in store.load()["records"])
    return {str(path): (path.stat().st_mtime_ns, path.read_bytes()) for path in paths}


def test_real_registry_and_all_sidecars_reproduce_entire_markdown(real_store):
    registry = real_store.load()
    content = real_store.evidence_path.read_bytes()
    assert render_evidence(registry["records"]).encode() == content
    assert hashlib.sha256(content).hexdigest() == registry["markdownSha256"]
    assert any(record["model"] == "wan3.0-video" for record in registry["records"])


def test_rechecking_registered_real_calls_preserves_every_evidence_file(copied_store):
    registry = copied_store.load()
    for record in registry["records"]:
        assert copied_store.register(record) is False
    before = evidence_files(copied_store)
    for _ in range(2):
        for record in registry["records"]:
            assert copied_store.register(copy.deepcopy(record)) is False
    assert evidence_files(copied_store) == before
    assert len(copied_store.load()["records"]) == len(registry["records"])


def test_replacing_markdown_hash_cannot_hide_changed_evidence(copied_store):
    registry = copied_store.load()
    changed = copied_store.evidence_path.read_bytes() + b"\n"
    copied_store.evidence_path.write_bytes(changed)
    write_private_json(copied_store.registry_path, {**registry, "markdownSha256": hashlib.sha256(changed).hexdigest()})
    before = copied_store.registry_path.read_bytes()
    with pytest.raises(ValueError, match="渲染不一致"):
        copied_store.register(registry["records"][0])
    assert copied_store.registry_path.read_bytes() == before


def test_changed_sidecar_prevents_any_registration(copied_store):
    registry = copied_store.load()
    record = registry["records"][0]
    sidecar = copied_store.directory(record) / "verification.json"
    write_private_json(sidecar, {**record, "verifiedAt": ""})
    before = copied_store.registry_path.read_bytes(), copied_store.evidence_path.read_bytes()
    with pytest.raises(ValueError, match="验证文件"):
        copied_store.register(record)
    assert (copied_store.registry_path.read_bytes(), copied_store.evidence_path.read_bytes()) == before


def test_existing_real_text_task_proves_frozen_history_structure_and_token_ledger():
    if not (PRODUCT_ROOT / "data" / "openfmv.sqlite3").is_file():
        pytest.skip("本产品尚无本人真实文本验收数据库")
    record, fingerprints = verify_text_data(Path("data"), TEXT_TASK)
    assert record["remoteRequestId"] == "8a666f1b-70a7-4af3-be65-f41220082e17"
    assert record["model"] == "deepseek-flash"
    assert (record["usage"]["prompt_tokens"], record["usage"]["completion_tokens"], record["usage"]["total_tokens"]) == (198, 70, 268)
    assert record["costMicros"] == 956 and record["targetWritten"] is True
    assert all(len(value) == 64 for value in fingerprints.values())
    assert record["filePermissions"]["database"] == "0600"
    assert record["filePermissions"]["secretMaster"] == "0600"
    assert not {"outputText", "result", "messages", "partialText", "apiKey"} & set(record)


def test_text_cannot_enter_success_record_without_actual_restart(real_store):
    record, _ = verify_text_data(Path("data"), TEXT_TASK)
    with pytest.raises(ValueError, match="标识或时间无效"):
        render_evidence([*real_store.load()["records"], record])
    record["verifiedAt"] = record["finishedAt"]
    with pytest.raises(ValueError, match="实际重启验证"):
        render_evidence([*real_store.load()["records"], record])


@pytest.fixture
def real_text_input():
    database = PRODUCT_ROOT / "data" / "openfmv.sqlite3"
    if not database.is_file():
        pytest.skip("需要本人已保存的真实旧文本请求")
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT task,input_json FROM text_generations WHERE id=?", (TEXT_TASK,)).fetchone()
    assert row is not None
    return row[0], json.loads(row[1])


def test_real_legacy_prompt_contract_verification_preserves_database_and_evidence(real_store, real_text_input):
    database = PRODUCT_ROOT / "data" / "openfmv.sqlite3"
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    before_evidence = evidence_files(real_store)
    task, frozen = real_text_input
    original = copy.deepcopy(frozen)
    assert frozen["promptVersion"] == "openfmv-text-2026-11-13"
    assert frozen["maxTokens"] == 4096 and frozen["maxAttempts"] == 1
    assert frozen["inputTokenUpperBound"] == 1751
    assert frozen["price"]["version"] == "deepseek-flash-2026-11-13"
    verify_text_prompt_contract(task, frozen)
    verify_text_data(Path("data"), TEXT_TASK)
    assert frozen == original
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash
    assert evidence_files(real_store) == before_evidence


@pytest.mark.parametrize("change", ["unknown_version", "current_version", "system", "user", "attempts", "price_version", "task"])
def test_tampered_real_request_cannot_bypass_prompt_contract(real_text_input, change):
    # 只在内存检查真实请求的篡改负例，不创建调用或修改历史。
    task, frozen = real_text_input
    if change == "unknown_version":
        frozen["promptVersion"] = "openfmv-text-unknown"
    elif change == "current_version":
        frozen["promptVersion"] = PROMPT_VERSION
    elif change == "system":
        frozen["messages"][0]["content"] += "\n更改已冻结的系统提示词"
    elif change == "user":
        frozen["messages"][1]["content"] += "\n更改已冻结的用户输入"
    elif change == "attempts":
        frozen["maxAttempts"] = 2
    elif change == "price_version":
        frozen["price"]["version"] = "deepseek-flash-unknown"
    else:
        task = "summary"
    with pytest.raises(ValueError):
        verify_text_prompt_contract(task, frozen)
