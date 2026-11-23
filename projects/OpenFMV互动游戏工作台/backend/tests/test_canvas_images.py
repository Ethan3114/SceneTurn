import hashlib
import sqlite3
import uuid
from pathlib import Path

from test_api import scene_id
from test_codex_connections import account_project, pair
from test_codex_images import capable
from test_generation_canvas import connection, node, save_canvas


def test_canvas_image_uses_saved_links_and_requires_confirmation(live_service):
    with live_service.client() as owner, live_service.client() as other:
        project = account_project(owner)
        account_project(other)
        _, claimed = pair(owner, project)
        headers = capable(owner, claimed)
        reference = (Path(__file__).resolve().parents[2] / "frontend/public/logo.png").read_bytes()
        uploaded = owner.post(f"/projects/{project}/assets", files={"file": ("logo.png", reference, "image/png")})
        uploaded.raise_for_status()
        asset_id = uploaded.json()["asset"]["id"]
        envelope = owner.get(f"/projects/{project}").json()
        prompt = "按@[node:look]绘制@[node:brief]。"
        nodes = [node("source", "config", "分镜图", generationMode="image", composerContent=prompt,
            modelProfileId="codex-image", imageWidth=1536, imageHeight=1024, outputNodeId="result"),
            node("result", "image", "分镜图结果", pendingResult=True),
            node("look", "image", "构图参考", storageKey=asset_id, content=f"/api/v1/assets/{asset_id}/content"),
            node("brief", "text", "分镜描述", content="清晨的图书馆，长桌朝向拱窗。")]
        edges = [connection("brief", "source"), connection("look", "source"), connection("source", "result")]
        envelope = save_canvas(owner, envelope, nodes, edges)
        path = f"/projects/{project}/canvas-image-jobs"
        body = {"operationId": str(uuid.uuid4()), "expectedRevision": envelope["revision"], "sceneId": scene_id(envelope),
            "sourceNodeId": "source", "outputNodeId": "result", "modelProfileId": "codex-image", "prompt": prompt, "width": 1536, "height": 1024}
        rejected = owner.post(path, json={**body, "referenceAssetIds": []})
        assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "CANVAS_IMAGE_REFERENCES_CHANGED"
        rejected = owner.post(path, json={**body, "modelProfileId": "sensetime-fast", "width": 1520})
        assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "CANVAS_IMAGE_SIZE_UNSUPPORTED"
        rejected = owner.post(path, json={**body, "modelProfileId": "seedream-5-5", "width": 1024, "height": 768})
        assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "ARK_IMAGE_PARAMETERS_UNSUPPORTED"
        assert other.post(path, json=body).status_code == 404
        created = owner.post(path, json=body)
        assert created.status_code == 201, created.text
        job = created.json()["job"]
        assert job["status"] == "awaiting_confirmation" and job["assetId"] is None
        assert job["width"] == 1536 and job["height"] == 1024 and job["modelProfileId"] == "codex-image"
        assert "prompt" not in job and "price" not in job
        assert owner.post(path, json=body).json() == created.json()
        assert other.get(path).status_code == 404
        assert owner.post("/connections/images/claim", json={}, headers=headers).json() == {"result": None}
        confirm = f"{path}/{job['id']}/confirm"
        approval = {"expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()), "accountUsageConfirmed": True}
        assert owner.post(confirm, json={**approval, "accountUsageConfirmed": False}).status_code == 422
        assert other.post(confirm, json=approval).status_code == 404
        confirmed = owner.post(confirm, json=approval)
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["job"]["status"] == "queued"
        assert owner.post(confirm, json=approval).json() == confirmed.json()
        manifest = owner.post("/connections/images/claim", json={}, headers=headers)
        assert manifest.status_code == 200, manifest.text
        payload = manifest.json()["result"]
        assert payload["job"]["id"] == job["id"] and payload["submissionAttempted"] is False
        assert "按【参考图1】绘制【文本1】。" in payload["job"]["prompt"]
        assert "【文本1】\n清晨的图书馆，长桌朝向拱窗。" in payload["job"]["prompt"]
        assert "画幅 3:2" in payload["job"]["prompt"]
        assert payload["job"]["referenceAssets"][0]["assetId"] == asset_id
        fetched = owner.get(f"/connections/images/{job['id']}/references/{asset_id}/content", headers=headers)
        assert fetched.status_code == 200 and hashlib.sha256(fetched.content).digest() == hashlib.sha256(reference).digest()
        assert owner.get(f"/projects/{project}/codex-images").json() == {"jobs": []}
        assert owner.get(path).json()["jobs"][0]["status"] == "submitting"
        with sqlite3.connect(live_service.data_dir / "openfmv.sqlite3") as database:
            assert database.execute("SELECT run_id, submitted_at FROM image_jobs WHERE id=?", (job["id"],)).fetchone() == (None, None)


def test_canvas_image_rejects_confirmation_when_connected_text_changes(live_service):
    with live_service.client() as client:
        project = account_project(client)
        _, claimed = pair(client, project)
        capable(client, claimed)
        envelope = client.get(f"/projects/{project}").json()
        nodes = [node("source", "config", "分镜图", generationMode="image", composerContent="",
            modelProfileId="codex-image", imageWidth=1024, imageHeight=1024, outputNodeId="result"),
            node("result", "image", "镜头结果", pendingResult=True), node("brief", "text", "描述", content="上午")]
        edges = [connection("brief", "source"), connection("source", "result")]
        envelope = save_canvas(client, envelope, nodes, edges)
        path = f"/projects/{project}/canvas-image-jobs"
        body = {"operationId": str(uuid.uuid4()), "expectedRevision": envelope["revision"], "sceneId": scene_id(envelope),
            "sourceNodeId": "source", "outputNodeId": "result", "modelProfileId": "codex-image", "prompt": "", "width": 1024, "height": 1024}
        created = client.post(path, json=body)
        assert created.status_code == 201, created.text
        job_id = created.json()["job"]["id"]
        nodes[2]["metadata"]["content"] = "傍晚"
        envelope = save_canvas(client, envelope, nodes, edges)
        rejected = client.post(f"{path}/{job_id}/confirm", json={
            "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()), "accountUsageConfirmed": True})
        assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "CANVAS_IMAGE_INPUT_CHANGED"
        nodes[2]["metadata"]["content"] = "上午"
        nodes[0]["metadata"]["imageWidth"] = 1536
        envelope = save_canvas(client, envelope, nodes, edges)
        rejected = client.post(f"{path}/{job_id}/confirm", json={
            "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4()), "accountUsageConfirmed": True})
        assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "CANVAS_IMAGE_PARAMETERS_CHANGED"
        assert client.get(path).json()["jobs"][0]["status"] == "awaiting_confirmation"
