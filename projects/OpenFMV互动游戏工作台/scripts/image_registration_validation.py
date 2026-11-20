import asyncio
import copy
import hashlib
import os
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.agent_models import AgentRun
from app.core.config import Settings
from app.core.errors import AppError
from app.image_models import ImageAccount, ImageJob
from app.models import Asset, Project, ProjectVersion
from app.services.image_queue import _submission_digest, _submitted_job, claim_job, execute_job, finalize_response, image_worker_done, image_worker_loop, recover_jobs, require_image_worker
from app.services.projects import lock_write, payload_digest, record_project_version, require_project


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "0847961a-7349-4df2-9420-6fb9abac76a8"
JOB_ID = "e6c97fef-9148-416f-a992-3726122d6299"


@pytest.fixture
def saved_images(tmp_path):
    snapshot_value = os.environ.get("OPENFMV_IMAGE_REGISTRATION_SNAPSHOT")
    data_value = os.environ.get("OPENFMV_IMAGE_RESPONSE_DATA")
    if not snapshot_value or not data_value:
        pytest.fail("请显式提供OPENFMV_IMAGE_REGISTRATION_SNAPSHOT及OPENFMV_IMAGE_RESPONSE_DATA；验证仅使用已授权副本和真实保存响应。")
    snapshot, source_data = Path(snapshot_value).resolve(), Path(data_value).resolve()
    if not snapshot.is_file() or not source_data.is_dir():
        pytest.fail("指定的SQLite副本或真实图片响应目录不存在。")
    with TemporaryDirectory(dir=tmp_path) as temporary:
        directory = Path(temporary)
        database = directory / "openfmv.sqlite3"
        with (
            closing(sqlite3.connect(f"{snapshot.as_uri()}?immutable=1", uri=True)) as source,
            closing(sqlite3.connect(database)) as destination,
        ):
            source.backup(destination)
        shutil.copytree(source_data / "image-responses", directory / "image-responses")
        shutil.copytree(source_data / "media/images", directory / "media/images")
        engine = create_engine(f"sqlite:///{database}", connect_args={"check_same_thread": False, "timeout": 10})
        try:
            @event.listens_for(engine, "connect")
            def configure_sqlite(connection, _record):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=10000")

            factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
            settings = Settings(_env_file=None, data_dir=directory, worker_enabled=False)
            with factory() as session:
                run = session.get(AgentRun, RUN_ID)
                reference = run.limits_json["materialContext"]["referenceAssets"][0]
                asset = session.get(Asset, reference["assetId"])
                official = ROOT / "docs/设计参考/斗破苍穹-20261117/origin-poster.png"
                assert hashlib.sha256(official.read_bytes()).hexdigest() == reference["sha256"] == asset.sha256
                target = directory / asset.relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(official, target)
            yield factory, settings
        finally:
            engine.dispose()


def user_edit(factory, project_id, owner_id):
    with factory() as session:
        lock_write(session)
        project = require_project(session, project_id, owner_id)
        snapshot = copy.deepcopy(project.data_json)
        snapshot["title"] = "登记期间保留的人工编辑"
        project.title, project.data_json = snapshot["title"], snapshot
        project.revision += 1
        record_project_version(session, project)
        session.commit()
        return project.revision


def checked_finalize(session, job, settings):
    failure = None
    try:
        finalize_response(session, job, settings)
    except SQLAlchemyError as error:
        failure = f"真实图片登记失败：{type(error).__name__}；SQLite错误类别：{getattr(error.orig, 'sqlite_errorname', None)}"
    if failure is not None:
        pytest.fail(failure, pytrace=False)


def test_real_saved_response_refreshes_project_after_actual_concurrent_commit(saved_images):
    factory, settings = saved_images
    with factory() as stale:
        job = stale.get(ImageJob, JOB_ID)
        project = require_project(stale, job.project_id, job.owner_id)
        run = stale.get(AgentRun, job.run_id)
        account = stale.get(ImageAccount, job.account_id)
        original_revision = project.revision
        original_assets = {asset["id"] for asset in project.data_json["assets"]}
        submitted, attempts = job.submitted_at, job.attempt_count
        stale.commit()
        new_revision = asyncio.run(asyncio.to_thread(user_edit, factory, project.id, project.owner_id))
        assert project.revision == original_revision and new_revision == original_revision + 1
        checked_finalize(stale, job, settings)
        assert job.status == "ready" and job.asset_id
        assert project.revision == new_revision + 1 and project.title == "登记期间保留的人工编辑"
        assert {asset["id"] for asset in project.data_json["assets"]} == {*original_assets, job.asset_id}
        assert run.project_revision == original_revision
        assert account.active_job_id is None
        assert (job.submitted_at, job.attempt_count) == (submitted, attempts)
        assert stale.scalar(select(ProjectVersion.id).where(ProjectVersion.project_id == project.id, ProjectVersion.revision == project.revision))


def quarantine_state(factory):
    with factory() as session:
        account = session.scalar(select(ImageAccount).where(ImageAccount.owner_id == session.get(AgentRun, RUN_ID).owner_id, ImageAccount.slot == 3))
        job = session.get(ImageJob, account.active_job_id)
        return (account.status, account.active_job_id, job.status, job.error_code, job.attempt_count, job.submitted_at)


@pytest.mark.parametrize("initial_status", ["response_saved", "submission_unknown"])
def test_startup_registers_saved_response_and_waits_for_unexpired_unknown(saved_images, initial_status):
    factory, settings = saved_images
    with factory() as session:
        job = session.get(ImageJob, JOB_ID)
        submitted = (job.attempt_count, job.submitted_at)
        queued = set(session.scalars(select(ImageJob.id).where(ImageJob.run_id == RUN_ID, ImageJob.status == "queued")))
        assert len(queued) == 3
        response_digest = hashlib.sha256((settings.data_dir / job.response_relpath).read_bytes()).hexdigest()
        waiting_account = session.scalar(select(ImageAccount).where(ImageAccount.owner_id == job.owner_id, ImageAccount.slot == 3))
        session.get(ImageJob, waiting_account.active_job_id).submitted_at = time.time() - 60
        if initial_status == "submission_unknown":
            job.status, job.response_relpath, job.error_code = initial_status, None, "IMAGE_SUBMISSION_UNKNOWN"
            session.get(ImageAccount, job.account_id).status = "quarantined"
        session.commit()
    original_quarantine = quarantine_state(factory)

    async def recover_only():
        stop = asyncio.Event()
        stop.set()
        await image_worker_loop(settings, factory, stop)

    asyncio.run(recover_only())
    with factory() as session:
        job = session.get(ImageJob, JOB_ID)
        assert job.status == "ready" and job.error_code is None and job.asset_id
        assert (job.attempt_count, job.submitted_at) == submitted
        assert hashlib.sha256((settings.data_dir / job.response_relpath).read_bytes()).hexdigest() == response_digest
        assert session.get(ImageAccount, job.account_id).active_job_id is None
    with factory() as session:
        assert claim_job(session) is None
        assert set(session.scalars(select(ImageJob.id).where(ImageJob.run_id == RUN_ID, ImageJob.status == "queued"))) == queued
    assert quarantine_state(factory) == original_quarantine


def test_repeated_real_response_registration_is_idempotent(saved_images):
    factory, settings = saved_images
    with factory() as session:
        job = session.get(ImageJob, JOB_ID)
        initial = session.get(Project, job.project_id).revision
        session.commit()
        checked_finalize(session, job, settings)
        asset_id, completed = job.asset_id, job.finished_at
        checked_finalize(session, job, settings)
        assert (job.asset_id, job.finished_at) == (asset_id, completed)
        assert session.get(Project, job.project_id).revision == initial + 1
        assert len(session.scalars(select(Asset).where(Asset.relpath == f"media/images/{JOB_ID}.png")).all()) == 1


def test_two_real_local_finalizers_serialize_one_png_and_one_version(saved_images):
    factory, settings = saved_images
    png = settings.data_dir / "media/images" / f"{JOB_ID}.png"
    png.unlink()
    with factory() as session:
        job = session.get(ImageJob, JOB_ID)
        project_id, initial = job.project_id, session.get(Project, job.project_id).revision

    def register():
        with factory() as session:
            job = session.get(ImageJob, JOB_ID)
            checked_finalize(session, job, settings)
            return job.asset_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _index: register(), range(2)))
    assert first == second and png.is_file()
    with factory() as session:
        assert session.get(Project, project_id).revision == initial + 1
        assert len(session.scalars(select(Asset).where(Asset.relpath == f"media/images/{JOB_ID}.png")).all()) == 1


def test_real_local_registration_failure_is_visible_and_startup_reuses_response(saved_images, caplog):
    factory, settings = saved_images
    png = settings.data_dir / "media/images" / f"{JOB_ID}.png"
    original_png = png.read_bytes()
    png.write_bytes(original_png + b"changed")
    original_quarantine = quarantine_state(factory)

    async def fail_worker():
        task = asyncio.create_task(image_worker_loop(settings, factory, asyncio.Event()))
        task.add_done_callback(image_worker_done)
        await asyncio.wait({task})
        await asyncio.sleep(0)
        with pytest.raises(AppError) as health:
            require_image_worker(task)
        assert health.value.status == 503 and health.value.code == "IMAGE_WORKER_FAILED"

    asyncio.run(fail_worker())
    with factory() as session:
        job = session.get(ImageJob, JOB_ID)
        assert job.status == "response_saved" and job.error_code == "IMAGE_REGISTRATION_FAILED"
        assert job.asset_id is None and session.get(ImageAccount, job.account_id).active_job_id == job.id
        assert (settings.data_dir / job.response_relpath).is_file()
    assert "ValueError" in caplog.text and "INSERT INTO" not in caplog.text
    png.write_bytes(original_png)
    asyncio.run(execute_job(JOB_ID, settings, factory))
    with factory() as session:
        job = session.get(ImageJob, JOB_ID)
        assert job.status == "ready" and job.error_code is None
    assert quarantine_state(factory) == original_quarantine


def test_registration_never_releases_a_different_active_job(saved_images):
    factory, settings = saved_images
    with factory() as session:
        job = session.get(ImageJob, JOB_ID)
        account = session.get(ImageAccount, job.account_id)
        other = session.scalar(select(ImageJob).where(ImageJob.run_id == RUN_ID, ImageJob.status == "queued"))
        other.account_id, other.status, account.active_job_id = account.id, "claimed", other.id
        other_id = other.id
        session.commit()
    with pytest.raises(ValueError):
        asyncio.run(execute_job(JOB_ID, settings, factory))
    with factory() as session:
        job = session.get(ImageJob, JOB_ID)
        assert job.status == "response_saved" and job.error_code == "IMAGE_REGISTRATION_FAILED"
        assert session.get(ImageAccount, job.account_id).active_job_id == other_id


def test_changed_frozen_reference_stops_registration_and_preserves_saved_result(saved_images):
    factory, settings = saved_images
    with factory() as session:
        job = session.get(ImageJob, JOB_ID)
        reference = job.request_json["referenceAssets"][0]
        asset = session.get(Asset, reference["assetId"])
        path = settings.data_dir / asset.relpath
        path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(AppError):
        asyncio.run(execute_job(JOB_ID, settings, factory))
    with factory() as session:
        job = session.get(ImageJob, JOB_ID)
        assert job.status == "response_saved" and job.asset_id is None
        assert session.get(ImageAccount, job.account_id).active_job_id == job.id
        assert job.error_code == "IMAGE_REGISTRATION_FAILED"


@pytest.mark.parametrize("change", ["account", "request", "attempt", "submitted", "quarantine"])
def test_actual_submission_identity_rejects_changed_state(saved_images, change):
    factory, _settings = saved_images
    with factory() as session:
        job = session.get(ImageJob, JOB_ID)
        account = session.get(ImageAccount, job.account_id)
        job.status = "submitting"
        submission = {"accountId": account.id, "attemptCount": job.attempt_count, "submittedAt": job.submitted_at,
            "inputSha256": _submission_digest(job)}
        if change == "account":
            account.active_job_id = None
        elif change == "request":
            job.request_json = {**job.request_json, "width": 2048}
        elif change == "attempt":
            job.attempt_count += 1
        elif change == "submitted":
            job.submitted_at += 1
        elif change == "quarantine":
            account.status = "quarantined"
        session.commit()
        before = (payload_digest(job.request_json), job.status, job.attempt_count, job.submitted_at, account.active_job_id, account.status)
    with factory() as session:
        lock_write(session)
        with pytest.raises(ValueError):
            _submitted_job(session, JOB_ID, submission)
        job = session.get(ImageJob, JOB_ID)
        account = session.get(ImageAccount, job.account_id)
        assert (payload_digest(job.request_json), job.status, job.attempt_count, job.submitted_at, account.active_job_id, account.status) == before
