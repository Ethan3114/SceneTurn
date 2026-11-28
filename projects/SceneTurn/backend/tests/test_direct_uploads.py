import hashlib
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.services.direct_uploads import DirectUploadCreate, reserve_upload
from app.services.transfer_worker import claim


@dataclass(frozen=True)
class Account:
    identifier: str
    credentials: dict


def register(client):
    credentials = {"email": f"direct-{uuid.uuid4()}@example.com", "password": str(uuid.uuid4())}
    response = client.post("/auth/register", json=credentials)
    assert response.status_code == 201
    return Account(response.json()["user"]["id"], credentials)


def upload_body(filename="archive.zip", data=b"declared upload bytes"):
    return {"operationId": str(uuid.uuid4()), "filename": filename, "contentType": "application/octet-stream",
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def test_local_requests_retain_multipart_and_verify_project_owner(live_service):
    with live_service.client() as owner, live_service.client() as stranger:
        register(owner)
        register(stranger)
        project = owner.post("/projects", json={"title": "素材分段上传"}).json()["project"]["id"]
        path = f"/projects/{project}/assets/direct-upload"
        response = owner.post(path, json=upload_body("video.mp4"))
        assert response.json() == {"mode": "multipart"}
        assert response.headers["cache-control"] == "no-store"
        assert stranger.post(path, json=upload_body("video.mp4")).status_code == 404
        assert owner.post("/transfers/imports/direct-upload", json=upload_body()).json() == {"mode": "multipart"}


def test_reserved_upload_is_persistent_scoped_and_not_run_without_the_file(live_service):
    settings = Settings(_env_file=None, data_dir=live_service.data_dir)
    engine = create_engine(settings.database_url)
    with live_service.client() as owner, live_service.client() as different_session, live_service.client() as stranger:
        account = register(owner)
        register(stranger)
        assert different_session.post("/auth/login", json=account.credentials).status_code == 200
        first = owner.post("/projects", json={"title": "上传所属项目"}).json()["project"]["id"]
        second = owner.post("/projects", json={"title": "另一个所属项目"}).json()["project"]["id"]
        token = owner.cookies.get("sceneturn_session")
        asset = DirectUploadCreate.model_validate(upload_body("video.mp4"))
        archive = DirectUploadCreate.model_validate(upload_body())
        with Session(engine, expire_on_commit=False) as db:
            reserved, reused = reserve_upload(db, settings, account.identifier, first, "asset_upload", token, asset)
            assert not reused
            asset_id = reserved.id
            assert reserved.request_json["directUpload"]["key"] == f"incoming/{asset_id}/source"
        with Session(engine, expire_on_commit=False) as db:
            resumed, reused = reserve_upload(db, settings, account.identifier, first, "asset_upload", token, asset)
            assert reused and resumed.id == asset_id
            import_job, reused = reserve_upload(db, settings, account.identifier, None, "import", token, archive)
            assert not reused
            import_id = import_job.id
            assert import_job.request_json["directUpload"]["key"] == f"transfers/{import_id}/source.zip"
            with pytest.raises(AppError) as conflict:
                reserve_upload(db, settings, account.identifier, second, "asset_upload", token, asset)
            assert conflict.value.code == "OPERATION_CONFLICT"

        asset_path = f"/projects/{first}/assets/direct-upload/{asset_id}"
        complete = f"{asset_path}/complete"
        assert owner.get(asset_path).json()["status"] == "awaiting_upload"
        assert owner.get(f"/projects/{second}/assets/direct-upload/{asset_id}").status_code == 404
        assert stranger.post(complete).status_code == 404
        assert different_session.post(complete).status_code == 404
        assert owner.post(complete).json()["error"]["code"] == "DIRECT_STORAGE_UNAVAILABLE"
        assert owner.get(f"/transfers/{import_id}").status_code == 404
        assert owner.get("/transfers").json() == {"transfers": []}
        assert owner.get(asset_path).json()["status"] == "awaiting_upload"
        assert claim(lambda: Session(engine, expire_on_commit=False), str(uuid.uuid4())) is None
    engine.dispose()
