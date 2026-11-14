from __future__ import annotations

import pytest

from shifde.adapters.base import AdapterFailure, ConnectionConfig, Credential
from shifde.adapters.httputil import auth_headers, join_url
from shifde.adapters.rest_http import RestHttpAdapter


@pytest.mark.parametrize(
    "path",
    [
        "/../team-b/capture",
        "/a/../../team-b",
        "/%2e%2e/team-b",
        "/%2E%2E/team-b",
        "/a/%2e%2e/%2e%2e/b",
        "/%252e%252e/b",
        "/./x",
        "/a/..",
    ],
)
def test_join_url_refuses_dot_segments_in_any_encoding(path: str) -> None:
    with pytest.raises(AdapterFailure):
        join_url("https://gw.example/team-a", path)


def test_join_url_still_joins_ordinary_paths() -> None:
    assert (
        join_url("https://gw.example/team-a", "/v1/x?q=..") == "https://gw.example/team-a/v1/x?q=.."
    )
    assert join_url("https://gw.example/team-a/", "/a.b/..c") == "https://gw.example/team-a/a.b/..c"


def test_rest_config_refuses_dot_segment_paths_and_forbidden_header_names() -> None:
    adapter = RestHttpAdapter()
    base = {"base_url": "https://gw.example/team-a"}
    for key in ("health_path", "openapi_path"):
        errs = adapter.validate_config({**base, key: "/../team-b/capture"})
        assert errs and errs[0].path == key
    assert adapter.validate_config({**base, "health_path": "/health"}) == []
    # `api_key_header` cannot be set in config at all (its name trips the credential scan), and
    # the header builder refuses the transport-level names anyway
    assert adapter.validate_config({**base, "api_key_header": "X-Key"})
    for name in ("Authorization", "cookie", "Host", "Proxy-Authorization"):
        conn = ConnectionConfig(
            id=None,
            name="n",
            adapter_key="rest.http",
            config={"api_key_header": name},
            auth_method="api_key",
            credential=Credential("v"),
        )
        with pytest.raises(AdapterFailure):
            auth_headers(conn)
