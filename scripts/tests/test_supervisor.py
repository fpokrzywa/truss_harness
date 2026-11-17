"""Tests for scripts/handoff.py and scripts/supervisor/supervise.py (python -m unittest discover -s scripts/tests).

Each test builds a throwaway repository with a local bare `origin`, so nothing here touches the
real checkout, the network or Claude. The three end-of-session cases the owner asked for:
  - a clean handoff;
  - a forced turn end whose result text is empty;
  - a crash mid-unit that leaves a dirty tree and an unpushed commit.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


handoff = _load("handoff", SCRIPTS / "handoff.py")
supervise = _load("supervise", SCRIPTS / "supervisor" / "supervise.py")

SID = "11111111-2222-3333-4444-555555555555"


def run(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


class RepoCase(unittest.TestCase):
    """A clone of a bare origin, on main, with one pushed commit."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.repo = root / "work"
        run(root, "init", "--quiet", "--bare", "--initial-branch=main", str(self.origin))
        run(root, "clone", "--quiet", str(self.origin), str(self.repo))
        for k, v in (("user.name", "Test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
            run(self.repo, "config", k, v)
        (self.repo / "README.md").write_text("start\n")
        run(self.repo, "add", ".")
        run(self.repo, "commit", "--quiet", "-m", "start")
        run(self.repo, "push", "--quiet", "-u", "origin", "main")
        # The supervisor's own files go to a temp dir, never the real .devdata.
        sup = root / "sup"
        self.alerts: list[tuple[str, str]] = []
        self._saved = {k: getattr(supervise, k) for k in ("SUPDIR", "LOGS", "STATE", "PAUSE", "alert")}
        supervise.SUPDIR, supervise.LOGS = str(sup), str(sup / "logs")
        supervise.STATE, supervise.PAUSE = str(sup / "state.json"), str(sup / "PAUSE")
        supervise.alert = lambda title, body: self.alerts.append((title, body))
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            setattr(supervise, k, v)
        self.tmp.cleanup()

    def commit_push(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text)
        run(self.repo, "add", name)
        run(self.repo, "commit", "--quiet", "-m", f"add {name}")
        run(self.repo, "push", "--quiet", "origin", "main")

    def write_handoff(self) -> int:
        return handoff.write(self.repo, "admin part 2", "start admin part 3", SID)


class HandoffRecordTests(RepoCase):
    def test_write_then_verify(self) -> None:
        self.commit_push("work.txt", "unit done\n")
        base = run(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.write_handoff(), 0)
        ok, detail, record = handoff.verify(self.repo, SID)
        self.assertTrue(ok, detail)
        self.assertEqual(record["head_sha"], base)
        self.assertEqual(run(self.repo, "rev-parse", "HEAD"), run(self.repo, "rev-parse", "origin/main"))

    def test_write_refuses_dirty_tree(self) -> None:
        (self.repo / "stray.txt").write_text("x")
        self.assertEqual(self.write_handoff(), 1)
        self.assertFalse((self.repo / handoff.record_path("main")).exists())

    def test_write_refuses_unpushed_commit(self) -> None:
        (self.repo / "a.txt").write_text("a")
        run(self.repo, "add", "a.txt")
        run(self.repo, "commit", "--quiet", "-m", "local only")
        self.assertEqual(self.write_handoff(), 1)

    def test_verify_rejects_other_session(self) -> None:
        self.assertEqual(self.write_handoff(), 0)
        ok, detail, _ = handoff.verify(self.repo, "someone-else")
        self.assertFalse(ok)
        self.assertIn("expected someone-else", detail)

    def test_verify_rejects_work_after_handoff(self) -> None:
        self.assertEqual(self.write_handoff(), 0)
        self.commit_push("later.txt", "more work after the handoff\n")
        ok, detail, _ = handoff.verify(self.repo, SID)
        self.assertFalse(ok)
        self.assertIn("does not change only", detail)

    def test_verify_rejects_stale_record_from_earlier_session(self) -> None:
        self.assertEqual(self.write_handoff(), 0)
        ok, _, _ = handoff.verify(self.repo, "next-lead-session")
        self.assertFalse(ok)


class ThreeEndingsTests(RepoCase):
    """The owner's three cases, through classify_end and salvage."""

    def test_case_1_clean_handoff(self) -> None:
        self.commit_push("unit.txt", "done\n")
        self.assertEqual(self.write_handoff(), 0)
        kind, detail = supervise.classify_end(str(self.repo), SID, 0, "HANDOFF COMPLETE\n...",
                                              handed_off=lambda s, t: True)
        self.assertEqual(kind, "handoff", detail)
        self.assertEqual(supervise.salvage(str(self.repo), SID), "")  # nothing to salvage

    def test_case_2_forced_turn_end_with_empty_result_text(self) -> None:
        # The goal check blocked the handoff until Claude Code's 9-block cap ended the turn:
        # the result text is empty and, in the worst case, the transcript is not readable either.
        self.commit_push("unit.txt", "done\n")
        self.assertEqual(self.write_handoff(), 0)
        kind, detail = supervise.classify_end(str(self.repo), SID, 0, "", handed_off=lambda s, t: False)
        self.assertEqual(kind, "handoff", detail)

    def test_case_2b_old_style_handoff_falls_back_to_transcript(self) -> None:
        # No HANDOFF.json, but the transcript says HANDOFF COMPLETE and the checkout is clean and pushed.
        self.commit_push("unit.txt", "done\n")
        kind, _ = supervise.classify_end(str(self.repo), SID, 0, "", handed_off=lambda s, t: True)
        self.assertEqual(kind, "handoff-fallback")

    def test_case_3_crash_mid_unit_with_dirty_tree(self) -> None:
        self.commit_push("unit.txt", "pushed part\n")
        pushed = run(self.repo, "rev-parse", "HEAD")
        (self.repo / "local.txt").write_text("committed, never pushed\n")
        run(self.repo, "add", "local.txt")
        run(self.repo, "commit", "--quiet", "-m", "unpushed work")
        unpushed = run(self.repo, "rev-parse", "HEAD")
        (self.repo / "unit.txt").write_text("half-edited\n")          # modified tracked file
        (self.repo / "new_module.py").write_text("print('wip')\n")   # untracked file

        kind, _ = supervise.classify_end(str(self.repo), SID, 0, "", handed_off=lambda s, t: False)
        self.assertEqual(kind, "unrecognized")
        # Even a transcript that claims a handoff is not trusted on a dirty, unpushed checkout.
        kind, _ = supervise.classify_end(str(self.repo), SID, 0, "HANDOFF COMPLETE", handed_off=lambda s, t: True)
        self.assertEqual(kind, "unrecognized")

        text = supervise.salvage(str(self.repo), SID)
        self.assertIn("stash", text)
        self.assertIn("salvage/11111111-", text)
        self.assertEqual(run(self.repo, "rev-parse", "HEAD"), pushed)          # back to last pushed commit
        self.assertEqual(run(self.repo, "status", "--porcelain"), "")         # clean
        self.assertIn("supervisor salvage 11111111", run(self.repo, "stash", "list"))
        stash_files = run(self.repo, "stash", "show", "--include-untracked", "--name-only", "stash@{0}")
        self.assertIn("unit.txt", stash_files)
        self.assertIn("new_module.py", stash_files)
        branches = run(self.repo, "branch", "--list", "salvage/*", "--format=%(objectname)")
        self.assertEqual(branches, unpushed)                                  # nothing lost
        self.assertNotIn(unpushed, run(self.repo, "ls-remote", "origin"))     # and nothing pushed


class LoopTests(RepoCase):
    """supervise() with a fake lead runner and no real waiting."""

    def loop(self, endings: list[tuple[str | None, str, str]], goal_done_after: int | None = None):
        calls, sleeps, notes = [], [], []
        it = iter(endings)

        def fake_run(n, note):
            calls.append(note)
            sid, text, action = next(it)
            if action == "handoff":
                self.commit_push(f"unit{len(calls)}.txt", "x\n")
                handoff.write(self.repo, "u", "next", sid)
            elif action == "crash":
                (self.repo / f"wip{len(calls)}.txt").write_text("wip\n")
            return sid, text, 0.0, "fake.log", False

        line = supervise.supervise(
            1, run=fake_run, sleep=sleeps.append, repo=str(self.repo),
            goal_done=lambda r: goal_done_after is not None and len(calls) >= goal_done_after,
            met=lambda s, t: False, handed_off=lambda s, t: False)
        return line, calls, sleeps

    def test_handoffs_restart_at_once_and_done_ends(self) -> None:
        line, calls, sleeps = self.loop([("a", "", "handoff"), ("b", "", "handoff"), ("c", "", "handoff")],
                                        goal_done_after=3)
        self.assertEqual(line, "GOAL-DONE goal=1")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [])

    def test_unrecognized_ends_back_off_then_alert_after_five_restarts(self) -> None:
        line, calls, sleeps = self.loop([(f"s{i}", "", "crash") for i in range(6)])
        self.assertTrue(line.startswith("FAILING goal=1"), line)
        self.assertEqual(len(calls), 6)                       # first lead + 5 restarts
        self.assertEqual(sleeps, [60, 120, 240, 480, 960])    # 1, 2, 4, 8, 16 minutes
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("5 fresh leads in a row", self.alerts[0][1])
        self.assertIn("stash", calls[1])                      # each restart is told what was salvaged

    def test_a_handoff_resets_the_failure_count(self) -> None:
        endings = [("s0", "", "crash")] * 4 + [("h", "", "handoff")] + [("t", "", "crash")] * 6
        line, calls, sleeps = self.loop(endings)
        self.assertTrue(line.startswith("FAILING"))
        self.assertEqual(sleeps, [60, 120, 240, 480] + [60, 120, 240, 480, 960])

    def test_goal_2_handoff_is_not_goal_met_even_though_the_goal_check_says_met(self) -> None:
        # 2026-11-17: outcome A (handoff) also records met=true; goal 2 stopped after its first lead.
        calls = []

        def fake_run(n, note):
            calls.append(n)
            sid = f"s{len(calls)}"
            if len(calls) < 3:
                self.commit_push(f"g{len(calls)}.txt", "x\n")
                handoff.write(self.repo, "u", "next", sid)
            return sid, "", 0.0, "fake.log", False

        line = supervise.supervise(2, run=fake_run, sleep=lambda s: None, repo=str(self.repo),
                                   goal_done=lambda r: False, met=lambda s, t: True,
                                   handed_off=lambda s, t: False)
        self.assertEqual(len(calls), 3)  # two handoffs relaunched; the third lead's met=true ends it
        self.assertTrue(line.startswith("GOAL-MET goal=2 sid=s3"), line)

    def test_pause_file_holds_new_leads(self) -> None:
        Path(supervise.PAUSE).parent.mkdir(parents=True, exist_ok=True)
        Path(supervise.PAUSE).write_text("owner pause")
        waits = []

        def sleep(sec):
            waits.append(sec)
            if len(waits) == 3:
                os.remove(supervise.PAUSE)

        started = []
        supervise.supervise(1, run=lambda n, note: started.append(1) or ("x", "", 0.0, "l", False),
                            sleep=sleep, repo=str(self.repo), goal_done=lambda r: bool(started),
                            met=lambda s, t: False, handed_off=lambda s, t: False)
        self.assertEqual(waits, [60, 60, 60])
        self.assertEqual(len(started), 1)


class WatchdogTests(RepoCase):
    def test_alerts_after_thirty_minutes_without_lead_or_commit(self) -> None:
        watchdog = _load("watchdog", SCRIPTS / "supervisor" / "watchdog.py")
        now = 10_000_000_000.0  # far future: the last commit is "old"
        problems, since = watchdog.check(str(self.repo), 30, None, now, lead_alive=lambda p: False, state={})
        self.assertEqual(len(problems), 1)                    # commit age only; no-lead clock just started
        problems, _ = watchdog.check(str(self.repo), 30, since, now + 31 * 60, lambda p: False, {})
        self.assertEqual(len(problems), 2)
        problems, since = watchdog.check(str(self.repo), 30, since, now + 31 * 60, lambda p: True,
                                         {"slots": {"fe-areas-4": {"pid": 4242}}})
        self.assertIsNone(since)                              # a parallel slot counts as a running lead


if __name__ == "__main__":
    unittest.main()
