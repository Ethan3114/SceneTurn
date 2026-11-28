import asyncio
import copy
import hashlib
import json
import secrets
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict

import pytest
from fastapi import UploadFile
from sqlalchemy.exc import DBAPIError
from starlette.datastructures import Headers

from app.core.config import Settings
from app.core.security import hash_password
from app.services.cloudbase_assets import AssetRegistration, CloudBaseAssetRecord
from app.services.cloudbase_direct_uploads import AssetUploadJob, UploadAcceptance, UploadReservation, UploadSettlement
from app.services.cloudbase_imports import TransferRecord, _preview, _verify_preview_media
from app.services.deployment_queue import epoch_milliseconds
from app.services.media import asset_view, resolve_asset_path, save_upload
from app.services.project_transfers import file_sha256, transfer_root, transfer_view
from app.services.projects import empty_canvas, initial_project, payload_digest, scene_node
from app.services.transfer_rpc import ClaimedTransfer
from app.transfer_schemas import TransferView
from conftest import PRODUCT_ROOT
from test_postgresql_persistence import postgres_runtime


def test_project_rpc_scopes_and_saves_history_atomically(postgres_runtime, sample_video, tmp_path, request):
    admin, _, _ = postgres_runtime
    scope = secrets.token_urlsafe(48)
    sql = PRODUCT_ROOT / "backend" / "sql" / "cloudbase"
    with admin.begin() as connection:
        for role in ("anon", "authenticated", "service_role"):
            connection.exec_driver_sql(f"CREATE ROLE {role}")
        for fragment in ("access.sql", "auth.sql", "projects.sql", "assets.sql", "transfers.sql", "asset_uploads.sql", "imports.sql"):
            connection.exec_driver_sql((sql / fragment).read_text(encoding="utf-8"), execution_options={"no_parameters": True})
        connection.exec_driver_sql("""
            INSERT INTO sceneturn.gateway_credentials(token_sha256) VALUES (sha256(convert_to(%s, 'UTF8')))
        """, (scope,))

    def cleanup():
        with admin.begin() as connection:
            connection.exec_driver_sql("""
                DROP FUNCTION public.sceneturn_schema_revision(),
                    public.sceneturn_auth_register(uuid, text, text, text, integer),
                    public.sceneturn_auth_login_lookup(text, text),
                    public.sceneturn_auth_login_finish(text, text, text, text, text, integer),
                    public.sceneturn_auth_session_user(text), public.sceneturn_auth_logout(text),
                    public.sceneturn_project_list(text), public.sceneturn_project_get(text, text),
                    public.sceneturn_project_create(text, text, jsonb, jsonb),
                    public.sceneturn_project_save(text, text, integer, text, text, jsonb, jsonb),
                    public.sceneturn_asset_upload_target(text, text), public.sceneturn_asset_get(text, text),
                    public.sceneturn_asset_register(text, text, uuid, text, text, text, text, integer, text, integer, integer, integer, jsonb),
                    public.sceneturn_asset_upload_reserve(text, text, uuid, text, text, text, text, text, integer, text),
                    public.sceneturn_asset_upload_get(text, text, text, text),
                    public.sceneturn_asset_upload_complete(text, text, text, text, uuid, bigint),
                    public.sceneturn_asset_upload_settle(uuid, uuid, jsonb, text, text),
                    public.sceneturn_transfer_read(uuid, uuid),
                    public.sceneturn_transfer_accept_export(uuid, uuid, uuid, text, text, integer, text, text, jsonb, jsonb, uuid, bigint),
                    public.sceneturn_transfer_resume_export(uuid, uuid, text, text, uuid, bigint),
                    public.sceneturn_transfer_claim(uuid, uuid, uuid, bigint, boolean, uuid, bigint),
                    public.sceneturn_transfer_renew(uuid, uuid),
                    public.sceneturn_transfer_settle_export(uuid, uuid, jsonb, text, text),
                    public.sceneturn_import_reserve(text, uuid, text, text, text, text, text, integer, text),
                    public.sceneturn_import_read_upload(text, text, text),
                    public.sceneturn_import_enqueue(text, uuid, text, text, text, text, uuid, bigint),
                    public.sceneturn_import_resume(text, uuid, text, uuid, bigint),
                    public.sceneturn_import_settle(uuid, uuid, jsonb, text, text, text, text),
                    public.sceneturn_transfer_list(text, text), public.sceneturn_transfer_summary(text, text);
            """)
            for role in ("anon", "authenticated", "service_role"):
                connection.exec_driver_sql(f"DROP ROLE {role}")

    request.addfinalizer(cleanup)

    @contextmanager
    def gateway(token=scope):
        with admin.begin() as connection:
            connection.exec_driver_sql("SET LOCAL ROLE service_role")
            connection.exec_driver_sql("SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps({"role": "service_role"}),))
            connection.exec_driver_sql("SELECT set_config('request.headers', %s, true)",
                (json.dumps({"x-sceneturn-database-scope": token}),))
            yield connection

    with pytest.raises(DBAPIError) as failure, gateway(secrets.token_urlsafe(48)) as connection:
        connection.exec_driver_sql("SELECT public.sceneturn_project_list(%s)", (str(uuid.uuid4()),))
    assert failure.value.orig.sqlstate == "42501"

    owner, neighbor, project_id = (str(uuid.uuid4()) for _ in range(3))
    initial, scene_id = initial_project(project_id, "原始故事")
    with gateway() as connection:
        for user_id in (owner, neighbor):
            registered = connection.exec_driver_sql("SELECT public.sceneturn_auth_register(%s::uuid, %s, %s, %s, 300)",
                (user_id, f"{user_id}@example.com", hash_password(secrets.token_urlsafe(20)), secrets.token_hex(32)))
            assert registered.scalar_one()["id"] == user_id
        created = connection.exec_driver_sql("SELECT public.sceneturn_project_create(%s, %s, %s::jsonb, %s::jsonb)",
            (owner, project_id, json.dumps(initial), json.dumps(empty_canvas(scene_id)))).scalar_one()["envelope"]
        assert created["revision"] == 1 and created["canvases"] == [empty_canvas(scene_id)]
        assert connection.exec_driver_sql("SELECT public.sceneturn_project_list(%s)", (neighbor,)).scalar_one() == {"projects": []}
        listing = connection.exec_driver_sql("SELECT public.sceneturn_project_list(%s)", (owner,)).scalar_one()["projects"]
        assert [item["id"] for item in listing] == [project_id]
    with pytest.raises(DBAPIError) as failure, gateway() as connection:
        connection.exec_driver_sql("SELECT public.sceneturn_project_get(%s, %s)", (neighbor, project_id))
    assert failure.value.orig.sqlstate == "PT404"

    def save(snapshot, revision, operation_id, owner_id=owner):
        payload = {"project": snapshot, "expectedRevision": revision, "operationId": operation_id}
        with gateway() as connection:
            return connection.exec_driver_sql("SELECT public.sceneturn_project_save(%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)",
                (owner_id, project_id, revision, operation_id, payload_digest(payload),
                 json.dumps(snapshot), json.dumps(empty_canvas("")))).scalar_one()["envelope"]

    draft = copy.deepcopy(created["project"])
    added_scene_id = str(uuid.uuid4())
    draft["graphData"]["nodes"].append(scene_node(added_scene_id, "新增场景"))
    draft["graphData"]["nodes"][0]["data"]["timeline"] = None
    draft["metadata"]["serverRevision"] = 8
    bad_asset = copy.deepcopy(draft)
    bad_asset["graphData"]["nodes"][-1]["data"]["timeline"]["tracks"][0]["clips"].append({"assetId": str(uuid.uuid4())})
    with pytest.raises(DBAPIError) as failure:
        save(bad_asset, 1, "未登记素材")
    assert failure.value.orig.sqlstate == "PT422" and "ASSET_NOT_REGISTERED" in str(failure.value.orig)
    with pytest.raises(DBAPIError) as failure:
        save(draft, 1, "邻居写入", owner_id=neighbor)
    assert failure.value.orig.sqlstate == "PT404"

    def concurrent_save(label):
        candidate = {**draft, "title": label}
        try:
            return label, save(candidate, 1, label)
        except DBAPIError as exc:
            assert exc.orig.sqlstate == "PT409" and "REVISION_CONFLICT" in str(exc.orig)
            return label, None

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(concurrent_save, ("编辑甲", "编辑乙")))
    assert sum(result is None for _, result in outcomes) == 1
    operation_id, saved = next((label, result) for label, result in outcomes if result is not None)
    assert saved["revision"] == 2 and saved["project"]["metadata"]["serverRevision"] == 2
    assert saved["project"]["createdAt"] == created["project"]["createdAt"]
    assert {canvas["sceneId"] for canvas in saved["canvases"]} == {scene_id, added_scene_id}
    assert save({**draft, "title": operation_id}, 1, operation_id) == saved
    with pytest.raises(DBAPIError) as failure:
        save({**draft, "title": "其他内容"}, 1, operation_id)
    assert failure.value.orig.sqlstate == "PT409" and "OPERATION_CONFLICT" in str(failure.value.orig)

    with gateway() as connection:
        current = connection.exec_driver_sql("SELECT public.sceneturn_project_get(%s, %s)", (owner, project_id)).scalar_one()
        assert current["envelope"] == saved
    with admin.connect() as connection:
        versions = connection.exec_driver_sql("""
            SELECT revision, data_json, canvases_json FROM sceneturn.project_versions WHERE project_id = %s ORDER BY revision
        """, (project_id,)).all()
        assert [version.revision for version in versions] == [1, 2]
        assert versions[-1].data_json == saved["project"] and versions[-1].canvases_json == saved["canvases"]
        assert connection.exec_driver_sql("SELECT count(*) FROM sceneturn.operations WHERE project_id = %s", (project_id,)).scalar_one() == 1

    with pytest.raises(DBAPIError) as failure, gateway() as connection:
        connection.exec_driver_sql("SELECT public.sceneturn_asset_upload_target(%s, %s)", (neighbor, project_id))
    assert failure.value.orig.sqlstate == "PT404"
    settings = Settings(_env_file=None, data_dir=tmp_path / "files", tmp_dir=tmp_path / "media-staging")
    with sample_video.open("rb") as source:
        media = asyncio.run(save_upload(UploadFile(source, filename="场景.mp4", headers=Headers({"content-type": "video/mp4"})), settings))
    asset_id = str(uuid.uuid4())
    with gateway() as connection:
        assert connection.exec_driver_sql("SELECT public.sceneturn_asset_upload_target(%s, %s)", (owner, project_id)).scalar_one() is True
        uploaded = connection.exec_driver_sql("""
            SELECT public.sceneturn_asset_register(%s, %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """, (owner, project_id, asset_id, media.name, media.relpath, media.poster_relpath, media.mime_type,
            media.bytes, media.sha256, media.duration_ms, media.width, media.height, json.dumps(media.metadata_json))).scalar_one()
        registration = AssetRegistration.model_validate(uploaded, strict=True)
        published = registration.public_envelope()
        assert published["revision"] == 3 and published["canvases"] == saved["canvases"]
        assert published["project"]["assets"][-1]["id"] == asset_id
        assert published["project"]["assets"][-1]["metadata"]["poster"] == asset_view(registration.asset)["posterUrl"]
    with gateway() as connection:
        descriptor = connection.exec_driver_sql("SELECT public.sceneturn_asset_get(%s, %s)", (owner, asset_id)).scalar_one()
        record = CloudBaseAssetRecord.model_validate(descriptor, strict=True)
    assert hashlib.sha256(resolve_asset_path(settings, record.relpath).read_bytes()).hexdigest() == record.sha256
    assert resolve_asset_path(settings, record.poster_relpath).stat().st_size > 0
    with pytest.raises(DBAPIError) as failure, gateway() as connection:
        connection.exec_driver_sql("SELECT public.sceneturn_asset_get(%s, %s)", (neighbor, asset_id))
    assert failure.value.orig.sqlstate == "PT404"
    with admin.connect() as connection:
        stored_version = connection.exec_driver_sql("""
            SELECT data_json FROM sceneturn.project_versions WHERE project_id = %s AND revision = 3
        """, (project_id,)).scalar_one()
    assert stored_version["assets"][-1]["id"] == asset_id

    with sample_video.open("rb") as source:
        media = asyncio.run(save_upload(UploadFile(source, filename="直传.mp4", headers=Headers({"content-type": "video/mp4"})), settings))
    check_direct_upload(admin, gateway, owner, project_id, media)
    check_import(gateway, owner, neighbor, settings, sample_video)


def check_direct_upload(admin, gateway, owner, project_id, media):
    session_hash = secrets.token_hex(32)
    other_session = secrets.token_hex(32)
    operation_id = str(uuid.uuid4())
    identity = payload_digest({"projectId": project_id, "operationId": operation_id, "sha256": media.sha256})
    identifier = str(uuid.uuid4())

    def reserve(identifier, operation_id, identity, session_hash):
        with gateway() as connection:
            result = connection.exec_driver_sql("""
                SELECT public.sceneturn_asset_upload_reserve(%s, %s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)
            """, (owner, project_id, identifier, operation_id, identity, session_hash, media.name,
                media.mime_type, media.bytes, media.sha256)).scalar_one()
        return UploadReservation.model_validate(result, strict=True)

    reserved = reserve(identifier, operation_id, identity, session_hash)
    assert reserved.job.status == "awaiting_upload" and reserved.job.id == identifier and not reserved.reused
    assert reserve(str(uuid.uuid4()), operation_id, identity, session_hash).job == reserved.job
    with pytest.raises(DBAPIError) as failure:
        reserve(str(uuid.uuid4()), operation_id, identity, other_session)
    assert failure.value.orig.sqlstate == "PT409" and "UPLOAD_SESSION_CHANGED" in str(failure.value.orig)
    for _ in range(3):
        reserve(str(uuid.uuid4()), str(uuid.uuid4()), secrets.token_hex(32), session_hash)
    with pytest.raises(DBAPIError) as failure:
        reserve(str(uuid.uuid4()), str(uuid.uuid4()), secrets.token_hex(32), session_hash)
    assert failure.value.orig.sqlstate == "PT429"

    marker, due = str(uuid.uuid4()), epoch_milliseconds() + 45_000

    def complete(session_hash):
        with gateway() as connection:
            result = connection.exec_driver_sql("""
                SELECT public.sceneturn_asset_upload_complete(%s, %s, %s, %s, %s::uuid, %s::bigint)
            """, (owner, project_id, identifier, session_hash, marker, due)).scalar_one()
        return UploadAcceptance.model_validate(result, strict=True)

    with pytest.raises(DBAPIError) as failure:
        complete(other_session)
    assert failure.value.orig.sqlstate == "PT404"
    accepted = complete(session_hash)
    assert accepted.queued and accepted.job.status == "queued"
    assert not complete(session_hash).queued

    def claim(worker_id, marker, due, recovery, recovery_due):
        with gateway() as connection:
            return connection.exec_driver_sql("""
                SELECT public.sceneturn_transfer_claim(%s::uuid, %s::uuid, %s::uuid, %s::bigint, true, %s::uuid, %s::bigint)
            """, (identifier, worker_id, marker, due, recovery, recovery_due)).scalar_one()

    worker, recovery = str(uuid.uuid4()), str(uuid.uuid4())
    recovery_due = epoch_milliseconds() + 65_000
    claimed = claim(worker, marker, due, recovery, recovery_due)
    assert claimed["state"] == "claimed" and claimed["job"]["kind"] == "asset_upload"
    assert claim(str(uuid.uuid4()), marker, due, str(uuid.uuid4()), recovery_due)["state"] == "stale"
    with admin.begin() as connection:
        connection.exec_driver_sql("""
            UPDATE sceneturn.transfer_jobs SET lease_expires_at = extract(epoch FROM clock_timestamp()) - 1 WHERE id = %s
        """, (identifier,))
    successor, next_marker = str(uuid.uuid4()), str(uuid.uuid4())
    resumed = claim(successor, recovery, recovery_due, next_marker, epoch_milliseconds() + 65_000)
    assert resumed["state"] == "claimed" and resumed["job"]["id"] == identifier

    def settle(worker_id, asset):
        with gateway() as connection:
            result = connection.exec_driver_sql("""
                SELECT public.sceneturn_asset_upload_settle(%s::uuid, %s::uuid, %s::jsonb, null, null)
            """, (identifier, worker_id, json.dumps(asset))).scalar_one()
        return UploadSettlement.model_validate(result, strict=True)

    assert settle(worker, asdict(media)).state == "lease_lost"
    with pytest.raises(DBAPIError) as failure:
        settle(successor, {**asdict(media), "sha256": secrets.token_hex(32)})
    assert failure.value.orig.sqlstate == "PT422"
    completed = settle(successor, asdict(media))
    assert completed.state == "settled" and completed.obsolete["revision"] == next_marker
    with gateway() as connection:
        status = connection.exec_driver_sql("SELECT public.sceneturn_asset_upload_get(%s, %s, %s, NULL)",
            (owner, project_id, identifier)).scalar_one()
        job = AssetUploadJob.model_validate(status, strict=True)
        assert job.status == "ready" and job.asset_id == identifier
        envelope = connection.exec_driver_sql("SELECT public.sceneturn_project_get(%s, %s)", (owner, project_id)).scalar_one()["envelope"]
        assert envelope["revision"] == 4 and envelope["project"]["assets"][-1]["id"] == identifier


def check_import(gateway, owner, neighbor, settings, sample_video):
    source_id = str(uuid.uuid4())
    snapshot, _ = initial_project(source_id, "验证完整项目导入")
    snapshot["assets"].append({"id": str(uuid.uuid4()), "type": "video", "name": "原片.mp4", "relativePath": "media/原片.mp4",
        "path": "media/原片.mp4", "importedAt": snapshot["createdAt"]})

    def call(statement, *args):
        with gateway() as connection:
            return connection.exec_driver_sql(statement, args).scalar_one()

    def claim(identifier, marker, due_ms):
        worker = str(uuid.uuid4())
        raw = call("SELECT public.sceneturn_transfer_claim(%s::uuid, %s::uuid, %s::uuid, %s::bigint, true, %s::uuid, %s::bigint)",
            identifier, worker, marker, due_ms, str(uuid.uuid4()), epoch_milliseconds() + 65_000)
        assert raw["state"] == "claimed"
        return ClaimedTransfer.model_validate(raw["job"])

    def enqueue(identifier, source, operation):
        sha256, marker, due = file_sha256(source), str(uuid.uuid4()), epoch_milliseconds() + 45_000
        result = call("SELECT public.sceneturn_import_enqueue(%s, %s::uuid, null, %s, %s, %s, %s::uuid, %s::bigint)",
            owner, identifier, operation, payload_digest({"sha256": sha256}), sha256, marker, due)
        return result, marker, due

    def restore_archive(compression):
        identifier, operation = str(uuid.uuid4()), str(uuid.uuid4())
        source = transfer_root(settings, identifier) / "source.zip"
        source.parent.mkdir(parents=True)
        with zipfile.ZipFile(source, "w", compression=compression) as archive:
            archive.writestr("project.json", json.dumps(snapshot))
            archive.write(sample_video, "media/原片.mp4")
        accepted, marker, due = enqueue(identifier, source, operation)
        assert accepted["queued"] and accepted["job"]["status"] == "queued"
        claimed = claim(identifier, marker, due)
        preview = _preview(settings, claimed)
        confirmation = payload_digest({"id": identifier, "source": preview["sourceDigest"], "preview": preview["view"]})
        assert call("SELECT public.sceneturn_import_settle(%s::uuid, %s::uuid, %s::jsonb, %s, null, null, null)",
            identifier, claimed.lease_owner, json.dumps(preview), confirmation)["state"] == "settled"
        summary = call("SELECT public.sceneturn_transfer_summary(%s, %s)", owner, identifier)
        assert "private_json" not in summary and "request_json" not in summary
        with pytest.raises(DBAPIError) as failure:
            call("SELECT public.sceneturn_transfer_summary(%s, %s)", neighbor, identifier)
        assert failure.value.orig.sqlstate == "PT404"
        view = TransferView.model_validate(transfer_view(TransferRecord.model_validate(summary)), strict=True)
        assert view.status == "awaiting_confirmation" and view.confirmation_hash == confirmation and view.projects[0].asset_count == 1
        listed = call("SELECT public.sceneturn_transfer_list(%s, null)", owner)
        assert listed[0]["id"] == identifier and "private_json" not in listed[0]
        assert call("SELECT public.sceneturn_transfer_list(%s, null)", neighbor) == []
        assert not any(item["id"] == view.projects[0].id
            for item in call("SELECT public.sceneturn_project_list(%s)", owner)["projects"])

        marker, due = str(uuid.uuid4()), epoch_milliseconds() + 45_000
        with pytest.raises(DBAPIError) as failure:
            call("SELECT public.sceneturn_import_resume(%s, %s::uuid, %s, %s::uuid, %s::bigint)",
                owner, identifier, secrets.token_hex(32), marker, due)
        assert failure.value.orig.sqlstate == "PT409"
        resumed = call("SELECT public.sceneturn_import_resume(%s, %s::uuid, %s, %s::uuid, %s::bigint)",
            owner, identifier, confirmation, marker, due)
        assert resumed["queued"] and resumed["job"]["request_json"]["phase"] == "apply"
        claimed = claim(identifier, marker, due)
        validated = _verify_preview_media(settings, claimed)
        assert call("SELECT public.sceneturn_import_settle(%s::uuid, %s::uuid, null, null, %s, null, null)",
            identifier, claimed.lease_owner, validated)["state"] == "settled"
        result = call("SELECT public.sceneturn_transfer_read(%s::uuid, %s::uuid)", owner, identifier)
        assert result["status"] == "ready" and result["private_json"]["history"]["sourceProject"]["id"] == source_id
        assert not enqueue(str(uuid.uuid4()), source, str(uuid.uuid4()))[0]["queued"]
        return result

    restored = restore_archive(zipfile.ZIP_STORED)
    repeated = restore_archive(zipfile.ZIP_DEFLATED)
    assert repeated["id"] != restored["id"] and repeated["project_id"] == restored["project_id"]
    assert repeated["result_json"]["mappings"] == restored["result_json"]["mappings"]
    projects = call("SELECT public.sceneturn_project_list(%s)", owner)["projects"]
    assert len([item for item in projects if item["title"] == snapshot["title"]]) == 1
    imported = call("SELECT public.sceneturn_project_get(%s, %s)", owner, restored["project_id"])["envelope"]
    asset = call("SELECT public.sceneturn_asset_get(%s, %s)", owner, imported["project"]["assets"][0]["id"])
    assert file_sha256(resolve_asset_path(settings, asset["relpath"])) == file_sha256(sample_video)

    identifier, operation, session = str(uuid.uuid4()), str(uuid.uuid4()), secrets.token_hex(32)
    source = transfer_root(settings, identifier) / "source.zip"
    source.parent.mkdir(parents=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("备用项目/project.json", json.dumps(snapshot))
        archive.write(sample_video, "备用项目/media/原片.mp4")
    sha256 = file_sha256(source)
    reserved = call("SELECT public.sceneturn_import_reserve(%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)",
        owner, identifier, operation, payload_digest({"sha256": sha256}), session, "完整项目.zip", "application/zip", source.stat().st_size, sha256)
    assert reserved["job"]["status"] == "awaiting_upload"
    with pytest.raises(DBAPIError) as failure:
        call("SELECT public.sceneturn_import_read_upload(%s, %s, %s)", owner, identifier, secrets.token_hex(32))
    assert failure.value.orig.sqlstate == "PT404"
    marker, due = str(uuid.uuid4()), epoch_milliseconds() + 45_000
    direct = call("SELECT public.sceneturn_import_enqueue(%s, %s::uuid, %s, null, null, null, %s::uuid, %s::bigint)",
        owner, identifier, session, marker, due)
    assert direct["queued"] and direct["job"]["status"] == "queued"
