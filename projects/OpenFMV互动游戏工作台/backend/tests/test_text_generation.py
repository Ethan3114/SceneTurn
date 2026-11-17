import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.errors import AppError
from app.models import Base, Budget, Project
from app.services.generation import _canvas_composition
from app.services.text_generation import _reserve
from app.services.text_provider import MODEL, RESULTS, cost_micros, current_price, task_instructions
from conftest import FRONTEND_ORIGIN, PRODUCT_ROOT, LiveService, stop_process
from test_api import create_project, error_code, registered_client, scene_id
from test_generation_canvas import connection, node, save_canvas


class TextTestService:
    def __init__(self, directory):
        self.directory = directory.resolve()
        assert self.directory.is_relative_to(PRODUCT_ROOT / ".intermediate")
        self.data_dir = self.directory / "data"
        self.admin_email = f"text-admin-{uuid.uuid4().hex}@example.com"
        self.process = None
        self.log = None
        self.starts = 0
        self.temp = self.directory / "tmp"
        self.temp.mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}/api/v1"

    def client(self):
        return LiveService(self.base_url, self.data_dir).client()

    def start(self):
        assert self.process is None
        self.starts += 1
        environment = os.environ.copy()
        environment.update(
            OPENFMV_DATA_DIR=str(self.data_dir), OPENFMV_TMP_DIR=str(self.directory / "media"),
            OPENFMV_TEXT_ADMIN_EMAIL=self.admin_email, OPENFMV_WORKER_ENABLED="false",
            OPENFMV_FRONTEND_ORIGIN=FRONTEND_ORIGIN, TMPDIR=str(self.temp),
            XDG_CACHE_HOME=str(self.directory / "cache"), PYTHONPYCACHEPREFIX=str(self.directory / "pycache"),
        )
        log_path = self.directory / f"server-{self.starts}.log"
        self.log = log_path.open("wb")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port), "--no-access-log"],
            cwd=PRODUCT_ROOT / "backend", env=environment, stdin=subprocess.DEVNULL,
            stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 45
        while True:
            assert self.process.poll() is None, f"独立文本服务启动失败：{log_path}"
            with socket.socket() as probe:
                probe.settimeout(0.2)
                ready = probe.connect_ex(("127.0.0.1", self.port)) == 0
            if ready:
                break
            assert time.monotonic() < deadline, f"独立文本服务启动超时：{log_path}"
            time.sleep(0.1)
        with self.client() as client:
            assert client.get("/health").status_code == 200

    def stop(self):
        if self.process is not None:
            stop_process(self.process)
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None


@pytest.fixture(scope="module")
def text_service(tmp_path_factory, request):
    service = TextTestService(tmp_path_factory.mktemp("text-http"))
    request.addfinalizer(service.stop)
    service.start()
    return service


@pytest.fixture(scope="module")
def text_admin(text_service, request):
    client = text_service.client()
    request.addfinalizer(client.close)
    response = client.post("/auth/register", json={"email": text_service.admin_email, "password": secrets.token_urlsafe(18)})
    assert response.status_code == 201, response.text
    assert response.json()["user"]["email"] == text_service.admin_email
    assert client.cookies.get("openfmv_session")
    return client


def database_rows(service, statement, parameters=()):
    with sqlite3.connect(f"file:{service.data_dir / 'openfmv.sqlite3'}?mode=ro", uri=True) as database:
        return database.execute(statement, parameters).fetchall()


def empty_generation_state(service):
    assert database_rows(service, "SELECT COUNT(*) FROM text_provider_settings") == [(0,)]
    assert database_rows(service, "SELECT COUNT(*) FROM text_generations") == [(0,)]
    assert database_rows(service, "SELECT COUNT(*) FROM text_usage_ledger") == [(0,)]
    assert database_rows(service, "SELECT COUNT(*) FROM operations WHERE kind='text.generate'") == [(0,)]
    assert database_rows(service, "SELECT spent_cents,reserved_cents,text_spent_micros,text_reserved_micros FROM budgets WHERE id='global'") == [(0, 0, 0, 0)]


def test_revoked_admin_keeps_real_login_and_loses_text_spending_after_restart(tmp_path):
    service = TextTestService(tmp_path)
    service.start()
    try:
        with service.client() as client:
            registered = client.post("/auth/register", json={"email": service.admin_email, "password": secrets.token_urlsafe(18)})
            assert registered.status_code == 201
            owner_id = registered.json()["user"]["id"]
            envelope = text_canvas(client)
            payload = text_request(envelope)
            assert client.get("/settings/text").json()["canManage"] is True
            assert error_code(generate(client, envelope, payload), 503) == "TEXT_NOT_CONFIGURED"
            empty_generation_state(service)
            service.stop()
            service.admin_email = f"new-admin-{uuid.uuid4().hex}@example.com"
            service.start()
            authenticated = client.get("/auth/me")
            assert authenticated.status_code == 200
            assert authenticated.json()["user"]["id"] == owner_id
            assert client.get("/settings/text").json()["canManage"] is False
            assert error_code(client.put("/settings/text", json={"apiKey": ""}), 403) == "TEXT_SETTINGS_FORBIDDEN"
            assert error_code(generate(client, envelope, payload), 402) == "TEXT_BILLING_NOT_CONFIGURED"
            listed = client.get(f"/projects/{envelope['project']['id']}/text-generations")
            assert listed.status_code == 200 and listed.json()["textGenerations"] == []
            empty_generation_state(service)
    finally:
        service.stop()


def text_canvas(client):
    return save_canvas(client, create_project(client), [
        node("reference", "text", "真实创作资料", content="清晨，一位旅客走进车站。"),
        node("source", "config", "剧本摘要", generationMode="text", dramaTask="summary", composerContent="根据资料编写本集摘要"),
        node("output", "text", "独立文字结果"),
    ], [connection("reference", "source"), connection("source", "output")])


def text_request(envelope, **patch):
    return {"sceneId": scene_id(envelope), "sourceNodeId": "source", "outputNodeId": "output",
            "prompt": "根据资料编写本集摘要", "task": "summary", "expectedRevision": envelope["revision"],
            "operationId": str(uuid.uuid4()), **patch}


def generate(client, envelope, payload):
    return client.post(f"/projects/{envelope['project']['id']}/text-generations", json=payload)


def test_anonymous_cannot_read_or_update_platform_text_key(text_service):
    with text_service.client() as anonymous:
        error_code(anonymous.get("/settings/text"), 401)
        error_code(anonymous.put("/settings/text", json={"apiKey": ""}), 401)
    empty_generation_state(text_service)


def test_admin_and_normal_account_have_real_can_manage_state(text_service, text_admin):
    ordinary, _ = registered_client(text_service)
    with closing(ordinary):
        for client, expected in ((text_admin, True), (ordinary, False)):
            response = client.get("/settings/text")
            assert response.status_code == 200, response.text
            settings = response.json()
            assert set(settings) == {"configured", "provider", "model", "prices", "billingPolicyConfigured", "canManage"}
            assert settings["canManage"] is expected
            assert settings["configured"] is False and settings["billingPolicyConfigured"] is False
            assert settings["provider"] == "deepseek" and settings["model"] == MODEL
            assert settings["prices"]["currency"] == "CNY" and settings["prices"]["costUnit"] == "microYuan"
        assert error_code(ordinary.put("/settings/text", json={"apiKey": ""}), 403) == "TEXT_SETTINGS_FORBIDDEN"
    empty_generation_state(text_service)


@pytest.mark.parametrize("secret", ["", " ", "\n\t", " " * 2049])
def test_admin_rejects_empty_or_whitespace_key(text_service, text_admin, secret):
    assert error_code(text_admin.put("/settings/text", json={"apiKey": secret}), 422) == "TEXT_KEY_INVALID"
    empty_generation_state(text_service)


@pytest.mark.parametrize("patch", [{"model": "unknown-model"}, {"provider": "unknown-provider"}, {"extra": True}])
def test_settings_reject_unknown_models_providers_and_fields(text_service, text_admin, patch):
    assert error_code(text_admin.put("/settings/text", json={"apiKey": "", **patch}), 422) == "VALIDATION_ERROR"
    empty_generation_state(text_service)


def test_admin_unconfigured_provider_fails_without_jobs_or_reservations(text_service, text_admin):
    envelope = text_canvas(text_admin)
    payload = text_request(envelope)
    for _ in range(2):
        assert error_code(generate(text_admin, envelope, payload), 503) == "TEXT_NOT_CONFIGURED"
    loaded = text_admin.get(f"/projects/{envelope['project']['id']}")
    assert loaded.status_code == 200 and loaded.json() == envelope
    listed = text_admin.get(f"/projects/{envelope['project']['id']}/text-generations")
    assert listed.status_code == 200 and listed.json() == {"textGenerations": []}
    usage = text_admin.get(f"/projects/{envelope['project']['id']}/text-usage")
    assert usage.status_code == 200
    assert usage.json() == {"billingPolicyConfigured": False, "currency": "CNY", "costUnit": "microYuan", "inputTokens": 0, "outputTokens": 0, "totalTokens": 0, "costMicros": 0, "entries": []}
    empty_generation_state(text_service)


def test_normal_account_cannot_generate_until_sales_policy_is_configured(text_service):
    ordinary, _ = registered_client(text_service)
    with closing(ordinary):
        envelope = text_canvas(ordinary)
        assert error_code(generate(ordinary, envelope, text_request(envelope)), 402) == "TEXT_BILLING_NOT_CONFIGURED"
        assert ordinary.get(f"/projects/{envelope['project']['id']}/text-generations").json() == {"textGenerations": []}
        assert ordinary.get(f"/projects/{envelope['project']['id']}/text-usage").json()["billingPolicyConfigured"] is False
    empty_generation_state(text_service)


def test_project_and_task_routes_preserve_owner_isolation(text_service, text_admin):
    ordinary, _ = registered_client(text_service)
    with closing(ordinary), text_service.client() as anonymous:
        own = text_canvas(text_admin)
        other = text_canvas(ordinary)
        assert error_code(generate(text_admin, other, text_request(other)), 404) == "PROJECT_NOT_FOUND"
        for suffix in ("text-generations", "text-usage"):
            assert error_code(ordinary.get(f"/projects/{own['project']['id']}/{suffix}"), 404) == "PROJECT_NOT_FOUND"
            error_code(anonymous.get(f"/projects/{own['project']['id']}/{suffix}"), 401)
        identifier = str(uuid.uuid4())
        for client in (text_admin, ordinary):
            assert error_code(client.get(f"/text-generations/{identifier}"), 404) == "TEXT_GENERATION_NOT_FOUND"
            for action in ("stop", "resume", "apply"):
                body = {"expectedRevision": own["revision"], "operationId": str(uuid.uuid4())} if action == "apply" else None
                assert error_code(client.post(f"/text-generations/{identifier}/{action}", json=body), 404) == "TEXT_GENERATION_NOT_FOUND"
        error_code(anonymous.get(f"/text-generations/{identifier}"), 401)
    empty_generation_state(text_service)


def test_stale_project_revision_is_rejected_before_unconfigured_provider(text_service, text_admin):
    envelope = text_canvas(text_admin)
    assert error_code(generate(text_admin, envelope, text_request(envelope, expectedRevision=envelope["revision"] - 1)), 409) == "PROJECT_REVISION_CONFLICT"
    empty_generation_state(text_service)


@pytest.mark.parametrize("patch", [
    {"task": "unknown"}, {"task": "join"}, {"expectedRevision": True}, {"expectedRevision": "1"},
    {"expectedRevision": 0}, {"maxTokens": 63}, {"maxTokens": 32769}, {"maxTokens": True},
    {"sceneId": ""}, {"sourceNodeId": ""}, {"outputNodeId": ""}, {"operationId": ""},
    {"prompt": "甲" * 20001}, {"model": "unknown"},
])
def test_create_schema_rejects_invalid_inputs_without_side_effects(text_service, text_admin, patch):
    envelope = text_canvas(text_admin)
    assert error_code(generate(text_admin, envelope, text_request(envelope, **patch)), 422) == "VALIDATION_ERROR"
    assert text_admin.get(f"/projects/{envelope['project']['id']}").json() == envelope
    empty_generation_state(text_service)


@pytest.mark.parametrize("change,code", [
    ({"sceneId": "outside-scene"}, "SCENE_NOT_FOUND"),
    ({"sourceNodeId": "outside-source"}, "SOURCE_NODE_NOT_FOUND"),
    ({"outputNodeId": "outside-output"}, "GENERATION_OUTPUT_INVALID"),
    ({"outputNodeId": "source"}, "GENERATION_OUTPUT_INVALID"),
])
def test_text_composition_enforces_saved_scene_source_output(text_service, text_admin, change, code):
    envelope = text_canvas(text_admin)
    engine = create_engine(f"sqlite:///{text_service.data_dir / 'openfmv.sqlite3'}")
    with ExitStack() as cleanup:
        cleanup.callback(engine.dispose)
        with sessionmaker(engine)() as session:
            project = session.get(Project, envelope["project"]["id"])
            payload = text_request(envelope, **change)
            with pytest.raises(AppError) as failure:
                _canvas_composition(session, project, payload["sceneId"], payload["sourceNodeId"], payload["prompt"], payload["outputNodeId"], generation_mode="text", output_type="text")
            assert failure.value.code == code
    empty_generation_state(text_service)


def test_foreign_scene_nodes_cannot_be_used_in_text_composition(text_service, text_admin):
    envelope = text_canvas(text_admin)
    first_scene = scene_id(envelope)
    response = text_admin.post(f"/projects/{envelope['project']['id']}/scenes", json={"title": "独立剧情", "expectedRevision": envelope["revision"], "operationId": str(uuid.uuid4())})
    assert response.status_code == 201, response.text
    envelope = response.json()
    second_scene = next(item["id"] for item in envelope["project"]["graphData"]["nodes"] if item["type"] == "scene" and item["id"] != first_scene)
    envelope = save_canvas(text_admin, envelope, [node("foreign-source", "config", "其他场景来源", generationMode="text", composerContent="其他场景"), node("foreign-output", "text", "其他场景结果")], target_scene=second_scene)
    engine = create_engine(f"sqlite:///{text_service.data_dir / 'openfmv.sqlite3'}")
    with ExitStack() as cleanup:
        cleanup.callback(engine.dispose)
        with sessionmaker(engine)() as session:
            project = session.get(Project, envelope["project"]["id"])
            for source, output, prompt, expected in (("source", "foreign-output", "根据资料编写本集摘要", "GENERATION_OUTPUT_INVALID"), ("foreign-source", "output", "其他场景", "SOURCE_NODE_NOT_FOUND")):
                with pytest.raises(AppError) as failure:
                    _canvas_composition(session, project, first_scene, source, prompt, output, generation_mode="text", output_type="text")
                assert failure.value.code == expected
    empty_generation_state(text_service)


def test_unconfigured_text_state_and_owner_session_survive_restart(text_service, text_admin):
    envelope = text_canvas(text_admin)
    text_service.stop()
    text_service.start()
    response = text_admin.get("/settings/text")
    assert response.status_code == 200 and response.json()["canManage"] is True
    assert response.json()["configured"] is False
    assert text_admin.get(f"/projects/{envelope['project']['id']}").json() == envelope
    assert text_admin.get(f"/projects/{envelope['project']['id']}/text-generations").json() == {"textGenerations": []}
    assert database_rows(text_service, "SELECT version_num FROM alembic_version") == [("0012_project_transfers",)]
    empty_generation_state(text_service)


@pytest.mark.parametrize("task,invalid", [
    ("text", {}), ("text", {"text": " "}), ("text", {"text": ["正文"]}),
    ("summary", {"text": "甲" * 20001}), ("character", {"text": "角色", "extra": True}),
    ("prop", {"text": ""}), ("scene", {"text": 4}), ("plot", {"shots": []}),
    ("plot", {"shots": [{"no": 1, "text": "画面"}, {"no": 1, "text": "重复编号"}]}),
    ("plot", {"shots": [{"no": True, "text": "画面"}]}),
    ("plot", {"shots": [{"no": 1, "text": "甲" * 12001}]}),
    ("shot", {"text": "缺少编号"}), ("shot", {"no": 0, "text": "画面"}),
])
def test_result_schema_rejects_invalid_structure(task, invalid):
    with pytest.raises(ValidationError):
        RESULTS[task].model_validate_json(json.dumps(invalid, ensure_ascii=False))


def test_each_text_task_has_deterministic_instructions_and_schema():
    assert set(RESULTS) == {"text", "summary", "character", "prop", "scene", "plot", "shot"}
    for task, schema in RESULTS.items():
        instructions = task_instructions(task)
        assert json.dumps(schema.model_json_schema(), ensure_ascii=False) in instructions
        assert "禁止调用工具" in instructions and "【文本N】" in instructions


@pytest.mark.parametrize("hour,band", [(8, "offPeak"), (9, "peak"), (11, "peak"), (12, "offPeak"), (14, "peak"), (17, "peak"), (18, "offPeak")])
def test_price_band_uses_shanghai_time(hour, band):
    timestamp = datetime(2026, 11, 13, hour, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert current_price(timestamp)["band"] == band


@pytest.mark.parametrize("month,day", [(11, 15), (10, 1)])
def test_weekend_and_holiday_use_off_peak_price(month, day):
    timestamp = datetime(2026, month, day, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    assert current_price(timestamp)["band"] == "offPeak"


@pytest.mark.parametrize("hit,miss,output,expected", [(0, 0, 0, 0), (1, 0, 0, 1), (0, 1, 0, 2), (0, 0, 1, 8), (100, 200, 300, 2804), (1000000, 0, 0, 40000)])
def test_cost_calculation_keeps_micro_yuan_precision(hit, miss, output, expected):
    price = current_price(datetime(2026, 11, 13, 10, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    assert cost_micros(hit, miss, output, price) == expected


@pytest.mark.parametrize("spent,reserved,text_spent,text_reserved,remaining", [(1, 0, 0, 0, 10000), (0, 1, 0, 0, 10000), (0, 0, 1, 0, 19999), (0, 0, 0, 19999, 1)])
def test_text_reservation_shares_real_sqlite_budget_with_media(tmp_path, spent, reserved, text_spent, text_reserved, remaining):
    engine = create_engine(f"sqlite:///{tmp_path / 'budget.sqlite3'}")
    with ExitStack() as cleanup:
        cleanup.callback(engine.dispose)
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine)
        settings = Settings(budget_cents=2, worker_enabled=False)
        with factory.begin() as session:
            session.add(Budget(id="global", limit_cents=2, spent_cents=spent, reserved_cents=reserved, text_spent_micros=text_spent, text_reserved_micros=text_reserved))
        with factory.begin() as session:
            _reserve(session, remaining, settings)
        with factory() as session:
            record = session.get(Budget, "global")
            assert (record.spent_cents + record.reserved_cents) * 10000 + record.text_spent_micros + record.text_reserved_micros == 20000
            assert record.text_reserved_micros == text_reserved + remaining
            with pytest.raises(AppError) as failure:
                _reserve(session, 1, settings)
            assert failure.value.code == "BUDGET_EXCEEDED"
            session.rollback()
        with factory() as session:
            assert session.get(Budget, "global").text_reserved_micros == text_reserved + remaining


def test_parallel_real_budget_reservations_cannot_exceed_limit(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent-budget.sqlite3'}")
    with ExitStack() as cleanup:
        cleanup.callback(engine.dispose)
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine)
        settings = Settings(budget_cents=1, worker_enabled=False)
        with factory.begin() as session:
            session.add(Budget(id="global", limit_cents=1))

        def reserve():
            with factory.begin() as session:
                _reserve(session, 6000, settings)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(reserve), executor.submit(reserve)]
        errors = [future.exception() for future in futures]
        assert sum(error is None for error in errors) == 1
        assert [error.code for error in errors if error is not None] == ["BUDGET_EXCEEDED"]
        with factory() as session:
            record = session.get(Budget, "global")
            assert record.text_reserved_micros == 6000 and record.spent_cents == record.reserved_cents == record.text_spent_micros == 0
