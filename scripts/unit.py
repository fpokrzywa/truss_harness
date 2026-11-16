"""Parallel units: locks on shared files and the merge, and the "unit done" check (standard library only).

    python scripts/unit.py lock <resource> --unit <id>       # exit 0 ACQUIRED, exit 1 LOCKED by <unit>
    python scripts/unit.py unlock <resource> --unit <id>
    python scripts/unit.py locks                              # who holds what
    python scripts/unit.py done <id>                          # exit 0 UNIT DONE, else UNIT NOT DONE: <reason>

Locks live in the main checkout's .devdata/supervisor/locks/, which every worktree finds through git,
so one lock is seen by every lead. The supervisor takes each unit's declared resources
(docs/build/units.json) when it starts the unit's lead and frees them when the unit is done or fails.
A lead takes `merge` itself for its merge step (charter section 11), and takes any other shared
resource it finds it needs. Resources are named in docs/build/units.json.

`done <id>` passes only when origin/main's docs/build/units.json marks the unit done and this
checkout's HEAD is contained in origin/main.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8")


def main_checkout(cwd: Path) -> Path:
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(common).parent


def lock_dir(cwd: Path) -> Path:
    return main_checkout(cwd) / ".devdata" / "supervisor" / "locks"


def holder(cwd: Path, resource: str) -> dict | None:
    try:
        return json.loads((lock_dir(cwd) / f"{resource}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lock(cwd: Path, resource: str, unit: str) -> tuple[bool, str]:
    d = lock_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{resource}.json"
    record = json.dumps({"unit": unit, "since": time.strftime("%Y-%m-%d %H:%M:%S")})
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        h = holder(cwd, resource) or {}
        if h.get("unit") == unit:
            return True, f"ACQUIRED {resource} (already held by {unit})"
        return False, f"LOCKED {resource} by {h.get('unit')} since {h.get('since')}"
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(record)
    return True, f"ACQUIRED {resource} for {unit}"


def unlock(cwd: Path, resource: str, unit: str) -> tuple[bool, str]:
    h = holder(cwd, resource)
    if h is None:
        return True, f"{resource} was not locked"
    if h.get("unit") != unit:
        return False, f"{resource} is held by {h.get('unit')}, not {unit}"
    (lock_dir(cwd) / f"{resource}.json").unlink()
    return True, f"RELEASED {resource}"


def unlock_all(cwd: Path, unit: str) -> list[str]:
    freed = []
    d = lock_dir(cwd)
    for path in sorted(d.glob("*.json")) if d.exists() else []:
        if (holder(cwd, path.stem) or {}).get("unit") == unit:
            path.unlink()
            freed.append(path.stem)
    return freed


def unit_status(cwd: Path, unit: str, ref: str = "origin/main") -> str | None:
    shown = git(cwd, "show", f"{ref}:docs/build/units.json")
    if shown.returncode != 0:
        return None
    for u in json.loads(shown.stdout).get("units", []):
        if u.get("id") == unit:
            return u.get("status")
    return None


def done(cwd: Path, unit: str, fetch: bool = True) -> tuple[bool, str]:
    if fetch:
        git(cwd, "fetch", "--quiet", "origin")
    status = unit_status(cwd, unit)
    if status != "done":
        return False, f"origin/main's units.json has {unit} as {status!r}, not 'done'"
    head = git(cwd, "rev-parse", "HEAD").stdout.strip()
    if git(cwd, "merge-base", "--is-ancestor", head, "origin/main").returncode != 0:
        return False, f"HEAD {head[:9]} is not contained in origin/main"
    main = git(cwd, "rev-parse", "origin/main").stdout.strip()
    return True, f"{unit} head={head[:9]} origin/main={main[:9]}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("lock", "unlock"):
        p = sub.add_parser(name)
        p.add_argument("resource")
        p.add_argument("--unit", required=True)
    sub.add_parser("locks")
    p = sub.add_parser("done")
    p.add_argument("unit")
    args = ap.parse_args(argv)
    cwd = Path.cwd()
    if args.cmd == "locks":
        d = lock_dir(cwd)
        for path in sorted(d.glob("*.json")) if d.exists() else []:
            print(path.stem, path.read_text(encoding="utf-8"))
        return 0
    if args.cmd == "done":
        ok, detail = done(cwd, args.unit)
        print(("UNIT DONE " if ok else "UNIT NOT DONE: ") + detail)
        return 0 if ok else 1
    ok, detail = (lock if args.cmd == "lock" else unlock)(cwd, args.resource, args.unit)
    print(detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
