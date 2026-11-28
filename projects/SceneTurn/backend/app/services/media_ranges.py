import math

from app.core.errors import AppError


def required_media_end(clip):
    source_start = float(clip.get("sourceStart") or 0)
    rate = float(clip.get("playbackRate") or 1)
    duration = float(clip["duration"])
    source_duration = float(clip.get("sourceDuration") or duration * rate)
    if not all(math.isfinite(value) for value in (source_start, rate, duration, source_duration)) or source_start < 0 or rate <= 0 or duration <= 0 or source_duration < 0:
        raise AppError(422, "MEDIA_TRIM_INVALID", "目标片段的裁切或播放速度无效")
    if clip.get("freezeFrameTime") is not None:
        frozen = float(clip["freezeFrameTime"])
        if not math.isfinite(frozen) or frozen < 0:
            raise AppError(422, "MEDIA_TRIM_INVALID", "目标片段的定格时间无效")
        return frozen
    return source_start + source_duration
