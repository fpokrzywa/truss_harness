"""Print the lead session's current context size in tokens (standard library only).

The lead runs this after every accepted unit (charter section 10). The figure is the input
the model was sent on its most recent call: new tokens plus cached tokens.

The session is found from CLAUDE_CODE_SESSION_ID, or else from .devdata/lead_session, which
the supervising process writes. Run it from the lead session, not from an agent.

Usage: python scripts/context_size.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HANDOFF_AT = 300_000
TAIL_BYTES = 600_000


def session_id() -> str | None:
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return sid
    marker = REPO / ".devdata" / "lead_session"
    try:
        return marker.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def context_tokens(path: Path) -> int:
    size = path.stat().st_size
    with path.open("rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if isinstance(usage, dict):
            return sum(
                usage.get(key) or 0
                for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
    return 0


def main() -> int:
    sid = session_id()
    projects = Path.home() / ".claude" / "projects"
    paths = sorted(projects.glob(f"*/{sid}.jsonl")) if sid else []
    if not paths:
        print("context size unknown: session record not found; hand off after three accepted units")
        return 1
    tokens = context_tokens(paths[0])
    verdict = "HAND OFF NOW (charter section 10)" if tokens >= HANDOFF_AT else "continue"
    print(f"lead context: {tokens} tokens; handoff at {HANDOFF_AT}: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
