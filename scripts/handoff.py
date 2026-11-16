"""Write and verify the repo-based handoff record, docs/build/HANDOFF.json (standard library only).

A lead hands off (charter section 10) by running, as its last command:

    python scripts/handoff.py write --unit "<unit in progress, or none>" --next "<exact next step>"

`write` refuses unless the checkout is clean, on a branch, and that branch's HEAD is already pushed.
It then commits HANDOFF.json on its own, pushes, and prints the result of `verify`. The supervisor
and the goal check both rely on that printed line.

    python scripts/handoff.py verify [--session SID] [--no-fetch]

`verify` prints `HANDOFF VERIFIED ...` and exits 0 only when every one of these is true:
  - the working tree is clean (untracked files count; gitignored files do not);
  - HEAD equals its upstream (origin/<branch>), so the handoff is pushed;
  - HEAD is the commit that wrote HANDOFF.json, and it changed nothing else;
  - HANDOFF.json's head_sha is HEAD's parent: a file cannot hold its own commit's hash, so it
    records the commit it hands off, and the handoff commit sits directly on top of it;
  - HANDOFF.json's session_id is the expected session (when one is known).
Otherwise it prints `HANDOFF NOT VALID: <reason>` and exits 1.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

RECORD = "docs/build/HANDOFF.json"
FIELDS = ("session_id", "head_sha", "branch", "unit_in_progress", "next_step", "written_at")


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8"
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def current_session(repo: Path) -> str | None:
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return sid
    try:
        return (repo / ".devdata" / "lead_session").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def upstream_state(repo: Path, fetch: bool) -> tuple[str, str, str]:
    """(branch, HEAD sha, upstream sha). Raises if detached or without an upstream."""
    branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if not branch:
        raise RuntimeError("HEAD is detached")
    if fetch:
        git(repo, "fetch", "--quiet", "origin")
    head = git(repo, "rev-parse", "HEAD")
    upstream = git(repo, "rev-parse", f"origin/{branch}", check=False)
    if not upstream:
        raise RuntimeError(f"origin/{branch} does not exist")
    return branch, head, upstream


def is_clean(repo: Path) -> bool:
    return git(repo, "status", "--porcelain") == ""


def verify(repo: Path, session: str | None = None, fetch: bool = True) -> tuple[bool, str, dict]:
    try:
        branch, head, upstream = upstream_state(repo, fetch)
    except RuntimeError as exc:
        return False, str(exc), {}
    if not is_clean(repo):
        return False, "the working tree is not clean", {}
    if head != upstream:
        return False, f"HEAD {head[:9]} is not pushed (origin/{branch} is {upstream[:9]})", {}
    changed = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "HEAD").splitlines()
    if changed != [RECORD]:
        return False, f"HEAD does not change only {RECORD} (it changes {changed[:5]})", {}
    try:
        record = json.loads(git(repo, "show", f"HEAD:{RECORD}"))
    except (RuntimeError, ValueError) as exc:
        return False, f"{RECORD} is unreadable: {exc}", {}
    missing = [f for f in FIELDS if not record.get(f)]
    if missing:
        return False, f"{RECORD} lacks {missing}", record
    parent = git(repo, "rev-parse", "HEAD^", check=False)
    if record["head_sha"] != parent:
        return False, f"head_sha {record['head_sha'][:9]} is not HEAD's parent {parent[:9]}", record
    if record["branch"] != branch:
        return False, f"record names branch {record['branch']}, checkout is on {branch}", record
    if session and record["session_id"] != session:
        return False, f"record is for session {record['session_id']}, expected {session}", record
    return True, f"session={record['session_id']} branch={branch} head={head[:9]} base={parent[:9]}", record


def write(repo: Path, unit: str, next_step: str, session: str | None) -> int:
    if not session:
        print("HANDOFF NOT VALID: no session id (CLAUDE_CODE_SESSION_ID or .devdata/lead_session)")
        return 1
    try:
        branch, head, upstream = upstream_state(repo, fetch=True)
    except RuntimeError as exc:
        print(f"HANDOFF NOT VALID: {exc}")
        return 1
    if not is_clean(repo):
        print("HANDOFF NOT VALID: commit or discard every change first; `git status` must be empty")
        return 1
    if head != upstream:
        print(f"HANDOFF NOT VALID: push first; HEAD {head[:9]} differs from origin/{branch} {upstream[:9]}")
        return 1
    record = {
        "session_id": session,
        "head_sha": head,
        "branch": branch,
        "unit_in_progress": unit,
        "next_step": next_step,
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = repo / RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    git(repo, "add", RECORD)
    git(repo, "commit", "--quiet", "-m", f"Handoff record: session {session[:8]}, next: {next_step[:60]}")
    git(repo, "push", "--quiet", "origin", branch)
    ok, detail, _ = verify(repo, session, fetch=True)
    print(("HANDOFF VERIFIED " if ok else "HANDOFF NOT VALID: ") + detail)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("--unit", required=True)
    w.add_argument("--next", required=True, dest="next_step")
    v = sub.add_parser("verify")
    v.add_argument("--session")
    v.add_argument("--no-fetch", action="store_true")
    for p in (w, v):
        p.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    if args.cmd == "write":
        return write(repo, args.unit, args.next_step, current_session(repo))
    ok, detail, _ = verify(repo, args.session or current_session(repo), fetch=not args.no_fetch)
    print(("HANDOFF VERIFIED " if ok else "HANDOFF NOT VALID: ") + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
