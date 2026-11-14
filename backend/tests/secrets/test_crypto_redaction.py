"""Moved from SETH 3ba0e06 `tests/test_p2_secrets.py` (crypto primitives, redaction, the
`SecretValue` wrapper): assertions unchanged. The file's API, resolve and log tests need the app
`World` and move with admin (docs/build/carry_forward.md); `tests/secrets/test_service.py` covers
the service directly."""

from __future__ import annotations

import base64
import os
from uuid import uuid4

import pytest

from shifde.core.crypto import CryptoError, Envelope, KeyRing
from shifde.core.redaction import register_secret, scrub, scrub_text
from shifde.secrets import service as secrets_service

VALUE = "Sup3r-S3cret-Value-0123456789"


# ---------------------------------------------------------------- crypto primitives
def _ring(*kids: str) -> KeyRing:
    return KeyRing.parse(",".join(KeyRing.generate_entry(k) for k in kids))


def test_envelope_roundtrip_is_bound_to_tenant_secret_and_version() -> None:
    ring = _ring("k1")
    t, s = uuid4(), uuid4()
    env = ring.encrypt(b"plaintext", tenant_id=t, secret_id=s, version=1)
    assert VALUE.encode() not in env.ciphertext and env.kek_id == "k1" and len(env.nonce) == 12
    assert ring.decrypt(env, tenant_id=t, secret_id=s, version=1) == b"plaintext"
    for tenant, secret, version in ((uuid4(), s, 1), (t, uuid4(), 1), (t, s, 2), (None, s, 1)):
        with pytest.raises(CryptoError):
            ring.decrypt(env, tenant_id=tenant, secret_id=secret, version=version)
    tampered = Envelope(
        env.ciphertext[:-1] + bytes([env.ciphertext[-1] ^ 1]), env.wrapped_dek, env.nonce, "k1"
    )
    with pytest.raises(CryptoError):
        ring.decrypt(tampered, tenant_id=t, secret_id=s, version=1)
    again = ring.encrypt(b"plaintext", tenant_id=t, secret_id=s, version=1)
    assert (
        again.ciphertext != env.ciphertext and again.wrapped_dek != env.wrapped_dek
    )  # fresh DEK+nonce


def test_rewrap_moves_the_dek_to_the_active_kek_without_touching_the_payload() -> None:
    old = _ring("k1")
    t, s = uuid4(), uuid4()
    env = old.encrypt(b"data", tenant_id=t, secret_id=s, version=3)
    both = KeyRing({**{"k2": os.urandom(32)}, "k1": old.kek("k1")}, active="k2")
    wrapped, kid = both.rewrap(env.wrapped_dek, tenant_id=t, secret_id=s, old_kid="k1")
    moved = Envelope(env.ciphertext, wrapped, env.nonce, kid)
    assert kid == "k2" and wrapped != env.wrapped_dek
    assert both.decrypt(moved, tenant_id=t, secret_id=s, version=3) == b"data"
    only_new = KeyRing({"k2": both.kek("k2")}, active="k2")
    assert only_new.decrypt(moved, tenant_id=t, secret_id=s, version=3) == b"data"
    with pytest.raises(CryptoError):  # the retired KEK can no longer open it
        old.decrypt(moved, tenant_id=t, secret_id=s, version=3)
    with pytest.raises(CryptoError):
        only_new.decrypt(env, tenant_id=t, secret_id=s, version=3)  # still wrapped by k1


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "nocolon",
        "k1:not-base64!!",
        "k1:" + base64.b64encode(b"short").decode(),
        "k1:"
        + base64.b64encode(os.urandom(32)).decode()
        + ",k1:"
        + base64.b64encode(os.urandom(32)).decode(),
    ],
    # fixed ids: the random key bytes would otherwise name the case differently on each xdist
    # worker (collection must match across workers)
    ids=["empty", "no-colon", "not-base64", "short-key", "duplicate-kid"],
)
def test_keyring_rejects_malformed_entries(raw: str) -> None:
    with pytest.raises(ValueError):
        KeyRing.parse(raw)


def test_first_key_in_the_ring_is_active() -> None:
    ring = _ring("a", "b")
    assert ring.active_id == "a"


# ---------------------------------------------------------------- redaction
def test_registered_values_patterns_and_field_names_are_scrubbed() -> None:
    register_secret("registered-plaintext-1")
    out = scrub_text(
        "x registered-plaintext-1 y Bearer abcdefghijkl z shf_test_abcd1234_" + "A" * 43
    )
    assert (
        "registered-plaintext-1" not in out and "abcdefghijkl" not in out and "shf_test_" not in out
    )
    pem = "-----BEGIN PRIVATE KEY-----\nMIIabc\n-----END PRIVATE KEY-----"
    assert "MIIabc" not in scrub_text(pem)
    assert "pa55word" not in scrub_text("postgresql://user:pa55word@host/db")
    assert "AKIAABCDEFGHIJKLMNOP" not in scrub_text("key AKIAABCDEFGHIJKLMNOP")
    jwt_like = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl"
    assert jwt_like not in scrub_text(jwt_like)
    d = scrub(
        {"Authorization": "x", "client_secret": "y", "key_id": "kept", "items": [{"password": "p"}]}
    )
    assert d == {
        "Authorization": "[REDACTED]",
        "client_secret": "[REDACTED]",
        "key_id": "kept",
        "items": [{"password": "[REDACTED]"}],
    }


def test_secret_value_wrapper_never_renders_the_plaintext() -> None:
    v = secrets_service.SecretValue(b"wrapped-secret-xyz")
    assert "wrapped-secret-xyz" not in repr(v) and "wrapped-secret-xyz" not in str(v)
    assert "wrapped-secret-xyz" not in f"{v!r} {v}" and v.reveal() == "wrapped-secret-xyz"
    assert "wrapped-secret-xyz" not in scrub_text("value=wrapped-secret-xyz")  # auto-registered
