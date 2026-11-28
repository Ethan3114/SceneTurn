import asyncio
import signal
from pathlib import Path

from app.core.config import get_settings
from app.db import SessionLocal
from app.services.transfer_worker import transfer_worker_loop


async def main():
    settings = get_settings()
    root = Path(__file__).resolve().parents[2] / ".intermediate"
    if not settings.data_dir.resolve().is_relative_to(root):
        raise ValueError("传输验证进程只能访问本产品的隔离验证目录")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    await transfer_worker_loop(settings, SessionLocal, stop)


if __name__ == "__main__":
    asyncio.run(main())
