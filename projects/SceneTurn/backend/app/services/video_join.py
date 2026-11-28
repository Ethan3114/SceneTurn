import asyncio
import hashlib
import subprocess
import tempfile
from pathlib import Path

from app.core.config import Settings
from app.core.errors import AppError
from app.services.media import ProbeResult, StoredMedia, _validate_and_store, resolve_asset_path


async def join_videos(inputs: list[dict], settings: Settings) -> StoredMedia:
    return await asyncio.to_thread(_join_videos, inputs, settings)


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    if result.returncode != 0:
        raise AppError(422, "VIDEO_JOIN_FAILED", "视频拼接未成功，来源素材和任务记录已保留")
    return result


def _join_videos(inputs: list[dict], settings: Settings) -> StoredMedia:
    paths: list[Path] = []
    probes: list[ProbeResult] = []
    for item in inputs:
        path = resolve_asset_path(settings, item["relpath"])
        with path.open("rb") as stream:
            sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != item["bytes"] or sha256 != item["sha256"]:
            raise AppError(409, "VIDEO_JOIN_SOURCE_CHANGED", "原视频文件与保存记录不一致，拼接已终止")
        result = _run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-show_format", "-of", "json", str(path)], 30)
        probe = ProbeResult.model_validate_json(result.stdout)
        if not any(stream.codec_type == "video" for stream in probe.streams):
            raise AppError(422, "VIDEO_JOIN_SOURCE_INVALID", "参考素材缺少视频画面")
        paths.append(path)
        probes.append(probe)
    width = max(2, inputs[0]["width"] // 2 * 2)
    height = max(2, inputs[0]["height"] // 2 * 2)
    command = ["ffmpeg", "-nostdin", "-v", "error", "-xerror"]
    filters = []
    concat_inputs = []
    for index, (path, probe, item) in enumerate(zip(paths, probes, inputs, strict=True)):
        command.extend(["-protocol_whitelist", "file,pipe", "-i", str(path)])
        duration = item["durationMs"] / 1000
        filters.append(f"[{index}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,trim=duration={duration},setpts=PTS-STARTPTS[v{index}]")
        if any(stream.codec_type == "audio" for stream in probe.streams):
            filters.append(f"[{index}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=duration={duration},asetpts=PTS-STARTPTS[a{index}]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration},asetpts=PTS-STARTPTS[a{index}]")
        concat_inputs.append(f"[v{index}][a{index}]")
    filters.append("".join(concat_inputs) + f"concat=n={len(paths)}:v=1:a=1[vout][aout]")
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="join-", dir=settings.tmp_dir) as working:
        output = Path(working) / "joined.mp4"
        command.extend(["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "-y", str(output)])
        _run(command, 600)
        if not output.is_file() or output.stat().st_size > settings.max_upload_bytes:
            raise AppError(413, "VIDEO_JOIN_OUTPUT_TOO_LARGE", "拼接文件超过当前允许的素材大小")
        return _validate_and_store(output, settings, "拼接成片.mp4", True)
