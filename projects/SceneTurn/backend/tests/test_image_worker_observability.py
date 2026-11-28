import asyncio
import traceback

import pytest
from sqlalchemy import create_engine, text

from app.core.errors import AppError
from app.services.image_queue import image_worker_done, require_image_worker, wait_image_worker


def test_real_database_task_failure_is_unhealthy_and_logs_only_type(tmp_path, caplog):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.sqlite3'}")
    private_content = "应保留在数据库中的私有项目正文"
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE registrations (id INTEGER PRIMARY KEY, content TEXT)"))
        connection.execute(text("INSERT INTO registrations VALUES (1, :content)"), {"content": private_content})

    async def run():
        async def fail_registration():
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO registrations VALUES (1, :content)"), {"content": private_content})

        task = asyncio.create_task(fail_registration())
        task.add_done_callback(image_worker_done)
        await asyncio.wait({task})
        await asyncio.sleep(0)
        with pytest.raises(AppError) as unavailable:
            require_image_worker(task)
        assert unavailable.value.status == 503 and unavailable.value.code == "IMAGE_WORKER_FAILED"
        with pytest.raises(AppError) as shutdown:
            await wait_image_worker(task)
        rendered = "".join(traceback.format_exception(shutdown.value))
        assert shutdown.value.__suppress_context__
        assert "IntegrityError" not in rendered and "INSERT INTO" not in rendered and private_content not in rendered

    asyncio.run(run())
    assert "IntegrityError" in caplog.text
    assert "INSERT INTO" not in caplog.text and private_content not in caplog.text
    engine.dispose()


def test_real_healthy_task_and_disabled_worker_remain_healthy():
    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(stop.wait())
        require_image_worker(None)
        require_image_worker(task)
        stop.set()
        await wait_image_worker(task)
        require_image_worker(task)

    asyncio.run(run())


def test_real_cancelled_image_worker_is_reported_and_shutdown_is_sanitized(caplog):
    async def run():
        task = asyncio.create_task(asyncio.Event().wait())
        task.add_done_callback(image_worker_done)
        task.cancel()
        await asyncio.wait({task})
        await asyncio.sleep(0)
        with pytest.raises(AppError) as unavailable:
            require_image_worker(task)
        assert unavailable.value.code == "IMAGE_WORKER_FAILED"
        with pytest.raises(AppError) as shutdown:
            await wait_image_worker(task)
        assert shutdown.value.__suppress_context__

    asyncio.run(run())
    assert "CancelledError" in caplog.text
