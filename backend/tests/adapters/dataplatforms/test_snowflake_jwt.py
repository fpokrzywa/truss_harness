"""The pure Snowflake key-pair JWT helper (`shifde.adapters.snowflake_jwt`, SP6 design 6.2): RS256
signing with an explicit `iat`, the claims, and the fixed refusal reasons of a bad key."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any  # Any: JWT JSON, cryptography objects

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from shifde.adapters import snowflake_jwt
from shifde.adapters.dataplatforms.snowflake import SnowflakeAdapter
from tests.adapters.dataplatforms.helpers import pkcs8, rsa_key

IAT = 1_900_000_000


@pytest.fixture(autouse=True)
def clean_data() -> None:
    """These tests use no database: override the root conftest's per-test wipe."""


def _decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _pem(key: Any, fmt: Any, encryption: Any = None) -> str:
    return key.private_bytes(  # type: ignore[no-any-return]
        serialization.Encoding.PEM, fmt, encryption or serialization.NoEncryption()
    ).decode()


def test_sign_is_rs256_with_the_four_claims() -> None:
    key = rsa_key()
    token = snowflake_jwt.sign(key, "Acct1.us-east-2.aws", "Reader", IAT)
    head, body, signature = token.split(".")
    key.public_key().verify(
        _decode(signature), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    assert json.loads(_decode(head)) == {"alg": "RS256", "typ": "JWT"}
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()
    assert json.loads(_decode(body)) == {
        "iss": f"ACCT1.READER.{fingerprint}",
        "sub": "ACCT1.READER",
        "iat": IAT,
        "exp": IAT + 300,
    }
    assert snowflake_jwt.public_key_fingerprint(key.public_key()) == fingerprint


def test_signing_is_deterministic_in_iat() -> None:
    key = rsa_key()
    assert snowflake_jwt.sign(key, "a", "b", IAT) == snowflake_jwt.sign(key, "a", "b", IAT)
    assert snowflake_jwt.sign(key, "a", "b", IAT) != snowflake_jwt.sign(key, "a", "b", IAT + 1)


def test_a_good_key_loads() -> None:
    assert isinstance(snowflake_jwt.private_key(pkcs8(rsa_key())), rsa.RSAPrivateKey)


REASONS = {
    "pkcs1": (
        lambda: _pem(rsa_key(), serialization.PrivateFormat.TraditionalOpenSSL),
        "PKCS#1 keys (BEGIN RSA PRIVATE KEY) are not supported; convert the key to PKCS#8",
    ),
    "encrypted": (
        lambda: _pem(
            rsa_key(),
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"passphrase"),
        ),
        "passphrase-protected private keys are not supported",
    ),
    "ec": (
        lambda: pkcs8(ec.generate_private_key(ec.SECP256R1())),
        "the private key must be an RSA key",
    ),
    "short": (lambda: pkcs8(rsa_key("short", 1024)), "the RSA key must have at least 2048 bits"),
    "not_pem": (
        lambda: "just some text",
        "the private key must be an unencrypted PKCS#8 PEM (BEGIN PRIVATE KEY)",
    ),
    "garbage": (
        lambda: "-----BEGIN PRIVATE KEY-----\nbm90IGEga2V5\n-----END PRIVATE KEY-----\n",
        "the private key could not be read as a PEM private key",
    ),
}


@pytest.mark.parametrize("name", sorted(REASONS))
def test_bad_keys_get_the_same_fixed_reasons_as_before(name: str) -> None:
    make, reason = REASONS[name]
    value = make()
    assert snowflake_jwt.private_key(value) == reason
    (error,) = SnowflakeAdapter().validate_credential("key_pair", value)
    assert error.message == reason
