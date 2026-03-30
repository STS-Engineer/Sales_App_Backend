import bcrypt

from app.security import (
    LEGACY_BCRYPT_PREFIXES,
    hash_password,
    needs_password_rehash,
    verify_password,
)


def test_hash_password_round_trip_supports_long_passwords():
    password = "p" * 120
    password_hash = hash_password(password)

    assert password_hash.startswith("pbkdf2_sha256$")
    assert verify_password(password, password_hash)
    assert not verify_password("wrong-password", password_hash)


def test_verify_password_accepts_legacy_bcrypt_hashes():
    password = "legacy-password"
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode(
        "utf-8"
    )

    assert password_hash.startswith(LEGACY_BCRYPT_PREFIXES)
    assert verify_password(password, password_hash)
    assert needs_password_rehash(password_hash)


def test_verify_password_matches_truncated_legacy_bcrypt_passwords():
    password = "a" * 72 + "legacy-suffix"
    password_hash = bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode(
        "utf-8"
    )

    assert verify_password(password, password_hash)
