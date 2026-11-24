import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from smoke_registry import EvidenceStore, render_evidence, write_private_json


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_FILE = PRODUCT_ROOT / "docs" / "evidence" / "阶段2-1" / "冒烟记录.md"


class ComposedTextInput(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    nodeId: str = Field(min_length=1)
    type: Literal["text"]
    title: str
    text: str
    label: str


class ComposedInput(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    inputCompositionVersion: Literal[1]
    sourcePrompt: str
    prompt: str = Field(min_length=1, max_length=20000)
    inputs: list[ComposedTextInput]
    outputNodeId: str | None = None

    @model_validator(mode="after")
    def check_inputs(self):
        if not self.prompt.strip():
            raise ValueError("实际组装提示词为空")
        if len({item.nodeId for item in self.inputs}) != len(self.inputs):
            raise ValueError("已采用的文字来源包含重复节点")
        if [item.label for item in self.inputs] != [f"文本{index + 1}" for index in range(len(self.inputs))]:
            raise ValueError("文字来源编号与实际采用顺序不一致")
        return self


class GenerationBinding(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    projectId: str
    sceneId: str
    sourceNodeId: str = Field(min_length=1)
    outputNodeId: str | None = None
    projectRevision: int = Field(ge=1)
    input: dict


class GenerationEvidence(GenerationBinding):
    id: str
    clipId: str
    status: Literal["ready"]
    remoteTaskId: str = Field(min_length=1)
    remoteRequestId: str | None
    assetId: str = Field(min_length=1)
    usage: dict
    retryCount: int = Field(ge=0)
    startedAt: datetime
    finishedAt: datetime

    @model_validator(mode="after")
    def check_evidence(self):
        if self.finishedAt < self.startedAt:
            raise ValueError("真实任务结束时间早于开始时间")
        if self.input.get("provider") not in {None, "aliyun-wan"}:
            raise ValueError("该脚本只登记阿里云万相，Seedance 请使用 smoke_record_seedance.py")
        if not self.usage:
            raise ValueError("真实任务尚无供应商用量记录")
        if not self.input.get("model"):
            raise ValueError("真实任务缺少冻结的模型标识")
        if "inputCompositionVersion" in self.input:
            composed = ComposedInput.model_validate(self.input)
            if self.input["request"]["prompt"] != composed.sourcePrompt:
                raise ValueError("原始请求与冻结的来源提示词不一致")
            if self.outputNodeId != composed.outputNodeId:
                raise ValueError("任务与冻结输入的结果节点不一致")
        return self


def session_cookies(cookie_file: Path, base_url: str) -> httpx.Cookies:
    path = cookie_file.resolve(strict=True)
    if not path.is_relative_to(PRODUCT_ROOT / ".intermediate"):
        raise ValueError("会话文件必须保存在本产品被忽略的.intermediate目录内")
    if path.stat().st_mode & 0o077:
        raise ValueError("会话文件权限必须为0600")
    state = json.loads(path.read_text(encoding="utf-8"))
    hostname = urlparse(base_url).hostname
    cookies = httpx.Cookies()
    for cookie in state["cookies"]:
        if cookie["name"] == "openfmv_session" and cookie["domain"].lstrip(".") == hostname:
            cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
    if not any(cookie.name == "openfmv_session" for cookie in cookies.jar):
        raise ValueError("会话文件中没有与目标服务匹配的本人会话")
    return cookies


def validate_media(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    probe = json.loads(result.stdout)
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    if int(video["width"]) <= 0 or int(video["height"]) <= 0 or float(probe["format"]["duration"]) <= 0:
        raise ValueError("真实生成文件缺少有效的视频尺寸与时长")
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], check=True, capture_output=True)
    return {
        "durationSeconds": float(probe["format"]["duration"]),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def validate_adopted_clip(project: dict, scene_id: str, clip_id: str, asset_id: str) -> None:
    scene = next((node for node in project["graphData"]["nodes"] if node["id"] == scene_id and node["type"] == "scene"), None)
    if scene is None:
        raise ValueError("指定任务的原场景未出现在当前作品")
    timeline = scene["data"].get("timeline")
    if not isinstance(timeline, dict) or timeline.get("version") != 2:
        raise ValueError("采用结果未写入NodeTimeline v2")
    clip = next((clip for track in timeline["tracks"] for clip in track["clips"] if clip["id"] == clip_id), None)
    if clip is None or clip.get("assetId") != asset_id:
        raise ValueError("固定目标片段引用的素材与真实生成任务不一致")
    if not any(asset["id"] == asset_id for asset in project["assets"]):
        raise ValueError("真实生成素材尚未登记至所属项目")


def validate_adoption(project: dict, generation: GenerationEvidence) -> None:
    validate_adopted_clip(project, generation.sceneId, generation.clipId, generation.assetId)


def validate_canvas_binding(canvases: list[dict], generation: GenerationBinding, *, output_type: str = "video") -> None:
    if "inputCompositionVersion" not in generation.input:
        return
    canvas = next(item for item in canvases if item["sceneId"] == generation.sceneId)
    source = next(item for item in canvas["nodes"] if item["id"] == generation.sourceNodeId)
    metadata = source.get("metadata", {})
    source_prompt = metadata.get("composerContent")
    if source_prompt is None:
        source_prompt = metadata.get("prompt")
    if source_prompt is None:
        source_prompt = source.get("prompt", metadata.get("content", "") if source.get("type") == "text" else "")
    if source_prompt != generation.input["sourcePrompt"]:
        raise ValueError("来源节点的真实历史原文与冻结输入不一致")
    for item in generation.input["inputs"]:
        historical_node = next(node for node in canvas["nodes"] if node["id"] == item["nodeId"])
        historical_metadata = historical_node.get("metadata", {})
        if historical_node["type"] != "text" or item["text"] != (historical_metadata.get("content") or historical_metadata.get("prompt")):
            raise ValueError("已采用文字与真实历史节点的类型或正文不一致")
    if generation.outputNodeId is not None:
        output = next(item for item in canvas["nodes"] if item["id"] == generation.outputNodeId)
        if output["id"] == source["id"] or output["type"] != output_type:
            raise ValueError("真实任务的结果节点需要绑定同场景的独立结果节点")


def verify_local_data(data_directory: Path, task_id: str, owner_id: str | None = None) -> tuple[GenerationEvidence, Path]:
    sys.path.insert(0, str(PRODUCT_ROOT / "backend"))
    from app.models import Asset, Generation, Project, ProjectVersion, User

    directory = (PRODUCT_ROOT / data_directory).resolve(strict=True)
    if not directory.is_relative_to(PRODUCT_ROOT):
        raise ValueError("只读验证的数据目录必须位于本产品目录内")
    database_file = (directory / "openfmv.sqlite3").resolve(strict=True)
    if not database_file.is_relative_to(directory):
        raise ValueError("真实数据库文件必须位于明确指定的数据目录内")
    engine = create_engine(f"sqlite:///file:{database_file}?mode=ro&uri=true")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA query_only = ON")
        with Session(bind=connection, autoflush=False) as session:
            task = session.get(Generation, task_id)
            if task is None:
                raise ValueError("该数据目录中不存在指定的真实任务")
            if owner_id is not None and task.owner_id != owner_id:
                raise ValueError("真实历史任务与正常HTTP会话的账号不一致")
            if task.status != "ready" or not task.remote_task_id or not task.asset_id:
                raise ValueError("指定任务尚未完成真实生成与媒体保存")
            project = session.get(Project, task.project_id)
            asset = session.get(Asset, task.asset_id)
            owner = session.get(User, task.owner_id)
            if project is None or asset is None or owner is None:
                raise ValueError("真实任务的账号、项目或素材记录不完整")
            if project.owner_id != owner.id or asset.owner_id != owner.id or asset.project_id != project.id or asset.generation_id != task.id:
                raise ValueError("真实任务、项目、账号与生成素材的归属不一致")
            generation = GenerationEvidence.model_validate({
                "id": task.id,
                "projectId": task.project_id,
                "sceneId": task.scene_id,
                "sourceNodeId": task.source_node_id,
                "outputNodeId": task.input_json.get("outputNodeId"),
                "projectRevision": task.project_revision,
                "clipId": task.clip_id,
                "status": task.status,
                "remoteTaskId": task.remote_task_id,
                "remoteRequestId": task.remote_request_id,
                "assetId": task.asset_id,
                "input": task.input_json,
                "usage": {key: value for key, value in task.usage_json.items() if key != "resultUrl"},
                "retryCount": task.retry_count,
                "startedAt": task.started_at,
                "finishedAt": task.finished_at,
            })
            validate_adoption(project.data_json, generation)
            if "inputCompositionVersion" in task.input_json:
                history = session.scalar(select(ProjectVersion).where(ProjectVersion.project_id == task.project_id, ProjectVersion.revision == task.project_revision, ProjectVersion.owner_id == task.owner_id))
                if history is None or history.data_json["id"] != task.project_id:
                    raise ValueError("真实任务缺少报价时项目版本的历史快照")
                validate_canvas_binding(history.canvases_json, generation)
            media_path = (directory / asset.relpath).resolve(strict=True)
            if not media_path.is_relative_to(directory / "media") or not media_path.is_file():
                raise ValueError("生成素材文件不在该数据目录的媒体范围内")
            if media_path.stat().st_size != asset.bytes or hashlib.sha256(media_path.read_bytes()).hexdigest() != asset.sha256:
                raise ValueError("真实生成文件的大小或哈希与数据库记录不一致")
    engine.dispose()
    return generation, media_path


def write_evidence(generation: GenerationEvidence, media: dict, directory: Path) -> bool:
    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    model = str(generation.input["model"])
    if "|" in model or "\n" in model:
        raise ValueError("模型标识包含无效字符")
    request_id = generation.remoteRequestId or "-"
    if "|" in request_id or "\n" in request_id:
        raise ValueError("供应商请求标识包含无效字符")
    seconds = (generation.finishedAt - generation.startedAt).total_seconds()
    verification = {
        "verifiedAt": timestamp,
        "generationId": generation.id,
        "remoteTaskId": generation.remoteTaskId,
        "remoteRequestId": generation.remoteRequestId,
        "projectId": generation.projectId,
        "sceneId": generation.sceneId,
        "clipId": generation.clipId,
        "assetId": generation.assetId,
        "model": model,
        "usage": generation.usage,
        "retryCount": generation.retryCount,
        "startedAt": generation.startedAt.isoformat(),
        "finishedAt": generation.finishedAt.isoformat(),
        "elapsedSeconds": seconds,
        "media": media,
    }
    store = EvidenceStore(PRODUCT_ROOT)
    if directory.resolve() != store.directory(verification).resolve():
        raise ValueError("真实任务证据目录与索引约定不一致")
    return store.register(verification)


def main() -> None:
    parser = argparse.ArgumentParser(description="只读取本人已确认的真实Wan任务、媒体及采用结果，自动写入验证记录")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="本产品本人数据与历史目录，SQLite以只读模式打开；默认data")
    parser.add_argument("--cookie-file", type=Path, help="权限0600的Playwright storageState JSON，位于.intermediate；新组合任务同时核对本机data-dir历史")
    parser.add_argument("--base-url", default="http://127.0.0.1:8310/api/v1")
    parser.add_argument("--wait-seconds", type=int, default=0)
    arguments = parser.parse_args()
    directory = PRODUCT_ROOT / ".intermediate" / "smoke" / hashlib.sha256(arguments.task_id.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    if arguments.cookie_file is None:
        generation, media_path = verify_local_data(arguments.data_dir, arguments.task_id)
        media = validate_media(media_path)
        if write_evidence(generation, media, directory):
            print(f"本人数据目录中的真实任务验证通过：{generation.id}；首次调用证据已由脚本写入{EVIDENCE_FILE.relative_to(PRODUCT_ROOT)}")
        return
    cookies = session_cookies(arguments.cookie_file, arguments.base_url)
    with httpx.Client(base_url=arguments.base_url, cookies=cookies, headers={"Origin": "http://127.0.0.1:3210"}, timeout=60) as service:
        me = service.get("/auth/me")
        me.raise_for_status()
        owner_id = me.json()["user"]["id"]
        deadline = time.monotonic() + arguments.wait_seconds
        while True:
            response = service.get(f"/generations/{arguments.task_id}")
            response.raise_for_status()
            task = response.json()["generation"]
            if task["status"] == "ready":
                break
            if task["status"] in {"failed", "submission_unknown"} or time.monotonic() >= deadline:
                raise RuntimeError(f"真实任务尚未可用，当前状态：{task['status']}")
            time.sleep(2)
        generation = GenerationEvidence.model_validate(task)
        if generation.id != arguments.task_id:
            raise ValueError("接口返回的任务标识与请求不一致")
        project_response = service.get(f"/projects/{generation.projectId}")
        project_response.raise_for_status()
        envelope = project_response.json()
        validate_adoption(envelope["project"], generation)
        if "inputCompositionVersion" in generation.input:
            historical_generation, _ = verify_local_data(arguments.data_dir, arguments.task_id, owner_id)
            if historical_generation != generation:
                raise ValueError("正式HTTP任务与本人数据目录的真实任务不一致")
        response = service.get(f"/assets/{generation.assetId}/content")
        response.raise_for_status()
        media_path = directory / "generated-video.mp4"
        descriptor = os.open(media_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(response.content)
    media = validate_media(media_path)
    if write_evidence(generation, media, directory):
        print(f"真实任务验证通过：{generation.id}；首次调用证据已由脚本写入{EVIDENCE_FILE.relative_to(PRODUCT_ROOT)}")


if __name__ == "__main__":
    main()
