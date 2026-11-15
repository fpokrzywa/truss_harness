"""Run request body validation (`runtime.schemas`).

Moved from SETH 3ba0e06 with assertions unchanged: `test_p5_core.py::test_run_body_rejects_unstorable_json`.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from shifde.runtime.schemas import RunCreate


@pytest.mark.parametrize(
    "over",
    [
        {"input": "a\x00b"},
        {"input": "\ud800"},
        {"variables": {"x": float("nan")}},
        {"variables": {"x": [float("inf")]}},
        {"variables": {"a\x00": 1}},
        {"surplus": 1},
        {"agent_id": None},
    ],
)
def test_run_body_rejects_unstorable_json(over: dict[str, Any]) -> None:
    base: dict[str, Any] = {
        "kind": "agent",
        "agent_id": "01900000-0000-7000-8000-0000000000aa",
        "workspace": "w",
        "project": "p",
        "input": "x",
    }
    with pytest.raises(ValidationError):
        RunCreate.model_validate({**base, **over})
    assert RunCreate.model_validate(base).input == "x"
