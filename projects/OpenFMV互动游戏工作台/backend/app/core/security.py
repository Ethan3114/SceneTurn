import hashlib
import os
import secrets

from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from app.core.config import Settings


password_hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    return password_hasher.verify(password, stored_hash)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def initialize_secret_store(settings: Settings) -> None:
    if settings.master_key is not None:
        Fernet(read_master_key(settings))
        return
    directory = settings.data_dir.resolve() / "secrets"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = directory / "master.key"
    if not key_path.exists():
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(Fernet.generate_key())
            stream.flush()
            os.fsync(stream.fileno())
    os.chmod(key_path, 0o600)
    Fernet(read_master_key(settings))


def read_master_key(settings: Settings) -> bytes:
    if settings.master_key is not None:
        return settings.master_key.get_secret_value().encode()
    return (settings.data_dir.resolve() / "secrets" / "master.key").read_bytes()


def encode_secret(value: str, settings: Settings) -> str:
    return Fernet(read_master_key(settings)).encrypt(value.encode()).decode()


def decode_secret(value: str, settings: Settings) -> str:
    return Fernet(read_master_key(settings)).decrypt(value.encode()).decode()
