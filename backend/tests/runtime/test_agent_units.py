"""SETH 3ba0e06 `test_p5_core.py` tool-call parsing tests, moved with `agent.py` (runtime part 7,
assertions unchanged)."""

from __future__ import annotations

from typing import Any

import pytest

from shifde.runtime.agent import parse_tool_calls


@pytest.mark.parametrize(
    "text_in,expected",
    [
        ("plain answer", None),
        ('{"other": 1}', None),
        ("not { json", None),
        (
            '{"tool_calls": [{"name": "a", "arguments": {"x": 1}}]}',
            [{"name": "a", "arguments": {"x": 1}}],
        ),
        ('```json\n{"tool_calls": [{"name": "a"}]}\n```', [{"name": "a", "arguments": {}}]),
    ],
)
def test_tool_call_parsing(text_in: str, expected: Any) -> None:
    assert parse_tool_calls(text_in) == expected


@pytest.mark.parametrize(
    "text_in",
    [
        '{"tool_calls": []}',
        '{"tool_calls": "x"}',
        '{"tool_calls": [{"arguments": {}}]}',
        '{"tool_calls": [{"name": "a", "arguments": []}]}',
        '{"tool_calls": [' + ",".join(['{"name": "a"}'] * 9) + "]}",
    ],
)
def test_malformed_tool_calls_raise(text_in: str) -> None:
    with pytest.raises(ValueError):
        parse_tool_calls(text_in)
