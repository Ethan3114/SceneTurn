import json
import secrets
import uuid
from hmac import compare_digest

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings
from app.core.security import decode_secret, encode_secret
from app.services.model_provider_repository import ProviderRegistrySnapshot
from conftest import PRODUCT_ROOT
from test_api import create_project
from test_postgresql_persistence import postgres_runtime
from test_transfer_rpc_postgresql import transfer_rpc_database


@pytest.fixture
def provider_rpc_database(transfer_rpc_database, request):
    admin, _, _ = transfer_rpc_database
    script = PRODUCT_ROOT / "backend" / "sql" / "cloudbase" / "model_providers.sql"
    with admin.begin() as connection:
        connection.exec_driver_sql(script.read_text(), execution_options={"no_parameters": True})

    def cleanup():
        with admin.begin() as connection:
            connection.exec_driver_sql("DROP FUNCTION public.sceneturn_model_registry(uuid, uuid)")
            connection.exec_driver_sql("DROP FUNCTION public.sceneturn_model_owner_update(uuid, uuid, text, integer, jsonb, text, boolean)")

    request.addfinalizer(cleanup)
    return transfer_rpc_database


def test_real_registry_keeps_scoped_provider_keys_encrypted_and_checks_revision(provider_rpc_database):
    admin, start, rpc_transaction = provider_rpc_database
    with start("model-providers") as service, service.client() as creator, service.client() as neighbor:
        account = {"email": f"provider-{uuid.uuid4().hex}@example.com", "password": secrets.token_urlsafe(24)}
        other_account = {"email": f"provider-{uuid.uuid4().hex}@example.com", "password": secrets.token_urlsafe(24)}
        creator_id = creator.post("/auth/register", json=account).json()["user"]["id"]
        other_id = neighbor.post("/auth/register", json=other_account).json()["user"]["id"]
        project = create_project(creator)
    project_id = project["project"]["id"]
    settings = Settings(_env_file=None, master_key=Fernet.generate_key().decode())
    secret = secrets.token_urlsafe(32)
    encrypted = encode_secret(secret, settings)
    registry = text("SELECT public.sceneturn_model_registry(:owner, :project)")
    store = text("""SELECT public.sceneturn_model_owner_update(
        :owner, :identifier, :provider, :revision, CAST(:config AS jsonb), :encrypted, :workspace)""")
    values = {"owner": creator_id, "identifier": str(uuid.uuid4()), "provider": "aliyun-wan", "revision": 0,
        "config": json.dumps({"region": "cn-beijing", "workspaceId": "owned-beijing"}), "encrypted": encrypted, "workspace": True}

    with pytest.raises(DBAPIError) as blocked:
        with rpc_transaction(scope=False) as connection:
            connection.execute(store, values)
    assert blocked.value.orig.sqlstate == "42501"
    with pytest.raises(DBAPIError) as plaintext:
        with rpc_transaction() as connection:
            connection.execute(store, {**values, "encrypted": secret})
    assert plaintext.value.orig.sqlstate == "PT422"
    with pytest.raises(DBAPIError) as malformed:
        with rpc_transaction() as connection:
            connection.execute(store, {**values, "config": json.dumps({"region": "cn-beijing"})})
    assert malformed.value.orig.sqlstate == "PT422"
    with admin.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM sceneturn.model_provider_connections")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM sceneturn.provider_settings")).scalar_one() == 0

    with rpc_transaction() as connection:
        assert connection.execute(store, values).scalar_one() == 1
        raw = connection.execute(registry, {"owner": creator_id, "project": project_id}).scalar_one()
        snapshot = ProviderRegistrySnapshot.model_validate(raw)
        assert snapshot.project_revision == project["revision"]
        assert snapshot.connections["aliyun-wan"].configured and snapshot.wan_workspace == "owned-beijing"
        assert not snapshot.connections["volcengine-ark"].configured
        if encrypted in json.dumps(raw):
            pytest.fail("供应商读取返回了密文")
    with rpc_transaction() as connection:
        raw = connection.execute(registry, {"owner": other_id, "project": None}).scalar_one()
        other = ProviderRegistrySnapshot.model_validate(raw)
        assert not other.connections["aliyun-wan"].configured and other.wan_workspace is None
    with pytest.raises(DBAPIError) as foreign_project:
        with rpc_transaction() as connection:
            connection.execute(registry, {"owner": other_id, "project": project_id})
    assert foreign_project.value.orig.sqlstate == "PT404"

    revision = {**values, "revision": 1, "encrypted": None, "config": json.dumps({"region": "cn-beijing", "workspaceId": "new-beijing"})}
    with rpc_transaction() as connection:
        assert connection.execute(store, revision).scalar_one() == 2
    with pytest.raises(DBAPIError) as stale:
        with rpc_transaction() as connection:
            connection.execute(store, revision)
    assert stale.value.orig.sqlstate == "PT409"
    with rpc_transaction() as connection:
        ark = {**values, "identifier": str(uuid.uuid4()), "provider": "volcengine-ark",
            "config": json.dumps({"region": "cn-beijing"}), "workspace": False}
        assert connection.execute(store, ark).scalar_one() == 1
        own = ProviderRegistrySnapshot.model_validate(connection.execute(registry, {"owner": creator_id, "project": None}).scalar_one())
        assert own.connections["volcengine-ark"].configured
    with admin.connect() as connection:
        retained = connection.execute(text("""SELECT connection.api_key_encrypted, legacy.api_key_encrypted, legacy.workspace_id
            FROM sceneturn.model_provider_connections AS connection JOIN sceneturn.provider_settings AS legacy
            ON legacy.owner_id = connection.owner_id WHERE connection.provider_id = 'aliyun-wan'""")).one()
        if not (compare_digest(retained[0], encrypted) and compare_digest(retained[1], encrypted)
                and compare_digest(decode_secret(retained[0], settings), secret)):
            pytest.fail("供应商密文没有正确保留")
        assert retained[2] == "new-beijing"
