"""Tests for parallel units: scripts/unit.py and supervise.supervise_parallel (no Claude, no network).

Run: python -m unittest discover -s scripts/tests
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


unit = _load("unit", SCRIPTS / "unit.py")
supervise = _load("supervise", SCRIPTS / "supervisor" / "supervise.py")


class FakeLead:
    """Ends after `steps` checks; on ending, applies `effect` (e.g. mark its unit done)."""

    def __init__(self, uid, steps, effect, log):
        self.uid, self.left, self.effect, self.pid, self.sid, self.cwd = uid, steps, effect, 1, uid, "."
        log.append(("start", uid))

    def step(self):
        self.left -= 1
        if self.left > 0:
            return True
        self.effect()
        return False

    def outcome(self):
        return self.uid, "", 0.0, "fake.log", False


class Harness:
    def __init__(self, units, behaviour):
        self.units = units          # list of dicts, mutated as units finish
        self.behaviour = behaviour  # uid -> list of "done" | "handoff" | "crash" per launch
        self.log, self.locks, self.alerts, self.clock = [], {}, [], 0.0
        self.max_running = 0
        self.running_now = set()

    def by_id(self, uid):
        return next(u for u in self.units if u["id"] == uid)

    def start(self, u, note):
        uid = u["id"]
        action = self.behaviour[uid].pop(0)
        self.running_now.add(uid)
        self.max_running = max(self.max_running, len(self.running_now))
        for other in self.running_now - {uid}:
            shared = set(u.get("resources", [])) & set(self.by_id(other).get("resources", []))
            assert not shared, f"{uid} and {other} ran together on {shared}"

        def effect():
            self.running_now.discard(uid)
            if action == "done":
                self.by_id(uid)["status"] = "done"
            self.log.append((action, uid))

        return FakeLead(uid, 2, effect, self.log)

    def end(self, u, lead, outcome):
        action = [a for a, x in self.log if x == u["id"] and a != "start"][-1]
        return ("handoff", "") if action == "handoff" else ("unrecognized", "stashed")

    def take(self, u):
        if any(self.locks.get(r, u["id"]) != u["id"] for r in u.get("resources", [])):
            return False
        for r in u.get("resources", []):
            self.locks[r] = u["id"]
        return True

    def free(self, uid):
        for r in [r for r, h in self.locks.items() if h == uid]:
            del self.locks[r]

    def sleep(self, sec):
        self.clock += sec

    def run(self, slots=3):
        saved = supervise.alert
        supervise.alert = lambda t, b: self.alerts.append((t, b))
        try:
            return supervise.supervise_parallel(
                slots, units=lambda: [dict(u) for u in self.units], start=self.start, end=self.end,
                take=self.take, free=self.free, final=lambda: "FINAL", sleep=self.sleep, now=lambda: self.clock)
        finally:
            supervise.alert = saved


def U(uid, deps=(), res=(), status="todo", final=False):
    return {"id": uid, "title": uid, "branch": uid, "depends_on": list(deps), "resources": list(res),
            "status": status, **({"final": True} if final else {})}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self._pause = supervise.PAUSE
        supervise.PAUSE = str(Path(tempfile.mkdtemp()) / "PAUSE")
        self._state = (supervise.STATE, supervise.SUPDIR, supervise.LOGS)
        tmp = tempfile.mkdtemp()
        supervise.SUPDIR, supervise.STATE, supervise.LOGS = tmp, tmp + "/state.json", tmp + "/logs"

    def tearDown(self):
        supervise.PAUSE = self._pause
        supervise.STATE, supervise.SUPDIR, supervise.LOGS = self._state

    def test_at_most_three_at_once_and_dependencies_wait(self):
        units = [U("a"), U("b"), U("c"), U("d"), U("e", deps=["a"]), U("final", final=True)]
        h = Harness(units, {u: ["done"] for u in "abcde"})
        self.assertEqual(h.run(3), "FINAL")
        self.assertEqual(h.max_running, 3)
        starts = [u for a, u in h.log if a == "start"]
        self.assertEqual(starts[:3], ["a", "b", "c"])
        self.assertLess(h.log.index(("done", "a")), h.log.index(("start", "e")))

    def test_units_sharing_a_resource_never_run_together(self):
        units = [U("fe4", res=["frontend-nav"]), U("fe5", res=["frontend-nav"]), U("x"), U("f", final=True)]
        h = Harness(units, {"fe4": ["done"], "fe5": ["done"], "x": ["done"]})
        self.assertEqual(h.run(3), "FINAL")   # FakeLead start asserts no overlap

    def test_handoff_relaunches_same_unit_and_keeps_its_locks(self):
        units = [U("a", res=["migrations"]), U("b", res=["migrations"]), U("f", final=True)]
        h = Harness(units, {"a": ["handoff", "handoff", "done"], "b": ["done"]})
        self.assertEqual(h.run(3), "FINAL")
        starts = [u for a, u in h.log if a == "start"]
        self.assertEqual(starts, ["a", "a", "a", "b"])   # b waited for a's migrations lock

    def test_waiting_on_owner_blocks_dependants_and_alerts_once(self):
        units = [U("rt", status="waiting_owner"), U("app2", deps=["rt"]), U("fe", res=[]), U("f", final=True)]
        h = Harness(units, {"fe": ["done"]})
        calls = {"n": 0}

        def sleep(sec):
            h.clock += sec
            calls["n"] += 1
            if calls["n"] == 20:          # the owner approves: the lead marks it todo
                h.by_id("rt")["status"] = "todo"
                h.behaviour["rt"] = ["done"]
                h.behaviour["app2"] = ["done"]

        h.sleep = sleep
        self.assertEqual(h.run(3), "FINAL")
        owner_alerts = [b for t, b in h.alerts if t == "Build waiting on you"]
        self.assertEqual(len(owner_alerts), 1)
        self.assertIn("rt", owner_alerts[0])
        self.assertIn("app2", owner_alerts[0])

    def test_failing_unit_backs_off_then_gives_up_without_stopping_others(self):
        units = [U("bad"), U("good"), U("f", final=True)]
        h = Harness(units, {"bad": ["crash"] * 6, "good": ["done"]})
        line = h.run(3)
        self.assertTrue(line.startswith("FAILING parallel failing=['bad']"), line)
        self.assertEqual([u for a, u in h.log if a == "start"].count("bad"), 6)
        self.assertIn(("done", "good"), h.log)
        self.assertEqual([t for t, _ in h.alerts], ["Build unit failing", "Build supervisor stopped"])


class LockAndDoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin, self.repo = root / "o.git", root / "w"
        g = lambda cwd, *a: subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True)  # noqa: E731
        self.g = g
        g(root, "init", "-q", "--bare", "--initial-branch=main", str(self.origin))
        g(root, "clone", "-q", str(self.origin), str(self.repo))
        for k, v in (("user.name", "T"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            g(self.repo, "config", k, v)
        (self.repo / "docs" / "build").mkdir(parents=True)
        self.write_units("todo")
        g(self.repo, "push", "-q", "-u", "origin", "main")
        g(self.repo, "worktree", "add", "-q", str(root / "w" / ".worktrees" / "u1"), "-b", "u1")
        self.wt = root / "w" / ".worktrees" / "u1"

    def tearDown(self):
        self.g(self.repo, "worktree", "remove", "--force", str(self.wt))
        self.tmp.cleanup()

    def write_units(self, status):
        (self.repo / "docs" / "build" / "units.json").write_text(json.dumps({"units": [{"id": "u1", "status": status}]}))
        self.g(self.repo, "add", "-A")
        self.g(self.repo, "commit", "-q", "-m", f"units {status}")

    def test_lock_is_shared_across_worktrees(self):
        self.assertTrue(unit.lock(self.repo, "merge", "u1")[0])
        ok, msg = unit.lock(self.wt, "merge", "u2")
        self.assertFalse(ok)
        self.assertIn("LOCKED merge by u1", msg)
        self.assertFalse(unit.unlock(self.wt, "merge", "u2")[0])     # only the holder may release
        self.assertTrue(unit.unlock(self.wt, "merge", "u1")[0])
        self.assertTrue(unit.lock(self.wt, "merge", "u2")[0])
        self.assertEqual(unit.unlock_all(self.repo, "u2"), ["merge"])

    def test_done_needs_status_done_on_origin_and_head_contained(self):
        self.assertFalse(unit.done(self.wt, "u1")[0])
        self.write_units("done")
        self.assertFalse(unit.done(self.repo, "u1")[0])             # not pushed yet
        self.g(self.repo, "push", "-q", "origin", "main")
        self.assertTrue(unit.done(self.repo, "u1")[0])


class UnitGoalTests(unittest.TestCase):
    def test_unit_goal_fits_the_4000_character_limit_for_every_unit(self):
        units = json.loads((REPO / "docs" / "build" / "units.json").read_text(encoding="utf-8"))["units"]
        for u in units:
            text = supervise.unit_prompt(u, f"S:/Development_2026/shi_work/agt_trus_harness/.worktrees/{u['id']}", str(REPO))
            self.assertNotIn("<<", text)
            self.assertLess(len(text) - len("/goal "), 4000, u["id"])
            self.assertTrue(text.startswith("/goal This condition is met by EITHER outcome A OR outcome B"))

    def test_dependencies_name_real_units_and_have_no_cycle(self):
        units = {u["id"]: u for u in json.loads((REPO / "docs" / "build" / "units.json").read_text(encoding="utf-8"))["units"]}
        for u in units.values():
            for d in u["depends_on"]:
                self.assertIn(d, units)
        seen, order = set(), []

        def visit(uid, path=()):
            self.assertNotIn(uid, path, f"cycle through {uid}")
            for d in units[uid]["depends_on"]:
                visit(d, path + (uid,))
            if uid not in seen:
                seen.add(uid)
                order.append(uid)

        for uid in units:
            visit(uid)
        self.assertEqual(order[-1], "goal1-final")


if __name__ == "__main__":
    unittest.main()


class RestartOnMovedBranchTests(unittest.TestCase):
    """2026-11-17: the app-2 lead moved .worktrees/app-2 to branch app-2-merge-wip and handed off; the
    supervisor then failed to add a second worktree at the same path, five times, and gave up."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin, self.repo = root / "origin.git", root / "repo"
        run = lambda *a, cwd=root: subprocess.run(a, cwd=cwd, check=True, capture_output=True)
        run("git", "init", "--quiet", "--bare", "-b", "main", str(self.origin))
        run("git", "clone", "--quiet", str(self.origin), str(self.repo))
        for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
            run("git", "config", k, v, cwd=self.repo)
        (self.repo / "a.txt").write_text("a\n")
        run("git", "add", "-A", cwd=self.repo)
        run("git", "commit", "--quiet", "-m", "a", cwd=self.repo)
        run("git", "push", "--quiet", "origin", "HEAD:main", cwd=self.repo)
        run("git", "branch", "unit-x", cwd=self.repo)
        run("git", "push", "--quiet", "origin", "unit-x", cwd=self.repo)
        self.wt = self.repo / ".worktrees" / "x"
        run("git", "worktree", "add", "--quiet", str(self.wt), "unit-x", cwd=self.repo)
        run("git", "checkout", "--quiet", "-b", "unit-x-wip", cwd=self.wt)
        self.run = run

    def tearDown(self):
        self.tmp.cleanup()

    def test_moved_worktree_is_reused_and_its_branch_named(self):
        u = {"id": "x", "branch": "unit-x"}
        path = supervise.unit_worktree(u, repo=str(self.repo).replace("\\", "/"))
        self.assertEqual(Path(path).resolve(), self.wt.resolve())
        self.assertEqual(supervise._worktree_branch(path), "unit-x-wip")

    def test_stray_folder_is_not_taken_for_a_worktree(self):
        stray = self.repo / ".worktrees" / "stray"
        stray.mkdir(parents=True)
        self.assertEqual(supervise._worktree_branch(str(stray)), "")

    def test_state_write_survives_a_reader_holding_the_file(self):
        calls = {"n": 0}
        real = supervise.os.replace

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "Access is denied")
            return real(src, dst)

        old = (supervise.SUPDIR, supervise.STATE)
        supervise.SUPDIR = str(Path(self.tmp.name) / "sup")
        supervise.STATE = str(Path(supervise.SUPDIR) / "state.json")
        supervise.os.replace = flaky
        try:
            supervise.write_state(status="x")
            self.assertEqual(json.loads(Path(supervise.STATE).read_text())["status"], "x")
            calls["n"] = -1000  # never succeeds: the write is skipped, the supervisor keeps running
            supervise.write_state(status="y")
        finally:
            supervise.os.replace = real
            supervise.SUPDIR, supervise.STATE = old
