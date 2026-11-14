"""`shifde.identity.passwords.PasswordService` (argon2id)."""

from __future__ import annotations

import pytest

from shifde.identity.passwords import MIN_PASSWORD_LENGTH, PasswordService


@pytest.fixture(scope="module")
def passwords() -> PasswordService:
    return PasswordService(fast=True)


def test_hash_verifies_and_is_salted(passwords: PasswordService) -> None:
    first, second = passwords.hash("correct horse battery"), passwords.hash("correct horse battery")
    assert first != second and first.startswith("$argon2id$")
    assert passwords.verify(first, "correct horse battery")
    assert not passwords.verify(first, "wrong horse battery")


@pytest.mark.parametrize("stored", [None, "", "not-a-hash", "$argon2id$v=19$broken"])
def test_verify_never_raises_and_unknown_accounts_never_verify(
    passwords: PasswordService, stored: str | None
) -> None:
    assert passwords.verify(stored, "shifde-timing-equalisation-placeholder") is False


def test_minimum_length_is_seths() -> None:
    assert MIN_PASSWORD_LENGTH == 12
