import copy
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.deps import current_user, get_db
from app.core.config import get_settings
from app.core.errors import AppError
from app.models import Asset, Operation, User
from app.provider_models import ModelProviderConnection
from app.schemas import WireModel
from app.services.projects import get_operation, lock_write, require_project, save_operation
from app.services.media import resolve_asset_path


router = APIRouter(tags=["local-audio"])
Mode = Literal["music", "effects", "voice"]
ROOT = "http://127.0.0.1:8188"
SAVERS = {"SaveAudio", "SaveAudioMP3", "SaveAudioOpus"}
LOCAL_TYPES = SAVERS | {"CheckpointLoaderSimple", "UNETLoader", "CLIPLoader", "DualCLIPLoader", "TripleCLIPLoader", "VAELoader",
                        "KSampler", "KSamplerAdvanced", "ConditioningZeroOut", "ConditioningCombine", "CLIPTextEncode",
                        "ConditioningStableAudio", "EmptyLatentAudio", "EmptyAceStepLatentAudio", "EmptyAceStep1.5LatentAudio",
                        "TextEncodeAceStepAudio", "TextEncodeAceStepAudio1.5", "VAEDecodeAudio", "ModelSamplingSD3",
                        "ModelSamplingAuraFlow", "ModelSamplingDiscrete", "PrimitiveString", "PrimitiveStringMultiline",
                        "PrimitiveFloat", "PrimitiveInt", "LoraLoaderModelOnly", "LatentApplyOperationCFG",
                        "LatentOperationTonemapReinhard", "BasicGuider", "CFGGuider", "RandomNoise", "KSamplerSelect",
                        "BasicScheduler", "SamplerCustomAdvanced", "LoadAudio"}


class AudioGraph(WireModel):
    name: str = Field(min_length=1, max_length=80)
    workflow: dict[str, dict] = Field(min_length=1, max_length=80)


class AudioJob(WireModel):
    operation_id: UUID
    mode: Mode
    prompt: str = Field(min_length=1, max_length=4000)
    lyrics: str = Field(default="", max_length=2000)
    reference_asset_id: str | None = Field(default=None,max_length=100)
    voice_consent: bool = False


def config(session: Session, user: User):
    return session.scalar(select(ModelProviderConnection).where(ModelProviderConnection.scope_key == user.id, ModelProviderConnection.owner_id == user.id, ModelProviderConnection.provider_id == "comfy-local-audio"))


def approved_graph(graph: dict):
    configured = get_settings().comfy_trusted_audio_nodes
    trusted = LOCAL_TYPES | {s.strip() for s in configured.split(",") if s.strip()}
    prompts = []
    outputs = []
    for key, node in graph.items():
        kind = node.get("class_type")
        inputs = node.get("inputs")
        if not key.isdecimal() or kind not in trusted or not isinstance(inputs, dict):
            raise AppError(422, "AUDIO_WORKFLOW_NODE_INVALID", "工作流包含未授权的节点。请安装本机音频节点并在服务端设置信任清单。")
        if kind in SAVERS:
            outputs.append(key)
        if kind=="LoadAudio" and inputs.get("audio")!="{{referenceAudio}}":
            raise AppError(422,"AUDIO_REFERENCE_REQUIRED","LoadAudio 的文件输入需使用 {{referenceAudio}}，从当前账号画布读取。")
        for name, value in inputs.items():
            if value == "{{prompt}}":
                prompts.append(key)
            elif isinstance(value, str) and (len(value) > 4000 or re.search(r"[\\]|://|(?:^|/)\.\.(?:/|$)|^/", value)):
                raise AppError(422, "AUDIO_WORKFLOW_INPUT_INVALID", "工作流包含外部地址或不受支持的路径。")
            elif isinstance(value, list) and not (len(value) == 2 and isinstance(value[0], str) and value[0] in graph and type(value[1]) is int):
                raise AppError(422, "AUDIO_WORKFLOW_LINK_INVALID", "工作流节点连线无效。")
            elif isinstance(value, dict):
                raise AppError(422, "AUDIO_WORKFLOW_INPUT_INVALID", "节点输入须为值或连线。")
            if value in ("{{prompt}}","{{lyrics}}") and name not in {"text","tags","lyrics","description","prompt","voice_description","instruct","value"}:
                raise AppError(422, "AUDIO_WORKFLOW_INPUT_INVALID", "提示词只能传给文字输入。")
    if not prompts or len(outputs) != 1:
        raise AppError(422, "AUDIO_WORKFLOW_OUTPUT_INVALID", "工作流须有 {{prompt}} 文字输入及一个 SaveAudio 输出。")
    return outputs[0]


@router.get("/media-tools/audio-workflows")
def list_workflows(user: User = Depends(current_user), session: Session = Depends(get_db)):
    record = config(session, user)
    workflows = record.config_json if record else {}
    return {"models": {k: v["name"] for k, v in workflows.items()}}


@router.put("/media-tools/audio-workflows/{mode}")
def configure(mode: Mode, body: AudioGraph, user: User = Depends(current_user), session: Session = Depends(get_db)):
    if len(body.model_dump_json()) > 96_000:
        raise AppError(413, "AUDIO_WORKFLOW_TOO_LARGE", "工作流超过 96 KB。")
    approved_graph(body.workflow)
    lock_write(session)
    record = config(session, user)
    if not record:
        record = ModelProviderConnection(owner_id=user.id, scope_key=user.id, provider_id="comfy-local-audio",config_json={})
        session.add(record)
    record.config_json = {**record.config_json, mode: body.model_dump()}
    session.commit()
    return {"name": body.name}


def local_request(method, path, **kwargs):
    try:
        with httpx.Client(base_url=ROOT, trust_env=False, timeout=12, follow_redirects=False) as client:
            response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise AppError(503, "LOCAL_AUDIO_OFFLINE", "无法连接本机 ComfyUI，请检查 8188 端口。") from exc
    if response.status_code >= 300:
        raise AppError(422, "LOCAL_AUDIO_REJECTED", "本机 ComfyUI 拒绝了工作流，请检查模型和节点。")
    return response.json()


@router.post("/projects/{project_id}/media-tools/audio-jobs", status_code=202)
def submit(project_id: str, body: AudioJob, user: User = Depends(current_user), session: Session = Depends(get_db)):
    project = require_project(session, project_id, user.id)
    payload = body.model_dump(mode="json")
    op_id = str(body.operation_id)
    lock_write(session)
    previous = get_operation(session, user.id, project.id, op_id, "local-audio", payload)
    if previous:
        return {"id": op_id, "status": previous["status"]}
    record = config(session, user)
    if not record or body.mode not in record.config_json:
        raise AppError(409, "AUDIO_WORKFLOW_REQUIRED", "请先配置当前账号的本机音频模型。")
    if body.mode == "voice" and not 20 <= len(body.prompt.strip()) <= 1000:
        raise AppError(422, "VOICE_DESCRIPTION_INVALID", "音色描述须为 20～1000 字。")
    graph = copy.deepcopy(record.config_json[body.mode]["workflow"])
    output = approved_graph(graph)
    available = local_request("GET","/object_info")
    if any(n["class_type"] not in available for n in graph.values()):
        raise AppError(422, "LOCAL_AUDIO_NODE_MISSING", "本机 ComfyUI 缺少工作流所需的节点。")
    remote_id = str(uuid4())
    reference_nodes = [n for n in graph.values() if n["class_type"]=="LoadAudio"]
    if bool(reference_nodes)!=bool(body.reference_asset_id):
        raise AppError(422,"AUDIO_REFERENCE_MISMATCH","参考音频与工作流不匹配。使用带有 LoadAudio 的工作流时须连接一项音频。")
    if body.reference_asset_id:
        if body.mode=="voice" and not body.voice_consent:
            raise AppError(422,"VOICE_CONSENT_REQUIRED","使用参考声音前请确认已经获得声音所有者授权。")
        asset=session.scalar(select(Asset).where(Asset.id==body.reference_asset_id,Asset.owner_id==user.id,Asset.project_id==project.id))
        if not asset or not asset.mime_type.startswith("audio/"):
            raise AppError(404,"AUDIO_REFERENCE_NOT_FOUND","所选音频不属于当前项目。")
        if asset.bytes>16*1024*1024:
            raise AppError(413,"AUDIO_REFERENCE_TOO_LARGE","参考音频不得超过 16 MiB。")
        path=resolve_asset_path(get_settings(),asset.relpath)
        name=asset.sha256+path.suffix
        subfolder="sceneturn/"+user.id
        with path.open("rb") as stream:
            received=local_request("POST","/upload/image",data={"type":"input","subfolder":subfolder,"overwrite":"true"},files={"image":(name,stream,asset.mime_type)})
        if received.get("name")!=name or received.get("subfolder")!=subfolder:
            raise AppError(502,"AUDIO_REFERENCE_UPLOAD_FAILED","本机模型服务没有确认参考音频。")
        for n in reference_nodes:
            n["inputs"]["audio"]=subfolder+"/"+name
    for n in graph.values():
        for name,v in n["inputs"].items():
            if v == "{{prompt}}": n["inputs"][name] = body.prompt
            elif v == "{{lyrics}}": n["inputs"][name] = body.lyrics
    graph[output]["inputs"]["filename_prefix"] = "sceneturn/" + remote_id
    job = {"remote": remote_id, "output": output, "status":"queued", "mode":body.mode}
    save_operation(session,user.id,project.id,op_id,"local-audio",payload,job)
    session.commit()
    try:
        response = local_request("POST","/prompt", json={"prompt_id":remote_id,"prompt":graph})
    except AppError as exc:
        row = _job(session,user,project_id,body.operation_id)
        row.response_json = {**job,"status":"failed" if exc.status == 422 else "unconfirmed"}
        session.commit()
        return {"id":op_id,"status":row.response_json["status"]}
    returned = str(response.get("prompt_id") or "")
    if not returned:
        row = _job(session,user,project_id,body.operation_id)
        row.response_json = {**job,"status":"unconfirmed"}
        session.commit()
        return {"id":op_id,"status":"unconfirmed"}
    if returned != remote_id:
        row = _job(session,user,project_id,body.operation_id)
        row.response_json = {**job,"remote":returned}
        session.commit()
    return {"id":op_id,"status":"queued"}


def _job(session: Session, user: User, project_id: str, job_id: UUID):
    require_project(session,project_id,user.id)
    row = session.scalar(select(Operation).where(Operation.project_id==project_id,Operation.owner_id==user.id,Operation.operation_id==str(job_id),Operation.kind=="local-audio"))
    if row is None:
        raise AppError(404,"AUDIO_JOB_NOT_FOUND","当前项目中没有这项任务。")
    return row


@router.get("/projects/{project_id}/media-tools/audio-jobs/{job_id}")
def status(project_id: str,job_id: UUID,user: User = Depends(current_user),session: Session=Depends(get_db)):
    row = _job(session,user,project_id,job_id)
    data = row.response_json
    if data["status"] not in {"ready","failed"}:
        history = local_request("GET","/history/"+data["remote"]).get(data["remote"],{})
        if history.get("status",{}).get("status_str") == "error":
            data = {**data,"status":"failed"}
        elif history.get("status",{}).get("completed"):
            audio = history.get("outputs",{}).get(data["output"],{}).get("audio",[])
            if not audio or not isinstance(audio[0],dict):
                data={**data,"status":"failed"}
            else:
                file=audio[0]
                valid = file.get("type")=="output" and Path(file.get("filename","")).name.startswith(data["remote"]) and file.get("subfolder")=="sceneturn" and "/" not in file["filename"] and "\\" not in file["filename"]
                data={**data,"status":"ready","file":file} if valid else {**data,"status":"failed"}
        if data!=row.response_json:
            row.response_json=data
            session.commit()
    return {"id":str(job_id),"status":data["status"]}


@router.get("/projects/{project_id}/media-tools/audio-jobs/{job_id}/audio")
def result(project_id:str,job_id:UUID,user:User=Depends(current_user),session:Session=Depends(get_db)):
    data=_job(session,user,project_id,job_id).response_json
    if data["status"]!="ready":
        raise AppError(409,"AUDIO_JOB_PENDING","音频尚未完成。")
    settings=get_settings()
    settings.tmp_dir.mkdir(parents=True,exist_ok=True)
    working=tempfile.TemporaryDirectory(prefix="audio-export-",dir=settings.tmp_dir)
    try:
        source=Path(working.name)/"source"
        with httpx.Client(base_url=ROOT,trust_env=False,timeout=30,follow_redirects=False) as client, client.stream("GET","/view",params={k:data["file"][k] for k in ("type","subfolder","filename")}) as response, source.open("wb") as stream:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                if stream.tell()+len(chunk)>min(settings.max_upload_bytes,64*1024*1024):
                    raise AppError(413,"AUDIO_RESULT_TOO_LARGE","音频文件超过大小限制。")
                stream.write(chunk)
        output=Path(working.name)/"audio.wav"
        subprocess.run(["ffmpeg","-nostdin","-v","error","-protocol_whitelist","file,pipe","-i",str(source),"-map","0:a:0","-t","600","-ar","24000","-ac","2","-c:a","pcm_s16le",str(output)],check=True,timeout=60,capture_output=True)
        return FileResponse(output,media_type="audio/wav",filename="audio.wav",background=BackgroundTask(working.cleanup),headers={"Cache-Control":"no-store"})
    except (httpx.HTTPError,subprocess.SubprocessError) as exc:
        working.cleanup()
        raise AppError(502,"AUDIO_RESULT_INVALID","无法读取模型输出的音频。") from exc
    except BaseException:
        working.cleanup()
        raise
