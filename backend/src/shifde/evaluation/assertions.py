"""Evaluation assertions (sub-project 3 design section 6, settled call 21): pure functions over a
case run's output text.

The output text is the run's output document itself when it is a string, else its canonical
JSON (sorted keys, `(",", ":")`, non-ASCII kept). Kinds (a closed list): `equals`, `contains`,
`not_contains` (case-sensitive), `regex` (full match with Python `re`) and `json_equals` (the
text parses as JSON and equals the expected value). A case passes when every assertion passes.

`regex`: the `regex` package is not a dependency, so the patterns run in a child interpreter
(`sys.executable -I`, the patterns and texts on its stdin, never on its command line). Every
regex assertion of one scoring pass goes to one child, which answers them one by one: a pattern
that does not answer within 1 s fails ("regex timed out"), the child is killed and a fresh one
takes the patterns after it, and the whole pass stops at a 10 s budget, every pattern not
answered by then failing the same way (security review 034-1, finding 1). So a dataset of
catastrophic patterns (200 cases x 50 assertions) cannot hold the API for longer than the
budget. The patterns were capped at 200 characters and compiled when the dataset was written.
"""

from __future__ import annotations

import json
import queue
import subprocess  # nosec B404
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from typing import IO, Any  # Any: output documents and expected JSON values

from shifde.build.canonical import canonical_json

REGEX_TIMEOUT_S = 1.0  # one pattern
REGEX_BUDGET_S = 10.0  # every regex assertion of one scoring pass
REGEX_TIMED_OUT = "regex timed out"
# the child reads {"texts": [...], "items": [[pattern, text index], ...]} as UTF-8 JSON on stdin
# and writes one line per item, in order: 1 (match) or 0
_REGEX_CHILD = (
    "import json, re, sys\n"
    "d = json.loads(sys.stdin.buffer.read().decode('utf-8'))\n"
    "for p, i in d['items']:\n"
    "    sys.stdout.write('1\\n' if re.fullmatch(p, d['texts'][i]) else '0\\n')\n"
    "    sys.stdout.flush()\n"
)


def output_text(output: Any) -> str:  # Any: the run's output document
    return output if isinstance(output, str) else canonical_json(output)


def _feed(stream: IO[bytes], data: bytes) -> None:
    try:
        stream.write(data)
        stream.close()
    except OSError:  # the child was killed before it read everything
        pass


def _read(stream: IO[bytes], lines: queue.Queue[bytes]) -> None:
    for line in stream:
        lines.put(line)


def regex_full_matches(
    items: Sequence[tuple[str, str]],
    *,
    per_pattern_s: float = REGEX_TIMEOUT_S,
    budget_s: float = REGEX_BUDGET_S,
) -> list[bool | None]:
    """Whether each `(pattern, text)` matches in full; None for a pattern that did not answer
    within `per_pattern_s`, or before the whole `budget_s` ran out."""
    results: list[bool | None] = [None] * len(items)
    deadline = time.monotonic() + budget_s
    texts = list(dict.fromkeys(t for _, t in items))
    index = {t: i for i, t in enumerate(texts)}
    start = 0
    while start < len(items) and time.monotonic() < deadline:
        pending = items[start:]
        payload = json.dumps({"texts": texts, "items": [[p, index[t]] for p, t in pending]}).encode(
            "utf-8"
        )
        # a fixed argv: this interpreter, -I, a constant script; the input goes on stdin
        child = subprocess.Popen(  # nosec B603
            [sys.executable, "-I", "-c", _REGEX_CHILD],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert child.stdin is not None and child.stdout is not None
        lines: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=_feed, args=(child.stdin, payload), daemon=True).start()
        threading.Thread(target=_read, args=(child.stdout, lines), daemon=True).start()
        stuck = False
        try:
            for offset in range(len(pending)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return results
                # the first answer also waits for the child to start
                wait = min(remaining, per_pattern_s + (1.0 if offset == 0 else 0.0))
                try:
                    line = lines.get(timeout=wait)
                except queue.Empty:
                    stuck = True  # this pattern stays None; the next child starts after it
                    start += offset + 1
                    break
                if not line.strip():  # the child ended without answering
                    stuck = True
                    start += offset + 1
                    break
                results[start + offset] = line.strip() == b"1"
            if not stuck:
                return results
        finally:
            child.kill()
            child.wait()
    return results


def _check(kind: str, value: Any, text: str) -> str | None:  # Any: the expected value
    """None when a non-regex assertion passes, else why it fails."""
    if kind == "equals":
        return None if text == value else "the output does not equal the expected text"
    if kind == "contains":
        return None if value in text else "the output does not contain the expected text"
    if kind == "not_contains":
        return None if value not in text else "the output contains the refused text"
    if kind == "json_equals":
        try:
            parsed = json.loads(text)
        except ValueError:
            return "the output is not JSON"
        return None if parsed == value else "the output JSON does not equal the expected value"
    return f"unknown assertion kind {kind}"


def evaluate_cases(
    cases: Sequence[tuple[Sequence[Mapping[str, Any]], Any]],  # Any: assertions, output
) -> list[list[dict[str, Any]]]:  # Any: {id, passed, reason}
    """`{id, passed, reason}` per assertion of each `(assertions, output)`, in order (`reason`
    is null when it passes). Every regex assertion runs in one bounded child (see above)."""
    texts = [output_text(output) for _, output in cases]
    regexes = [
        (str(a["value"]), texts[n])
        for n, (assertions, _) in enumerate(cases)
        for a in assertions
        if a["kind"] == "regex"
    ]
    matches = iter(regex_full_matches(regexes))
    out = []
    for n, (assertions, _) in enumerate(cases):
        results = []
        for a in assertions:
            if a["kind"] == "regex":
                matched = next(matches)
                reason = (
                    REGEX_TIMED_OUT
                    if matched is None
                    else None
                    if matched
                    else "the output does not match the pattern"
                )
            else:
                reason = _check(str(a["kind"]), a["value"], texts[n])
            results.append({"id": a["id"], "passed": reason is None, "reason": reason})
        out.append(results)
    return out


def evaluate(
    assertions: Sequence[Mapping[str, Any]],  # Any: stored assertions
    output: Any,  # Any: the run's output document
) -> list[dict[str, Any]]:  # Any: {id, passed, reason}
    """`evaluate_cases` for one case."""
    return evaluate_cases([(assertions, output)])[0]
