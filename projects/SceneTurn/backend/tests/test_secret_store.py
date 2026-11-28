from cryptography.fernet import Fernet
import pytest

from app.core.config import Settings
from app.core.security import decode_secret, encode_secret, initialize_secret_store


def test_runtime_key_survives_replacing_the_instance_filesystem(monkeypatch, tmp_path):
    monkeypatch.setenv("SCENETURN_MASTER_KEY", Fernet.generate_key().decode())
    first = Settings(_env_file=None, data_dir=tmp_path / "first")
    initialize_secret_store(first)
    encrypted = encode_secret("provider credential", first)

    replacement = Settings(_env_file=None, data_dir=tmp_path / "replacement")
    initialize_secret_store(replacement)
    assert decode_secret(encrypted, replacement) == "provider credential"
    assert not (replacement.data_dir / "secrets").exists()


def test_invalid_runtime_key_rejects_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("SCENETURN_MASTER_KEY", "invalid")
    settings = Settings(_env_file=None, data_dir=tmp_path)

    with pytest.raises(ValueError):
        initialize_secret_store(settings)
    assert not (tmp_path / "secrets").exists()
