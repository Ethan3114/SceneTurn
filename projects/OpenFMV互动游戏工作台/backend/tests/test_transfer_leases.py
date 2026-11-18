import asyncio
import hashlib
import io
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.services.transfer_worker import LEASE_SECONDS, renew_lease, transfer_worker_loop
from conftest import PRODUCT_ROOT
from test_api import registered_client
from test_media_tools import media_service
from test_project_transfers import actual_project, export, resource, wait_job


class TransferWorkerProcess:
    def __init__(self, service, name):
        self.log_path = service.directory / f"transfer-{name}.log"
        self.log = self.log_path.open("wb")
        environment = os.environ.copy()
        environment.update(OPENFMV_DATA_DIR=str(service.data_dir), OPENFMV_WORKER_ENABLED="false",
            PYTHONPATH=str(PRODUCT_ROOT / "backend"), TMPDIR=str(service.temp_dir),
            PYTHONPYCACHEPREFIX=str(service.directory / "pycache"), XDG_CACHE_HOME=str(service.directory / "cache"))
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("transfer_worker_process.py"))],
            cwd=PRODUCT_ROOT / "backend", env=environment, stdin=subprocess.DEVNULL,
            stdout=self.log, stderr=subprocess.STDOUT)

    def stop(self):
        if self.process.poll() is None:
            os.kill(self.process.pid, signal.SIGCONT)
            self.process.terminate()
            self.process.wait(timeout=15)
        self.log.close()

    def resume(self):
        os.kill(self.process.pid, signal.SIGCONT)


@pytest.fixture
def transfer_processes(media_service, request):
    def start(name):
        worker = TransferWorkerProcess(media_service, name)
        request.addfinalizer(worker.stop)
        return worker
    return start


def job_record(service, identifier):
    database = (service.data_dir / "openfmv.sqlite3").resolve()
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM transfer_jobs WHERE id=?", (identifier,)).fetchone()
        return dict(row)


def queue_game(client, sample_video):
    source, _ = actual_project(client, sample_video)
    pending = export(client, source["project"]["id"], source["revision"], "game")
    response = client.post(f"/transfers/{pending['id']}/confirm", json={"confirmationHash": pending["confirmationHash"]})
    assert response.status_code == 200, response.text
    assert response.json()["transfer"]["status"] == "queued"
    return source, pending["id"]


def pause_during_real_render(worker, service, identifier):
    directory = service.data_dir / "transfers" / identifier
    deadline = time.monotonic() + 20
    while True:
        assert worker.process.poll() is None, worker.log_path.read_text()
        manifests = list(directory.glob("*/game.json"))
        if manifests:
            os.kill(worker.process.pid, signal.SIGSTOP)
            _, status = os.waitpid(worker.process.pid, os.WUNTRACED)
            assert os.WIFSTOPPED(status)
            record = job_record(service, identifier)
            assert record["status"] == "processing" and record["lease_owner"]
            return manifests[0].parent, record
        assert time.monotonic() < deadline, "未观察到实际游戏播放器生成"
        time.sleep(0.005)


def assert_game_archive(client, value, source):
    raw = resource(client, value["downloadUrl"])
    assert raw.status_code == 200, raw.text
    with zipfile.ZipFile(io.BytesIO(raw.content)) as archive:
        assert archive.testzip() is None
        assert json.loads(archive.read("game.json"))["metadata"]["sourceRevision"] == source["revision"]
        assert "OpenFMVRuntimeCore" in archive.read("index.html").decode()
    return hashlib.sha256(raw.content).hexdigest()


def test_same_execution_survives_real_pause_beyond_lease_and_processes_next_game(media_service, transfer_processes, sample_video):
    with closing(registered_client(media_service)[0]) as client:
        source, identifier = queue_game(client, sample_video)
        worker = transfer_processes("same-owner")
        _, paused = pause_during_real_render(worker, media_service, identifier)
        assert LEASE_SECONDS == 30
        time.sleep(LEASE_SECONDS + 1)
        assert job_record(media_service, identifier)["lease_expires_at"] < time.time()
        worker.resume()
        ready = wait_job(client, identifier)
        assert ready["status"] == "ready", ready
        assert_game_archive(client, ready, source)
        assert job_record(media_service, identifier)["error_code"] is None
        next_source, next_id = queue_game(client, sample_video)
        _, next_paused = pause_during_real_render(worker, media_service, next_id)
        assert next_paused["lease_owner"] != paused["lease_owner"]
        worker.resume()
        next_ready = wait_job(client, next_id)
        assert next_ready["status"] == "ready", next_ready
        assert_game_archive(client, next_ready, next_source)
        engine = create_engine(f"sqlite:///{media_service.data_dir / 'openfmv.sqlite3'}")
        try:
            assert renew_lease(sessionmaker(engine), identifier, paused["lease_owner"]) is False
        finally:
            engine.dispose()
        assert worker.process.poll() is None


@pytest.mark.parametrize("old_execution_error", [False, True])
def test_reclaimed_game_keeps_new_result_when_old_process_resumes(media_service, transfer_processes, sample_video, old_execution_error):
    with closing(registered_client(media_service)[0]) as client:
        source, identifier = queue_game(client, sample_video)
        first = transfer_processes("old")
        first_directory, paused = pause_during_real_render(first, media_service, identifier)
        time.sleep(LEASE_SECONDS + 1)
        assert job_record(media_service, identifier)["lease_expires_at"] < time.time()
        second = transfer_processes("new")
        ready = wait_job(client, identifier)
        assert ready["status"] == "ready", ready
        digest = assert_game_archive(client, ready, source)
        registered = job_record(media_service, identifier)
        assert Path(json.loads(registered["private_json"])["directory"]).name != first_directory.name
        second.stop()
        parent = first_directory.parent
        original_mode = stat.S_IMODE(parent.stat().st_mode)
        try:
            if old_execution_error:
                parent.chmod(0o500)
            first.resume()
            next_source, next_id = queue_game(client, sample_video)
            next_ready = wait_job(client, next_id)
            assert next_ready["status"] == "ready", next_ready
            assert_game_archive(client, next_ready, next_source)
            assert (first_directory / "index.html").is_file()
            if old_execution_error:
                assert not first_directory.with_suffix(".zip").exists()
            else:
                with zipfile.ZipFile(first_directory.with_suffix(".zip")) as old_archive:
                    assert old_archive.testzip() is None
            assert job_record(media_service, identifier) == registered
        finally:
            parent.chmod(original_mode)
        assert job_record(media_service, identifier) == registered
        assert assert_game_archive(client, ready, source) == digest
        assert first.process.poll() is None
        assert "TRANSFER_LEASE_LOST" not in first.log_path.read_text()
        assert paused["lease_owner"]


def test_sqlite_claim_wait_keeps_real_async_worker_responsive(media_service):
    engine = create_engine(f"sqlite:///{media_service.data_dir / 'openfmv.sqlite3'}", connect_args={"check_same_thread": False, "timeout": 10})
    factory = sessionmaker(engine, expire_on_commit=False)
    connection = sqlite3.connect(media_service.data_dir / "openfmv.sqlite3")
    connection.execute("BEGIN IMMEDIATE")
    ticks = []

    async def run():
        stop = asyncio.Event()
        async def release():
            for _ in range(4):
                await asyncio.sleep(0.05)
                ticks.append(time.monotonic())
            connection.rollback()
            stop.set()
        await asyncio.gather(transfer_worker_loop(Settings(_env_file=None, data_dir=media_service.data_dir), factory, stop), release())

    started = time.monotonic()
    try:
        asyncio.run(run())
        assert len(ticks) == 4 and ticks[-1] - started < 2
    finally:
        connection.close()
        engine.dispose()
