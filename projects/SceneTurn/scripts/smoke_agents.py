import argparse
import hashlib
import html
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

import httpx
import jsonpatch
from jsonpointer import resolve_pointer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agent_schemas import AgentDecision, RunLimits
from app.agent_models import AgentRun
from app.core.config import Settings, sqlite_database_path
from app.core.errors import AppError
from app.core.security import decode_secret
from app.models import Budget, BudgetReservation, TextProviderSettings
from app.services.projects import lock_write
from app.services.agent import stop_run
from app.services.agent_provider import DEFAULT_AGENT_PROTOCOL, JSON_AGENT_PROTOCOL, THINKING_JSON_AGENT_PROTOCOL, SUPPORTED_PLATFORM_PROTOCOLS, agent_business_tools, agent_request_body, agent_response_schema, agent_tools, decode_platform_message, is_json_protocol, provider_endpoint, strict_schema
from app.creative_artifacts import ARTIFACT_CONTRACT_VERSION, SUPPORTED_ARTIFACT_CONTRACT_VERSIONS, artifact_arguments_schema, image_prompt_items, render_artifact, validate_artifact
from app.services.text_generation import _reserve
from app.services.text_provider import MODEL, Usage, cost_micros, current_price, model_aliases
from app.tools.project_tools import LEGACY_TOOL_CATALOG_VERSION, TOOL_CATALOG_VERSION
from agent_smoke_evidence import AgentCallEvidence, digest, load_agent_evidence, register_agent_evidence, write_private_json
from smoke_text import start_service, stop_service, within_product


ROLES = {
    "writer": ("编剧", "交付可以据以制作的完整20秒场景剧本：包含故事前提、人物目标、戏剧冲突、场景与时间、按顺序展开的行动和对白、情绪变化、因果关系、结尾和连续性要求。四个5秒镜头的动作与对白应在20秒内可执行。说明两个互动选择分别造成的后续变化，明确哪些内容来自已有设定、哪些内容为创作补充。"),
    "character_designer": ("角色设计师", "分别交付林秋、陈伯的专业角色设定，明确年龄、体态、脸部特征、发型、肤色、服装层次与材质、固定颜色、随身物件、性格在动作中的表现，以及镜头之间必须保持的识别特征。每个人物交付一份三视图参考设定图的完整正向提示词、负向提示词和生成规格，共两份。提示词覆盖主体、构图、视图排布、姿态表情、光线、色彩、画面风格与禁止项，能直接用于后续一致性制作。"),
    "scene_designer": ("场景设计师", "交付雨夜南站候车室的完整空间和美术设计：明确入口、窗户、长椅、修表位置与人物活动区域的关系，列出固定道具及摆放，描述材质、雨水痕迹、主辅光源、色温、明暗层次和色彩。场景全景和怀表关键道具各提供一份可直接用于生图的完整正向提示词、负向提示词和生成规格，共两份提示词，并明确跨镜头连续性要求。"),
    "storyboarder": ("分镜师", "把本场景组织为S01至S04四个镜头，每镜5秒，总时长20秒；逐镜写明编号、时长、景别、机位、镜头运动、构图、人物位置、动作、表情、对白或声音、转场和连续性。镜头依次覆盖环境建立、怀表线索、两人交谈与互动决定，每个镜头提供可直接生图的完整正向提示词、负向提示词和生成规格，包含固定人物特征与场景特征，不使用‘同上’或依赖未提供的外部提示词。"),
    "media_producer": ("影像制作师", "本次交付S02怀表线索特写、S03两人交谈中景两个关键镜头的完整媒体制作任务。每镜分别填写完整生图提示词、完整视频提示词、输入参考要求、模型能力要求、画幅、分辨率、5秒时长、数量、负向提示词、连续性约束与质量检查。制作策略说明人物参考图、场景与道具参考图、分镜静帧及图生视频的依赖，并覆盖S01和S04的素材输入要求。明确实际素材尚未生成，外部媒体请求需本人确认；本次只保存专业制作说明。"),
    "editor": ("剪辑师", "交付S01至S04四个镜头的完整剪辑方案，每段5秒、总时长20秒：包含镜头顺序、时间范围、叙事节奏、动作与视线匹配、声音衔接、转场、互动选择出现和消失的时机、分支衔接规则及成片规格。区分已有资料和待生成素材，列出素材准备与剪辑检查要求；本次只保存方案，不启动拼接或修改媒体。"),
    "interaction_designer": ("互动设计师", "交付可供互动实现的详细方案：说明玩家目标、两项选择的按钮文案与各自后果、显示时机、选择时限与超时规则、状态变量、进入条件、退出条件、分支合流及重复进入的处理。保证选择具有剧情意义，明确目标场景尚待建立时的引用状态，给出边界情况和验收步骤，不虚构已经存在的对象标识。"),
    "sound_designer": ("声音设计师", "交付完整声音设计：逐项安排林秋与陈伯的声线、对白演绎、雨声与车站环境声、怀表机械声、必要的音乐及互动反馈，写明进入退出时机、时长或循环、音量层次、情绪与画面关系。提供可交给配音、音乐和音效工具的具体提示词与规格，说明授权和素材来源待确认事项；本次不调用音频模型。"),
    "director": ("创作总监", "交付本场景的专业创作方案：包括创作意图、故事与人物关系、戏剧节拍、视觉基调、人物和场景一致性原则、镜头重点、互动设计目标、声音要求、各角色交付物及先后依赖、风险与具体检查标准。安排从剧本到人物和场景设计、分镜图、媒体制作、剪辑及互动的工作顺序，明确当前已有设定与待制作素材；本次完成方案后独立核对已保存产物。"),
}
SETTING = "故事发生在雨夜的南站候车室。修表师名叫林秋，老人名叫陈伯，场景只有这两位主要人物。以下视觉身份由本次验证作者提供，属于验证输入约定：林秋为28岁男性，黑色短发，穿灰绿色工装夹克、黑色高领衫、深色工装裤和棕色短靴，工具卷包固定在右腰；陈伯为68岁男性，银色短发，穿藏青羊毛大衣、灰蓝衬衫、深灰毛背心、灰色裤子和黑色皮鞋，携带收起的木柄伞。怀表为旧黄铜圆壳、泛黄表盘、无字刻度和旧金属表链。陈伯带来这只指针倒着走的旧机械怀表，请林秋检查；故障的原因应符合机械常识，两人都没有超能力。人物通过检查怀表和交谈显露对离别的不同态度，结尾由玩家决定继续询问怀表往事，或者先着手修理。本次主场景成片20秒，S01环境建立、S02怀表线索特写、S03两人交谈中景、S04互动决定前的双人画面，四镜各5秒。S04结束时画面停留并显示两项选择，选择等待与后续分支分别设计。影片采用写实真人影视风格、16:9横向画幅；雨夜、人物服装、怀表外观和候车室空间关系在镜头之间保持一致。其余美术细节、动作和对白可以专业创作，并在assumptions中记录。当前只有文字设定，没有已生成的图片、视频、音频或已建立的分支目标。"
RUN_LIMITS = {"maxSteps": 12, "maxTokens": 32768, "maxCostMicros": 10000000}
MAXIMUM_MICROS = len(ROLES) * RUN_LIMITS["maxCostMicros"]
TERMINAL = {"completed", "failed", "stopped", "conflict", "submission_unknown", "waiting_confirmation", "waiting_connection"}
TABLES = ("agent_runs", "agent_steps", "agent_calls", "budget_reservations", "budgets", "projects", "project_versions", "project_memories", "canvases", "operations", "assets", "generations", "text_generations", "text_usage_ledger")


class RevisionFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    schemaVersion: Literal[1]
    sourceVerificationId: str
    roles: dict[str, Annotated[list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]], Field(min_length=1, max_length=30)]]

    @field_validator("sourceVerificationId")
    @classmethod
    def valid_identity(cls, value):
        if str(uuid.UUID(value)) != value:
            raise ValueError("反馈中的原验证ID必须使用规范UUID")
        return value

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, value):
        if not value or not set(value) <= set(ROLES):
            raise ValueError("反馈须指定有效的专业角色")
        return value


def utc(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


@contextmanager
def read_database(data: Path):
    with closing(sqlite3.connect(f"{sqlite_database_path(data).as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection


def database_snapshot(data: Path) -> dict:
    with read_database(data) as connection:
        snapshot = {}
        for table in TABLES:
            columns = "id, limit_cents, spent_cents, reserved_cents, text_spent_micros, text_reserved_micros" if table == "budgets" else "*"
            snapshot[table] = [dict(row) for row in connection.execute(f"SELECT {columns} FROM {table} ORDER BY id")]
    return snapshot


def accounting(data: Path) -> dict:
    database = sqlite_database_path(data)
    if not database.exists():
        return {"spentMicros": 0, "reservedMicros": 0, "calls": []}
    with read_database(data) as connection:
        budget = connection.execute("SELECT * FROM budgets WHERE id = 'global'").fetchone()
        reservations = [dict(row) for row in connection.execute("SELECT * FROM budget_reservations ORDER BY id")]
        calls = [dict(row) for row in connection.execute("SELECT * FROM agent_calls ORDER BY created_at, id")]
        if any(row["purpose"] != "agent-model" for row in reservations):
            raise ValueError("隔离验收出现预期以外的预算操作")
        spent = sum(row["cost_micros"] for row in reservations)
        reserved = sum(row["reserved_micros"] for row in reservations)
        if budget is None or budget["spent_cents"] != 0 or budget["reserved_cents"] != 0 or (budget["text_spent_micros"], budget["text_reserved_micros"]) != (spent, reserved):
            raise ValueError("隔离Agent预算与实际调用预留不一致")
        if spent != sum(call["cost_micros"] or 0 for call in calls):
            raise ValueError("隔离Agent调用与实际费用账目不一致")
        if {row["id"] for row in reservations} != {call["id"] for call in calls}:
            raise ValueError("隔离Agent调用缺少唯一费用预留")
    return {"spentMicros": spent, "reservedMicros": reserved, "calls": [{"id": call["id"], "runId": call["run_id"], "requestId": call["remote_request_id"], "status": call["status"], "costMicros": call["cost_micros"]} for call in calls]}


def reconcile(source_engine, data: Path, verification_id: str) -> dict:
    actual = accounting(data)
    with Session(source_engine) as session:
        lock_write(session)
        reservation = session.get(BudgetReservation, verification_id)
        if reservation is None or reservation.purpose != "real-agent-smoke":
            raise ValueError("主预算中的Agent验证预留不存在")
        additional = actual["spentMicros"] - reservation.cost_micros
        remaining = actual["reservedMicros"]
        if additional < 0 or additional + remaining > reservation.reserved_micros:
            raise ValueError("实际Agent验证费用超过预留，现有预留继续保留")
        status = "unknown" if remaining else "settled"
        if additional == 0 and reservation.reserved_micros == remaining and reservation.status == status and reservation.details_json.get("actualCalls") == actual["calls"]:
            return {"verificationId": verification_id, **actual}
        changed = session.execute(update(Budget).where(Budget.id == "global", Budget.text_reserved_micros >= reservation.reserved_micros).values(
            text_reserved_micros=Budget.text_reserved_micros - reservation.reserved_micros + remaining,
            text_spent_micros=Budget.text_spent_micros + additional,
        ))
        if changed.rowcount != 1:
            raise ValueError("主预算中的Agent验证结算发生冲突")
        reservation.cost_micros, reservation.reserved_micros = actual["spentMicros"], remaining
        reservation.status, reservation.updated_at = status, time.time()
        reservation.details_json = {**reservation.details_json, "actualCalls": actual["calls"], "reconciledAt": utc(time.time())}
        session.commit()
    return {"verificationId": verification_id, **actual}


def stop_pending_runs(data: Path, verification_id: str) -> None:
    database = sqlite_database_path(data)
    if not database.exists():
        return
    engine = create_engine(f"sqlite:///{database}", connect_args={"timeout": 10})
    with Session(engine, expire_on_commit=False) as session:
        pending = session.scalars(select(AgentRun).where(AgentRun.status.not_in(["completed", "failed", "stopped", "submission_unknown"]))).all()
        for run in pending:
            stop_run(session, run.project_id, run.id, run.owner_id, f"smoke-stop-{verification_id}-{run.id}")
    engine.dispose()


def response_json(response: httpx.Response) -> dict:
    response.raise_for_status()
    return response.json()


def create_sample(client: httpx.Client, role: str, revision: dict | None = None) -> dict:
    title, instruction = ROLES[role]
    envelope = response_json(client.post("/projects", json={"title": f"真实角色验收：{title}"}))
    project_id = envelope["project"]["id"]
    scene_id = next(node["id"] for node in envelope["project"]["graphData"]["nodes"] if node["type"] == "scene")
    controller_id, target_id = str(uuid.uuid4()), str(uuid.uuid4())
    plan_requirement = ("本次计划安排两个步骤：第一步kind=produce，dependsOn=[]；第二步kind=verify，dependsOn=[0]，只核对第一步的真实保存版本和哈希，不再修改正文。" if role == "director" else f"本次计划只安排一个{title}步骤，kind=produce，dependsOn=[]。")
    business_goal = f"请读取本项目已保存的‘创作约定’记忆，以{title}的专业职责完成以下交付。{instruction}产物须符合当前角色的专业结构，填写全部必要字段；本岗位的专业内容与实际来源应完整可读，交接材料只列下游需要的输入、约束及验收要求。禁止使用省略号、‘同上’或‘略’代替必要内容。内容长度由职责和交付要求决定。"
    execution_instructions = ("先提交本次计划。" + plan_requirement +
        f"创作步骤依次通过read_project读取项目、retrieve_context读取设定，再通过write_artifact一次写入现有targetId={target_id}。"
        "write_artifact的artifact必须符合本角色的严格Schema，sources至少引用实际读取的‘创作约定’记忆（kind=memory，referenceId为返回的真实记忆ID），其他来源引用实际读取的项目或本人要求；创作补充写入assumptions。"
        "write_artifact回执确认保存成功后，说明实际交付并完成本步骤，禁止继续调用write_artifact或重复改写。"
        "只允许read_project、retrieve_context、write_artifact及只读validate_artifacts核对已保存产物；write_artifact必须明确传入targetId。保持既有姓名与设定，不创建新节点，不申请图片、视频或音频，不修改其他内容。")
    if revision is not None:
        revision_method = revision.get("method", "patch")
        if revision_method not in {"patch", "write"}:
            raise ValueError("专业修订必须明确使用patch或write")
        business_goal += "\n\n本次审阅反馈：\n" + "\n".join(f"{index + 1}. {item}" for index, item in enumerate(revision["feedback"]))
        business_goal += "\n当前授权文字节点保存原完整正文，请依反馈修订并完整交付本岗位专业产物。内容质量待本人重新评分。"
        execution_instructions = ("先提交本次计划。" + plan_requirement + f"依次通过read_project读取项目、retrieve_context读取targetIds=[{target_id}]、maxChars=24000，核对原正文完整且未截断。"
            f"随后通过read_artifact读取targetId={target_id}的完整真实artifact、baseArtifactSha256、revision与patchablePaths。"
            "artifactDraft是验证作者提供的原真实模型产物归档，原引用尚未在当前新项目核验。")
        execution_instructions += ("依反馈仅用patch_artifact修改明确字段，传入刚读取的真实baseArtifactSha256与expectedRevision；对patchablePaths列出的授权字段提交replace，保留反馈未涉及的原字段。"
            if revision_method == "patch" else
            f"本次明确采用完整专业写入修订，仅用write_artifact一次写入现有targetId={target_id}，artifact必须包含本角色完整严格结构，依据反馈补齐缺失实体及其全部专业内容，并保留其他完整内容。禁止使用patch_artifact或切换修订方式。")
        execution_instructions += (
            f"sources同时引用本项目的创作约定memory与canvas_node={target_id}。"
            "原来源列表、嵌套sourceBasis及场景引用中需要当前对象的标识使用实际读取到的本项目ID重新绑定。"
            "节点revisionLineage中的原proof、run及其他对象ID仅用于归档核对；实际sources.referenceId使用当前新项目读取到的标识。"
            f"真实{'patch_artifact' if revision_method == 'patch' else 'write_artifact'}成功后调用validate_artifacts核验本次产物，核验成功后完成步骤。"
            f"只允许read_project、retrieve_context、read_artifact、{'patch_artifact' if revision_method == 'patch' else 'write_artifact'}和validate_artifacts。"
            "只处理当前授权目标，不创建节点、不申请任何媒体，不修改其他内容；成功保存后禁止继续改写。")
    goal = business_goal + "\n\n工具执行要求：\n" + execution_instructions
    controller = {"id": controller_id, "type": "agent", "title": f"{title}任务", "position": {"x": 40, "y": 80}, "width": 340, "height": 360,
        "metadata": {"role": role, "executor": "platform", "goal": goal, "targetIds": [target_id]}}
    target_metadata = {"content": revision["originalText"], "revisionLineage": revision["lineage"], "artifactDraft": revision["originalArtifact"],
        "artifactDraftProvenance": {"inputType": "archived_model_artifact", "providedBy": "validation_author", "referenceScope": "archive", "sourcesValidated": False,
            "artifactSha256": revision["lineage"]["sourceArtifactSha256"]}} if revision is not None else {"content": ""}
    target = {"id": target_id, "type": "text", "title": "本人授权的文字结果", "position": {"x": 500, "y": 80}, "width": 360, "height": 260, "metadata": target_metadata}
    canvas = response_json(client.put(f"/projects/{project_id}/canvases/{scene_id}", json={"expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()),
        "nodes": [controller, target], "connections": [{"id": str(uuid.uuid4()), "fromNodeId": controller_id, "toNodeId": target_id}], "viewport": {"x": 20, "y": 30, "k": 0.8}}))
    memory = response_json(client.post(f"/projects/{project_id}/memories", json={"expectedRevision": canvas["revision"], "operationId": str(uuid.uuid4()),
        "kind": "setting", "subject": "创作约定", "content": SETTING, "sources": [{"kind": "project", "id": project_id, "revision": canvas["revision"]}]}))
    if revision is not None:
        context = response_json(client.post(f"/projects/{project_id}/context/retrieve", json={"sceneId": scene_id, "targetIds": [target_id], "maxChars": 24000}))
        if not any(item["id"] == target_id and item["content"] == revision["originalText"] and not item.get("truncated") for item in context["canvasTexts"]) or not any(item["id"] == memory["memory"]["id"] and item["content"] == SETTING for item in context["memories"]):
            raise ValueError("当前有界读取容量不能完整保存本次修订原文及创作约定，尚未提交模型任务")
    payload = {"operationId": str(uuid.uuid4()), "expectedRevision": memory["revision"], "sceneId": scene_id, "controllerNodeId": controller_id,
        "role": role, "executor": "platform", "deliveryMode": "professional", "goal": goal, "targetIds": [target_id], "limits": RUN_LIMITS}
    run = response_json(client.post(f"/projects/{project_id}/agent-runs", json=payload))["run"]
    retry = response_json(client.post(f"/projects/{project_id}/agent-runs", json=payload))["run"]
    if retry["id"] != run["id"]:
        raise ValueError("同一真实角色任务被重复创建")
    return {"projectId": project_id, "sceneId": scene_id, "controllerId": controller_id, "targetId": target_id, "runId": run["id"], "role": role,
        "goal": goal, "businessGoal": business_goal, "executionInstructions": execution_instructions, "input": SETTING,
        "artifactContractVersion": ARTIFACT_CONTRACT_VERSION, "artifactSchema": artifact_arguments_schema(role), "requestedModel": MODEL,
        "toolCatalogVersion": TOOL_CATALOG_VERSION,
        "memoryId": memory["memory"]["id"], "controller": controller, "request": payload,
        **({"revisionLineage": revision["lineage"], "revisionInputText": revision["originalText"], "revisionInputArtifact": revision["originalArtifact"], "revisionMethod": revision_method,
            "revisionInputRevision": memory["revision"], "revisionFeedback": revision["feedback"]} if revision is not None else {})}


def wait_for_sample(client: httpx.Client, sample: dict, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        run = response_json(client.get(f"/projects/{sample['projectId']}/agent-runs/{sample['runId']}"))["run"]
        if run["status"] in TERMINAL:
            if run["status"] != "completed":
                raise RuntimeError(f"真实角色任务未完成：{sample['role']}，任务{run['id']}，状态{run['status']}")
            return run
        if time.monotonic() >= deadline:
            raise TimeoutError(f"真实角色任务超时，保留原任务及费用预留：{run['id']}")
        time.sleep(0.5)


def verify_artifact_delivery(sample: dict, target: dict, receipts: list[dict]) -> dict:
    if sample.get("revisionMethod") not in {None, "patch", "write"} or sample.get("revisionMethod") is not None and "revisionLineage" not in sample:
        raise ValueError("专业产物的修订方式或归档来源无效")
    metadata = target.get("metadata", {})
    artifact = validate_artifact(sample["role"], metadata.get("creativeArtifact"))
    if not any(source["kind"] == "memory" and source["referenceId"] == sample["memoryId"] for source in artifact["sources"]):
        raise ValueError("专业角色产物没有引用本次实际读取的创作约定记忆")
    output = metadata.get("content")
    if target["type"] != "text" or not isinstance(output, str) or output != render_artifact(artifact):
        raise ValueError("实际专业产物与完整呈现正文不一致")
    if metadata.get("artifactContractVersion") != sample["artifactContractVersion"] or sample["artifactContractVersion"] not in SUPPORTED_ARTIFACT_CONTRACT_VERSIONS:
        raise ValueError("实际专业产物契约版本与本次冻结版本不一致")
    if sample["artifactSchema"] != artifact_arguments_schema(sample["role"]):
        raise ValueError("本次角色Schema与提交前保存的Schema不一致")
    writes = [receipt for receipt in receipts if receipt["tool"] in {"write_artifact", "write_script", "patch_artifact"}]
    patch_mode = sample.get("revisionMethod") == "patch"
    if patch_mode:
        if not writes or any(receipt["tool"] != "patch_artifact" or receipt["arguments"].get("targetId") != sample["targetId"] for receipt in writes):
            raise ValueError("专业字段修订缺少真实授权patch_artifact写入")
        verify_patch_chain(sample, receipts, writes)
    elif len(writes) != 1 or writes[0]["tool"] != "write_artifact" or writes[0]["arguments"].get("targetId") != sample["targetId"]:
        raise ValueError("专业角色产物缺少唯一授权write_artifact写入")
    write = writes[-1]
    if (not patch_mode and validate_artifact(sample["role"], write["arguments"].get("artifact")) != artifact) or write["result"].get("artifact") != artifact:
        raise ValueError("原工具参数、写入回执和节点专业产物不一致")
    artifact_sha, content_sha = digest(artifact), hashlib.sha256(output.encode()).hexdigest()
    if write["result"].get("artifactSha256") != artifact_sha or write["result"].get("artifactContractVersion") != sample["artifactContractVersion"]:
        raise ValueError("实际专业产物回执缺少一致的契约版本和哈希")
    saved = next((item for item in write["result"].get("savedTexts", []) if item["nodeId"] == sample["targetId"]), None)
    if saved is None or saved["text"] != output or saved["sha256"] != content_sha:
        raise ValueError("实际专业产物的正文回执与完整内容或哈希不一致")
    prompt_items = image_prompt_items(artifact)
    if metadata.get("artifactContentSha256") != content_sha or metadata.get("generationPrompts") != prompt_items or write["result"].get("promptItems") != prompt_items:
        raise ValueError("实际专业产物正文哈希或后续生成输入与原回执不一致")
    comparison = verify_write_revision(sample, receipts, write) if sample.get("revisionMethod") == "write" else None
    return {"artifact": artifact, "artifactContractVersion": sample["artifactContractVersion"],
        "artifactSha256": artifact_sha, "contentSha256": content_sha, "artifactSchema": sample["artifactSchema"],
        "artifactSchemaSha256": digest(sample["artifactSchema"]), "writeReceipt": write,
        **({"patchReceipts": writes} if patch_mode else {}),
        **({"revisionComparison": comparison} if comparison is not None else {})}


def verify_write_revision(sample: dict, receipts: list[dict], write: dict) -> dict:
    original = validate_artifact(sample["role"], sample["revisionInputArtifact"])
    if digest(original) != sample["revisionLineage"]["sourceArtifactSha256"] or render_artifact(original) != sample["revisionInputText"]:
        raise ValueError("完整写入修订的归档结构、原正文与来源SHA不一致")
    allowed = {"read_project", "retrieve_context", "read_artifact", "write_artifact", "validate_artifacts"}
    if any(receipt["tool"] not in allowed for receipt in receipts):
        raise ValueError("完整写入修订使用了未经本次明确授权的工具")
    reads = [receipt for receipt in receipts[:receipts.index(write)] if receipt["tool"] == "read_artifact" and receipt["arguments"].get("targetId") == sample["targetId"]]
    read = next((receipt for receipt in reversed(reads) if receipt["result"].get("artifact") == original), None)
    if (read is None or read["result"].get("baseArtifactSha256") != digest(original) or read["result"].get("artifactSha256") != digest(original)
            or read["result"].get("revision") != sample["revisionInputRevision"] or read["result"].get("targetId") != sample["targetId"]
            or read["result"].get("referenceScope") != "archive" or read["result"].get("sourcesValidated") is not False
            or read["result"].get("artifactContractVersion") != sample["artifactContractVersion"]
            or read["result"].get("contentSha256") != hashlib.sha256(sample["revisionInputText"].encode()).hexdigest()):
        raise ValueError("完整写入修订缺少对应授权原稿、真实SHA与实际读取版本")
    final = validate_artifact(sample["role"], write["arguments"]["artifact"])
    changes = jsonpatch.make_patch(original, final).patch
    content_paths = [change["path"] for change in changes if change["path"] != "/sources" and not change["path"].startswith("/sources/") and "/sourceBasis" not in change["path"]]
    if not content_paths:
        raise ValueError("完整写入修订没有产生来源重绑以外的专业内容变化")
    return {"method": "write", "readOperationId": read["operationId"], "baseArtifactSha256": digest(original),
        "artifactSha256": digest(final), "changedPaths": sorted({change["path"] for change in changes}),
        "contentChangedPaths": sorted(set(content_paths))}


def verify_patch_chain(sample: dict, receipts: list[dict], writes: list[dict]) -> None:
    current = validate_artifact(sample["role"], sample["revisionInputArtifact"])
    if digest(current) != sample["revisionLineage"]["sourceArtifactSha256"] or render_artifact(current) != sample["revisionInputText"]:
        raise ValueError("归档结构化原稿与原正文、来源SHA不一致")
    previous_index = -1
    for write in writes:
        arguments, result = write["arguments"], write["result"]
        index = receipts.index(write)
        reads = [receipt for receipt in receipts[previous_index + 1:index] if receipt["tool"] == "read_artifact" and receipt["arguments"].get("targetId") == sample["targetId"]]
        read = next((receipt["result"] for receipt in reversed(reads) if receipt["result"].get("artifactSha256") == digest(current) and receipt["result"].get("revision") == arguments["expectedRevision"]), None)
        if (read is None or read.get("artifact") != current or arguments["baseArtifactSha256"] != digest(current)
                or read.get("baseArtifactSha256") != digest(current) or read.get("targetId") != sample["targetId"]
                or read.get("artifactContractVersion") != sample["artifactContractVersion"] or read.get("contentSha256") != hashlib.sha256(render_artifact(current).encode()).hexdigest()):
            raise ValueError("字段补丁缺少对应完整原稿、真实base SHA和实际读取版本")
        if previous_index < 0 and (read.get("referenceScope") != "archive" or read.get("sourcesValidated") is not False):
            raise ValueError("归档原稿读取不能声明已通过当前来源核验")
        patches = arguments["patches"]
        allowed_paths = {item["path"] for item in read["patchablePaths"]}
        if any(patch["op"] not in {"test", "replace"} or patch["op"] == "replace" and patch["path"] not in allowed_paths for patch in patches):
            raise ValueError("字段补丁超出实际读取的可修改路径或操作类型")
        expected = validate_artifact(sample["role"], jsonpatch.JsonPatch(patches).apply(current, in_place=False))
        if result.get("baseArtifact") != current or result.get("baseArtifactSha256") != digest(current) or result.get("patches") != patches or result.get("artifact") != expected:
            raise ValueError("实际补丁、原稿、未修改范围或完整结果与确定性JSON Patch复算不一致")
        content = render_artifact(expected)
        content_sha = hashlib.sha256(content.encode()).hexdigest()
        saved = next((item for item in result.get("savedTexts", []) if item["nodeId"] == sample["targetId"]), None)
        if (result.get("artifactSha256") != digest(expected) or result.get("artifactContractVersion") != sample["artifactContractVersion"]
                or result.get("promptItems") != image_prompt_items(expected) or saved is None or saved["text"] != content or saved["sha256"] != content_sha):
            raise ValueError("字段补丁回执没有保留完整正文、独立提示词与一致哈希")
        changed = list(dict.fromkeys(patch["path"] for patch in patches if patch["op"] == "replace" and digest(resolve_pointer(current, patch["path"])) != digest(resolve_pointer(expected, patch["path"]))))
        if result.get("changedPaths") != changed:
            raise ValueError("字段补丁回执的实际修改路径与完整原稿比较不一致")
        current, previous_index = expected, index


def verify_revision_delivery(database, sample: dict, target: dict, receipts: list[dict]) -> None:
    if "revisionLineage" not in sample:
        return
    lineage = sample["revisionLineage"]
    metadata = target["metadata"]
    if metadata.get("revisionLineage") != lineage or hashlib.sha256(sample["revisionInputText"].encode()).hexdigest() != lineage["sourceContentSha256"]:
        raise ValueError("修订产物的归档来源或完整原文哈希发生变化")
    if not any(source["kind"] == "canvas_node" and source["referenceId"] == sample["targetId"] for source in metadata["creativeArtifact"]["sources"]):
        raise ValueError("修订专业产物没有引用实际读取的当前原文节点")
    patches = [receipt for receipt in receipts if receipt["tool"] == "patch_artifact"]
    source_revision = patches[-1]["arguments"]["expectedRevision"] if sample.get("revisionMethod") == "patch" and patches else sample["revisionInputRevision"]
    if not any(binding["kind"] == "canvas_node" and binding["id"] == sample["targetId"] and binding.get("bindingMode") == "input_snapshot" and binding["revision"] == source_revision for binding in metadata.get("artifactSources", [])):
        raise ValueError("修订原文没有保存为实际输入版本快照")
    contexts = [receipt for receipt in receipts if receipt["tool"] == "retrieve_context"]
    if not any(any(item["id"] == sample["targetId"] and item["content"] == sample["revisionInputText"] and not item.get("truncated") for item in receipt["result"].get("canvasTexts", [])) for receipt in contexts):
        raise ValueError("修订工具没有实际完整读取原正文")
    validations = [receipt for receipt in receipts if receipt["tool"] == "validate_artifacts"]
    if not validations or any(receipt["result"].get("valid") is not True for receipt in validations):
        raise ValueError("修订产物尚未经过实际只读核验")
    version = database.execute("SELECT canvases_json FROM project_versions WHERE project_id=? AND revision=?", (sample["projectId"], sample["revisionInputRevision"])).fetchone()
    if version is None:
        raise ValueError("修订原完整正文缺少实际项目历史")
    canvas = next(item for item in json.loads(version["canvases_json"]) if item["sceneId"] == sample["sceneId"])
    original = next(node for node in canvas["nodes"] if node["id"] == sample["targetId"])
    if original["metadata"].get("content") != sample["revisionInputText"] or original["metadata"].get("revisionLineage") != lineage:
        raise ValueError("修订输入与原保存版本不一致")
    if sample.get("revisionMethod") in {"patch", "write"} and (original["metadata"].get("artifactDraft") != sample["revisionInputArtifact"] or metadata.get("artifactDraft") != sample["revisionInputArtifact"]):
        raise ValueError("结构化归档原稿没有保留实际初始版本")
    if sample.get("revisionMethod") in {"patch", "write"}:
        provenance = original["metadata"].get("artifactDraftProvenance", {})
        if provenance.get("referenceScope") != "archive" or provenance.get("sourcesValidated") is not False or provenance.get("artifactSha256") != digest(sample["revisionInputArtifact"]):
            raise ValueError("归档原稿缺少真实作者输入属性与未验证来源标记")
        for receipt in (item for item in receipts if item["tool"] == ("patch_artifact" if sample["revisionMethod"] == "patch" else "write_artifact")):
            version = database.execute("SELECT canvases_json FROM project_versions WHERE project_id=? AND revision=?", (sample["projectId"], receipt["revision"])).fetchone()
            if version is None:
                raise ValueError("专业修订候选缺少实际保存版本")
            canvas = next(item for item in json.loads(version["canvases_json"]) if item["sceneId"] == sample["sceneId"])
            candidate = next(node for node in canvas["nodes"] if node["id"] == sample["targetId"])
            expected = receipt["result"]["artifact"]
            if candidate["metadata"].get("creativeArtifact") != expected or candidate["metadata"].get("content") != render_artifact(expected) or candidate["metadata"].get("generationPrompts") != image_prompt_items(expected):
                raise ValueError("专业修订的历史候选与原始工具回执不一致")


def verify_sample(client: httpx.Client, data: Path, sample: dict, run: dict) -> dict:
    if run["status"] != "completed" or run["role"] != sample["role"] or run["executor"] != "platform" or run["targetIds"] != [sample["targetId"]] or run["createdNodeIds"]:
        raise ValueError("真实角色运行结果或授权目标不一致")
    if run.get("deliveryMode") != "professional" or run.get("artifactContractVersion") != sample["artifactContractVersion"]:
        raise ValueError("专业角色任务的持久交付模式或契约版本与本次冻结值不一致")
    if "toolCatalogVersion" in sample and run.get("toolCatalogVersion") != sample["toolCatalogVersion"]:
        raise ValueError("实际任务工具目录与提交前冻结版本不一致")
    if sample.get("requestedModel") and any(call["model"] not in model_aliases(sample["requestedModel"]) for call in run["calls"]):
        raise ValueError("真实角色回复的模型与本次冻结的专业模型不一致")
    canvas = response_json(client.get(f"/projects/{sample['projectId']}/canvases/{sample['sceneId']}"))
    nodes = {node["id"]: node for node in canvas["canvas"]["nodes"]}
    if set(nodes) != {sample["controllerId"], sample["targetId"]} or nodes[sample["controllerId"]] != sample["controller"]:
        raise ValueError("真实角色修改了授权范围以外的画布对象")
    output = nodes[sample["targetId"]]["metadata"].get("content", "")
    if not isinstance(output, str) or not output.strip():
        raise ValueError("真实角色没有交付有效的目标正文")
    receipts = [receipt for step in run["steps"] for receipt in step["toolResults"]]
    allowed = {"read_project", "retrieve_context", "read_artifact", "patch_artifact", "validate_artifacts"} if sample.get("revisionMethod") == "patch" else {"read_project", "retrieve_context", "read_artifact", "write_artifact", "validate_artifacts"} if sample.get("revisionMethod") == "write" else {"read_project", "retrieve_context", "write_artifact", "validate_artifacts"}
    if not receipts or any(receipt["tool"] not in allowed for receipt in receipts):
        raise ValueError("真实角色没有遵守本次工具授权")
    delivery = verify_artifact_delivery(sample, nodes[sample["targetId"]], receipts)
    writes = [receipt for receipt in receipts if receipt["tool"] in {"write_artifact", "patch_artifact"}]
    contexts = [receipt for receipt in receipts if receipt["tool"] == "retrieve_context"]
    reads = [receipt for receipt in receipts if receipt["tool"] == "read_project"]
    if not reads:
        raise ValueError("真实角色工具未读取当前项目")
    if sample["role"] == "director":
        validations = [receipt for receipt in receipts if receipt["tool"] == "validate_artifacts"]
        if not any(step.get("kind") == "verify" and step.get("dependsOn") for step in run["steps"]) or not validations or any(receipt["result"].get("valid") is not True for receipt in validations):
            raise ValueError("创作总监任务尚未完成独立的真实产物核验步骤")
    if not contexts or receipts.index(reads[0]) >= receipts.index(contexts[0]):
        raise ValueError("实际角色未按项目、上下文顺序读取来源")
    if not contexts or receipts.index(contexts[0]) >= receipts.index(writes[0]):
        raise ValueError("实际角色尚未读取有效上下文就提交了产物")
    if not any(any(memory["id"] == sample["memoryId"] and memory["content"] == sample["input"] and memory["valid"] for memory in receipt["result"]["memories"]) for receipt in contexts):
        raise ValueError("真实角色工具未读取已保存的设定及其来源")
    memories = response_json(client.get(f"/projects/{sample['projectId']}/memories"))
    if len(memories["memories"]) != 1 or memories["memories"][0]["id"] != sample["memoryId"]:
        raise ValueError("真实角色改变了既有记忆")
    with read_database(data) as connection:
        verify_revision_delivery(connection, sample, nodes[sample["targetId"]], receipts)
        for receipt in receipts:
            row = connection.execute("SELECT response_json FROM operations WHERE project_id=? AND operation_id=? AND kind='agent.tool'", (sample["projectId"], receipt["operationId"])).fetchone()
            if row is None or json.loads(row["response_json"]) != {"result": receipt["result"], "revision": receipt["revision"]}:
                raise ValueError("工具结果缺少对应的真实幂等操作记录")
        history = connection.execute("SELECT * FROM project_versions WHERE project_id=? AND revision=?", (sample["projectId"], run["projectRevision"])).fetchone()
        if history is None:
            raise ValueError("真实角色写入没有保留项目版本")
        frozen_canvas = next(value for value in json.loads(history["canvases_json"]) if value["sceneId"] == sample["sceneId"])
        frozen_target = next(node for node in frozen_canvas["nodes"] if node["id"] == sample["targetId"])
        if frozen_target["metadata"].get("content") != output or frozen_target["metadata"].get("creativeArtifact") != delivery["artifact"] or frozen_target["metadata"].get("artifactContractVersion") != delivery["artifactContractVersion"]:
            raise ValueError("项目历史与实际专业产物、完整正文或契约版本不一致")
    return {"run": run, "canvas": canvas, "memories": memories, "output": output, "qualityIssues": [],
        "delivery": delivery, "writeReceipt": writes[0], "operationIds": [receipt["operationId"] for receipt in receipts]}


def call_records(data: Path, *, restarted: bool, success: bool, run_ids: set[str] | None = None, expected_status: str = "completed") -> list[dict]:
    if not sqlite_database_path(data).exists():
        return []
    records = []
    with read_database(data) as connection:
        rows = connection.execute("SELECT c.*, r.project_id, r.scene_id AS run_scene, r.goal AS run_goal, r.target_ids_json AS run_targets, r.limits_json AS run_limits, r.status AS run_status FROM agent_calls c JOIN agent_runs r ON c.run_id=r.id ORDER BY c.created_at, c.id").fetchall()
        corrections, rejected = {}, {}
        for row in rows:
            if run_ids is not None and row["run_id"] not in run_ids:
                continue
            frozen = json.loads(row["input_json"])
            usage_json = json.loads(row["usage_json"])
            platform = frozen.get("protocol") in SUPPORTED_PLATFORM_PROTOCOLS
            request_model = MODEL
            if frozen.get("protocol") is not None and not platform:
                raise ValueError("实际角色调用使用未支持的已冻结协议")
            key = (row["run_id"], row["step_id"])
            correction = frozen.get("correctionAttempt", 0)
            if platform:
                protocol = frozen["protocol"]
                json_protocol = is_json_protocol(protocol)
                catalog_version = frozen.get("toolCatalogVersion", LEGACY_TOOL_CATALOG_VERSION)
                request_model = frozen["requestBody"]["model"]
                limits = json.loads(row["run_limits"])
                if limits.get("toolCatalogVersion", LEGACY_TOOL_CATALOG_VERSION) != catalog_version:
                    raise ValueError("实际调用工具目录与任务冻结版本不一致")
                if request_model != frozen.get("model", MODEL) or request_model != limits.get("model", MODEL):
                    raise ValueError("实际请求模型与任务、调用及完整请求体的冻结模型不一致")
                if row["model"] not in model_aliases(request_model):
                    raise ValueError("供应商实际响应模型不属于冻结请求模型的官方标识")
                allowed_names = {tool["function"]["name"] for tool in agent_business_tools(row["role"], frozen["planning"], catalog_version=catalog_version)}
                frozen_names = [tool["function"]["name"] for tool in frozen["tools"]]
                if len(frozen_names) != len(set(frozen_names)) or not set(frozen_names) <= allowed_names:
                    raise ValueError("实际冻结原生函数超出岗位或阶段允许范围")
                if json_protocol:
                    business_tools = frozen.get("businessTools")
                    response_schema = frozen.get("responseSchema")
                    if frozen["tools"] != [] or business_tools != agent_business_tools(row["role"], frozen["planning"], catalog_version=catalog_version) or response_schema != agent_response_schema(frozen["planning"], protocol=protocol):
                        raise ValueError("JSON角色协议缺少完整岗位业务Schema、阶段响应Schema或空原生函数清单")
                    if frozen.get("providerEndpoint") != provider_endpoint(protocol) or any(message.get("role") == "tool" or "tool_call_id" in message or "tool_calls" in message for message in frozen["messages"]):
                        raise ValueError("JSON角色请求的供应商地址或实际消息类型不符合冻结协议")
                    if protocol == THINKING_JSON_AGENT_PROTOCOL and any("reasoning_content" in message for message in frozen["messages"]):
                        raise ValueError("思考模式不能把供应商的内部推理字段回传为对话历史")
                    project_tools = [tool for tool in business_tools if tool["function"]["name"] not in {"submit_plan", "complete_step"}]
                    system_message = frozen["messages"][0]
                    if system_message.get("role") != "system" or "\n响应Schema：" + json.dumps(response_schema, ensure_ascii=False) not in system_message["content"] or "\n业务工具及本岗位完整参数Schema：" + json.dumps(project_tools, ensure_ascii=False) not in system_message["content"]:
                        raise ValueError("JSON角色实际系统消息没有传递冻结的完整响应和岗位业务Schema")
                    if "toolCallId" in usage_json or "nativeFunction" in usage_json:
                        raise ValueError("JSON角色调用不能登记原生函数标识")
                elif protocol in {"native-tools-v2-strict", "native-tools-v3-strict-ref", "native-tools-v4-materials-strict"}:
                    business_tools = frozen.get("businessTools")
                    if not isinstance(business_tools, list) or [tool["function"]["name"] for tool in business_tools] != frozen_names:
                        raise ValueError("原生严格协议缺少对应的完整原业务Schema")
                    adapted = [{"type": tool["type"], "function": {**tool["function"], "parameters": strict_schema(tool["function"]["parameters"], protocol=protocol), "strict": True}} for tool in business_tools]
                    if frozen["tools"] != adapted or frozen.get("providerEndpoint") != provider_endpoint(protocol):
                        raise ValueError("原生严格协议的实际Schema适配、strict设置或官方Beta地址不一致")
                elif frozen.get("providerEndpoint") is not None and frozen["providerEndpoint"] != provider_endpoint(protocol):
                    raise ValueError("历史原生协议的供应商地址与原协议不一致")
                request_body = agent_request_body(frozen["messages"], frozen["maxTokens"], frozen["tools"], frozen["planning"], Settings(text_max_output_tokens=frozen["maxTokens"]), protocol=protocol, model=request_model, catalog_version=catalog_version)
                if request_body != frozen.get("requestBody"):
                    raise ValueError("实际角色请求与冻结的完整供应商请求、Schema或参数不一致")
                expected_upper = len(json.dumps(frozen["requestBody"], ensure_ascii=False, separators=(",", ":")).encode()) + 2048
                if frozen["inputTokenUpperBound"] != expected_upper or not frozen.get("promptVersion") or frozen.get("providerRevision") is None:
                    raise ValueError("实际角色请求缺少完整Schema、输入上界或配置版本")
                if frozen.get("correctionOf"):
                    original = rejected.get(key)
                    if type(correction) is not int or correction != corrections.get(key, 0) + 1 or correction > 2 or original is None or original["id"] != frozen["correctionOf"]:
                        raise ValueError("原生参数修正没有对应本步骤最近的真实拒绝记录或超出两次上限")
                    original_input = json.loads(original["input_json"])
                    if original_input.get("protocol") != protocol or original_input["projectRevision"] != frozen["projectRevision"] or original["remote_request_id"] is None or not json.loads(original["usage_json"]).get("settled"):
                        raise ValueError("原生参数修正跨越原始版本或原拒绝请求尚未结算")
                    corrections[key] = correction
                elif correction:
                    raise ValueError("原生参数修正次数缺少原拒绝调用标识")
                if frozen.get("manualRepairOf"):
                    original = connection.execute("SELECT * FROM agent_calls WHERE id=? AND run_id=? AND step_id=?", (frozen["manualRepairOf"], row["run_id"], row["step_id"])).fetchone()
                    operation = connection.execute("SELECT id FROM operations WHERE project_id=? AND operation_id=? AND kind='agent.resume'", (row["project_id"], frozen.get("manualRepairOperationId"))).fetchone()
                    if original is None or operation is None or original["status"] != "tool_rejected" or not json.loads(original["usage_json"]).get("settled") or json.loads(original["input_json"])["projectRevision"] != frozen["projectRevision"]:
                        raise ValueError("手动重试缺少实际拒绝、原版本及恢复操作记录")
                for acknowledgement in frozen.get("acknowledgedUnknownCalls", []):
                    original = connection.execute("SELECT * FROM agent_calls WHERE id=? AND run_id=?", (acknowledgement["callId"], row["run_id"])).fetchone()
                    confirmation = next((item for item in limits.get("unknownRetryConfirmations", []) if item["callId"] == acknowledgement["callId"] and item["operationId"] == acknowledgement["operationId"]), None)
                    operation = connection.execute("SELECT * FROM operations WHERE project_id=? AND operation_id=? AND kind='agent.retry_unknown'", (row["project_id"], acknowledgement["operationId"])).fetchone()
                    reservation = connection.execute("SELECT * FROM budget_reservations WHERE id=?", (acknowledgement["callId"],)).fetchone()
                    if original is None or confirmation is None or operation is None or reservation is None or original["status"] != "submission_unknown" or reservation["status"] != "reserved" or reservation["reserved_micros"] <= 0:
                        raise ValueError("未知请求重试缺少实际未知记录、费用预留及本人确认")
                    payload = confirmation.get("payload") or {"runId": row["run_id"], "operationId": acknowledgement["operationId"], "expectedRevision": confirmation["projectRevision"], "callId": acknowledgement["callId"], "confirmed": True, "maxSteps": confirmation["maxSteps"]}
                    if operation["payload_hash"] != digest(payload):
                        raise ValueError("未知请求重试与实际确认参数不一致")
                if frozen.get("limitExtensionOperationId"):
                    extension = next(item for item in limits["requestLimitExtensions"] if item["operationId"] == frozen["limitExtensionOperationId"])
                    operation = connection.execute("SELECT * FROM operations WHERE project_id=? AND operation_id=? AND kind='agent.extend_limit'", (row["project_id"], extension["operationId"])).fetchone()
                    if operation is None or operation["payload_hash"] != digest(extension["payload"]):
                        raise ValueError("提高请求上限缺少本人实际确认记录")
            valid = False
            if row["status"] == "ready" or platform and usage_json.get("settled") and row["remote_request_id"]:
                version = connection.execute("SELECT revision FROM project_versions WHERE project_id=? AND revision=?", (row["project_id"], frozen["projectRevision"])).fetchone()
                messages = frozen["messages"]
                if len(messages) < 2 or messages[1]["role"] != "user":
                    raise ValueError("真实角色请求缺少冻结的初始任务消息")
                instruction = json.loads(messages[1]["content"])
                if version is None or frozen["sceneId"] != row["run_scene"] or instruction["goal"] != row["run_goal"] or instruction["authorizedTargetIds"] != json.loads(row["run_targets"]) or instruction["projectRevision"] != frozen["projectRevision"]:
                    raise ValueError("真实角色输入与冻结项目版本、场景或授权目标不一致")
                if platform:
                    limits = json.loads(row["run_limits"])
                    if limits.get("providerProtocol") != frozen["protocol"] or instruction.get("deliveryMode") != limits.get("deliveryMode") or instruction.get("artifactContractVersion") != limits.get("artifactContractVersion"):
                        raise ValueError("实际原生角色请求与持久协议、交付模式或契约版本不一致")
                    message = json.loads(row["raw_output"])
                    saved_response = json.loads(row["response_json"]) if row["response_json"] is not None else None
                    try:
                        parsed, identifier = decode_platform_message(message, frozen["planning"], row["role"], protocol=protocol, catalog_version=catalog_version)
                    except AppError as error:
                        parsed = None
                        if row["status"] == "ready" or saved_response is not None:
                            raise ValueError("角色的拒绝输出被登记为可执行映射") from error
                    else:
                        mapping_missing = saved_response is None and row["status"] in {"ready", "tool_rejected"}
                        mapping_changed = saved_response is not None and parsed != AgentDecision.model_validate(saved_response).model_dump(by_alias=True)
                        if mapping_missing or mapping_changed or usage_json.get("toolCallId") != identifier or json_protocol and identifier is not None:
                            raise ValueError("原始供应商message、真实函数标识和确定性AgentDecision映射不一致")
                    finish_valid = usage_json.get("providerFinishReason") == "stop" if json_protocol else usage_json.get("nativeFinishReason") == "tool_calls"
                    artifact_valid = row["status"] == "ready" and parsed is not None and usage_json.get("toolExecutionDone") is True and finish_valid and usage_json.get("toolError") is None
                    if artifact_valid:
                        control = usage_json.get("controlResult")
                        if parsed["action"] == "plan":
                            if not isinstance(control, dict) or control.get("accepted") is not True or control.get("plan") != parsed["plan"]:
                                raise ValueError("原生计划函数缺少实际接受计划的持久回执")
                        elif parsed["action"] == "complete":
                            if not isinstance(control, dict) or control.get("completed") is not True or control.get("stepId") != row["step_id"] or control.get("summary") != parsed["summary"]:
                                raise ValueError("原生完成函数缺少对应实际步骤的完成回执")
                        elif connection.execute("SELECT id FROM operations WHERE project_id=? AND operation_id=? AND kind='agent.tool'", (row["project_id"], f"agent-{row['id']}")).fetchone() is None:
                            raise ValueError("原生项目函数缺少实际幂等操作回执")
                    if row["status"] == "tool_rejected":
                        error = usage_json.get("toolError")
                        if not isinstance(error, dict) or not error.get("code") or error.get("businessExecuted") is not False or usage_json.get("toolExecutionDone"):
                            raise ValueError("原生工具拒绝缺少确定未执行的真实错误记录")
                        if connection.execute("SELECT id FROM operations WHERE project_id=? AND operation_id=? AND kind='agent.tool'", (row["project_id"], f"agent-{row['id']}")).fetchone() is not None:
                            raise ValueError("同一原生工具被同时记录为成功写入和参数拒绝")
                else:
                    parsed = AgentDecision.model_validate_json(row["raw_output"]).model_dump(by_alias=True)
                    if parsed != AgentDecision.model_validate_json(row["response_json"]).model_dump(by_alias=True):
                        raise ValueError("真实原始模型输出与结构化调用结果不一致")
                    artifact_valid = True
                    if parsed["action"] == "tool" and parsed["tool"] == "write_artifact":
                        try:
                            validate_artifact(row["role"], parsed["arguments"].get("artifact"))
                        except ValidationError:
                            artifact_valid = False
                usage = Usage.model_validate(usage_json)
                reservation = connection.execute("SELECT * FROM budget_reservations WHERE id=?", (row["id"],)).fetchone()
                price = frozen["price"]
                actual = cost_micros(usage.prompt_cache_hit_tokens, usage.prompt_cache_miss_tokens, usage.completion_tokens, price)
                if not row["remote_request_id"] or (row["input_tokens"], row["output_tokens"], row["total_tokens"]) != (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens):
                    raise ValueError("实际角色请求标识或供应商用量不一致")
                if usage.prompt_tokens > frozen["inputTokenUpperBound"] or usage.completion_tokens > frozen["maxTokens"] or price != current_price(row["created_at"], model=request_model) or usage_json.get("price") != price:
                    raise ValueError("实际角色请求的输入上界或冻结价格不一致")
                if not usage_json.get("settled") or reservation is None or reservation["purpose"] != "agent-model" or reservation["status"] != "settled" or reservation["reserved_micros"] != 0 or reservation["cost_micros"] != actual or row["cost_micros"] != actual:
                    raise ValueError("真实角色调用与费用账目尚未完整结算")
                valid = artifact_valid
            if platform and row["status"] == "tool_rejected":
                if not usage_json.get("settled") or row["cost_micros"] is None or not row["remote_request_id"]:
                    raise ValueError("原生工具拒绝尚未结算实际请求用量与费用")
                rejected[key] = row
            record = AgentCallEvidence(callId=row["id"], runId=row["run_id"], projectId=row["project_id"], role=row["role"], provider=row["provider"], model=row["model"], execution="platform-api",
                requestId=row["remote_request_id"], inputTokens=row["input_tokens"], outputTokens=row["output_tokens"], totalTokens=row["total_tokens"], costMicros=row["cost_micros"],
                startedAt=utc(row["created_at"]), finishedAt=utc(row["finished_at"]) if row["finished_at"] else None,
                structureValid=valid, restartVerified=restarted, retryCount=frozen.get("jsonRepairAttempt", 0) + correction, result="通过" if valid and success and restarted and row["run_status"] == expected_status else "失败",
                inputSha256=digest(frozen), outputSha256=hashlib.sha256((row["raw_output"] or "").encode()).hexdigest()).model_dump()
            records.append(record)
    return records


def save_samples(directory: Path, samples: list[dict], verified: dict, collection_manifest: dict | None = None) -> dict:
    if any((directory / name).exists() for name in ("samples.json", "samples.html")):
        raise FileExistsError("样例输出目录已包含原报告，请使用独立输出目录")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    values = [{"role": sample["role"], "roleTitle": ROLES[sample["role"]][0], "runId": sample["runId"], "projectId": sample["projectId"],
        "goal": sample["goal"], "businessGoal": sample.get("businessGoal"), "executionInstructions": sample.get("executionInstructions"),
        "requestedModel": sample.get("requestedModel"),
        "setting": sample["input"], "output": verified[sample["runId"]]["output"], "qualityIssues": verified[sample["runId"]]["qualityIssues"],
        "operationIds": verified[sample["runId"]]["operationIds"],
        "delivery": verified[sample["runId"]].get("delivery"), "writeReceipt": verified[sample["runId"]].get("writeReceipt"),
        **({"revisionLineage": sample["revisionLineage"], "revisionFeedback": sample["revisionFeedback"], "revisionInputText": sample["revisionInputText"]} if "revisionLineage" in sample else {}),
        **({"revisionMethod": sample["revisionMethod"], "revisionInputArtifact": sample["revisionInputArtifact"]} if sample.get("revisionMethod") in {"patch", "write"} else {}),
        **({"currentSelection": sample["currentSelection"]} if "currentSelection" in sample else {})} for sample in samples]
    write_private_json(directory / "samples.json", values)
    sections = []
    for value in values:
        quality = "；".join(value["qualityIssues"]) or "待本人按专业完整性、可直接用于制作程度、设定一致性及表达质量评分"
        business = f"<h3>业务交付要求</h3><pre>{html.escape(value['businessGoal'])}</pre>" if value["businessGoal"] else ""
        technical = value["executionInstructions"] or value["goal"]
        delivery = value["delivery"]
        artifact = ("<details><summary>完整结构、正向提示词、负向提示词与生成规格</summary><pre>" + html.escape(json.dumps(delivery["artifact"], ensure_ascii=False, indent=2)) + "</pre></details>"
            "<details><summary>契约、Schema及实际工具回执</summary><pre>" + html.escape(json.dumps({key: item for key, item in delivery.items() if key != "artifact"}, ensure_ascii=False, indent=2)) + "</pre></details>") if delivery else ""
        revision = ("<details><summary>审阅反馈、原完整正文与归档来源</summary><pre>" + html.escape(json.dumps({key: value[key] for key in ("revisionFeedback", "revisionLineage", "revisionInputText", "revisionMethod", "revisionInputArtifact") if key in value}, ensure_ascii=False, indent=2)) + "</pre></details>") if "revisionLineage" in value else ""
        selection = ("<details><summary>当前采用版本、实际调用与费用</summary><pre>" + html.escape(json.dumps(value["currentSelection"], ensure_ascii=False, indent=2)) + "</pre></details>") if "currentSelection" in value else ""
        model = f"<p>请求模型：{html.escape(value['requestedModel'])}</p>" if value["requestedModel"] else ""
        sections.append(f"<section id='{html.escape(value['role'], quote=True)}'><h2>{html.escape(value['roleTitle'])}</h2>{model}{selection}<h3>输入设定</h3><pre>{html.escape(value['setting'])}</pre>{business}<h3>真实完整交付</h3><pre>{html.escape(value['output'])}</pre>{artifact}{revision}<details><summary>工具执行说明</summary><pre>{html.escape(technical)}</pre></details><p>{html.escape(quality)}</p><p>任务：{html.escape(value['runId'])}</p></section>")
    navigation = " · ".join(f"<a href='#{html.escape(value['role'], quote=True)}'>{html.escape(value['roleTitle'])}</a>" for value in values)
    manifest_section = ("<p>本报告包含九类岗位分别生成的完整样例。完整创作链按已定阶段验收。来源及修订共"
        + str(collection_manifest["actualCallCount"]) + "次实际调用，供应商预估成本¥" + format(collection_manifest["totalCostMicros"] / 1_000_000, ".6f")
        + "，包含全部修订祖先的调用费用。</p><details><summary>版本选择、原始证明与全部实际调用费用</summary><pre>"
        + html.escape(json.dumps(collection_manifest, ensure_ascii=False, indent=2)) + "</pre></details>") if collection_manifest is not None else ""
    page = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>SceneTurn 真实角色交付</title><style>body{max-width:1100px;margin:32px auto;padding:0 20px;font:16px/1.8 system-ui;color:#202020}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}section{border-bottom:1px solid #ccc;padding:24px 0}details{margin:18px 0}summary{cursor:pointer;font-weight:600}nav{line-height:2.2}a{color:#2554a8}</style><h1>真实角色专业交付</h1><p>保留各角色完整实际输出、提示词、规格、来源及工具证据。由本人按符合要求、连贯性、来源使用及表达质量评分；每项1至3分，1为不能接受，2为可用，3为满意。</p>" + manifest_section + "<nav>" + navigation + "</nav>" + "".join(sections) + "</html>"
    descriptor = os.open(directory / "samples.html", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(page)
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in ("samples.json", "samples.html")}


def verify_existing(verification_id: str) -> dict:
    directory = ROOT / ".intermediate" / "agent-smoke" / verification_id
    proof_path = directory / "proof.json"
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    data = within_product(Path(proof["dataDirectory"]))
    recovered = proof.get("recovered") is True
    if recovered:
        if proof["completed"] or proof["restartVerified"] or proof["beforePid"] is not None or proof["afterPid"] is not None or proof["snapshotSha256"] is not None or proof["sampleFiles"]:
            raise ValueError("恢复的部分验证不能包含完整验收或进程重启声明")
        if digest(database_snapshot(data)) != proof["recoverySnapshotSha256"] or hashlib.sha256((directory / "requests.json").read_bytes()).hexdigest() != proof["requestFileSha256"]:
            raise ValueError("部分验证的真实数据库或原请求文件与恢复证明不一致")
    else:
        if not proof["restartVerified"] or digest(database_snapshot(data)) != proof["snapshotSha256"]:
            raise ValueError("原实际重启证明与当前持久数据不一致")
        for name in ("before-restart.json", "after-restart.json"):
            if digest(json.loads((directory / name).read_text(encoding="utf-8"))) != proof["snapshotSha256"]:
                raise ValueError("实际重启前后的原始数据快照与保存哈希不一致")
    for name, expected in proof["sampleFiles"].items():
        if name not in {"samples.json", "samples.html"} or hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError("真实内容样例文件与原保存哈希不一致")
    calls = call_records(data, restarted=proof["restartVerified"], success=proof["completed"])
    if calls != proof["calls"] or accounting(data) != proof["accounting"]:
        raise ValueError("已有实际调用、原始时间或费用记录发生变化")
    source = within_product(Path(proof["providerDataDirectory"]))
    with read_database(source) as connection:
        reservation = connection.execute("SELECT * FROM budget_reservations WHERE id=? AND purpose='real-agent-smoke'", (verification_id,)).fetchone()
        budget = connection.execute("SELECT * FROM budgets WHERE id='global'").fetchone()
        recorded = connection.execute("SELECT COALESCE(SUM(cost_micros),0) FROM budget_reservations").fetchone()[0]
        ledger = connection.execute("SELECT COALESCE(SUM(cost_micros),0) FROM text_usage_ledger").fetchone()[0]
        pending = connection.execute("SELECT COALESCE(SUM(reserved_micros),0) FROM budget_reservations").fetchone()[0]
        pending_text = connection.execute("SELECT COALESCE(SUM(reserved_cost_micros),0) FROM text_generations").fetchone()[0]
        if reservation is None or reservation["cost_micros"] != proof["accounting"]["spentMicros"] or reservation["reserved_micros"] != proof["accounting"]["reservedMicros"]:
            raise ValueError("主预算与原Agent验证的实际结算不一致")
        if budget is None or budget["text_spent_micros"] != recorded + ledger or budget["text_reserved_micros"] != pending + pending_text:
            raise ValueError("主预算与所有已发生费用及未结预留不守恒")
    registry = load_agent_evidence()
    if any(not any(entry["callId"] == call["callId"] for entry in registry["records"]) for call in calls):
        raise ValueError("原实际调用缺少正式登记")
    result = {"verificationId": verification_id, "readonlyVerified": True, "calls": len(calls), "costMicros": proof["accounting"]["spentMicros"]}
    if recovered:
        result.update(completed=False, restartVerified=False, recovered=True)
    if "selectedRoles" in proof:
        selected_roles = proof["selectedRoles"]
        if not selected_roles or len(selected_roles) != len(set(selected_roles)) or not set(selected_roles) <= set(ROLES):
            raise ValueError("原验证的角色范围记录无效")
        if proof.get("fullStageCoverage") != (proof["completed"] and set(selected_roles) == set(ROLES) and "revisionLineage" not in proof):
            raise ValueError("原验证的九角色验收覆盖记录不一致")
        result.update(selectedRoles=selected_roles, fullStageCoverage=proof["fullStageCoverage"])
    return result


def read_stored_samples(directory: Path, samples: list[dict], *, require_completed: bool = False) -> dict:
    if not samples or len(samples) > len(ROLES) or len({sample["role"] for sample in samples}) != len(samples) or any(sample["role"] not in ROLES for sample in samples):
        raise ValueError("评分样例须包含实际生成且角色唯一的有效任务")
    verified = {}
    with read_database(directory / "data") as database:
        for sample in samples:
            run = database.execute("SELECT * FROM agent_runs WHERE id=? AND project_id=? AND role=?", (sample["runId"], sample["projectId"], sample["role"])).fetchone()
            canvas = database.execute("SELECT data_json FROM canvases WHERE project_id=? AND scene_id=? AND owner_id=?", (sample["projectId"], sample["sceneId"], run["owner_id"])).fetchone() if run else None
            if run is None or canvas is None or require_completed and run["status"] != "completed":
                raise ValueError("实际样例任务、归属、完成状态或画布不存在")
            if run["goal"] != sample["goal"] or json.loads(run["target_ids_json"]) != [sample["targetId"]]:
                raise ValueError("样例请求与实际任务目标不一致")
            target = next(node for node in json.loads(canvas["data_json"])["nodes"] if node["id"] == sample["targetId"])
            output = target.get("metadata", {}).get("content")
            if target["type"] != "text" or not isinstance(output, str) or not output.strip():
                raise ValueError("实际样例尚未保存文字产物")
            receipts = [receipt for step in database.execute("SELECT tool_results_json FROM agent_steps WHERE run_id=? ORDER BY \"index\"", (run["id"],)) for receipt in json.loads(step["tool_results_json"])]
            professional = "artifactContractVersion" in sample
            delivery = verify_artifact_delivery(sample, target, receipts) if professional else None
            write_tool = "patch_artifact" if sample.get("revisionMethod") == "patch" else "write_artifact" if professional else "write_script"
            writes = [receipt for receipt in receipts if receipt["tool"] == write_tool and receipt["arguments"].get("targetId") == sample["targetId"]]
            if not writes or write_tool != "patch_artifact" and len(writes) != 1 or not professional and writes[-1]["arguments"].get("text") != output:
                raise ValueError("实际产物缺少唯一真实写入回执或原始正文不一致")
            for receipt in receipts:
                operation = database.execute("SELECT response_json FROM operations WHERE owner_id=? AND project_id=? AND operation_id=? AND kind='agent.tool'", (run["owner_id"], run["project_id"], receipt["operationId"])).fetchone()
                if operation is None or json.loads(operation["response_json"]) != {"result": receipt["result"], "revision": receipt["revision"]}:
                    raise ValueError("实际工具回执和操作记录不一致")
            history = database.execute("SELECT canvases_json FROM project_versions WHERE owner_id=? AND project_id=? AND revision=?", (run["owner_id"], run["project_id"], writes[-1]["revision"])).fetchone()
            if history is None:
                raise ValueError("实际产物缺少原保存版本")
            saved_canvas = next(item for item in json.loads(history["canvases_json"]) if item["sceneId"] == sample["sceneId"])
            saved_target = next(item for item in saved_canvas["nodes"] if item["id"] == sample["targetId"])
            if saved_target.get("metadata", {}).get("content") != output or professional and saved_target.get("metadata", {}).get("creativeArtifact") != delivery["artifact"]:
                raise ValueError("当前产物与原保存版本不一致，不能作为原始生成样例")
            verify_revision_delivery(database, sample, target, receipts)
            verified[run["id"]] = {"output": output, "qualityIssues": [], "delivery": delivery, "writeReceipt": writes[-1],
                "operationIds": [receipt["operationId"] for receipt in receipts]}
    return verified


def load_completed_bundle(verification_id: str, *, require_full_stage: bool = False) -> dict:
    identity = str(uuid.UUID(verification_id))
    directory = ROOT / ".intermediate" / "agent-smoke" / identity
    proof_bytes = (directory / "proof.json").read_bytes()
    proof = json.loads(proof_bytes)
    if proof.get("verificationId") != identity or within_product(Path(proof["dataDirectory"])) != (directory / "data").resolve():
        raise ValueError("来源证明的标识、实际数据库与归档目录不一致")
    if proof["completed"] is not True or proof["restartVerified"] is not True or require_full_stage and proof.get("fullStageCoverage") is not True:
        raise ValueError("来源须为完整通过真实运行及重启复核的证明；九角色来源须覆盖全部岗位")
    verify_existing(identity)
    samples = json.loads((directory / "requests.json").read_text(encoding="utf-8"))
    if ({sample["runId"] for sample in samples} != set(proof["runIds"])
            or "selectedRoles" in proof and {sample["role"] for sample in samples} != set(proof["selectedRoles"])
            or require_full_stage and {sample["role"] for sample in samples} != set(ROLES)):
        raise ValueError("来源样例与原证明任务或九角色范围不一致")
    verified = read_stored_samples(directory, samples, require_completed=True)
    stored = json.loads((directory / "samples.json").read_text(encoding="utf-8"))
    if len(stored) != len(samples):
        raise ValueError("原报告与实际任务的样例数量不一致")
    for sample in samples:
        report = next(item for item in stored if item["runId"] == sample["runId"] and item["role"] == sample["role"])
        actual = verified[sample["runId"]]
        if actual["delivery"] is None or report["output"] != actual["output"] or report["delivery"] != actual["delivery"]:
            raise ValueError("完整专业样例报告与当前真实保存产物不一致")
        if "revisionLineage" in sample and report.get("revisionLineage") != sample["revisionLineage"]:
            raise ValueError("修订报告缺少实际归档来源")
        if "revisionMethod" in sample and (report.get("revisionMethod") != sample["revisionMethod"] or report.get("revisionInputArtifact") != sample["revisionInputArtifact"]):
            raise ValueError("修订报告的明确方式或完整结构化原稿与实际请求不一致")
    if (directory / "proof.json").read_bytes() != proof_bytes:
        raise ValueError("只读核对期间原证明发生变化")
    return {"verificationId": identity, "directory": directory, "proof": proof, "proofSha256": hashlib.sha256(proof_bytes).hexdigest(), "samples": samples, "verified": verified}


def revision_base(bundle: dict) -> dict:
    if bundle["proof"].get("completed") is not True or bundle["proof"].get("restartVerified") is not True:
        raise ValueError("修订基准须完整通过真实运行及重启复核")
    lineage = bundle["proof"].get("revisionLineage")
    if "revisionLineage" not in bundle["proof"]:
        if any("revisionLineage" in sample for sample in bundle["samples"]):
            raise ValueError("独立角色基准不能包含未登记的修订来源")
        return bundle
    if not isinstance(lineage, dict) or not lineage.get("baseVerificationId"):
        raise ValueError("修订来源须关联完整通过的基准证明")
    base = load_completed_bundle(lineage["baseVerificationId"])
    if "revisionLineage" in base["proof"] or base["proofSha256"] != lineage.get("baseProofSha256"):
        raise ValueError("修订来源的独立基准或原保存哈希不一致")
    return base


def revision_ancestry(bundle: dict, base: dict, role: str) -> dict[str, dict]:
    if role not in ROLES or not any(sample["role"] == role for sample in base["samples"]):
        raise ValueError("修订基准没有所选角色的完整专业产物")
    chain, current = {}, bundle
    while current["verificationId"] != base["verificationId"]:
        identity = current["verificationId"]
        if identity in chain:
            raise ValueError("修订证明的归档来源存在循环")
        chain[identity] = current
        group = current["proof"].get("revisionLineage", {})
        if group.get("baseVerificationId") != base["verificationId"] or group.get("baseProofSha256") != base["proofSha256"]:
            raise ValueError("修订证明没有关联所选独立基准")
        sample = next((item for item in current["samples"] if item["role"] == role), None)
        if sample is None or role not in group.get("roles", {}):
            raise ValueError("修订来源链缺少所选角色")
        if "revisionMethod" in group and sample.get("revisionMethod") != group["revisionMethod"]:
            raise ValueError("修订来源链的明确执行方式与实际样例不一致")
        lineage = sample.get("revisionLineage", {})
        parent = base if group["sourceVerificationId"] == base["verificationId"] else load_completed_bundle(group["sourceVerificationId"])
        original = next((item for item in parent["samples"] if item["role"] == role), None)
        if original is None or group.get("sourceProofSha256") != parent["proofSha256"]:
            raise ValueError("修订的直接来源证明、哈希或岗位不一致")
        actual = parent["verified"][original["runId"]]
        write = actual["writeReceipt"]
        call_id = write["operationId"].removeprefix("agent-")
        call = next(item for item in parent["proof"]["calls"] if item["callId"] == call_id and item["runId"] == original["runId"])
        if (lineage != group["roles"][role] or lineage.get("sourceVerificationId") != parent["verificationId"] or lineage.get("sourceProofSha256") != parent["proofSha256"]
                or lineage.get("sourceRunId") != original["runId"] or lineage.get("sourceArtifactSha256") != actual["delivery"]["artifactSha256"]
                or lineage.get("sourceContentSha256") != actual["delivery"]["contentSha256"] or sample.get("revisionInputText") != actual["output"]
                or lineage.get("sourceWriteCallId") != call_id or lineage.get("sourceInputSha256") != call["inputSha256"] or lineage.get("sourceOutputSha256") != call["outputSha256"]
                or group.get("feedback", {}).get("sourceVerificationId") != parent["verificationId"]
                or sample.get("revisionFeedback") != group.get("feedback", {}).get("roles", {}).get(role) or lineage.get("feedbackSha256") != digest(sample["revisionFeedback"])):
            raise ValueError("修订角色的原完整正文、实际写入、反馈或归档来源不一致")
        current = parent
    chain[base["verificationId"]] = base
    return chain


def load_revision_inputs(verification_id: str, feedback_path: Path, selected_roles: list[str]) -> tuple[dict, dict]:
    bundle = load_completed_bundle(verification_id)
    base = revision_base(bundle)
    feedback_bytes = within_product(feedback_path).read_bytes()
    feedback = RevisionFeedback.model_validate_json(feedback_bytes)
    if feedback.sourceVerificationId != bundle["verificationId"] or not set(selected_roles) <= set(feedback.roles):
        raise ValueError("反馈的来源证明或所选岗位与本次修订范围不一致")
    inputs = {}
    for role in selected_roles:
        revision_ancestry(bundle, base, role)
        sample = next((item for item in bundle["samples"] if item["role"] == role), None)
        if sample is None:
            raise ValueError("修订来源没有所选角色的完整产物")
        actual = bundle["verified"][sample["runId"]]
        delivery = actual["delivery"]
        write = actual["writeReceipt"]
        call_id = write["operationId"].removeprefix("agent-")
        call = next(item for item in bundle["proof"]["calls"] if item["callId"] == call_id and item["runId"] == sample["runId"])
        if call["result"] != "通过" or not call["structureValid"]:
            raise ValueError("修订来源没有通过原真实写入调用核对")
        lineage = {"sourceVerificationId": bundle["verificationId"], "sourceProofSha256": bundle["proofSha256"], "sourceRunId": sample["runId"],
            "baseVerificationId": base["verificationId"], "baseProofSha256": base["proofSha256"],
            "sourceProjectId": sample["projectId"], "sourceSceneId": sample["sceneId"], "sourceTargetId": sample["targetId"], "sourceProjectRevision": write["revision"],
            "sourceArtifactSha256": delivery["artifactSha256"], "sourceContentSha256": delivery["contentSha256"], "sourceWriteCallId": call_id,
            "sourceWriteOperationId": write["operationId"], "sourceInputSha256": call["inputSha256"], "sourceOutputSha256": call["outputSha256"],
            "feedbackFileSha256": hashlib.sha256(feedback_bytes).hexdigest(), "feedbackSha256": digest(feedback.roles[role]), "referenceScope": "archive"}
        inputs[role] = {"originalText": actual["output"], "originalArtifact": delivery["artifact"], "feedback": feedback.roles[role], "lineage": lineage}
    return inputs, {"sourceVerificationId": bundle["verificationId"], "sourceProofSha256": bundle["proofSha256"],
        "baseVerificationId": base["verificationId"], "baseProofSha256": base["proofSha256"],
        "feedbackFileSha256": hashlib.sha256(feedback_bytes).hexdigest(), "feedback": feedback.model_dump(), "roles": {role: item["lineage"] for role, item in inputs.items()}}


def main() -> None:
    parser = argparse.ArgumentParser(description="通过真实DeepSeek Flash、项目记忆和write_artifact验证九角色专业交付，每角色最高10元、主预算最高预留90元；实际重启后自动登记阶段2-2证据")
    parser.add_argument("--provider-data", type=Path, default=ROOT / "data")
    parser.add_argument("--timeout", type=float, default=1800, help="每个真实角色任务等待秒数")
    parser.add_argument("--roles", nargs="+", choices=list(ROLES), help="仅验证指定角色的协议兼容性；默认验证全部九角色，阶段验收需要九角色齐备")
    parser.add_argument("--revision-from", help="以完整核验通过的独立角色、九角色或关联修订证明作为反馈修订来源，新建独立项目和证明")
    parser.add_argument("--feedback-file", type=Path, help="真实审阅反馈JSON；与--revision-from及--roles共同使用")
    parser.add_argument("--revision-method", choices=("patch", "write"), help="反馈修订方式，默认patch；write明确授权一次完整专业写入，不自动切换")
    parser.add_argument("--verify-run", help="只读复核已有验证ID，不新增模型调用或费用")
    parser.add_argument("--recover-run", help="恢复已结束验证的费用和失败证据登记，不提交模型请求")
    parser.add_argument("--check-contracts", action="store_true", help="只检查九角色Schema、输出Token限制和预算，不读取凭据或调用模型")
    arguments = parser.parse_args()
    selected_roles = arguments.roles if arguments.roles is not None else list(ROLES)
    if len(selected_roles) != len(set(selected_roles)):
        parser.error("同一角色不能重复验证")
    if arguments.roles is not None and (arguments.verify_run or arguments.recover_run):
        parser.error("只读复核与恢复登记使用原验证的角色范围")
    if bool(arguments.revision_from) != bool(arguments.feedback_file):
        parser.error("反馈修订须同时提供--revision-from与--feedback-file")
    if arguments.revision_from and (arguments.roles is None or arguments.verify_run or arguments.recover_run):
        parser.error("反馈修订须明确--roles，并使用独立的新任务")
    if arguments.revision_method is not None and not arguments.revision_from:
        parser.error("--revision-method须与实际反馈修订来源共同使用")
    revision_inputs, revision_lineage = ({}, None)
    if arguments.revision_from:
        revision_inputs, revision_lineage = load_revision_inputs(arguments.revision_from, arguments.feedback_file, selected_roles)
        revision_method = arguments.revision_method or "patch"
        for item in revision_inputs.values():
            item["method"] = revision_method
        revision_lineage["revisionMethod"] = revision_method
    maximum_micros = len(selected_roles) * RUN_LIMITS["maxCostMicros"]
    full_stage_selection = set(selected_roles) == set(ROLES) and revision_lineage is None
    if arguments.check_contracts:
        limits = RunLimits.model_validate(RUN_LIMITS)
        settings = Settings(text_max_output_tokens=limits.max_tokens)
        if len(ROLES) != 9 or MAXIMUM_MICROS != 90000000 or maximum_micros > MAXIMUM_MICROS or limits.max_cost_micros > settings.max_generation_cents * 10000:
            raise ValueError("专业角色验证范围或费用超过本次授权")
        if DEFAULT_AGENT_PROTOCOL != THINKING_JSON_AGENT_PROTOCOL:
            raise ValueError("新角色验证须使用冻结的Flash JSON思考模式")
        schemas = {role: digest(artifact_arguments_schema(role)) for role in selected_roles}
        protocol_schemas = {}
        for protocol in sorted(SUPPORTED_PLATFORM_PROTOCOLS):
            profile = {}
            catalog_version = TOOL_CATALOG_VERSION if is_json_protocol(protocol) else LEGACY_TOOL_CATALOG_VERSION
            tools = {role: {phase: agent_tools(role, planning, protocol=protocol, catalog_version=catalog_version) for phase, planning in (("planning", True), ("execution", False))} for role in selected_roles}
            business_tools = {role: {phase: agent_business_tools(role, planning, catalog_version=catalog_version) for phase, planning in (("planning", True), ("execution", False))} for role in selected_roles}
            responses = {phase: agent_response_schema(planning, protocol=protocol) for phase, planning in (("planning", True), ("execution", False))}
            if is_json_protocol(protocol):
                body = agent_request_body([{"role": "system", "content": "返回有效json对象"}, {"role": "user", "content": "检查请求配置"}], limits.max_tokens, [], True, settings, protocol=protocol, model=MODEL, catalog_version=catalog_version)
                if body["model"] != MODEL or any(definitions for phases in tools.values() for definitions in phases.values()) or "tools" in body or body.get("response_format") != {"type": "json_object"} or body.get("tool_choice") != "none":
                    raise ValueError("JSON角色请求未正确配置官方JSON Output与原生函数禁用规则")
                if protocol == THINKING_JSON_AGENT_PROTOCOL:
                    if body.get("thinking") != {"type": "enabled"} or body.get("reasoning_effort") != "high" or "temperature" in body:
                        raise ValueError("新JSON协议缺少冻结的思考模式与high推理设置")
                elif protocol == JSON_AGENT_PROTOCOL:
                    if body.get("thinking") != {"type": "disabled"} or body.get("temperature") != 0.4 or "reasoning_effort" in body:
                        raise ValueError("既有JSON协议的冻结请求参数发生变化")
                else:
                    raise ValueError("当前JSON协议缺少明确的参数核验要求")
                profile = {"thinking": body["thinking"], "reasoningEffort": body.get("reasoning_effort"), "temperature": body.get("temperature")}
            protocol_schemas[protocol] = {"rolePhaseCount": len(selected_roles) * 2, "toolCatalogVersion": catalog_version, "toolsSha256": digest(tools), "businessToolsSha256": digest(business_tools), "responseSchemasSha256": digest(responses), "providerEndpoint": provider_endpoint(protocol), **profile}
        print(json.dumps({"roleCount": len(selected_roles), "selectedRoles": selected_roles, "fullStageSelection": full_stage_selection,
            "stageRequiredRoleCount": len(ROLES), "maxOutputTokens": limits.max_tokens, "maxCostMicrosPerRole": limits.max_cost_micros,
            "maxTotalCostMicros": maximum_micros, "professionalModel": MODEL, "ordinaryTextModel": MODEL,
            "artifactContractVersion": ARTIFACT_CONTRACT_VERSION, "schemas": schemas, "defaultProtocol": DEFAULT_AGENT_PROTOCOL, "platformProtocols": protocol_schemas,
            "revisionMethod": revision_lineage["revisionMethod"] if revision_lineage is not None else None,
            **({"revisionLineage": {key: value for key, value in revision_lineage.items() if key != "feedback"}, "revisionInputContentSha256": {role: hashlib.sha256(item["originalText"].encode()).hexdigest() for role, item in revision_inputs.items()}} if revision_lineage is not None else {}),
            "modelRequests": 0}, ensure_ascii=False))
        return
    if arguments.verify_run:
        verification_id = str(uuid.UUID(arguments.verify_run))
        print(json.dumps(verify_existing(verification_id), ensure_ascii=False))
        return
    if arguments.recover_run:
        verification_id = str(uuid.UUID(arguments.recover_run))
        directory = ROOT / ".intermediate" / "agent-smoke" / verification_id
        data = within_product(directory / "data")
        proof_path = directory / "proof.json"
        if proof_path.exists():
            if json.loads(proof_path.read_text(encoding="utf-8")).get("recovered") is not True:
                raise ValueError("已有正式证明保持原样，请使用只读复核")
            result = verify_existing(verification_id)
            print(json.dumps({**result, "registeredCalls": 0}, ensure_ascii=False))
            return
        if not sqlite_database_path(data).is_file():
            raise ValueError("恢复登记要求已有真实数据库")
        with read_database(data) as connection:
            runs = [dict(row) for row in connection.execute("SELECT id,role,status,limits_json FROM agent_runs")]
        if not runs or any(run["status"] not in TERMINAL for run in runs):
            raise ValueError("恢复登记要求原真实任务已经结束")
        request_bytes = (directory / "requests.json").read_bytes()
        samples = json.loads(request_bytes)
        if {sample["runId"] for sample in samples} != {run["id"] for run in runs} or len(samples) != len(runs):
            raise ValueError("恢复登记的原请求文件与实际任务不一致")
        source = within_product(arguments.provider_data)
        with read_database(source) as connection:
            reservation = connection.execute("SELECT details_json FROM budget_reservations WHERE id=? AND purpose='real-agent-smoke'", (verification_id,)).fetchone()
        if reservation is None:
            raise ValueError("恢复登记缺少原主预算预留")
        details = json.loads(reservation["details_json"])
        selected_roles = details["roles"]
        run_roles = {run["id"]: run["role"] for run in runs}
        if Path(details["isolatedData"]).resolve() != data or [sample["role"] for sample in samples] != selected_roles or any(run_roles[sample["runId"]] != sample["role"] for sample in samples):
            raise ValueError("恢复登记的真实数据库、角色范围与原预算不一致")
        if any(any(json.loads(run["limits_json"])[key] != value for key, value in details["limits"].items()) for run in runs):
            raise ValueError("恢复登记的真实任务限制与原预算不一致")
        settings = Settings(data_dir=source)
        source_engine = create_engine(settings.database_url, connect_args={"timeout": 10})
        budget = reconcile(source_engine, data, verification_id)
        calls = call_records(data, restarted=False, success=False)
        proof = {"schemaVersion": 1, "verificationId": verification_id, "dataDirectory": str(data), "providerDataDirectory": str(source),
            "completed": False, "restartVerified": False, "beforePid": None, "afterPid": None, "snapshotSha256": None,
            "recovered": True, "recoverySnapshotSha256": digest(database_snapshot(data)), "requestFileSha256": hashlib.sha256(request_bytes).hexdigest(),
            "selectedRoles": selected_roles, "fullStageCoverage": False,
            **({"revisionLineage": details["revisionLineage"]} if "revisionLineage" in details else {}),
            "sampleFiles": {}, "calls": calls, "accounting": accounting(data), "budget": budget,
            "runIds": [sample["runId"] for sample in samples]}
        write_private_json(proof_path, proof)
        registered = register_agent_evidence(calls, proof_path) if calls else 0
        source_engine.dispose()
        print(json.dumps({"verificationId": verification_id, "completed": False, "recovered": True, "registeredCalls": registered,
            "costMicros": budget["spentMicros"], "reservedMicros": budget["reservedMicros"]}, ensure_ascii=False))
        return
    if not 30 <= arguments.timeout <= 1800:
        raise ValueError("真实角色等待时间必须在30至1800秒之间")
    os.environ["SCENETURN_TEXT_MAX_OUTPUT_TOKENS"] = str(RUN_LIMITS["maxTokens"])
    os.environ["SCENETURN_TEXT_TIMEOUT"] = "600"
    RunLimits.model_validate(RUN_LIMITS)
    if Settings().text_max_output_tokens != RUN_LIMITS["maxTokens"]:
        raise ValueError("专业角色验收需要服务端允许32768输出Token")
    source = within_product(arguments.provider_data)
    if not sqlite_database_path(source).is_file():
        raise ValueError("请先在本产品安全设置保存真实DeepSeek配置")
    environment = {**os.environ, "SCENETURN_DATA_DIR": str(source)}
    subprocess.run([str(ROOT / "backend" / ".venv" / "bin" / "python"), "-m", "alembic", "upgrade", "head"], cwd=ROOT / "backend", env=environment, check=True, capture_output=True)
    settings = Settings(data_dir=source)
    source_engine = create_engine(settings.database_url, connect_args={"timeout": 10})
    with Session(source_engine) as session:
        provider = session.get(TextProviderSettings, "platform")
        if provider is None or not provider.api_key_encrypted:
            raise ValueError("真实平台DeepSeek配置缺失，本次未发送模型请求")
        secret = decode_secret(provider.api_key_encrypted, settings)
    verification_id = str(uuid.uuid4())
    directory = ROOT / ".intermediate" / "agent-smoke" / verification_id
    directory.mkdir(parents=True, mode=0o700)
    data = directory / "data"
    if revision_lineage is not None:
        write_private_json(directory / "revision-feedback.json", revision_lineage["feedback"])
    with Session(source_engine) as session:
        lock_write(session)
        _reserve(session, maximum_micros, settings)
        session.add(BudgetReservation(id=verification_id, purpose="real-agent-smoke", reserved_micros=maximum_micros, cost_micros=0, status="reserved",
            details_json={"isolatedData": str(data), "roles": selected_roles, "limits": RUN_LIMITS,
                **({"revisionLineage": revision_lineage} if revision_lineage is not None else {})}))
        session.commit()
    admin, password = f"agent-smoke-{uuid.uuid4().hex}@example.com", secrets.token_urlsafe(24)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process, completed, restarted = None, False, False
    samples, verified, sample_files = [], {}, {}
    before_pid, after_pid, snapshot_sha = None, None, None
    with (directory / "service.log").open("w", encoding="utf-8") as log:
        try:
            process = start_service(data, admin, port, log)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", headers={"Origin": "http://127.0.0.1:3210"}, timeout=20, trust_env=False) as client:
                response_json(client.post("/auth/register", json={"email": admin, "password": password}))
                response_json(client.put("/settings/text", json={"provider": "deepseek", "model": MODEL, "apiKey": secret}))
                del secret
                for role in selected_roles:
                    sample = create_sample(client, role, revision_inputs.get(role))
                    samples.append(sample)
                    write_private_json(directory / "requests.json", samples)
                    run = wait_for_sample(client, sample, arguments.timeout)
                    verified[sample["runId"]] = verify_sample(client, data, sample, run)
                    if len(run["calls"]) > RUN_LIMITS["maxSteps"] or run["usage"]["costMicros"] > RUN_LIMITS["maxCostMicros"] or run["usage"]["reservedCostMicros"]:
                        raise ValueError("实际角色用量超过本次授权或存在未结预留")
                    delivery = verified[sample["runId"]]["delivery"]
                    print(json.dumps({"phase": "role_completed", "verificationId": verification_id, "role": role, "runId": run["id"], "calls": len(run["calls"]),
                        "totalTokens": run["usage"]["totalTokens"], "costMicros": run["usage"]["costMicros"], "artifactContractVersion": delivery["artifactContractVersion"],
                        "artifactSha256": delivery["artifactSha256"], "imagePromptCount": len(image_prompt_items(delivery["artifact"]))}, ensure_ascii=False), flush=True)
                sample_files = save_samples(directory, samples, verified)
                before = database_snapshot(data)
                if before["assets"] or before["generations"] or before["text_generations"] or before["text_usage_ledger"]:
                    raise ValueError("真实角色验收出现未授权的媒体或其他生成任务")
                write_private_json(directory / "before-restart.json", before)
                snapshot_sha, before_pid = digest(before), process.pid
                stop_service(process)
                if process.poll() is None:
                    raise ValueError("隔离服务尚未实际停止")
                process = start_service(data, admin, port, log)
                after_pid = process.pid
                if before_pid == after_pid:
                    raise ValueError("隔离服务没有实际更换进程")
                for sample in samples:
                    run = response_json(client.get(f"/projects/{sample['projectId']}/agent-runs/{sample['runId']}"))["run"]
                    after = verify_sample(client, data, sample, run)
                    if after != verified[sample["runId"]]:
                        raise ValueError("实际重启后角色任务、工具结果、记忆或目标正文发生变化")
                after = database_snapshot(data)
                if digest(after) != snapshot_sha:
                    raise ValueError("实际重启后调用、项目历史、记忆及费用账目发生变化")
                write_private_json(directory / "after-restart.json", after)
                if any(hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected for name, expected in sample_files.items()):
                    raise ValueError("实际重启后真实内容样例文件发生变化")
                completed, restarted = True, True
        finally:
            stop_service(process)
            stop_pending_runs(data, verification_id)
            budget = reconcile(source_engine, data, verification_id)
            calls = call_records(data, restarted=restarted, success=completed)
            proof = {"schemaVersion": 1, "verificationId": verification_id, "dataDirectory": str(data), "providerDataDirectory": str(source), "completed": completed,
                "selectedRoles": selected_roles, "fullStageCoverage": completed and full_stage_selection,
                **({"revisionLineage": revision_lineage} if revision_lineage is not None else {}),
                "restartVerified": restarted, "beforePid": before_pid, "afterPid": after_pid, "snapshotSha256": snapshot_sha,
                "sampleFiles": sample_files, "calls": calls, "accounting": accounting(data), "budget": budget,
                "runIds": [sample["runId"] for sample in samples]}
            proof_path = directory / "proof.json"
            write_private_json(proof_path, proof)
            registered = register_agent_evidence(calls, proof_path) if calls else 0
            source_engine.dispose()
            print(json.dumps({"verificationId": verification_id, "completed": completed, "restartVerified": restarted, "calls": len(calls),
                "selectedRoles": selected_roles, "fullStageCoverage": completed and full_stage_selection,
                "registeredCalls": registered, "costMicros": budget["spentMicros"], "reservedMicros": budget["reservedMicros"],
                "samples": str((directory / "samples.html").relative_to(ROOT)) if completed else None, "proof": str(proof_path.relative_to(ROOT))}, ensure_ascii=False), flush=True)
    if not completed:
        raise RuntimeError("真实角色验收尚未完成")


if __name__ == "__main__":
    main()
