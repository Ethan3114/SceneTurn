import json
from contextlib import closing
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.api.publications import view
from app.api.transfers import game_file
from app.core.config import Settings
from app.core.security import new_token
from app.services.cloudbase_publications import game, media, optional_publication, publication, publications
from app.services.transfer_worker import claim, process_transfer
from conftest import PRODUCT_ROOT
from test_api import registered_client
from test_postgresql_persistence import postgres_runtime
from test_project_transfers import actual_project, export


def test_real_cloudbase_publication_uses_confirmed_game_and_selected_assets(postgres_runtime, request, sample_video):
    admin, runtime, start = postgres_runtime
    scope = new_token()
    roles = ("anon", "authenticated", "service_role")
    sql_dir = PRODUCT_ROOT / "backend" / "sql" / "cloudbase"
    with admin.begin() as connection:
        for role in roles:
            connection.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
        for filename in ("access.sql", "publications.sql"):
            connection.connection.driver_connection.execute((sql_dir / filename).read_text(encoding="utf-8"))
        connection.execute(text("""
            INSERT INTO sceneturn.gateway_credentials (token_sha256) VALUES (sha256(convert_to(:scope, 'UTF8')))
        """), {"scope": scope})

    def cleanup():
        with admin.begin() as connection:
            connection.exec_driver_sql("""
                DROP FUNCTION public.sceneturn_publication_list();
                DROP FUNCTION public.sceneturn_publication_read(text);
                DROP FUNCTION public.sceneturn_publication_game(text, text);
                DROP FUNCTION public.sceneturn_publication_media(text, text);
                DROP FUNCTION public.sceneturn_publication_owner(text, text);
                DROP FUNCTION public.sceneturn_publication_prepare(text, text, text);
                DROP FUNCTION public.sceneturn_publication_save(text, text, uuid, boolean, text, text, text, text, text, text);
                DROP FUNCTION public.sceneturn_schema_revision();
            """)
            for role in roles:
                connection.exec_driver_sql(f"DROP ROLE {role}")

    request.addfinalizer(cleanup)

    def invoke(statement, parameters, *, scoped=True):
        with admin.begin() as connection:
            connection.execute(text("SELECT set_config('request.jwt.claims', :claims, true)"),
                {"claims": json.dumps({"role": "service_role"})})
            connection.execute(text("SELECT set_config('request.headers', :headers, true)"),
                {"headers": json.dumps({"x-sceneturn-database-scope": scope} if scoped else {})})
            connection.exec_driver_sql("SET LOCAL ROLE service_role")
            return connection.execute(text(statement), parameters).scalar_one()

    def denied(statement, parameters, sqlstate, message):
        with pytest.raises(DBAPIError) as denied_call:
            invoke(statement, parameters)
        assert denied_call.value.orig.sqlstate == sqlstate
        assert denied_call.value.orig.diag.message_primary == message

    listing = "SELECT public.sceneturn_publication_list()"
    owner_read = "SELECT public.sceneturn_publication_owner(:owner, :transfer)"
    prepare = "SELECT public.sceneturn_publication_prepare(:owner, :transfer, :title)"
    save = """
        SELECT public.sceneturn_publication_save(:owner, :transfer, CAST(:new_slug AS uuid), :published,
          :title, :description, :poster, :trailer, :archive, :index_hash)
    """
    read = "SELECT public.sceneturn_publication_read(:slug)"
    player = "SELECT public.sceneturn_publication_game(:slug, :member)"
    cover = "SELECT public.sceneturn_publication_media(:slug, :role)"

    with start("publication") as backend, closing(registered_client(backend)[0]) as owner, closing(registered_client(backend)[0]) as other:
        source, trailer = actual_project(owner, sample_video)
        _, other_trailer = actual_project(other, sample_video)
        _, unrelated_trailer = actual_project(owner, sample_video)
        owner_id = owner.get("/auth/me").json()["user"]["id"]
        other_id = other.get("/auth/me").json()["user"]["id"]
        pending = export(owner, source["project"]["id"], source["revision"], "game")
        assert pending["status"] == "awaiting_confirmation"
        settings = Settings(data_dir=backend.data_dir, tmp_dir=backend.data_dir / "work", _env_file=None)
        parameters = {"owner": owner_id, "transfer": pending["id"], "title": "确认版本试玩"}

        with pytest.raises(DBAPIError) as forbidden:
            invoke(listing, {}, scoped=False)
        assert forbidden.value.orig.sqlstate == "42501"
        assert publications.validate_python(invoke(listing, {}), strict=True) == []
        assert optional_publication.validate_python(invoke(owner_read, parameters), strict=True) is None
        denied(prepare, {**parameters, "owner": other_id}, "PT404", "TRANSFER_NOT_FOUND")
        denied(prepare, parameters, "PT404", "GAME_VERSION_NOT_FOUND")
        confirm = owner.post(f"/transfers/{pending['id']}/confirm", json={"confirmationHash": pending["confirmationHash"]})
        assert confirm.status_code == 200, confirm.text

        factory = sessionmaker(bind=runtime)
        worker_id = str(uuid4())
        assert claim(factory, worker_id, settings, identifier=pending["id"]) == pending["id"]
        process_transfer(factory, settings, pending["id"], worker_id)
        assert owner.get(f"/transfers/{pending['id']}").json()["transfer"]["status"] == "ready"

        verified = game.validate_python(invoke(prepare, parameters), strict=True)
        index = game_file(verified.as_transfer(), "index.html", settings)
        assert index.is_file()
        assert "这份创作依据只保留在完整工程中" not in index.read_text(encoding="utf-8")
        updated = {**parameters, "new_slug": str(uuid4()), "published": True, "description": "固定版本发布",
                   "poster": None, "trailer": trailer["id"], "archive": verified.private_json.archive_sha256,
                   "index_hash": verified.private_json.file_manifest[0].sha256}
        denied(save, {**updated, "owner": other_id}, "PT404", "TRANSFER_NOT_FOUND")
        denied(save, {**updated, "index_hash": "0" * 64}, "PT409", "GAME_FILE_CHANGED")
        denied(save, {**updated, "poster": trailer["id"]}, "PT404", "PUBLICATION_MEDIA_NOT_FOUND")
        denied(save, {**updated, "trailer": other_trailer["id"]}, "PT404", "PUBLICATION_MEDIA_NOT_FOUND")
        denied(save, {**updated, "trailer": unrelated_trailer["id"]}, "PT404", "PUBLICATION_MEDIA_NOT_FOUND")
        assert invoke(listing, {}) == []

        released = publication.validate_python(invoke(save, updated), strict=True)
        assert publications.validate_python(invoke(listing, {}), strict=True) == [released]
        public_view = view(publication.validate_python(invoke(read, {"slug": released.slug}), strict=True))
        assert "trailerAssetId" not in public_view and public_view["playUrl"].endswith("/play/index.html")
        assert "trailerAssetId" in view(released, owner=True)
        shown = media.validate_python(invoke(cover, {"slug": released.slug, "role": "trailer"}), strict=True)
        assert shown.mime_type == "video/mp4"
        assert (settings.data_dir / shown.relpath).read_bytes() == sample_video.read_bytes()
        play_parameters = {"slug": released.slug, "member": f"assets/{trailer['id']}.mp4"}
        playable = game.validate_python(invoke(player, play_parameters), strict=True)
        assert game_file(playable.as_transfer(), play_parameters["member"], settings).read_bytes() == sample_video.read_bytes()
        denied(player, {**play_parameters, "member": f"assets/{other_trailer['id']}.mp4"}, "PT404", "GAME_FILE_NOT_FOUND")
        denied(player, {**play_parameters, "member": "../../openfmv.sqlite3"}, "PT404", "GAME_FILE_NOT_FOUND")
        denied(cover, {"slug": released.slug, "role": "poster"}, "PT404", "PUBLICATION_MEDIA_NOT_FOUND")

        withdrawn = publication.validate_python(invoke(save, {**updated, "published": False}), strict=True)
        assert withdrawn.slug == released.slug and not withdrawn.published
        assert invoke(listing, {}) == []
        denied(read, {"slug": released.slug}, "PT404", "PUBLICATION_NOT_FOUND")
        denied(cover, {"slug": released.slug, "role": "trailer"}, "PT404", "PUBLICATION_NOT_FOUND")
        denied(player, play_parameters, "PT404", "PUBLICATION_NOT_FOUND")
        assert publication.validate_python(invoke(save, {**updated, "new_slug": str(uuid4())}), strict=True).slug == released.slug
