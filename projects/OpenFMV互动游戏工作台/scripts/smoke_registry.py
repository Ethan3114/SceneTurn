import fcntl
import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field


class ReverificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    verifiedAt: str = Field(min_length=1)
    generationIds: list[str] = Field(min_length=1)
    proofSha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schemaVersion: Literal[1, 2]
    records: list[dict]
    markdownSha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reverification: ReverificationModel | None = None


def record_kind(record: dict) -> str:
    if record.get("kind") == "text":
        return "text"
    if record.get("provider") == "volcengine-ark" and "media" in record and record.get("remoteTaskId"):
        return "seedance"
    if record.get("provider") not in {None, "aliyun-wan"}:
        raise ValueError("视频记录缺少受支持的供应商")
    if "media" in record and record.get("remoteTaskId"):
        return "wan"
    raise ValueError("真实调用记录缺少受支持的模型类型")


def validate_record(record: dict) -> None:
    kind = record_kind(record)
    for key in ("generationId", "model", "startedAt", "finishedAt", "verifiedAt"):
        value = record.get(key)
        if not isinstance(value, str) or not value or any(character in value for character in ("|", "\r", "\n")):
            raise ValueError("真实调用记录的标识或时间无效")
    started, finished = datetime.fromisoformat(record["startedAt"]), datetime.fromisoformat(record["finishedAt"])
    elapsed = record.get("elapsedSeconds")
    if finished < started or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed != (finished - started).total_seconds():
        raise ValueError("真实调用记录的开始、结束与耗时不一致")
    request_id = record.get("remoteRequestId")
    if request_id is not None and (not isinstance(request_id, str) or any(character in request_id for character in ("|", "\r", "\n"))):
        raise ValueError("真实调用记录的请求标识无效")
    if not isinstance(record.get("usage"), dict):
        raise ValueError("真实调用记录没有结构化用量")
    if kind == "text" and (record.get("provider") != "deepseek" or not request_id or record.get("restartVerified") is not True):
        raise ValueError("真实文本记录缺少供应商请求或实际重启验证")
    if kind == "seedance":
        tokens = record["usage"].get("completion_tokens")
        actual, quoted = record.get("actualCents"), record.get("quotedCents")
        if (not record["model"].startswith("doubao-seedance-") or not request_id or record.get("restartVerified") is not True
                or type(tokens) is not int or tokens <= 0 or type(actual) is not int or type(quoted) is not int or not 0 < actual <= quoted):
            raise ValueError("真实方舟视频记录缺少有效的模型回执、费用结算或实际重启验证")


def write_private_json(path: Path, value: dict) -> None:
    pending = path.with_suffix(path.suffix + ".pending")
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        json.dump(value, destination, ensure_ascii=False, indent=2, allow_nan=False)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(pending, path)


def render_evidence(records: list[dict], *, legacy_ui_label: bool = False, stage: str = "阶段2-1", reverification: dict | None = None) -> str:
    if not records:
        raise ValueError("真实调用记录不能为空")
    for record in records:
        validate_record(record)
    wan_models = sorted({record["model"] for record in records if record_kind(record) == "wan"})
    text_models = sorted({record["model"] for record in records if record_kind(record) == "text"})
    seedance_models = sorted({record["model"] for record in records if record_kind(record) == "seedance"})
    labels = []
    if wan_models:
        labels.append(f"阿里云 / {', '.join(wan_models)}")
    if text_models:
        labels.append(f"DeepSeek / {', '.join(text_models)}")
    if seedance_models:
        labels.append(f"火山方舟 / {', '.join(seedance_models)}")
    written_at = reverification["verifiedAt"] if stage == "阶段3-2" and reverification else records[-1]["verifiedAt"]
    rows = [
        "# 冒烟记录（冒烟脚本自动写入，不得手填）",
        "- 产品：OpenFMV互动游戏工作台",
        f"- 阶段：{stage}",
        f"- 模型：{'；'.join(labels)}",
        f"- 写入时间：{written_at}",
        "",
        "| 时间 | 链路 | 请求 ID | 输入 token | 输出 token | 首字耗时 | 总耗时 | 结构校验 | 重试次数 | 结果 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        started = datetime.fromisoformat(record["startedAt"]).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
        finished = datetime.fromisoformat(record["finishedAt"]).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
        if record_kind(record) == "wan":
            if stage == "阶段3-2":
                chain = f"原真实万相成片→当前隔离环境播放、项目导出和重启复核；任务{record['generationId']}；原调用开始{started}；完成{finished}"
            elif stage == "阶段2-4":
                chain = f"原真实Wan成片→当前完整项目与固定版本导出→独立播放及重启核对；任务{record['generationId']}；原调用开始{started}；完成{finished}"
            elif stage == "阶段2-3":
                chain = f"本人确认的连续首尾帧Wan生成→实际媒体→指定片段采用→重启恢复；任务{record['generationId']}；开始{started}；完成{finished}"
            else:
                chain = f"本人UI确认的Wan生成→持久任务→媒体→场景采用；任务{record['generationId']}；开始{started}；完成{finished}"
            input_tokens = record["usage"].get("input_tokens", "N/A（视频）")
            output_tokens = record["usage"].get("output_tokens", "N/A（视频）")
            first_token = "N/A（异步视频）"
        elif record_kind(record) == "seedance":
            chain = f"Seedance视频生成→供应商回执→费用结算→媒体采用及实际重启恢复；模型{record['model']}；任务{record['generationId']}；开始{started}；完成{finished}；结算{record['actualCents']}分"
            input_tokens = record["usage"].get("prompt_tokens", "N/A（供应商未返回）")
            output_tokens = record["usage"]["completion_tokens"]
            first_token = "N/A（异步视频）"
        else:
            origin = "本人UI请求的" if legacy_ui_label else "本人授权的"
            chain = f"{origin}DeepSeek文本生成→结构校验→Token账本→固定文字节点→实际重启恢复；模型{record['model']}；任务{record['generationId']}；开始{started}；完成{finished}；供应商预估成本{record['costMicros']}微元"
            input_tokens, output_tokens = record["usage"]["prompt_tokens"], record["usage"]["completion_tokens"]
            first_token = "N/A（非流式文本）"
        rows.append(f"| {started} | {chain} | {record.get('remoteRequestId') or '-'} | {input_tokens} | {output_tokens} | {first_token} | {record['elapsedSeconds']:.3f}s | 通过 | {record['retryCount']} | 通过 |")
    if reverification is not None:
        review = ReverificationModel.model_validate(reverification)
        description = {
            "阶段2-4": "原始任务、真实媒体、完整项目、来源映射、固定版本、离线按钮和QTE分支及实际重启恢复。",
            "阶段3-2": "原始任务和媒体摘要、桌面播放、完整项目备份、固定版本、互动分支及隔离服务实际重启恢复。",
        }.get(stage, "原始任务、真实媒体、浏览器播放、候选、裁切、互动、共享引用及实际重启恢复。")
        rows.extend(["", f"- 原任务复核时间：{review.verifiedAt}",
            f"- 复核任务数：{len(review.generationIds)}；实际调用时间、请求标识及用量保留原值。",
            f"- 复核内容：{description}",
            f"- 复核证据摘要：{review.proofSha256}"])
        if stage == "阶段3-2":
            rows.append("- 本次复核新增模型请求：0。")
    return "\n".join(rows) + "\n"


class EvidenceStore:
    def __init__(self, root: Path, stage: str = "阶段2-1"):
        directories = {"阶段2-1": "smoke", "阶段2-3": "smoke-media-candidates",
                       "阶段2-4": "smoke-project-transfers", "阶段3-1": "sceneturn-smoke", "阶段3-2": "sceneturn-release-smoke"}
        if stage not in directories:
            raise ValueError("不支持的媒体验证阶段")
        self.root = root.resolve()
        self.stage = stage
        self.registry_path = self.root / ".intermediate" / directories[stage] / "registry.json"
        self.evidence_path = self.root / "docs" / "evidence" / stage / "冒烟记录.md"

    def directory(self, record: dict) -> Path:
        identity = f"text:{record['generationId']}" if record_kind(record) == "text" else record["generationId"]
        return self.registry_path.parent / hashlib.sha256(identity.encode()).hexdigest()

    def load(self) -> dict:
        if not self.registry_path.exists():
            if self.evidence_path.exists():
                raise ValueError("已有冒烟记录缺少配套真实调用索引")
            return {"schemaVersion": 2, "records": []}
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        RegistryModel.model_validate(registry)
        if not self.evidence_path.is_file():
            raise ValueError("真实调用索引缺少正式冒烟记录")
        content = self.evidence_path.read_bytes()
        canonical = render_evidence(registry["records"], stage=self.stage, reverification=registry.get("reverification")).encode()
        legacy = render_evidence(registry["records"], legacy_ui_label=True, stage=self.stage, reverification=registry.get("reverification")).encode()
        if hashlib.sha256(content).hexdigest() != registry["markdownSha256"] or content not in (canonical, legacy):
            raise ValueError("冒烟记录、机器索引和真实调用渲染不一致")
        identities: set[tuple] = set()
        for record in registry["records"]:
            validate_record(record)
            key = (record_kind(record), record["generationId"])
            if key in identities:
                raise ValueError("真实调用索引中存在重复任务")
            identities.add(key)
            sidecar_path = self.directory(record) / "verification.json"
            if not sidecar_path.is_file() or sidecar_path.stat().st_mode & 0o077:
                raise ValueError("真实任务验证文件不存在或权限超过0600")
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if sidecar != record:
                raise ValueError("任务验证文件与已登记的真实调用不一致")
        if registry.get("reverification") is not None:
            review = registry["reverification"]
            proof = self.registry_path.parent / "reverification.json"
            if set(review["generationIds"]) != {record["generationId"] for record in registry["records"]} or not proof.is_file() or proof.stat().st_mode & 0o077 or hashlib.sha256(proof.read_bytes()).hexdigest() != review["proofSha256"]:
                raise ValueError("原任务复核记录与私有证据不一致")
        return registry

    def revalidate(self, verified_records: list[dict], proof: dict) -> None:
        if self.stage not in {"阶段2-3", "阶段2-4", "阶段3-2"}:
            raise ValueError("此阶段未定义原任务媒体复核")
        if self.stage == "阶段3-2" and (proof.get("restartVerified") is not True or proof.get("newModelRequests") != 0):
            raise ValueError("发布阶段须提供隔离服务实际重启及未新增模型请求的复核证据")
        self.registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.registry_path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            registry = self.load()
            source_records = EvidenceStore(self.root, "阶段2-4").load()["records"] if self.stage == "阶段3-2" else registry["records"]
            if registry["records"] and registry["records"] != source_records:
                raise ValueError("本阶段记录与已经登记的原始任务不一致")
            previous = {record["generationId"]: record for record in source_records}
            if not previous or len(verified_records) != len(previous) or {record["generationId"] for record in verified_records} != set(previous):
                raise ValueError("原任务复核必须覆盖全部已登记的真实调用")
            excluded = {"verifiedAt", "restartVerification"}
            for record in verified_records:
                validate_record(record)
                if {key: value for key, value in record.items() if key not in excluded} != {key: value for key, value in previous[record["generationId"]].items() if key not in excluded}:
                    raise ValueError("复核的真实调用与原始登记不一致")
            verified_at = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
            proof_path = self.registry_path.parent / "reverification.json"
            write_private_json(proof_path, {"verifiedAt": verified_at, "records": verified_records, "proof": proof})
            review = {"verifiedAt": verified_at, "generationIds": list(previous), "proofSha256": hashlib.sha256(proof_path.read_bytes()).hexdigest()}
            rendered = render_evidence(source_records, stage=self.stage, reverification=review)
            if self.stage == "阶段3-2" and not registry["records"]:
                for record in source_records:
                    directory = self.directory(record)
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    write_private_json(directory / "verification.json", record)
            pending = self.registry_path.with_suffix(".markdown.pending")
            pending.write_text(rendered, encoding="utf-8")
            self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(pending, self.evidence_path)
            write_private_json(self.registry_path, {**registry, "records": source_records, "reverification": review, "markdownSha256": hashlib.sha256(rendered.encode()).hexdigest()})

    def register(self, record: dict) -> bool:
        if self.stage == "阶段3-2":
            raise ValueError("发布阶段原任务须与当前端到端和重启证据一起复核")
        validate_record(record)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.registry_path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            registry = self.load()
            for previous in registry["records"]:
                same_task = previous["generationId"] == record["generationId"]
                same_request = record_kind(previous) == record_kind(record) and record.get("remoteRequestId") is not None and previous.get("remoteRequestId") == record["remoteRequestId"]
                same_remote_task = (record_kind(record) in {"wan", "seedance"} and record_kind(previous) == record_kind(record)
                    and previous["remoteTaskId"] == record["remoteTaskId"])
                if same_task or same_request or same_remote_task:
                    excluded = {"verifiedAt", "restartVerification"}
                    identity = {key: value for key, value in record.items() if key not in excluded}
                    saved = {key: value for key, value in previous.items() if key not in excluded}
                    if identity != saved:
                        raise ValueError("已登记的同一真实任务证据发生变化")
                    print(f"已登记真实任务复核通过：{record['generationId']}；原调用开始于{previous['startedAt']}，完成于{previous['finishedAt']}")
                    return False
            records = [*registry["records"], record]
            rendered = render_evidence(records, stage=self.stage)
            directory = self.directory(record)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
            write_private_json(directory / "verification.json", record)
            pending = self.registry_path.with_suffix(".markdown.pending")
            pending.write_text(rendered, encoding="utf-8")
            os.replace(pending, self.evidence_path)
            write_private_json(self.registry_path, {"schemaVersion": 2, "records": records, "markdownSha256": hashlib.sha256(rendered.encode()).hexdigest()})
            return True
