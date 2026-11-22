"""scripts/procctl.py (SP4 design 6.2): the restart policy, env-file parsing and refusals without a
cluster or the backend venv; then, in child processes against real `shifde-*` processes on a
scratch database, a hard-killed worker restarted, an exit-2 child not restarted and `down`
within the grace. The integration tests skip without the backend venv, `.devdata/dev.env` or a
running cluster (CI's scripts job has none of them)."""

from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("procctl", ROOT / "scripts" / "procctl.py")
assert _SPEC and _SPEC.loader
procctl = importlib.util.module_from_spec(_SPEC)
sys.modules["procctl"] = procctl
_SPEC.loader.exec_module(procctl)

PROCCTL = ROOT / "scripts" / "procctl.py"
SCRATCH = Path(__file__).resolve().parent / "procctl_scratch.py"
VENV_PYTHON = procctl.VENV_BIN / ("python.exe" if procctl.WINDOWS else "python")
GRACE = 5


def run_main(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = procctl.main(list(argv))
    return code, out.getvalue()


class Backoff(unittest.TestCase):
    def test_delays_double_up_to_60(self) -> None:
        self.assertEqual(
            [procctl.backoff(n) for n in range(9)], [1, 2, 4, 8, 16, 32, 60, 60, 60]
        )

    def test_streak_grows_on_quick_crashes_and_resets_after_60_s_up(self) -> None:
        streak, delays = 0, []
        for _ in range(4):
            delay, streak = procctl.crash_delay(streak, uptime=3.0)
            delays.append(delay)
        self.assertEqual(delays, [1, 2, 4, 8])
        self.assertEqual(procctl.crash_delay(streak, uptime=60.0), (1, 1))
        self.assertEqual(procctl.crash_delay(streak, uptime=59.0), (16, 5))


class ExitPolicy(unittest.TestCase):
    def test_crash_codes_restart(self) -> None:
        for code in (1, 15, -9, 137):
            self.assertEqual(procctl.exit_policy(code), "restart", code)

    def test_contract_refusals_do_not_restart(self) -> None:
        reasons = {
            2: "configuration refused",
            3: "unsafe database role",
            4: "schema revision mismatch",
            5: "cannot authenticate",
        }
        for code, reason in reasons.items():
            self.assertEqual(procctl.exit_policy(code), "refused", code)
            self.assertEqual(procctl.EXIT_REASONS[code], reason)

    def test_unrequested_zero_does_not_restart(self) -> None:
        self.assertEqual(procctl.exit_policy(0), "unrequested_zero")


class EnvFile(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "procctl.env"

    def test_bom_comments_blank_lines_and_quotes(self) -> None:
        self.path.write_bytes(
            "﻿A=1\n# B=2\n\n  C =  'three'\nD=\"four\"\nE='mixed\"\nF=a=b\n".encode()
        )
        self.assertEqual(
            procctl.parse_env_file(self.path),
            {"A": "1", "C": "three", "D": "four", "E": "'mixed\"", "F": "a=b"},
        )

    def test_bad_line_exits_2_naming_the_line_not_its_content(self) -> None:
        self.path.write_text("SHIFDE_ENV=development\nsupersecretvalue\n", encoding="utf-8")
        code, out = run_main("up", "--env-file", str(self.path), "--state-dir", self.dir.name)
        self.assertEqual(code, 2)
        self.assertIn("line 2", out)
        self.assertNotIn("supersecretvalue", out)

    def test_missing_file_exits_2(self) -> None:
        code, out = run_main("up", "--env-file", str(self.path), "--state-dir", self.dir.name)
        self.assertEqual(code, 2)
        self.assertIn("env file not found", out)


class Refusals(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = Path(self.dir.name) / "state"
        self.env = Path(self.dir.name) / "procctl.env"

    def test_up_without_a_worker_health_port_exits_2_and_starts_nothing(self) -> None:
        for content in ("SHIFDE_ENV=development\n", "SHIFDE_WORKER_HEALTH_PORT=0\n"):
            self.env.write_text(content, encoding="utf-8")
            with unittest.mock.patch.dict(os.environ):
                os.environ.pop("SHIFDE_WORKER_HEALTH_PORT", None)
                with unittest.mock.patch.object(procctl.subprocess, "Popen") as popen:
                    code, out = run_main(
                        "up", "--env-file", str(self.env), "--state-dir", str(self.state)
                    )
            self.assertEqual(code, 2)
            self.assertIn("SHIFDE_WORKER_HEALTH_PORT", out)
            popen.assert_not_called()
            self.assertFalse(self.state.exists())

    def test_status_and_down_with_an_empty_state_dir(self) -> None:
        code, out = run_main("status", "--state-dir", self.dir.name)
        self.assertEqual((code, out.strip()), (1, "procctl: not running"))
        code, out = run_main("down", "--state-dir", self.dir.name)
        self.assertEqual((code, out.strip()), (0, "procctl: not running"))


# integration ---------------------------------------------------------------------------------


def _skip_reason(database: bool) -> str | None:
    for name in ("shifde-api", "shifde-worker"):
        if not (procctl.VENV_BIN / (f"{name}.exe" if procctl.WINDOWS else name)).is_file():
            return f"backend venv has no {name} (run uv sync in backend)"
    if not database:
        return None
    if not (ROOT / ".devdata" / "dev.env").is_file():
        return ".devdata/dev.env is missing (run python scripts/dev_db.py env)"
    status = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "dev_db.py"), "status"],
        capture_output=True,
        check=False,
    )
    if status.returncode != 0:
        return "the local cluster is not running (python scripts/dev_db.py start)"
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get(port: int, path: str) -> int | None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request("GET", path)
        return conn.getresponse().status
    except OSError:
        return None
    finally:
        conn.close()


def _hard_kill(pid: int) -> None:
    # Windows: os.kill with SIGTERM is TerminateProcess
    os.kill(pid, signal.SIGTERM if procctl.WINDOWS else signal.SIGKILL)  # type: ignore[attr-defined,unused-ignore]


class Integration(unittest.TestCase):
    """One `procctl.py up` per test, in its own temporary directory, ports and state dir."""

    database = True

    def setUp(self) -> None:
        reason = _skip_reason(self.database)
        if reason:
            raise unittest.SkipTest(reason)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        self.state = self.tmp / "state"
        self.env_file = self.tmp / "procctl.env"
        self.api_port = _free_port()
        self.worker_port = _free_port()
        self.env_file.write_text(
            "SHIFDE_ENV=development\n"
            "SHIFDE_API_HOST=127.0.0.1\n"
            f"SHIFDE_API_PORT={self.api_port}\n"
            f"SHIFDE_WORKER_HEALTH_PORT={self.worker_port}\n"
            f"SHIFDE_API_SHUTDOWN_GRACE_SECONDS={GRACE}\n"
            f"SHIFDE_WORKER_SHUTDOWN_GRACE_SECONDS={GRACE}\n",
            encoding="utf-8",
        )
        self.up: subprocess.Popen[bytes] | None = None
        self.up_log = self.tmp / "up.log"

    def scratch_database(self) -> None:
        made = subprocess.run(
            [str(VENV_PYTHON), str(SCRATCH), "create", str(self.env_file)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(made.returncode, 0, made.stderr[-500:])
        name = made.stdout.strip().splitlines()[-1]
        self.addCleanup(
            subprocess.run,
            [str(VENV_PYTHON), str(SCRATCH), "drop", name],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )

    def start_up(self) -> None:
        kwargs: dict[str, object] = {}
        if procctl.WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        log = self.up_log.open("ab")
        self.addCleanup(log.close)
        self.up = subprocess.Popen(  # noqa: S603 - this checkout's procctl.py
            [sys.executable, str(PROCCTL), "up", "--env-file", str(self.env_file),
             "--state-dir", str(self.state)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            **kwargs,  # type: ignore[call-overload]
        )  # fmt: skip
        self.addCleanup(self.cleanup)

    def cleanup(self) -> None:
        state = procctl.read_state(self.state)
        self.procctl("down", timeout=60)
        if self.up is not None and self.up.poll() is None:
            self.up.kill()
            self.up.wait()
        for child in (state or {}).get("children", []):
            if isinstance(child.get("pid"), int) and procctl.pid_alive(child["pid"]):
                procctl.hard_kill_pid(child["pid"])

    def procctl(self, *argv: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PROCCTL), *argv, "--state-dir", str(self.state)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def output(self) -> str:
        return self.up_log.read_text(encoding="utf-8", errors="replace")

    def wait_for(self, check: object, seconds: float, what: str) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():  # type: ignore[operator]
                return
            time.sleep(0.5)
        self.fail(f"{what} within {seconds} s; procctl output:\n{self.output()}")

    def wait_healthy(self) -> None:
        self.wait_for(lambda: self.procctl("status").returncode == 0, 120, "status exit 0")

    def children(self) -> dict[str, dict[str, object]]:
        state = procctl.read_state(self.state) or {}
        return {c["name"]: c for c in state.get("children", [])}


class HardKilledWorker(Integration):
    def test_a_hard_killed_worker_is_restarted_and_readiness_returns(self) -> None:
        self.scratch_database()
        self.start_up()
        self.wait_healthy()
        old = self.children()["worker"]
        self.assertEqual(old["restarts"], 0)
        _hard_kill(int(old["pid"]))  # type: ignore[call-overload]

        def restarted() -> bool:
            worker = self.children().get("worker", {})
            return (
                worker.get("pid") not in (None, old["pid"])
                and worker.get("restarts") == 1
                and _get(self.worker_port, "/readyz") == 200
                and self.procctl("status").returncode == 0
            )

        self.wait_for(restarted, 30, "a restarted, ready worker")
        self.assertIn("worker restarting in 1 s", self.output())


class ConfigurationRefused(Integration):
    database = False  # the API refuses its settings before it opens a connection

    def test_an_exit_2_child_is_not_restarted(self) -> None:
        with self.env_file.open("a", encoding="utf-8") as f:
            f.write("SHIFDE_WORKER_POLL_MS=not-a-number\n")
        self.start_up()
        assert self.up is not None
        self.assertEqual(self.up.wait(60), 2, self.output())
        self.assertIn("exited 2 (configuration refused); not restarted", self.output())
        self.assertNotIn("restarting", self.output())


class DownWithinGrace(Integration):
    def test_down_exits_0_within_the_grace(self) -> None:
        self.scratch_database()
        self.start_up()
        self.wait_healthy()
        pids = [int(c["pid"]) for c in self.children().values()]  # type: ignore[call-overload]
        started = time.monotonic()
        down = self.procctl("down", timeout=GRACE + 30)
        elapsed = time.monotonic() - started
        self.assertEqual(down.returncode, 0, down.stdout + down.stderr)
        self.assertLess(elapsed, GRACE + 15)
        assert self.up is not None
        self.assertEqual(self.up.wait(15), 0, self.output())
        self.assertEqual([p for p in pids if procctl.pid_alive(p)], [])
        self.assertIn("api stopped", self.output())
        self.assertIn("worker stopped", self.output())


if __name__ == "__main__":
    unittest.main()
