"""Envelope-encryption primitives (SECURITY_MODEL section 5): AES-256-GCM per-secret DEK wrapped
by a KEK from the keyring (`SHIFDE_SECRET_KEYS`: `kid:base64key[,kid:base64key...]`, first
active)."""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12


class CryptoError(Exception):
    """Decryption/unwrap failed (wrong KEK, tampered data, wrong AAD)."""


@dataclass(frozen=True)
class Envelope:
    ciphertext: bytes
    wrapped_dek: bytes  # wrap-nonce || AES-GCM(KEK, DEK)
    nonce: bytes
    kek_id: str


def aad_for(tenant_id: UUID | None, secret_id: UUID, version: int) -> bytes:
    return f"{tenant_id or 'installation'}|{secret_id}|{version}".encode()


def wrap_aad(tenant_id: UUID | None, secret_id: UUID) -> bytes:
    """AAD for the DEK wrap: version-independent so a rewrap never needs the secret version."""
    return f"wrap|{tenant_id or 'installation'}|{secret_id}".encode()


class KeyRing:
    def __init__(self, keys: dict[str, bytes], active: str) -> None:
        if active not in keys:
            raise ValueError("active key id missing from keyring")
        self._keys = keys
        self.active_id = active

    @classmethod
    def parse(cls, raw: str) -> KeyRing:
        keys: dict[str, bytes] = {}
        active: str | None = None
        for part in (p.strip() for p in raw.split(",")):
            if not part:
                continue
            kid, sep, b64 = part.partition(":")
            if not sep or not kid:
                raise ValueError("SHIFDE_SECRET_KEYS entries must look like kid:base64key")
            try:
                key = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("SHIFDE_SECRET_KEYS contains invalid base64") from exc
            if len(key) != KEY_BYTES:
                raise ValueError("SHIFDE_SECRET_KEYS keys must be 32 bytes")
            if kid in keys:
                raise ValueError("SHIFDE_SECRET_KEYS contains a duplicate key id")
            keys[kid] = key
            active = active or kid
        if active is None:
            raise ValueError("SHIFDE_SECRET_KEYS is empty")
        return cls(keys, active)

    @staticmethod
    def generate_entry(kid: str = "dev1") -> str:
        return f"{kid}:{base64.b64encode(os.urandom(KEY_BYTES)).decode()}"

    def kek(self, kid: str) -> bytes:
        try:
            return self._keys[kid]
        except KeyError as exc:
            raise CryptoError(f"unknown key id {kid!r}") from exc

    def encrypt(
        self, plaintext: bytes, *, tenant_id: UUID | None, secret_id: UUID, version: int
    ) -> Envelope:
        dek = os.urandom(KEY_BYTES)
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad_for(tenant_id, secret_id, version))
        wrapped = self._wrap(dek, wrap_aad(tenant_id, secret_id), self.active_id)
        return Envelope(ciphertext, wrapped, nonce, self.active_id)

    def decrypt(
        self, env: Envelope, *, tenant_id: UUID | None, secret_id: UUID, version: int
    ) -> bytes:
        dek = self._unwrap(env.wrapped_dek, wrap_aad(tenant_id, secret_id), env.kek_id)
        try:
            return AESGCM(dek).decrypt(
                env.nonce, env.ciphertext, aad_for(tenant_id, secret_id, version)
            )
        except InvalidTag as exc:
            raise CryptoError("secret ciphertext failed authentication") from exc

    def rewrap(
        self, wrapped_dek: bytes, *, tenant_id: UUID | None, secret_id: UUID, old_kid: str
    ) -> tuple[bytes, str]:
        """Re-wrap a DEK under the active KEK; the payload ciphertext is untouched."""
        aad = wrap_aad(tenant_id, secret_id)
        dek = self._unwrap(wrapped_dek, aad, old_kid)
        return self._wrap(dek, aad, self.active_id), self.active_id

    def _wrap(self, dek: bytes, aad: bytes, kid: str) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + AESGCM(self.kek(kid)).encrypt(nonce, dek, aad)

    def _unwrap(self, wrapped: bytes, aad: bytes, kid: str) -> bytes:
        try:
            return AESGCM(self.kek(kid)).decrypt(wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:], aad)
        except InvalidTag as exc:
            raise CryptoError("wrapped key failed authentication") from exc
