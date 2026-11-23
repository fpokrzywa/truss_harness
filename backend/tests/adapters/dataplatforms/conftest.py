"""Fixtures of the data-platform adapter tests (helpers in `helpers.py`)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from shifde.adapters import oauth2
from shifde.adapters.dataplatforms.fakes import FakeDatabricksWorkspace
from tests.adapters.dataplatforms.helpers import make_fake


@pytest.fixture(autouse=True)
def _clean_token_cache() -> Iterator[None]:
    oauth2.TOKEN_CACHE.clear()
    yield
    oauth2.TOKEN_CACHE.clear()


@pytest.fixture
def fake() -> FakeDatabricksWorkspace:
    return make_fake()
