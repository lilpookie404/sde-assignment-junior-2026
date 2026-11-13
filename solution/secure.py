
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_PHONE_LOOKUP_PURPOSE = b"phone-lookup-v1"


def load_or_create_key(path):
    key_path = Path(path).expanduser()
    if not key_path.is_absolute():
        raise ValueError("key path must be absolute and outside the repository")

    key_path = key_path.resolve()
    if key_path.is_relative_to(_REPOSITORY_ROOT):
        raise ValueError("key path must be outside the repository")

    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "wb") as key_file:
            key_file.write(Fernet.generate_key())

    key_path.chmod(0o600)
    key = key_path.read_bytes().strip()
    Fernet(key)
    return key


def encrypt_pii(value, key):
    return Fernet(key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_pii(value, key):
    return Fernet(key).decrypt(value.encode("ascii")).decode("utf-8")


def phone_lookup_digest(canonical_phone, key):
    lookup_key = hmac.new(key, _PHONE_LOOKUP_PURPOSE, hashlib.sha256).digest()
    return hmac.new(
        lookup_key, canonical_phone.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def bind_database_key(connection, key):
    fingerprint = hashlib.sha256(key).hexdigest()
    with connection:
        row = connection.execute(
            "SELECT value FROM pipeline_metadata WHERE name = 'key_fingerprint'"
        ).fetchone()
        if row:
            if not hmac.compare_digest(row[0], fingerprint):
                raise ValueError("encryption key does not match this database")
            return

        encrypted = connection.execute(
            "SELECT phone_encrypted FROM leads LIMIT 1"
        ).fetchone()
        if encrypted:
            try:
                decrypt_pii(encrypted[0], key)
            except InvalidToken as error:
                raise ValueError(
                    "encryption key does not match this database"
                ) from error
        connection.execute(
            "INSERT INTO pipeline_metadata VALUES ('key_fingerprint', ?)",
            (fingerprint,),
        )
