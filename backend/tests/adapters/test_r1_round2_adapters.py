from __future__ import annotations

import pytest

from shifde.adapters.httputil import join_url
from shifde.adapters.rest_http import RestHttpAdapter

BASE = "https://gw.example/team-a"


BS, TAB, NL = chr(92), chr(9), chr(10)
MUST_REFUSE = [
    "/." + TAB + "./team-b/capture",
    "/." + NL + "./team-b",
    TAB + "/../team-b",
    "/" + TAB + "../team-b",
    "/%20../x",
    "/x/%0d%0a",
    "/x\x00y",
    "/..;/team-b",
    "/.;x=1./team-b",
    "/%2525252e%2525252e/x",
    "/%252525252e%252525252e/x",
    "/ ../x",
    "/.. /x",
    "/" + BS + ".." + BS + "x",
    "/.%09./x",
    "/a/%2e%2e%5c..",
    "/../team-b/capture",
    "/%2E%2E/team-b",
    "/./x",
    "/a/..",
]


@pytest.mark.parametrize("path", MUST_REFUSE)
def test_paths_that_could_leave_the_base_path_are_refused(path: str) -> None:
    try:
        joined = join_url(BASE, path)
    except Exception as exc:  # noqa: BLE001 - any AdapterFailure is a refusal
        assert type(exc).__name__ == "AdapterFailure"
        return
    raise AssertionError(f"{path!r} was joined to {joined!r}")


@pytest.mark.parametrize("path", ["/../team-b/capture", "/%20../x", "/..;/team-b"])
def test_config_validation_refuses_the_same_paths(path: str) -> None:
    adapter = RestHttpAdapter()
    for key in ("health_path", "openapi_path"):
        errs = adapter.validate_config({"base_url": BASE, key: path})
        assert errs and errs[0].path == key, (key, path)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/", "https://gw.example/team-a/"),
        ("/health", "https://gw.example/team-a/health"),
        ("/v1/items/1?x=1&y=a%20b", "https://gw.example/team-a/v1/items/1?x=1&y=a%20b"),
        ("/a.b/c..d", "https://gw.example/team-a/a.b/c..d"),
        ("/v1/x?next=/../y", "https://gw.example/team-a/v1/x?next=/../y"),
        ("/team-a/x", "https://gw.example/team-a/team-a/x"),
    ],
)
def test_ordinary_paths_still_join(path: str, expected: str) -> None:
    assert join_url(BASE, path) == expected
    assert join_url("https://gw.example", path).startswith("https://gw.example/")
