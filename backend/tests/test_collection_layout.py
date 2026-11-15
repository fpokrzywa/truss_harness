"""Every module's tests are collected by a full run (B-1914).

pytest's default ``norecursedirs`` contains ``build``, which once dropped
``tests/build`` from every full-suite run without an error. This test fails if
any directory under ``tests/`` that holds test files matches a pattern pytest
will not recurse into.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
TESTS = BACKEND / "tests"


def _norecursedirs() -> list[str]:
    config = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    options = config["tool"]["pytest"]["ini_options"]
    assert "norecursedirs" in options, (
        "set norecursedirs explicitly; pytest's default skips 'build'"
    )
    patterns = options["norecursedirs"]
    assert isinstance(patterns, list)
    return [str(p) for p in patterns]


def test_no_test_directory_is_skipped_by_norecursedirs() -> None:
    patterns = _norecursedirs()
    skipped: list[str] = []
    for test_file in TESTS.rglob("test_*.py"):
        relative = test_file.parent.relative_to(TESTS)
        for part in relative.parts:
            if any(fnmatch.fnmatch(part, pattern) for pattern in patterns):
                skipped.append(str(test_file.relative_to(BACKEND)))
                break
    assert skipped == []


def test_every_module_test_directory_holds_collected_tests() -> None:
    module_dirs = sorted(d.name for d in TESTS.iterdir() if d.is_dir() and any(d.glob("test_*.py")))
    assert "build" in module_dirs
