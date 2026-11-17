import base64
import io

import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field


MODEL = "sensenova-u1.5-fast"
BASE_URL = "https://token.sensenova.cn/v1"
MAX_IMAGE_BYTES = 32 * 1024 * 1024


class SenseTimeItem(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    b64_json: str = Field(min_length=1, max_length=MAX_IMAGE_BYTES * 2)


class SenseTimeResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    data: list[SenseTimeItem] = Field(min_length=1, max_length=1)


def image_size(width: int, height: int) -> str:
    if not (512 <= width <= 4096 and 512 <= height <= 4096 and width % 32 == height % 32 == 0 and max(width, height) <= min(width, height) * 3):
        raise ValueError("商汤图片宽高须为512至4096之间的32倍数，长宽比不得超过3")
    return f"{width}x{height}"


def decode_image(payload: dict, width: int, height: int) -> bytes:
    response = SenseTimeResponse.model_validate(payload)
    raw = base64.b64decode(response.data[0].b64_json, validate=True)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("商汤图片超过大小上限")
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != "PNG" or image.size != (width, height):
            raise ValueError("商汤图片格式或尺寸与确认参数不一致")
        image.verify()
    return raw


async def submit_image(key: str, request: dict, references: list[tuple[str, bytes]]) -> httpx.Response:
    payload = {"model": MODEL, "prompt": request["prompt"], "n": 1,
        "size": image_size(request["width"], request["height"]), "watermark": False,
        "prompt_extend": False, "response_format": "b64_json", "output_format": "png"}
    if len(references) > 5:
        raise ValueError("商汤参考图片最多5张")
    if references:
        payload["images"] = [{"image_url": f"data:{mime};base64,{base64.b64encode(raw).decode()}"} for mime, raw in references]
    endpoint = "/images/edits" if references else "/images/generations"
    async with httpx.AsyncClient(timeout=600, trust_env=False) as client:
        return await client.post(BASE_URL + endpoint, headers={"Authorization": f"Bearer {key}"}, json=payload)
