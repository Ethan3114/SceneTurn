from typing import Annotated, Literal

from pydantic import Field, SecretStr, TypeAdapter, ValidationError

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import verify_password
from app.schemas import UserView, WireModel
from app.services.cloudbase_database import call_cloudbase_rpc


class LockedLogin(WireModel):
    status: Literal["locked"]
    retry_after: float = Field(gt=0, allow_inf_nan=False)


class LoginAccount(WireModel):
    id: str
    password_hash: SecretStr


class LoginLookup(WireModel):
    status: Literal["lookup"]
    account: LoginAccount | None


class AcceptedLogin(WireModel):
    status: Literal["accepted"]
    user: UserView


class InvalidLogin(WireModel):
    status: Literal["invalid"]


LookupResult = Annotated[LockedLogin | LoginLookup, Field(discriminator="status")]
LoginResult = LockedLogin | AcceptedLogin | InvalidLogin

optional_user = TypeAdapter(UserView | None)
login_lookup = TypeAdapter(LookupResult)
login_result = TypeAdapter(Annotated[LoginResult, Field(discriminator="status")])
logout_result = TypeAdapter(bool)


class CloudBaseAuthRepository:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _request[T](self, name: str, payload: dict[str, object], result_type: TypeAdapter[T]) -> T:
        result = call_cloudbase_rpc(self.settings, f"sceneturn_auth_{name}", payload)
        try:
            return result_type.validate_python(result, strict=True)
        except ValidationError:
            raise AppError(503, "AUTH_STORAGE_INVALID_RESPONSE", "登录服务暂时不可用，请稍后重试") from None

    def register(self, user_id: str, email: str, password_hash: str, token_hash: str) -> UserView | None:
        return self._request("register", {
            "p_user_id": user_id,
            "p_email": email,
            "p_password_hash": password_hash,
            "p_token_hash": token_hash,
            "p_session_seconds": self.settings.session_seconds,
        }, optional_user)

    def login(self, email: str, password: str, source_hash: str, token_hash: str) -> LoginResult:
        result = self._request("login_lookup", {"p_source_hash": source_hash, "p_email": email}, login_lookup)
        if isinstance(result, LockedLogin):
            return result

        account = result.account
        verified_user_id = None
        verified_hash = None
        if account is not None and verify_password(password, account.password_hash.get_secret_value()):
            verified_user_id = account.id
            verified_hash = account.password_hash.get_secret_value()
        return self._request("login_finish", {
            "p_source_hash": source_hash,
            "p_email": email,
            "p_verified_user_id": verified_user_id,
            "p_verified_password_hash": verified_hash,
            "p_token_hash": token_hash if verified_hash is not None else None,
            "p_session_seconds": self.settings.session_seconds,
        }, login_result)

    def session_user(self, token_hash: str) -> UserView | None:
        return self._request("session_user", {"p_token_hash": token_hash}, optional_user)

    def logout(self, token_hash: str) -> bool:
        return self._request("logout", {"p_token_hash": token_hash}, logout_result)
