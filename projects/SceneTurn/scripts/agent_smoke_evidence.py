import fcntl
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from smoke_registry import EVIDENCE_TITLE, evidence_product_line, uses_legacy_evidence_product


ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / ".intermediate" / "agent-smoke"
EVIDENCE = ROOT / "docs" / "evidence" / "阶段2-2" / "冒烟记录.md"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def write_private_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending = path.with_suffix(path.suffix + ".pending")
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        json.dump(value, destination, ensure_ascii=False, indent=2, allow_nan=False)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(pending, path)


class AgentCallEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    callId: str = Field(min_length=1, max_length=200)
    runId: str = Field(min_length=1, max_length=200)
    projectId: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    execution: Literal["platform-api", "personal-codex"]
    requestId: str | None = None
    inputTokens: int | None = Field(default=None, ge=0)
    outputTokens: int | None = Field(default=None, ge=0)
    totalTokens: int | None = Field(default=None, ge=0)
    costMicros: int | None = Field(default=None, ge=0)
    startedAt: str
    finishedAt: str | None = None
    structureValid: bool
    restartVerified: bool
    retryCount: int = Field(default=0, ge=0)
    result: Literal["通过", "失败", "瑕疵待打分"]
    inputSha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    outputSha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("callId", "runId", "projectId", "role", "provider", "model", "requestId", "startedAt", "finishedAt")
    @classmethod
    def safe_cell(cls, value):
        if value is not None and any(character in value for character in ("|", "\r", "\n")):
            raise ValueError("记录字段不能包含表格控制字符")
        return value

    @model_validator(mode="after")
    def actual_call(self):
        started = datetime.fromisoformat(self.startedAt)
        finished = datetime.fromisoformat(self.finishedAt) if self.finishedAt else None
        if started.tzinfo is None or finished is not None and (finished.tzinfo is None or finished < started):
            raise ValueError("实际调用时间或时区无效")
        if self.result == "通过" and (not self.structureValid or not self.restartVerified or finished is None):
            raise ValueError("通过记录必须具备结构、实际完成时间及重启验证")
        if self.execution == "platform-api" and self.result == "通过":
            if not self.requestId or None in (self.inputTokens, self.outputTokens, self.totalTokens, self.costMicros):
                raise ValueError("平台API通过记录必须具有真实请求及完整用量")
            if self.inputTokens + self.outputTokens != self.totalTokens:
                raise ValueError("实际Token总量不一致")
        return self


def _time(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def render_agent_evidence(records: list[dict], *, legacy_product_label: bool = False) -> str:
    if not records:
        raise ValueError("没有可登记的真实调用")
    models = sorted({f"{entry['provider']} / {entry['model']}" for entry in records})
    rows = [EVIDENCE_TITLE, evidence_product_line(legacy_product_label), "- 阶段：阶段2-2",
        f"- 模型：{'；'.join(models)}", f"- 写入时间：{records[-1]['registeredAt']}", "",
        "| 时间 | 链路 | 请求 ID | 输入 token | 输出 token | 首字耗时 | 总耗时 | 结构校验 | 重试次数 | 结果 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for entry in records:
        record = AgentCallEvidence.model_validate({key: value for key, value in entry.items() if key not in {"proofPath", "proofSha256", "registeredAt"}})
        elapsed = f"{(datetime.fromisoformat(record.finishedAt) - datetime.fromisoformat(record.startedAt)).total_seconds():.3f}s" if record.finishedAt else "待核实"
        cost = f"{record.costMicros}微元" if record.costMicros is not None else "未知"
        chain = f"角色{record.role}→结构化项目工具→持久结果；实际重启核对{'通过' if record.restartVerified else '未完成'}；任务{record.runId}；调用{record.callId}；供应商预估成本{cost}"
        rows.append(f"| {_time(record.startedAt)} | {chain} | {record.requestId or '-'} | {record.inputTokens if record.inputTokens is not None else '未知'} | {record.outputTokens if record.outputTokens is not None else '未知'} | N/A（非流式） | {elapsed} | {'通过' if record.structureValid else '未通过'} | {record.retryCount} | {record.result} |")
    return "\n".join(rows) + "\n"


def load_agent_evidence() -> dict:
    path = DIRECTORY / "evidence-registry.json"
    if not path.exists():
        if EVIDENCE.exists():
            raise ValueError("阶段2-2已有记录但缺少机器调用索引")
        return {"schemaVersion": 1, "records": []}
    registry = json.loads(path.read_text(encoding="utf-8"))
    if set(registry) != {"schemaVersion", "records", "markdownSha256"} or registry["schemaVersion"] != 1 or not isinstance(registry["records"], list):
        raise ValueError("阶段2-2真实调用索引结构无效")
    content = EVIDENCE.read_bytes()
    rendered = render_agent_evidence(registry["records"], legacy_product_label=uses_legacy_evidence_product(content)).encode()
    if hashlib.sha256(content).hexdigest() != registry["markdownSha256"] or content != rendered:
        raise ValueError("阶段2-2正式记录、完整哈希与实际调用不一致")
    identities, requests = set(), set()
    proofs = {}
    for record in registry["records"]:
        if record["callId"] in identities:
            raise ValueError("阶段2-2出现重复实际调用")
        identities.add(record["callId"])
        request_key = (record["provider"], record["requestId"])
        if record["requestId"] and request_key in requests:
            raise ValueError("同一供应商请求被重复登记")
        requests.add(request_key)
        proof_path = (ROOT / record["proofPath"]).resolve()
        if not proof_path.is_relative_to(ROOT / ".intermediate") or not proof_path.is_file() or proof_path.stat().st_mode & 0o077:
            raise ValueError("实际调用证明文件缺失、越界或权限超过0600")
        if str(proof_path) not in proofs:
            content = proof_path.read_bytes()
            proofs[str(proof_path)] = (hashlib.sha256(content).hexdigest(), json.loads(content))
        proof_hash, proof = proofs[str(proof_path)]
        base = {key: value for key, value in record.items() if key not in {"proofPath", "proofSha256", "registeredAt"}}
        if proof_hash != record["proofSha256"] or base not in proof["calls"]:
            raise ValueError("实际调用证明与已登记调用不一致")
    return registry


def register_agent_evidence(records: list[dict], proof_path: Path) -> int:
    selected = proof_path.resolve()
    if not selected.is_relative_to(ROOT / ".intermediate") or selected.stat().st_mode & 0o077:
        raise ValueError("实际调用证明文件必须在本产品中间目录并使用0600权限")
    calls = [AgentCallEvidence.model_validate(record).model_dump() for record in records]
    content = selected.read_bytes()
    proof = json.loads(content)
    if proof.get("calls") != calls:
        raise ValueError("待登记调用与真实验证证明不一致")
    DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(DIRECTORY / "evidence.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = load_agent_evidence()
        entries = list(registry["records"])
        added = 0
        for call in calls:
            existing = next((entry for entry in entries if entry["callId"] == call["callId"] or call["requestId"] is not None and (entry["provider"], entry["requestId"]) == (call["provider"], call["requestId"])), None)
            if existing is not None:
                base = {key: value for key, value in existing.items() if key not in {"proofPath", "proofSha256", "registeredAt"}}
                if base != call:
                    raise ValueError("同一真实调用的证据发生变化")
                continue
            entries.append({**call, "proofPath": str(selected.relative_to(ROOT)), "proofSha256": hashlib.sha256(content).hexdigest(), "registeredAt": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")})
            added += 1
        if not added:
            return 0
        rendered = render_agent_evidence(entries).encode()
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        pending = DIRECTORY / "evidence.markdown.pending"
        descriptor = os.open(pending, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(rendered)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(pending, EVIDENCE)
        write_private_json(DIRECTORY / "evidence-registry.json", {"schemaVersion": 1, "records": entries, "markdownSha256": hashlib.sha256(rendered).hexdigest()})
        return added
