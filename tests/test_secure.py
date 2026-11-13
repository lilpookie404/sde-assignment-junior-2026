import hashlib
import hmac
import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from solution import secure


def test_load_or_create_key_reuses_external_key(tmp_path):
    key_path = tmp_path / "pii.key"

    first = secure.load_or_create_key(key_path)
    second = secure.load_or_create_key(key_path)

    assert first == second
    assert key_path.read_bytes() == first


def test_load_or_create_key_rejects_repository_path():
    key_path = Path(secure.__file__).with_name("pii.key")

    with pytest.raises(ValueError, match="outside the repository"):
        secure.load_or_create_key(key_path)

    assert not key_path.exists()


def test_load_or_create_key_hardens_existing_key_permissions(tmp_path):
    key_path = tmp_path / "pii.key"
    key_path.write_bytes(Fernet.generate_key())
    key_path.chmod(0o644)

    secure.load_or_create_key(key_path)

    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_pii_encryption_round_trip_recovers_phone_suffix(tmp_path):
    key = secure.load_or_create_key(tmp_path / "pii.key")
    phone = "+919876543210"

    encrypted = secure.encrypt_pii(phone, key)
    decrypted = secure.decrypt_pii(encrypted, key)

    assert encrypted != phone
    assert decrypted == phone
    assert decrypted[-4:] == "3210"


def test_phone_lookup_digest_is_stable(tmp_path):
    key_path = tmp_path / "pii.key"
    key = secure.load_or_create_key(key_path)
    phone = "+919876543210"

    first = secure.phone_lookup_digest(phone, key)
    second = secure.phone_lookup_digest(
        phone, secure.load_or_create_key(key_path)
    )
    direct = hmac.new(key, phone.encode("utf-8"), hashlib.sha256).hexdigest()

    assert first == second
    assert first != direct
    assert first != phone
    assert len(first) == 64
