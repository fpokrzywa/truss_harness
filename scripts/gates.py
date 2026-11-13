"""Run the gate set (charter section 5) from a clean checkout of a git ref.

Usage (from any checkout of this repository):

    python scripts/gates.py --ref skeleton
    python scripts/gates.py --ref main --tests tests/core tests/schema
    python scripts/gates.py --ref main --no-frontend --workers 8

The ref is checked out, detached, in the worktree `.worktrees/<name>` (default `verify`) of the
main checkout. Everything git does not track is removed except the dependency caches
(`backend/.venv`, `frontend/node_modules`) and `.devdata/`; the caches are then reconciled with
the lockfiles (`uv sync --frozen`, `pnpm install --frozen-lockfile`). The worktree's database
comes from `scripts/dev_db.py env` (the local cluster must be running).

Each gate prints one line: name, exit code, seconds, and the last non-empty output line. Full
output goes to `.worktrees/_gate-logs/<name>/<gate>.log`. A failing gate also prints the tail of
its log. Exit code 0 only if every gate exits 0. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

TAIL_LINES = 40


@dataclass
class Gate:
    name: str
    argv: list[str]
    cwd: str  # relative to the checkout root: "", "backend" or "frontend"
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Result:
    gate: Gate
    code: int
    seconds: float
    last_line: str


def plan(*, tests: list[str], workers: str, frontend: bool, backend: bool = True) -> list[Gate]:
    """The ordered gate list. Pure: no I/O, so it can be unit-tested."""
    uv_env = {"UV_LINK_MODE": "copy"}
    gates = [Gate("scan-secrets", ["python", "-I", "scripts/validate/scan-secrets.py"], "")]
    if backend:
        gates += [
            Gate("uv sync --frozen", ["uv", "sync", "--frozen"], "backend", uv_env),
            Gate("ruff format --check", ["uv", "run", "ruff", "format", "--check", "."], "backend", uv_env),
            Gate("ruff check", ["uv", "run", "ruff", "check", "."], "backend", uv_env),
            Gate("mypy src", ["uv", "run", "mypy", "src"], "backend", uv_env),
            Gate("lint-imports", ["uv", "run", "lint-imports"], "backend", uv_env),
            Gate(
                "pytest " + (" ".join(tests) if tests else "(full suite)"),
                ["uv", "run", "pytest", "-n", workers, *tests],
                "backend",
                uv_env,
            ),
        ]
    gates.append(Gate("scripts unittest", ["python", "-m", "unittest", "discover", "-s", "scripts/tests"], ""))
    if frontend:
        gates += [
            Gate("pnpm install --frozen-lockfile", ["pnpm", "install", "--frozen-lockfile"], "frontend"),
            Gate("pnpm lint", ["pnpm", "lint"], "frontend"),
            Gate("pnpm typecheck", ["pnpm", "typecheck"], "frontend"),
            Gate("pnpm test", ["pnpm", "test"], "frontend"),
            Gate("pnpm build", ["pnpm", "build"], "frontend"),
        ]
    return gates


def last_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped[:160]
    return ""


def tail(text: str, n: int = TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-n:])


def safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def main_checkout(cwd: Path) -> Path:
    common = Path(_git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return common.parent


def prepare(main: Path, name: str, ref: str) -> Path:
    tree = main / ".worktrees" / name
    if not (tree / ".git").exists():
        _git(main, "worktree", "add", "--detach", str(tree), ref)
    else:
        _git(tree, "checkout", "--detach", "--force", ref)
    _git(tree, "reset", "--hard", "-q")
    excludes = ["backend/.venv/", "frontend/node_modules/", ".devdata/"]
    _git(tree, "clean", "-xdff", "-q", *[a for e in excludes for a in ("-e", e)])
    return tree


def run_gate(gate: Gate, root: Path, logs: Path) -> Result:
    exe = shutil.which(gate.argv[0]) or gate.argv[0]
    env = {**os.environ, **gate.env}
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [exe, *gate.argv[1:]],
            cwd=root / gate.cwd if gate.cwd else root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except OSError as exc:
        code, out = 127, f"could not start {gate.argv[0]}: {exc}"
    seconds = time.monotonic() - start
    (logs / f"{safe_name(gate.name)}.log").write_text(out, encoding="utf-8")
    return Result(gate, code, seconds, last_line(out))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the gate set from a clean checkout of a ref.")
    parser.add_argument("--ref", required=True, help="branch, tag or commit to check")
    parser.add_argument("--name", default="verify", help="worktree name under .worktrees/")
    parser.add_argument("--tests", nargs="*", default=[], help="pytest targets (default: all)")
    parser.add_argument("--workers", default="8", help="pytest-xdist workers (default 8)")
    parser.add_argument("--no-frontend", action="store_true")
    parser.add_argument("--no-backend", action="store_true")
    args = parser.parse_args(argv)

    main_root = main_checkout(Path.cwd())
    tree = prepare(main_root, args.name, args.ref)
    commit = _git(tree, "rev-parse", "--short", "HEAD")
    logs = main_root / ".worktrees" / "_gate-logs" / args.name
    logs.mkdir(parents=True, exist_ok=True)
    print(f"gates: ref {args.ref} at {commit}, clean checkout in {tree}")

    if not args.no_backend:
        env_proc = subprocess.run(
            [sys.executable, "scripts/dev_db.py", "env"], cwd=tree, capture_output=True, text=True
        )
        if env_proc.returncode != 0:
            print(f"dev_db.py env failed (exit {env_proc.returncode}); is the cluster running?")
            return 2

    gates = plan(
        tests=args.tests,
        workers=args.workers,
        frontend=not args.no_frontend and (tree / "frontend" / "package.json").exists(),
        backend=not args.no_backend,
    )
    failed = 0
    total = time.monotonic()
    for gate in gates:
        res = run_gate(gate, tree, logs)
        status = "PASS" if res.code == 0 else "FAIL"
        print(f"[{status}] exit {res.code:>3}  {res.seconds:7.1f}s  {gate.name}  | {res.last_line}")
        if res.code != 0:
            failed += 1
            log = (logs / f"{safe_name(gate.name)}.log").read_text(encoding="utf-8")
            print("    --- tail ---")
            print("\n".join("    " + line for line in tail(log).splitlines()))
    print(f"gates: {len(gates) - failed}/{len(gates)} passed in {time.monotonic() - total:.0f}s; logs in {logs}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
