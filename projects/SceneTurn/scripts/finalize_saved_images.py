import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings
from app.db import SessionLocal
from app.image_models import ImageJob
from app.services.image_queue import claim_job, execute_job


async def main():
    with SessionLocal() as session:
        identifiers = session.scalars(select(ImageJob.id).where(ImageJob.status == "response_saved")).all()
    for identifier in identifiers:
        await execute_job(identifier, get_settings(), SessionLocal)
    print(f"已检查{len(identifiers)}项保存的真实响应；没有提交模型请求。")
    if "--drain-confirmed" in sys.argv:
        with SessionLocal() as session:
            identifier = claim_job(session)
        if identifier:
            await execute_job(identifier, get_settings(), SessionLocal)
            print("已处理一项原本确认的排队请求。")


if __name__ == "__main__":
    asyncio.run(main())
