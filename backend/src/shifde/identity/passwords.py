"""Password hashing (argon2id) with a constant-time-ish verify that never raises on bad input."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12


class PasswordService:
    """Wraps argon2-cffi. ``fast=True`` (tests only) uses minimal cost parameters."""

    def __init__(self, *, fast: bool = False) -> None:
        self._hasher = (
            PasswordHasher(time_cost=1, memory_cost=8, parallelism=1) if fast else PasswordHasher()
        )
        # Verified against when the account does not exist, so timing does not reveal existence.
        self._dummy_hash = self._hasher.hash("shifde-timing-equalisation-placeholder")

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        target = password_hash if password_hash else self._dummy_hash
        try:
            ok = self._hasher.verify(target, password)
        except (VerifyMismatchError, InvalidHashError, VerificationError):
            return False
        # An empty stored hash is no hash: it verified the public placeholder password (B-907).
        return ok and bool(password_hash)
