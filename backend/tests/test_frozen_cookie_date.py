"""The `frozen_cookie_date` fixture (B-2320) keeps two clearing-cookie headers byte-identical
even when the second response is built after a second boundary."""

from __future__ import annotations

import time

import pytest
from starlette.responses import Response


def _clearing_header() -> str:
    response = Response()
    response.delete_cookie("sid", path="/", secure=True)
    return response.headers["set-cookie"]


def test_without_the_fixture_a_second_boundary_changes_expires() -> None:
    first = _clearing_header()
    time.sleep(1.1)
    assert _clearing_header() != first


@pytest.mark.usefixtures("frozen_cookie_date")
def test_with_the_fixture_headers_stay_exactly_equal_across_a_second_boundary() -> None:
    first = _clearing_header()
    time.sleep(1.1)
    assert _clearing_header() == first


@pytest.mark.usefixtures("frozen_cookie_date")
def test_with_the_fixture_twenty_pairs_across_several_second_boundaries_never_differ() -> None:
    for _ in range(20):
        first = _clearing_header()
        time.sleep(0.12)
        assert _clearing_header() == first
