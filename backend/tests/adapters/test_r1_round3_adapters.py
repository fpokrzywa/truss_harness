from __future__ import annotations

import pytest

from shifde.adapters.base import AdapterFailure
from shifde.adapters.httputil import join_url
from shifde.adapters.rest_http import RestHttpAdapter


@pytest.mark.parametrize(
    ("path", "base", "expected"),
    [
        (
            "/files/annual%20report.pdf",
            "https://gw.example/team-a",
            "/team-a/files/annual%20report.pdf",
        ),
        ("/documents:batchGet", "https://gw.example/v1", "/v1/documents:batchGet"),
        ("/v1/a%2Fb", "https://gw.example", "/v1/a%2Fb"),
        ("/v1/caf%C3%A9", "https://gw.example", "/v1/caf%C3%A9"),
        ("/v1/x?y=a b", "https://gw.example", "/v1/x"),
        (
            "/.well-known/openid-configuration",
            "https://gw.example",
            "/.well-known/openid-configuration",
        ),
        ("/foo.../bar", "https://gw.example", "/foo.../bar"),
        ("/x", "https://gw.example:8443/", "/x"),
        ("/https:/x", "https://gw.example/team-a", "/team-a/https:/x"),  # stays under the base
    ],
)
def test_ordinary_paths_join_under_the_base(path: str, base: str, expected: str) -> None:
    from urllib.parse import urlsplit

    joined = urlsplit(join_url(base, path))
    assert joined.path == expected
    assert (joined.scheme, joined.netloc) == (urlsplit(base).scheme, urlsplit(base).netloc)


@pytest.mark.parametrize(
    "path",
    [
        "/ ../x",
        "/.. /x",
        "/%20../x",
        "/%20..%20/x",
        "/. /x",
        "/x y",
        "/x/%0d%0a",
        "/x\ty",
        "/.%09./x",
    ],
)
def test_whitespace_is_refused_raw_and_dot_segments_hidden_by_it_are_caught(path: str) -> None:
    with pytest.raises(AdapterFailure):
        join_url("https://gw.example/team-a", path)


def test_a_base_url_with_a_query_or_fragment_is_refused() -> None:
    adapter = RestHttpAdapter()
    for base in ("https://gw.example/v1?api-version=2", "https://gw.example/v1#frag"):
        errs = adapter.validate_config({"base_url": base})
        assert errs and errs[0].path == "base_url" and "query" in errs[0].message
        with pytest.raises(AdapterFailure, match="query string or a fragment"):
            join_url(base, "/x")
    assert adapter.validate_config({"base_url": "https://gw.example/v1"}) == []
