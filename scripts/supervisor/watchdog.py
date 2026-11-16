"""Watches the build from outside the supervisor and alerts the owner when it has stalled.

  python -I scripts/supervisor/watchdog.py [--minutes 30]

It alerts when either of these lasts 30 minutes:
  - no lead is running: no live lead pid in .devdata/supervisor/state.json, which also covers a
    dead supervisor;
  - no commit has landed on any branch, local or origin, which covers worktree branches.
Each alert includes the last lead session id and the last 20 lines of the supervisor log, and
repeats every 30 minutes while the condition lasts. It stays quiet while .devdata/supervisor/PAUSE
exists. It never stops or starts anything.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import alerts  # noqa: E402
import supervise as s  # noqa: E402


def last_commit_age_min(repo: str, now: float) -> float:
    out = s.git_out(repo, "for-each-ref", "--sort=-committerdate", "--count=1",
                    "--format=%(committerdate:unix)", "refs/heads", "refs/remotes")
    return (now - int(out)) / 60 if out.isdigit() else float("inf")


def check(repo: str, limit_min: float, no_lead_since: float | None, now: float,
          lead_alive=s.pid_alive, state=None) -> tuple[list[str], float | None]:
    """Returns (problems, updated no_lead_since)."""
    state = s.read_state() if state is None else state
    problems = []
    if lead_alive(state.get("lead_pid")):
        no_lead_since = None
    else:
        no_lead_since = no_lead_since or now
        if (now - no_lead_since) / 60 >= limit_min:
            problems.append(f"no lead has been running for {(now - no_lead_since) / 60:.0f} min "
                            f"(supervisor status: {state.get('status')!r})")
    age = last_commit_age_min(repo, now)
    if age >= limit_min:
        problems.append(f"no commit on any branch for {age:.0f} min")
    return problems, no_lead_since


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    args = ap.parse_args()
    no_lead_since, last_alert = None, 0.0
    while True:
        now = time.time()
        if os.path.exists(s.PAUSE):
            no_lead_since = None
        else:
            problems, no_lead_since = check(s.REPO, args.minutes, no_lead_since, now)
            if problems and now - last_alert >= args.minutes * 60:
                state = s.read_state()
                alerts.notify(s.LOGS, "SETH build needs a look",
                              "; ".join(problems) + f". Last lead session: {state.get('sid')}.\n"
                              "Last 20 supervisor log lines:\n" + s.log_tail(20))
                last_alert = now
        time.sleep(60)


if __name__ == "__main__":
    main()
