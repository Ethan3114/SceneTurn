from typing import Literal

from pydantic import Field, TypeAdapter, ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.models import new_id
from app.schemas import WireModel
from app.services.cloudbase_database import call_cloudbase_rpc
from app.transfer_models import TransferJob


class PublicationRecord(WireModel):
    slug: str = Field(min_length=1, max_length=36)
    title: str = Field(max_length=160)
    description: str = Field(max_length=2000)
    poster_asset_id: str | None
    trailer_asset_id: str | None
    published: bool
    created_at: float = Field(allow_inf_nan=False)


class PublicationFile(WireModel):
    path: str = Field(min_length=1, max_length=1024)


class PublicationFileProof(PublicationFile):
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicationGameFiles(WireModel):
    directory: str = Field(min_length=1, max_length=256)
    files: list[PublicationFile] = Field(max_length=1)
    file_manifest: list[PublicationFileProof] = Field(min_length=1, max_length=1)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublicationGame(WireModel):
    id: str = Field(min_length=1, max_length=36)
    private_json: PublicationGameFiles
    status: Literal["ready"]
    kind: Literal["game"]

    def as_transfer(self) -> TransferJob:
        return TransferJob(id=self.id, kind=self.kind, status=self.status,
                           private_json=self.private_json.model_dump(by_alias=True))


class PublishedMedia(WireModel):
    relpath: str = Field(min_length=1, max_length=1024)
    mime_type: str = Field(min_length=1, max_length=100)


publications = TypeAdapter(list[PublicationRecord])
publication = TypeAdapter(PublicationRecord)
optional_publication = TypeAdapter(PublicationRecord | None)
game = TypeAdapter(PublicationGame)
media = TypeAdapter(PublishedMedia)


class CloudBasePublications:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _request[T](self, name: str, result_type: TypeAdapter[T], **parameters) -> T:
        value = call_cloudbase_rpc(self.settings, f"sceneturn_publication_{name}", parameters)
        try:
            return result_type.validate_python(value, strict=True)
        except ValidationError:
            raise AppError(502, "DATABASE_GATEWAY_RESPONSE_INVALID", "数据库网关返回内容无效") from None

    def list(self) -> list[PublicationRecord]:
        return self._request("list", publications)

    def read(self, slug: str) -> PublicationRecord:
        return self._request("read", publication, p_slug=slug)

    def game(self, slug: str, name: str) -> PublicationGame:
        return self._request("game", game, p_slug=slug, p_name=name)

    def media(self, slug: str, role: Literal["poster", "trailer"]) -> PublishedMedia:
        return self._request("media", media, p_slug=slug, p_role=role)

    def owner(self, owner_id: str, transfer_id: str) -> PublicationRecord | None:
        return self._request("owner", optional_publication, p_owner_id=owner_id, p_transfer_id=transfer_id)

    def prepare(self, owner_id: str, transfer_id: str, title: str) -> PublicationGame:
        return self._request("prepare", game, p_owner_id=owner_id, p_transfer_id=transfer_id, p_title=title)

    def save(self, owner_id: str, transfer_id: str, *, published: bool, title: str, description: str,
             poster_asset_id: str | None, trailer_asset_id: str | None,
             verified: PublicationGame | None = None) -> PublicationRecord | None:
        return self._request("save", optional_publication, p_owner_id=owner_id, p_transfer_id=transfer_id,
            p_new_slug=new_id(), p_published=published, p_title=title, p_description=description,
            p_poster_asset_id=poster_asset_id, p_trailer_asset_id=trailer_asset_id,
            p_expected_archive_sha256=verified.private_json.archive_sha256 if verified is not None else None,
            p_expected_index_sha256=verified.private_json.file_manifest[0].sha256 if verified is not None else None)
