import subprocess
from pathlib import Path
from typing import Literal

from pydantic import Field
from PIL import Image, ImageDraw, ImageFont

from app.core.errors import AppError
from app.schemas import WireModel
from app.services.media import ProbeResult


class EditClip(WireModel):
    asset_id: str = Field(min_length=1, max_length=100)
    start: float = Field(ge=0, le=14400, allow_inf_nan=False)
    end: float = Field(gt=0, le=14400, allow_inf_nan=False)
    gain: float = Field(default=1, ge=0, le=2, allow_inf_nan=False)
    fade_in: float = Field(default=0, ge=0, le=60, allow_inf_nan=False)
    fade_out: float = Field(default=0, ge=0, le=60, allow_inf_nan=False)
    offset: float = Field(default=0, ge=0, le=600, allow_inf_nan=False)


class EditCaption(WireModel):
    start: float = Field(ge=0, le=600, allow_inf_nan=False)
    end: float = Field(gt=0, le=600, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=240)


class CropRegion(WireModel):
    x: float = Field(ge=0, lt=1, allow_inf_nan=False)
    y: float = Field(ge=0, lt=1, allow_inf_nan=False)
    right: float = Field(gt=0, le=1, allow_inf_nan=False)
    bottom: float = Field(gt=0, le=1, allow_inf_nan=False)


class MediaEditRequest(WireModel):
    videos: list[EditClip] = Field(default_factory=list, max_length=24)
    audio: list[EditClip] = Field(default_factory=list, max_length=16)
    captions: list[EditCaption] = Field(default_factory=list, max_length=40)
    ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    resolution: Literal[480, 720, 1080] = 720
    fps: Literal[24, 25, 30, 60] = 30
    container: Literal["mp4", "webm", "wav"] = "mp4"
    keep_source_format: bool = False
    crop: CropRegion | None = None


def _invalid(message: str):
    raise AppError(422, "MEDIA_EDIT_INVALID", message)


def _command(command: list[str], working: Path, timeout: int):
    try:
        result = subprocess.run(command, cwd=working, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AppError(504, "MEDIA_EDIT_TIMEOUT", "素材剪辑超时，请缩短片段。") from exc
    if result.returncode:
        raise AppError(422, "MEDIA_EDIT_FAILED", "无法读取或编码所选素材。")
    return result.stdout


def _caption_image(path: Path, text: str, width: int, height: int):
    fonts = [Path("/System/Library/Fonts/STHeiti Medium.ttc"), Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"), Path("C:/Windows/Fonts/msyh.ttc")]
    font_file = next((f for f in fonts if f.is_file()), None)
    if not font_file:
        raise AppError(422, "CAPTION_FONT_MISSING", "导出字幕需要在服务器安装 Noto Sans CJK 中文字体。")
    font = ImageFont.truetype(str(font_file), max(18, height // 22))
    image = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(image)
    lines = []
    line = ""
    for character in text:
        if character == "\n" or font.getlength(line + character) > width * 0.9:
            lines.append(line)
            line = ""
        if character != "\n":
            line += character
    lines.append(line)
    draw.multiline_text((width / 2, height * 0.92), "\n".join(lines), font=font, anchor="md", align="center", fill="white", stroke_width=2, stroke_fill="black")
    image.save(path)


def render_edit(request: MediaEditRequest, paths: dict[str, Path], working: Path, maximum_bytes: int) -> Path:
    clips = [*request.videos, *request.audio]
    if not clips or bool(request.videos) == (request.container == "wav"):
        _invalid("视频导出需要视频片段，WAV 导出需要音轨。")
    duration = sum(c.end - c.start for c in request.videos) if request.videos else max(c.offset + c.end - c.start for c in request.audio)
    if not 0 < duration <= 600:
        _invalid("每次导出的时长须在 10 分钟以内。")
    if any(not 0 <= c.start < c.end <= duration for c in request.captions) or (request.captions and not request.videos):
        _invalid("字幕起止时间须位于视频范围内。")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-filter_complex_threads", "2"]
    probes = {}
    for key, path in paths.items():
        raw = _command(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_format", "-show_streams", "-of", "json", str(path)], working, 25)
        probes[key] = ProbeResult.model_validate_json(raw)
    filters = []
    video_streams = []
    audio_streams = []
    short = request.resolution
    long = round(short * 16 / 9 / 2) * 2
    width, height = {"16:9": (long, short), "9:16": (short, long), "1:1": (short, short)}[request.ratio]
    fps = str(request.fps)
    if request.crop and (not request.keep_source_format or request.crop.right <= request.crop.x or request.crop.bottom <= request.crop.y):
        _invalid("视频裁切需要单独选择一段视频和有效的矩形范围。")
    if request.keep_source_format:
        if len(request.videos) != 1 or request.audio or request.captions:
            _invalid("保留来源画幅时需要单独选择一段视频。")
        source = next((s for s in probes[request.videos[0].asset_id].streams if s.codec_type == "video"), None)
        if not source or min(source.width,source.height)<2 or max(source.width,source.height)>8192:
            _invalid("原视频画幅不受支持。")
        width, height = source.width, source.height
        if source.avg_frame_rate not in {"0/0","0/1"}:
            fps = source.avg_frame_rate
    for index, clip in enumerate(clips):
        probe = probes[clip.asset_id]
        kinds = {s.codec_type for s in probe.streams}
        length = clip.end - clip.start
        is_video = index < len(request.videos)
        if length < 0.04 or clip.end > (probe.format.duration or 0) + 0.06:
            _invalid("片段的入点或出点超出素材时长。")
        if (is_video and ("video" not in kinds or clip.offset)) or (not is_video and ("audio" not in kinds or clip.offset >= duration)):
            _invalid("素材类型或音轨位置与剪辑轨道不匹配。")
        if clip.fade_in + clip.fade_out > length + 0.001:
            _invalid("淡入和淡出时长之和超过片段时长。")
        cmd.extend(["-protocol_whitelist", "file,pipe", "-ss", str(clip.start), "-t", str(length), "-i", str(paths[clip.asset_id])])
        if is_video:
            geometry = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
            if request.crop:
                region = request.crop
                geometry = f"crop=w='max(2,2*trunc(iw*{region.right-region.x}/2))':h='max(2,2*trunc(ih*{region.bottom-region.y}/2))':x=iw*{region.x}:y=ih*{region.y}"
            filters.append(f"[{index}:v:0]{geometry},setsar=1,fps={fps},format=yuv420p,setpts=PTS-STARTPTS[v{index}]")
            video_streams.append(f"[v{index}][a{index}]")
        audio = f"[{index}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad" if "audio" in kinds else "anullsrc=r=48000:cl=stereo"
        audio += f",atrim=duration={length},asetpts=PTS-STARTPTS,volume={clip.gain}"
        if clip.fade_in:
            audio += f",afade=t=in:d={clip.fade_in}"
        if clip.fade_out:
            audio += f",afade=t=out:st={length - clip.fade_out}:d={clip.fade_out}"
        if not is_video:
            audio += f",adelay={round(clip.offset * 1000)}:all=1"
            audio_streams.append(f"[a{index}]")
        filters.append(f"{audio}[a{index}]")
    if video_streams:
        filters.append("".join(video_streams) + f"concat=n={len(video_streams)}:v=1:a=1[vmain][original]")
        audio_streams.insert(0, "[original]")
    video_label = "vmain"
    for index, caption in enumerate(request.captions):
        path = working / f"caption-{index}.png"
        _caption_image(path, caption.text, width, height)
        cmd.extend(["-protocol_whitelist", "file,pipe", "-i", str(path)])
        filters.append(f"[{video_label}][{len(clips) + index}:v:0]overlay=enable='between(t,{caption.start},{caption.end})'[sub{index}]")
        video_label = f"sub{index}"
    filters.append("".join(audio_streams) + f"amix=inputs={len(audio_streams)}:duration=longest:normalize=0,atrim=duration={duration}[aout]")
    cmd.extend(["-filter_complex", ";".join(filters)])
    if video_streams:
        cmd.extend(["-map", f"[{video_label}]"])
    cmd.extend(["-map", "[aout]", "-threads", "2"])
    codecs = {"mp4": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-movflags", "+faststart"],
              "webm": ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "6", "-c:a", "libopus"],
              "wav": ["-c:a", "pcm_s16le"]}
    output = working / ("edit." + request.container)
    _command([*cmd, *codecs[request.container], "-fs", str(maximum_bytes), str(output)], working, 600)
    if output.stat().st_size >= maximum_bytes:
        raise AppError(413, "MEDIA_EDIT_TOO_LARGE", "输出文件超过素材大小限制。")
    result = ProbeResult.model_validate_json(_command(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)], working, 30))
    if abs((result.format.duration or 0) - duration) > max(0.1, 2 / request.fps):
        _invalid("导出片段时长与时间轴不一致。")
    return output
