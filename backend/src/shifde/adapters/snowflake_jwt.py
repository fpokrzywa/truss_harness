"""Snowflake key-pair JWT signing and claims (SP6 design 6.2, Call S6-C).

Pure: no I/O, no settings, no clock, no cache. The caller passes `iat`; the connection adapter
(`dataplatforms.snowflake`) keeps its token cache and the native adapter (`native.snowflake`)
signs a fresh token for every call.
"""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

JWT_LIFETIME_S = 300
MIN_KEY_BITS = 2048

PEM_PKCS8 = "-----BEGIN PRIVATE KEY-----"  # scan-secrets: allow, a PEM header, no key material
PEM_ENCRYPTED = "-----BEGIN ENCRYPTED PRIVATE KEY-----"  # scan-secrets: allow, a PEM header
PEM_PKCS1 = "-----BEGIN RSA PRIVATE KEY-----"  # scan-secrets: allow, a PEM header

NOT_PKCS8 = "the private key must be an unencrypted PKCS#8 PEM (BEGIN PRIVATE KEY)"
ENCRYPTED = "passphrase-protected private keys are not supported"
PKCS1 = "PKCS#1 keys (BEGIN RSA PRIVATE KEY) are not supported; convert the key to PKCS#8"
UNREADABLE = "the private key could not be read as a PEM private key"
NOT_RSA = "the private key must be an RSA key"
TOO_SHORT = f"the RSA key must have at least {MIN_KEY_BITS} bits"


def private_key(value: str) -> rsa.RSAPrivateKey | str:
    """The RSA key of an unencrypted PKCS#8 PEM, or the fixed reason it is refused."""
    text = value.strip()
    if text.startswith(PEM_ENCRYPTED):
        return ENCRYPTED
    if text.startswith(PEM_PKCS1):
        return PKCS1
    if not text.startswith(PEM_PKCS8):
        return NOT_PKCS8
    try:
        key = serialization.load_pem_private_key(text.encode(), password=None)
    except TypeError:
        return ENCRYPTED
    except (ValueError, UnsupportedAlgorithm):
        return UNREADABLE
    if not isinstance(key, rsa.RSAPrivateKey):
        return NOT_RSA
    if key.key_size < MIN_KEY_BITS:
        return TOO_SHORT
    return key


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def public_key_fingerprint(key: rsa.RSAPublicKey) -> str:
    """`SHA256:<standard base64 of sha256(DER SubjectPublicKeyInfo)>`."""
    der = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()


def jwt_subject(account: str, user: str) -> str:
    """`<ACCOUNT>.<USER>`: both uppercased, the account cut to its locator when a region follows."""
    return f"{account.split('.', 1)[0].upper()}.{user.upper()}"


def claims(key: rsa.RSAPrivateKey, account: str, user: str, iat: int) -> dict[str, str | int]:
    """`iss = <SUB>.<fingerprint>`, `sub`, `iat` and `exp = iat + 300`."""
    sub = jwt_subject(account, user)
    iss = f"{sub}.{public_key_fingerprint(key.public_key())}"
    return {"iss": iss, "sub": sub, "iat": iat, "exp": iat + JWT_LIFETIME_S}


def sign_claims(key: rsa.RSAPrivateKey, payload: dict[str, str | int]) -> str:
    """The RS256 JWT of `payload` (header `{"alg": "RS256", "typ": "JWT"}`)."""
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode()
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{body}.{b64url(signature)}"


def sign(key: rsa.RSAPrivateKey, account: str, user: str, iat: int) -> str:
    """A key-pair JWT for `user` of `account`, issued at `iat`."""
    return sign_claims(key, claims(key, account, user, iat))
