"""Reference process supervisor for SETH (SP4 design 6.2). Standard library only, not product code.

    python scripts/procctl.py up --env-file F [--with-executor] [--with-web] [--state-dir D]
    python scripts/procctl.py down [--state-dir D]
    python scripts/procctl.py status [--state-dir D]

`up` runs in the foreground. It starts `shifde-api`, `shifde-worker`, `shifde-executor` (with
`--with-executor`) and the Next server (with `--with-web`: `pnpm build`, then `pnpm exec next
start -p <SHIFDE_WEB_PORT> -H 127.0.0.1`, default port 3710) in that order, each once the previous
one is ready. The children get `os.environ` overlaid with the env file's `KEY=VALUE` lines; their
output is appended to `<state-dir>/<name>.log`. `SHIFDE_WORKER_HEALTH_PORT` (and
`SHIFDE_EXECUTOR_HEALTH_PORT` with `--with-executor`) must be set: readiness is probed there.

Exit-code contract of the processes: 0 stopped on request, 1 crashed, 2 configuration refused,
3 unsafe database role, 4 schema revision mismatch, 5 cannot authenticate.

Restart policy: an exit procctl did not request with code 1, or any code outside 0 and 2-5 (a
signal, a hard kill), is a crash and the child is restarted after `min(2**n, 60)` seconds for its
n-th consecutive crash (n from 0); n resets when the child had been up at least 60 s. On 2-5 the
child is not restarted: procctl stops the others and `up` exits with that code. An unrequested
exit 0 is not restarted either, and `up` exits 1. A child not ready within 120 s stops `up`
with exit 1.

`down` writes `<state-dir>/stop`; `up` then signals every child gracefully (Windows
`CTRL_BREAK_EVENT`, elsewhere SIGTERM), waits for its grace plus 5 s and hard-kills what is left.
`up` exits 0 on a requested stop (the stop file, SIGINT, SIGTERM or SIGBREAK). `status` prints
liveness and readiness per child. procctl never prints, logs or stores a value from the env file
or the environment, other than the health URLs it probes.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
WINDOWS = sys.platform == "win32"
VENV_BIN = ROOT / "backend" / ".venv" / ("Scripts" if WINDOWS else "bin")
DEFAULT_STATE_DIR = ROOT / ".devdata" / "procctl"

EXIT_REASONS = {
    2: "configuration refused",
    3: "unsafe database role",
    4: "schema revision mismatch",
    5: "cannot authenticate",
}
BACKOFF_CAP = 60
RESET_AFTER = 60.0  # seconds up after which the crash streak starts again at 0
READY_TIMEOUT = 120.0
POLL = 0.5
PROBE_TIMEOUT = 2.0
KILL_MARGIN = 5.0  # `up`: grace + this before a hard kill
DOWN_MARGIN = 15.0  # `down`: largest grace + this before it hard-kills
WEB_GRACE = 10
DEFAULT_WEB_PORT = 3710
GRACES = {
    "api": ("SHIFDE_API_SHUTDOWN_GRACE_SECONDS", 30),
    "worker": ("SHIFDE_WORKER_SHUTDOWN_GRACE_SECONDS", 30),
    "executor": ("SHIFDE_EXECUTOR_SHUTDOWN_GRACE_SECONDS", 60),
}


class Refused(Exception):
    """`up` cannot start: exit 2 with this one line."""


def backoff(n: int) -> int:
    """Restart delay in seconds for the n-th consecutive crash (n from 0)."""
    return min(1 << n, BACKOFF_CAP)


def crash_delay(streak: int, uptime: float) -> tuple[int, int]:
    """(delay, next streak) for a crash after `uptime` seconds up with `streak` crashes before."""
    if uptime >= RESET_AFTER:
        streak = 0
    return backoff(streak), streak + 1


def exit_policy(code: int) -> str:
    """`restart`, `refused` (2-5: not restarted, `up` exits with the code) or `unrequested_zero`
    for an exit procctl did not request."""
    if code == 0:
        return "unrequested_zero"
    if code in EXIT_REASONS:
        return "refused"
    return "restart"


def parse_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` lines (UTF-8, optional BOM); blank lines and `#` lines skipped; one pair of
    matching quotes around the value removed. A bad line is refused by number, never content."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise Refused(f"procctl: env file not found: {path}") from None
    except (OSError, UnicodeDecodeError):
        raise Refused(f"procctl: env file cannot be read as UTF-8: {path}") from None
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            raise Refused(f"procctl: env file line {number} is not KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise Refused(f"procctl: {name} is not an integer") from None


def _probe_host(host: str) -> str:
    host = host.strip()
    return "127.0.0.1" if host in ("", "0.0.0.0") else host  # noqa: S104 - a bind-all host


@dataclass
class Child:
    name: str
    argv: list[str]
    cwd: Path
    live_url: str | None  # None: liveness is "process running" (web)
    ready_url: str
    grace: int
    ready_below: int = 300  # a readiness answer is ready below this status
    build_argv: list[str] | None = None  # web: run to completion before `argv`
    proc: subprocess.Popen[bytes] | None = None
    building: bool = False
    started: bool = False
    started_at: str | None = None
    started_mono: float = 0.0
    restarts: int = 0
    last_exit: int | None = None
    streak: int = 0
    restart_at: float | None = None
    ready_deadline: float | None = None  # set while waiting for readiness
    ready: bool = False

    def record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pid": self.proc.pid if self.proc and not self.building else None,
            "live_url": self.live_url,
            "ready_url": self.ready_url,
            "started_at": self.started_at,
            "restarts": self.restarts,
            "last_exit": self.last_exit,
            "grace_seconds": self.grace,
        }


def plan(env: dict[str, str], with_executor: bool, with_web: bool) -> list[Child]:
    """The children `up` runs, in start order. Refuses a missing health port or install."""
    worker_port = _int(env, "SHIFDE_WORKER_HEALTH_PORT", 0)
    if worker_port == 0:
        raise Refused("procctl: SHIFDE_WORKER_HEALTH_PORT must be set (readiness is probed there)")
    executor_port = _int(env, "SHIFDE_EXECUTOR_HEALTH_PORT", 0)
    if with_executor and executor_port == 0:
        raise Refused(
            "procctl: SHIFDE_EXECUTOR_HEALTH_PORT must be set (readiness is probed there)"
        )
    api = (
        f"http://{_probe_host(env.get('SHIFDE_API_HOST', ''))}:{_int(env, 'SHIFDE_API_PORT', 8710)}"
    )
    worker = f"http://{_probe_host(env.get('SHIFDE_WORKER_HEALTH_HOST', ''))}:{worker_port}"
    executor = f"http://{_probe_host(env.get('SHIFDE_EXECUTOR_HEALTH_HOST', ''))}:{executor_port}"
    graces = {name: _int(env, var, default) for name, (var, default) in GRACES.items()}
    web_port = _int(env, "SHIFDE_WEB_PORT", DEFAULT_WEB_PORT)

    def script(name: str) -> list[str]:
        path = VENV_BIN / (f"{name}.exe" if WINDOWS else name)
        if not path.is_file():
            raise Refused(f"procctl: {name} is not installed: run uv sync in backend")
        return [str(path)]

    children = [
        Child(
            "api", script("shifde-api"), ROOT, f"{api}/healthz", f"{api}/api/ready", graces["api"]
        ),
        Child(
            "worker",
            script("shifde-worker"),
            ROOT,
            f"{worker}/healthz",
            f"{worker}/readyz",
            graces["worker"],
        ),
    ]
    if with_executor:
        children.append(
            Child(
                "executor",
                script("shifde-executor"),
                ROOT,
                f"{executor}/healthz",
                f"{executor}/readyz",
                graces["executor"],
            )
        )
    if with_web:
        pnpm = shutil.which("pnpm")
        if not pnpm or not (FRONTEND / "node_modules").is_dir():
            raise Refused(
                "procctl: pnpm or the front end is not installed: run pnpm install in frontend"
            )
        start = [pnpm, "exec", "next", "start", "-p", str(web_port), "-H", "127.0.0.1"]
        children.append(
            Child(
                "web",
                start,
                FRONTEND,
                None,
                f"http://127.0.0.1:{web_port}/",
                WEB_GRACE,
                ready_below=400,
                build_argv=[pnpm, "build"],
            )
        )
    return children


def probe(url: str, below: int = 300) -> bool:
    """True on an answer with 200 <= status < `below` within the probe timeout; no redirects."""
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(
        parts.hostname or "127.0.0.1", parts.port, timeout=PROBE_TIMEOUT
    )
    try:
        conn.request("GET", parts.path or "/")
        status = conn.getresponse().status
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()
    return 200 <= status < below


def pid_alive(pid: int | None) -> bool:
    """Whether a process that need not be our child is running. Never `os.kill(pid, 0)` on Windows,
    where it would terminate the process."""
    if not pid:
        return False
    if WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def hard_kill_pid(pid: int) -> None:
    """Hard-kill a process that is not our child (`os.kill` with SIGTERM is TerminateProcess on
    Windows)."""
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM if WINDOWS else signal.SIGKILL)


def read_state(state_dir: Path) -> dict[str, Any] | None:
    for _ in range(10):  # Windows: the file can be mid-replace
        try:
            data = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            time.sleep(0.05)
            continue
        return data if isinstance(data, dict) else None
    return None


def say(line: str) -> None:
    print(f"procctl: {line}", flush=True)


class Supervisor:
    def __init__(self, children: list[Child], env: dict[str, str], state_dir: Path) -> None:
        self.children = children
        self.env = env
        self.state_dir = state_dir
        self.stop_file = state_dir / "stop"
        self.signalled = False

    # state ----------------------------------------------------------------------------------
    def write_state(self) -> None:
        data = {"supervisor_pid": os.getpid(), "children": [c.record() for c in self.children]}
        target = self.state_dir / "state.json"
        tmp = self.state_dir / "state.json.tmp"
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        for attempt in range(20):  # Windows: a reader can hold the file open for a moment
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)

    # processes ------------------------------------------------------------------------------
    def _spawn(self, child: Child, argv: list[str]) -> subprocess.Popen[bytes]:
        with open(self.state_dir / f"{child.name}.log", "ab") as log:
            kwargs: dict[str, Any] = {}
            if WINDOWS:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            # fixed argv: the backend venv's console scripts or pnpm
            return subprocess.Popen(  # noqa: S603
                argv,
                cwd=child.cwd,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **kwargs,
            )

    def start(self, child: Child) -> None:
        if child.started:
            child.restarts += 1
        child.started = True
        child.restart_at = None
        child.ready = False
        if child.build_argv and not child.restarts:
            child.building = True
            child.proc = self._spawn(child, child.build_argv)
            say(f"{child.name} build started (pid {child.proc.pid})")
        else:
            self._run(child)
        self.write_state()

    def _run(self, child: Child) -> None:
        child.building = False
        child.proc = self._spawn(child, child.argv)
        child.started_at = datetime.now(UTC).isoformat(timespec="seconds")
        child.started_mono = time.monotonic()
        child.ready_deadline = child.started_mono + READY_TIMEOUT
        say(f"{child.name} started (pid {child.proc.pid})")

    def graceful(self, proc: subprocess.Popen[bytes]) -> None:
        with contextlib.suppress(OSError):
            if WINDOWS:
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGTERM)

    # events ---------------------------------------------------------------------------------
    def stop_requested(self) -> bool:
        return self.signalled or self.stop_file.exists()

    def on_exit(self, child: Child, code: int) -> int | None:
        """Handle an exit procctl did not request; the `up` exit code when it must stop."""
        assert child.proc is not None
        if child.building:
            child.proc = None
            child.building = False
            if code != 0:
                say(f"{child.name} build exited {code}")
                return 1
            say(f"{child.name} build finished")
            self._run(child)
            self.write_state()
            return None
        uptime = time.monotonic() - child.started_mono
        child.proc = None
        child.last_exit = code
        child.ready = False
        child.ready_deadline = None
        say(f"{child.name} exited {code}")
        policy = exit_policy(code)
        if policy == "refused":
            say(f"{child.name} exited {code} ({EXIT_REASONS[code]}); not restarted")
            self.write_state()
            return code
        if policy == "unrequested_zero":
            say(f"{child.name} exited 0 without a stop request; not restarted")
            self.write_state()
            return 1
        delay, child.streak = crash_delay(child.streak, uptime)
        child.restart_at = time.monotonic() + delay
        say(f"{child.name} restarting in {delay} s")
        self.write_state()
        return None

    def tick(self) -> int | None:
        """One pass over the children; the `up` exit code when it must stop."""
        now = time.monotonic()
        for child in self.children:
            if not child.started:
                continue
            if child.proc is not None:
                code = child.proc.poll()
                if code is not None:
                    outcome = self.on_exit(child, code)
                    if outcome is not None:
                        return outcome
                    continue
                if child.building:
                    continue
                if child.ready_deadline is not None:
                    if probe(child.ready_url, child.ready_below):
                        child.ready = True
                        child.ready_deadline = None
                        say(f"{child.name} ready")
                    elif time.monotonic() > child.ready_deadline:
                        say(f"{child.name} not ready within {int(READY_TIMEOUT)} s")
                        return 1
            elif child.restart_at is not None and now >= child.restart_at:
                self.start(child)
        pending = [c for c in self.children if not c.started]
        if pending and all(c.ready for c in self.children if c.started):
            self.start(pending[0])
        return None

    def shutdown(self) -> None:
        running = [c for c in reversed(self.children) if c.proc and c.proc.poll() is None]
        for child in running:
            say(f"{child.name} stopping")
            assert child.proc is not None
            self.graceful(child.proc)
        signalled = time.monotonic()
        for child in running:
            assert child.proc is not None
            try:
                code = child.proc.wait(
                    max(0.0, signalled + child.grace + KILL_MARGIN - time.monotonic())
                )
            except subprocess.TimeoutExpired:
                child.proc.kill()
                child.proc.wait()
                say(f"{child.name} killed after grace")
            else:
                say(f"{child.name} stopped (exit {code})")
        for child in self.children:  # a child waiting to restart is not started again
            child.restart_at = None

    def run(self) -> int:
        def handler(_signum: int, _frame: object) -> None:
            self.signalled = True

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                signal.signal(sig, handler)
        outcome = 0
        try:
            self.write_state()
            while True:
                if self.stop_requested():
                    say("stop requested")
                    break
                result = self.tick()
                if result is not None:
                    outcome = result
                    break
                time.sleep(POLL)
        finally:
            self.shutdown()
            for path in (self.stop_file, self.state_dir / "state.json"):
                with contextlib.suppress(OSError):
                    path.unlink()
        return outcome


def cmd_up(env_file: Path, with_executor: bool, with_web: bool, state_dir: Path) -> int:
    try:
        env = {**os.environ, **parse_env_file(env_file)}
        children = plan(env, with_executor, with_web)
    except Refused as exc:
        print(exc, file=sys.stderr, flush=True)
        return 2
    state_dir.mkdir(parents=True, exist_ok=True)
    state = read_state(state_dir)
    if state and pid_alive(state.get("supervisor_pid")):
        print("procctl: already running", file=sys.stderr, flush=True)
        return 2
    with contextlib.suppress(FileNotFoundError):
        (state_dir / "stop").unlink()
    return Supervisor(children, env, state_dir).run()


def _recorded(state: dict[str, Any]) -> list[tuple[str, int]]:
    return [
        (str(c.get("name")), int(c["pid"]))
        for c in state.get("children", [])
        if isinstance(c, dict) and isinstance(c.get("pid"), int)
    ]


def cmd_down(state_dir: Path) -> int:
    state = read_state(state_dir)
    supervisor = state.get("supervisor_pid") if state else None
    if not state or (
        not pid_alive(supervisor) and not any(pid_alive(p) for _, p in _recorded(state))
    ):
        say("not running")
        return 0
    if pid_alive(supervisor):
        (state_dir / "stop").touch()
        graces = [int(c.get("grace_seconds") or 0) for c in state.get("children", [])]
        deadline = time.monotonic() + max(graces, default=0) + DOWN_MARGIN
        while pid_alive(supervisor) and time.monotonic() < deadline:
            time.sleep(POLL)
            state = read_state(state_dir) or state
        if not pid_alive(supervisor):
            say("stopped")
    left = [(n, p) for n, p in _recorded(state) if pid_alive(p)]
    if pid_alive(supervisor):
        left.insert(0, ("supervisor", int(supervisor)))  # type: ignore[arg-type]
    for name, pid in left:
        hard_kill_pid(pid)
        say(f"killed {name} (pid {pid})")
    if left:
        time.sleep(POLL)
        with contextlib.suppress(OSError):
            (state_dir / "stop").unlink()
    alive = [p for _, p in left if pid_alive(p)]
    return 1 if alive else 0


def cmd_status(state_dir: Path) -> int:
    state = read_state(state_dir)
    if not state or not pid_alive(state.get("supervisor_pid")):
        say("not running")
        return 1
    healthy = True
    for c in state.get("children", []):
        pid = c.get("pid")
        running = isinstance(pid, int) and pid_alive(pid)
        live = running and (probe(c["live_url"]) if c.get("live_url") else True)
        below = 300 if c.get("live_url") else 400  # web readiness: any status below 400
        ready = running and probe(c["ready_url"], below)
        healthy = healthy and live and ready
        print(
            f"{c.get('name')} pid={pid if running else '-'} live={'yes' if live else 'no'} "
            f"ready={'yes' if ready else 'no'} restarts={c.get('restarts', 0)}",
            flush=True,
        )
    return 0 if healthy else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="procctl", description="SETH reference process supervisor"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up", help="start the processes and supervise them (foreground)")
    up.add_argument("--env-file", type=Path, required=True, help="KEY=VALUE file for the children")
    up.add_argument("--with-executor", action="store_true", help="also run shifde-executor")
    up.add_argument("--with-web", action="store_true", help="also build and run the Next server")
    for name, text in (
        ("down", "stop a running up"),
        ("status", "liveness and readiness per child"),
    ):
        commands.add_parser(name, help=text)
    for sub in commands.choices.values():
        sub.add_argument(
            "--state-dir",
            type=Path,
            default=DEFAULT_STATE_DIR,
            help="state, stop file and logs (default .devdata/procctl)",
        )
    args = parser.parse_args(argv)
    if args.command == "up":
        return cmd_up(args.env_file, args.with_executor, args.with_web, args.state_dir)
    if args.command == "down":
        return cmd_down(args.state_dir)
    return cmd_status(args.state_dir)


if __name__ == "__main__":
    sys.exit(main())
