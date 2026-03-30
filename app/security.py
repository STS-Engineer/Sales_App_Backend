import base64
import binascii
import hashlib
import hmac
import secrets

import bcrypt

PBKDF2_PREFIX = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 600_000
PBKDF2_SALT_BYTES = 16
LEGACY_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    digest = _pbkdf2_digest(password, salt, PBKDF2_ITERATIONS)
    return (
        f"{PBKDF2_PREFIX}${PBKDF2_ITERATIONS}"
        f"${_b64encode(salt)}${_b64encode(digest)}"
    )


def verify_password(password: str, password_hash: str) -> bool:
    if password_hash.startswith(LEGACY_BCRYPT_PREFIXES):
        return _verify_legacy_bcrypt(password, password_hash)
    if password_hash.startswith(f"{PBKDF2_PREFIX}$"):
        return _verify_pbkdf2(password, password_hash)
    return False


def needs_password_rehash(password_hash: str) -> bool:
    return password_hash.startswith(LEGACY_BCRYPT_PREFIXES)


def _verify_legacy_bcrypt(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            _bcrypt_password_bytes(password),
            password_hash.encode("utf-8"),
        )
    except ValueError:
        return False


def _verify_pbkdf2(password: str, password_hash: str) -> bool:
    try:
        _, iterations_raw, salt_raw, digest_raw = password_hash.split("$", 3)
        iterations = int(iterations_raw)
        salt = _b64decode(salt_raw)
        expected_digest = _b64decode(digest_raw)
    except (ValueError, binascii.Error):
        return False

    actual_digest = _pbkdf2_digest(password, salt, iterations)
    return hmac.compare_digest(actual_digest, expected_digest)


def _pbkdf2_digest(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def _bcrypt_password_bytes(password: str) -> bytes:
    # Legacy bcrypt hashes only consider the first 72 bytes of the password.
    return password.encode("utf-8")[:72]


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
