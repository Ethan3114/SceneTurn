import os
import socket
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool

from app.postgresql import SCENETURN_APP_ROLE, SCENETURN_OWNER_ROLE, SCENETURN_SCHEMA, create_postgresql_engine, validate_postgresql_access
from conftest import FRONTEND_ORIGIN, PRODUCT_ROOT, LiveService, stop_process
from test_api import create_project, save_generation_node


@pytest.fixture
def postgres_runtime(tmp_path, request):
    admin_url = os.environ.get("SCENETURN_TEST_POSTGRES_ADMIN_URL")
    if not admin_url:
        pytest.skip("需指定本机独立 PostgreSQL 17 测试数据库")
    url = make_url(admin_url)
    assert url.host == "127.0.0.1" and url.database == "sceneturn_test" and url.username == "postgres"
    assert tmp_path.is_relative_to(PRODUCT_ROOT / ".intermediate")

    admin = create_engine(url.set(drivername="postgresql+psycopg"), poolclass=NullPool, hide_parameters=True)
    roles = (SCENETURN_APP_ROLE, SCENETURN_OWNER_ROLE, "sceneturn_neighbor")
    with admin.begin() as connection:
        assert 170000 <= int(connection.exec_driver_sql("SHOW server_version_num").scalar_one()) < 180000
        for role in roles:
            connection.exec_driver_sql(f"CREATE ROLE {role} LOGIN")
        connection.exec_driver_sql(f"CREATE SCHEMA {SCENETURN_SCHEMA} AUTHORIZATION {SCENETURN_OWNER_ROLE}")
        connection.exec_driver_sql("CREATE TABLE public.users (id integer PRIMARY KEY)")
        connection.exec_driver_sql("ALTER TABLE public.users OWNER TO sceneturn_neighbor")
        connection.exec_driver_sql("INSERT INTO public.users VALUES (101)")

    def clear_fixture():
        with admin.begin() as connection:
            connection.exec_driver_sql(f"DROP SCHEMA {SCENETURN_SCHEMA} CASCADE")
            connection.exec_driver_sql("DROP TABLE public.users")
            for role in roles:
                connection.exec_driver_sql(f"DROP ROLE {role}")
        admin.dispose()

    request.addfinalizer(clear_fixture)

    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    environment = {key: value for key, value in os.environ.items() if not key.startswith("OPENFMV_")}
    environment.update(
        OPENFMV_TMP_DIR=str(tmp_path / "media"),
        OPENFMV_WORKER_ENABLED="false",
        OPENFMV_FRONTEND_ORIGIN=FRONTEND_ORIGIN,
        OPENFMV_MASTER_KEY=Fernet.generate_key().decode(),
        TMPDIR=str(temp_dir),
        PYTHONDONTWRITEBYTECODE="1",
    )
    backend = PRODUCT_ROOT / "backend"
    owner_url, runtime_url = [url.set(username=role).render_as_string(hide_password=False)
                              for role in (SCENETURN_OWNER_ROLE, SCENETURN_APP_ROLE)]
    migration_env = {**environment, "OPENFMV_DATA_DIR": str(tmp_path / "migrate"), "OPENFMV_POSTGRES_URL": owner_url}
    for revision in ("0017_login_failures", "head"):
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", revision], cwd=backend, env=migration_env, check=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    @contextmanager
    def start(name):
        data_dir = tmp_path / name
        runtime_env = {**environment, "OPENFMV_DATA_DIR": str(data_dir), "OPENFMV_POSTGRES_URL": runtime_url}
        log_path = tmp_path / f"{name}.log"
        base_url = f"http://127.0.0.1:{port}/api/v1"
        with log_path.open("wb") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=backend, env=runtime_env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None and time.monotonic() < deadline, f"真实 PostgreSQL 后端启动失败，请查看 {log_path}"
                    with socket.socket() as connection:
                        connection.settimeout(0.2)
                        if connection.connect_ex(("127.0.0.1", port)) == 0:
                            break
                    time.sleep(0.1)
                with httpx.Client(base_url=base_url, trust_env=False) as client:
                    assert client.get("/health").status_code == 200
                yield LiveService(base_url, data_dir)
            finally:
                stop_process(process)

    return admin, create_postgresql_engine(runtime_url, allow_local_plaintext=True), start


def test_sqlite_preserves_canvas_node_history(live_service):
    with live_service.client() as client:
        assert client.post("/auth/register", json={"email": f"sqlite-{uuid.uuid4().hex}@example.com",
            "password": uuid.uuid4().hex}).status_code == 201
        envelope = create_project(client)
        project_id = envelope["project"]["id"]
        scene_id, node_id, revision = save_generation_node(client, envelope)
        versions = client.get(f"/projects/{project_id}/canvases/{scene_id}/nodes/{node_id}/versions")
        assert versions.status_code == 200
        assert [(item["revision"], item["node"]["id"]) for item in versions.json()["versions"]] == [(revision, node_id)]


def test_real_postgresql_preserves_transactions_and_project_on_restart(postgres_runtime):
    admin, runtime, start = postgres_runtime
    with runtime.connect() as connection:
        validate_postgresql_access(connection)
        with pytest.raises(ProgrammingError):
            connection.exec_driver_sql("SELECT id FROM public.users")
        connection.rollback()
        with pytest.raises(ProgrammingError):
            connection.exec_driver_sql("CREATE TABLE sceneturn.unexpected (id integer)")
    with admin.connect() as connection:
        assert connection.exec_driver_sql("SELECT id FROM public.users").scalar_one() == 101
        assert connection.exec_driver_sql("""
            SELECT count(*) > 30 AND bool_and(relrowsecurity)
            FROM pg_class JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
            WHERE nspname = 'sceneturn' AND relkind = 'r' AND relname <> 'alembic_version'
        """).scalar_one()

    with start("first") as service, service.client() as client:
        credentials = {"email": f"postgres-{uuid.uuid4().hex}@example.com", "password": uuid.uuid4().hex}
        assert client.post("/auth/register", json=credentials).status_code == 201
        created = client.post("/projects", json={"title": "重启之前"})
        assert created.status_code == 201
        envelope = created.json()
        project_id = envelope["project"]["id"]
        cookies = dict(client.cookies)

        def update(title):
            with service.client() as editor:
                editor.cookies.update(cookies)
                return editor.put(f"/projects/{project_id}", json={"operationId": str(uuid.uuid4()), "expectedRevision": envelope["revision"],
                    "project": {**envelope["project"], "title": title}})

        with ThreadPoolExecutor(max_workers=2) as requests:
            edits = list(requests.map(update, ("编辑甲", "编辑乙")))
        assert sorted(edit.status_code for edit in edits) == [200, 409]
        changed = next(edit.json() for edit in edits if edit.status_code == 200)
        assert changed["revision"] == envelope["revision"] + 1
        scene_id, node_id, revision = save_generation_node(client, changed)
        queued = client.post(f"/projects/{project_id}/exports", json={"kind": "backup", "operationId": str(uuid.uuid4()),
            "expectedRevision": revision})
        assert queued.status_code == 201
        assert queued.json()["transfer"]["status"] == "queued"
        transfer_id = queued.json()["transfer"]["id"]
        assert not (service.data_dir / "openfmv.sqlite3").exists()

    with start("restarted") as service, service.client() as client:
        client.cookies.update(cookies)
        assert client.get("/auth/me").status_code == 200
        restored = client.get(f"/projects/{project_id}").json()
        assert restored["revision"] == revision
        assert restored["project"]["title"] == changed["project"]["title"]
        versions = client.get(f"/projects/{project_id}/canvases/{scene_id}/nodes/{node_id}/versions")
        assert versions.status_code == 200
        assert versions.json()["versions"][0]["node"]["id"] == node_id
        assert client.get(f"/transfers/{transfer_id}").json()["transfer"]["status"] == "queued"
        with service.client() as neighbor:
            assert neighbor.post("/auth/register", json={"email": f"neighbor-{uuid.uuid4().hex}@example.com", "password": uuid.uuid4().hex}).status_code == 201
            assert neighbor.get(f"/projects/{project_id}").status_code == 404
        with admin.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'SceneTurn'").scalar_one() == 0
