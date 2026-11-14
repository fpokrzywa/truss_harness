from __future__ import annotations

import pytest

from shifde.adapters.base import AdapterFailure
from shifde.adapters.httputil import join_url
from shifde.adapters.rest_http import RestHttpAdapter


@pytest.mark.parametrize("base", ["https://gw.example/v1?", "https://gw.example/v1#"])
def test_a_base_url_ending_in_an_empty_query_or_fragment_is_refused(base: str) -> None:
    errs = RestHttpAdapter().validate_config({"base_url": base})
    assert errs and errs[0].path == "base_url"
    with pytest.raises(AdapterFailure):
        join_url(base, "/x")


@pytest.mark.parametrize(
    "path",
    [
        "/%c0%ae%c0%ae/x",
        "/..%c0%af../x",
        "/%C0%AE/x",
        "/%c1%9c/x",
        "/%25c0%25ae/x",
        "/a/%c0/b",
    ],
)
def test_overlong_utf8_escapes_are_refused(path: str) -> None:
    with pytest.raises(AdapterFailure):
        join_url("https://gw.example/team-a", path)


@pytest.mark.parametrize("path", ["/caf%E9", "/caf%C3%A9", "/x%80y", "/%e9t%e9"])
def test_other_non_utf8_bytes_are_ordinary(path: str) -> None:
    assert join_url("https://gw.example/team-a", path).startswith("https://gw.example/team-a/")
