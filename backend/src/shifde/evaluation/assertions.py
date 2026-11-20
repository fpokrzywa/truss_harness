"""Evaluation assertions (sub-project 3 design section 6, settled call 21): pure functions over a
case run's output text.

The output text is the run's output document itself when it is a string, else its canonical
JSON (sorted keys, `(",", ":")`, non-ASCII kept). Kinds (a closed list): `equals`, `contains`,
`not_contains` (case-sensitive), `regex` (full match with Python `re`) and `json_equals` (the
text parses as JSON and equals the expected value). A case passes when every assertion passes.

`regex`: the `regex` package is not a dependency, so a pattern runs in a child interpreter
(`sys.executable -I`, the pattern and the text on its stdin, never on its command line) killed
after 1 s; a timeout fails the assertion ("regex timed out"), so a catastrophic pattern cannot
hold the API. The pattern was capped at 200 characters and compiled when the dataset was written.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
from collections.abc import Mapping, Sequence
from typing import Any  # Any: output documents and expected JSON values

from shifde.build.canonical import canonical_json

REGEX_TIMEOUT_S = 1.0
REGEX_TIMED_OUT = "regex timed out"
# the child reads {"p": pattern, "t": text} as UTF-8 JSON on stdin and writes 1 (match) or 0
_REGEX_CHILD = (
    "import json, re, sys\n"
    "d = json.loads(sys.stdin.buffer.read().decode('utf-8'))\n"
    "sys.stdout.write('1' if re.fullmatch(d['p'], d['t']) else '0')\n"
)


def output_text(output: Any) -> str:  # Any: the run's output document
    return output if isinstance(output, str) else canonical_json(output)


def regex_full_match(pattern: str, text: str) -> bool | None:
    """Whether `pattern` matches the whole `text`; None when it did not finish within 1 s."""
    try:
        # a fixed argv: this interpreter, -I, a constant script; the input goes on stdin
        done = subprocess.run(  # nosec B603
            [sys.executable, "-I", "-c", _REGEX_CHILD],
            input=json.dumps({"p": pattern, "t": text}).encode("utf-8"),
            capture_output=True,
            timeout=REGEX_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    return done.stdout.strip() == b"1"


def _check(kind: str, value: Any, text: str) -> str | None:  # Any: the expected value
    """None when the assertion passes, else why it fails."""
    if kind == "equals":
        return None if text == value else "the output does not equal the expected text"
    if kind == "contains":
        return None if value in text else "the output does not contain the expected text"
    if kind == "not_contains":
        return None if value not in text else "the output contains the refused text"
    if kind == "regex":
        matched = regex_full_match(value, text)
        if matched is None:
            return REGEX_TIMED_OUT
        return None if matched else "the output does not match the pattern"
    if kind == "json_equals":
        try:
            parsed = json.loads(text)
        except ValueError:
            return "the output is not JSON"
        return None if parsed == value else "the output JSON does not equal the expected value"
    return f"unknown assertion kind {kind}"


def evaluate(
    assertions: Sequence[Mapping[str, Any]],  # Any: stored assertions
    output: Any,  # Any: the run's output document
) -> list[dict[str, Any]]:  # Any: {id, passed, reason}
    """`{id, passed, reason}` per assertion, in order (`reason` is null when it passes)."""
    text = output_text(output)
    results = []
    for a in assertions:
        reason = _check(str(a["kind"]), a["value"], text)
        results.append({"id": a["id"], "passed": reason is None, "reason": reason})
    return results
