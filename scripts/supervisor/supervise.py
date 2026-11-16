"""Runs one build goal in headless Claude Code lead sessions, one after another, until the goal is met.

  python -I scripts/supervisor/supervise.py <1|2>

Each lead gets goal N from docs/build/GOALS.md. A lead stops in one of three ways:

- **Handoff.** `scripts/handoff.py verify` passes for that lead: HANDOFF.json is committed and
  pushed on top of a clean tree. A fresh lead starts at once.
  - Fallback: if the repo record is missing but the transcript shows `HANDOFF COMPLETE` and the
    checkout is clean and pushed, that also counts as a handoff, and a warning is logged.
- **Goal met.** Goal 1's STATUS check passes, or the goal check recorded met=true. The supervisor
  prints `GOAL-DONE` (goal 1) or `GOAL-MET` (goal 2) and exits, for the supervising session to verify.
- **Anything else** (a crash, a forced turn end, usage limits, a forced stop). The supervisor:
  1. salvages the checkout: stashes uncommitted work, and keeps unpushed commits on a
     `salvage/<session>-<time>` branch;
  2. returns main to the last pushed commit;
  3. waits with backoff (1, 2, 4, 8, 16 minutes);
  4. starts a fresh lead, told what was salvaged.
  After 5 restarts fail in a row, it alerts the owner and exits with `FAILING`.

Other behaviour:
- **Context limit.** A lead at or over FORCE_AT tokens, with no agent running and git idle, is
  stopped. This is the only stop the supervisor itself makes. It is then salvaged and restarted
  as above, and does not count as a failure.
- **Stale.** STALE_MIN minutes without activity alerts the owner and keeps watching; the lead is
  left running.
- **Pause.** While `.devdata/supervisor/PAUSE` exists, no new lead starts. A running lead is
  never stopped by a pause.
- **One at a time.** Never run two supervisors at once. The state file enforces this: a second
  supervisor exits.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE)).replace("\\", "/")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import alerts  # noqa: E402
import handoff  # noqa: E402  (scripts/handoff.py)

PROJECTS = os.path.expanduser("~/.claude/projects")
CLAUDE = "C:/Users/FreddiePokrzywa/AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
SUPDIR = os.path.join(REPO, ".devdata", "supervisor")  # gitignored
LOGS = os.path.join(SUPDIR, "logs")
STATE = os.path.join(SUPDIR, "state.json")
PAUSE = os.path.join(SUPDIR, "PAUSE")
HANDOFF_AT = 200_000   # charter section 10: the lead hands off at the next unit boundary
FORCE_AT = 260_000     # the supervisor stops a lead that has gone well past it with no agent running
STALE_MIN = 120
POLL = 30
BACKOFF_MIN = [1, 2, 4, 8, 16]
MAX_RESTARTS = len(BACKOFF_MIN)
# Load no MCP connectors: the build needs none, and they cost ~35k tokens of context per lead.
EXTRA_FLAGS = ["--strict-mcp-config", "--mcp-config", os.path.join(HERE, "empty-mcp.json")]
SCRUB = ["CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_CHILD_SESSION",
         "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET",
         "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED",
         "CLAUDE_CODE_QUESTION_PREVIEW_FORMAT", "CLAUDE_CODE_STARTUP_FAILURE_RESULTS",
         "CLAUDE_CODE_EMIT_STARTUP_TIMING", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
         "CLAUDE_CODE_ENABLE_TASKS"]


# ---------------------------------------------------------------- small helpers

def note(msg: str) -> None:
    os.makedirs(LOGS, exist_ok=True)
    with open(os.path.join(LOGS, "supervisor.log"), "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def alert(title: str, body: str) -> None:
    note(f"ALERT {title}: {body[:300]}")
    alerts.notify(LOGS, title, body)


def git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8")


def git_out(repo: str, *args: str) -> str:
    return git(repo, *args).stdout.strip()


def git_busy(repo: str) -> bool:
    gitdir = git_out(repo, "rev-parse", "--absolute-git-dir")
    return any(os.path.exists(os.path.join(gitdir, n)) for n in ("index.lock", "MERGE_HEAD", "rebase-merge"))


def pid_alive(pid) -> bool:
    if not pid:
        return False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"], capture_output=True, text=True).stdout
    return str(int(pid)) in out


def read_state() -> dict:
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(**kw) -> None:
    os.makedirs(SUPDIR, exist_ok=True)
    state = read_state()
    state.update(kw, supervisor_pid=os.getpid(), heartbeat=time.time())
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE)


def transcript(sid: str) -> str | None:
    hits = glob.glob(f"{PROJECTS}/*/{sid}.jsonl")
    return hits[0] if hits else None


def tail_lines(path: str | None, tail_bytes: int = 3_000_000) -> list[str]:
    if not path:
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def last_context(sid: str) -> int:
    for line in reversed(tail_lines(transcript(sid), 600_000)):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message")
        if d.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict):
            u = m["usage"]
            return sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return 0


def _entries_since(sid: str, since: float):
    from datetime import datetime
    for line in tail_lines(transcript(sid)):
        try:
            d = json.loads(line)
            ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
        except (ValueError, KeyError, AttributeError, TypeError):
            continue
        if ts >= since:
            yield d


def handed_off_since(sid: str, since: float) -> bool:
    """Fallback signal: an assistant message starting HANDOFF COMPLETE after `since`."""
    for d in _entries_since(sid, since):
        m = d.get("message")
        if d.get("type") == "assistant" and isinstance(m, dict):
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text" and \
                        c.get("text", "").lstrip().startswith("HANDOFF COMPLETE"):
                    return True
    return False


def goal_met_since(sid: str, since: float) -> bool:
    met = False
    for d in _entries_since(sid, since):
        a = d.get("attachment")
        if isinstance(a, dict) and a.get("type") == "goal_status":
            met = bool(a.get("met"))
    return met


def agents_running(sid: str, fresh_min: int = 180) -> int:
    path = transcript(sid)
    if not path:
        return 0
    n, now = 0, time.time()
    for meta in glob.glob(path[:-6] + "/subagents/*.meta.json"):
        try:
            finished = json.load(open(meta, encoding="utf-8")).get("finished")
        except (OSError, ValueError):
            finished = None
        log = meta.replace(".meta.json", ".jsonl")
        if finished is not True and os.path.exists(log) and now - os.path.getmtime(log) < fresh_min * 60:
            n += 1
    return n


def newest_activity(sid: str, log: str) -> float:
    path = transcript(sid)
    paths = [log] + ([path] + glob.glob(path[:-6] + "/subagents/*.jsonl") if path else [])
    times = [os.path.getmtime(p) for p in paths if os.path.exists(p)]
    return max(times) if times else time.time()


def goal1_done(repo: str = REPO) -> bool:
    try:
        status = open(f"{repo}/docs/build/STATUS.md", encoding="utf-8").read().lower()
    except OSError:
        return False
    row = any(l.startswith("| software factory install") and l.split("|")[2].strip() == "done"
              for l in status.splitlines())
    main = git_out(repo, "rev-parse", "main")
    return bool(row and main and main == git_out(repo, "rev-parse", "origin/main"))


def goal_text(n: int, repo: str = REPO) -> str:
    import re
    text = open(f"{repo}/docs/build/GOALS.md", encoding="utf-8").read()
    return re.findall(r"```\n(/goal .*?)\n```", text, flags=re.S)[n - 1]


# ---------------------------------------------------------------- the decisions (unit-tested)

def classify_end(repo: str, sid: str | None, started: float, result_text: str,
                 handed_off=handed_off_since) -> tuple[str, str]:
    """('handoff' | 'handoff-fallback' | 'unrecognized', detail)."""
    if sid:
        ok, detail, _ = handoff.verify(__import__("pathlib").Path(repo), sid, fetch=True)
        if ok:
            return "handoff", detail
    else:
        detail = "no session id"
    says_handoff = "HANDOFF COMPLETE" in (result_text or "")[:400] or bool(sid and handed_off(sid, started))
    if says_handoff:
        git(repo, "fetch", "--quiet", "origin")
        clean = git_out(repo, "status", "--porcelain") == ""
        branch = git_out(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        pushed = bool(branch) and git_out(repo, "rev-parse", "HEAD") == git_out(repo, "rev-parse", f"origin/{branch}")
        if clean and pushed:
            return "handoff-fallback", f"transcript says HANDOFF COMPLETE, checkout clean and pushed; repo record: {detail}"
        return "unrecognized", f"transcript says HANDOFF COMPLETE but clean={clean} pushed={pushed}; repo record: {detail}"
    return "unrecognized", f"no handoff; repo record: {detail}"


def salvage(repo: str, sid: str | None, branch: str = "main") -> str:
    """Return the checkout to the last pushed commit without losing anything. Returns a note."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = (sid or "unknown")[:8]
    notes = []
    git(repo, "fetch", "--quiet", "origin")
    if git_out(repo, "status", "--porcelain"):
        r = git(repo, "stash", "push", "--include-untracked", "-m", f"supervisor salvage {tag} {stamp}")
        if r.returncode != 0:
            raise RuntimeError(f"git stash failed: {r.stderr.strip()}")
        notes.append(f"uncommitted work was stashed as `stash@{{0}}` (\"supervisor salvage {tag} {stamp}\")")
    current = git_out(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if current != branch:
        notes.append(f"the checkout was on `{current or 'a detached HEAD'}` and was switched back to `{branch}`")
        r = git(repo, "checkout", "--quiet", branch)
        if r.returncode != 0:
            raise RuntimeError(f"git checkout {branch} failed: {r.stderr.strip()}")
    upstream = git_out(repo, "rev-parse", f"origin/{branch}")
    head = git_out(repo, "rev-parse", "HEAD")
    if head != upstream:
        unpushed = git_out(repo, "rev-list", "--count", f"origin/{branch}..HEAD")
        if unpushed and unpushed != "0":
            keep = f"salvage/{tag}-{stamp}"
            git(repo, "branch", keep, "HEAD")
            notes.append(f"{unpushed} unpushed commit(s) on `{branch}` were kept on branch `{keep}` (not pushed)")
        r = git(repo, "reset", "--quiet", "--hard", f"origin/{branch}")
        if r.returncode != 0:
            raise RuntimeError(f"git reset failed: {r.stderr.strip()}")
        notes.append(f"`{branch}` was returned to the last pushed commit {upstream[:9]}")
    if not notes:
        return ""
    return ("The previous lead (session " + tag + ") ended without a valid handoff. The supervisor salvaged "
            "the main checkout: " + "; ".join(notes) + ". Agent worktrees under .worktrees/ were not touched "
            "and may hold uncommitted work. Inspect the salvage and the worktrees, decide what to keep, and "
            "record what you decided in docs/build/DECISIONS.md before continuing from docs/build/HANDOFF.md.")


# ---------------------------------------------------------------- running a lead

def run_lead(n: int, append_prompt: str = "") -> tuple[str | None, str, float, str, bool]:
    """Start one lead and watch it. Returns (sid, result_text, started, log, forced)."""
    os.makedirs(LOGS, exist_ok=True)
    log = os.path.join(LOGS, f"goal{n}-{time.strftime('%Y%m%d-%H%M%S')}.log")
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env["DISABLE_AUTO_COMPACT"] = "1"   # the owner wants handoffs, never compaction
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = str(8 * 3600 * 1000)  # default 10 min cuts agents off
    cmd = [CLAUDE, "-p", goal_text(n), "--permission-mode", "auto", "--permission-prompts", "none",
           "--model", "opus", "--output-format", "stream-json", "--verbose", *EXTRA_FLAGS]
    if append_prompt:
        cmd += ["--append-system-prompt", append_prompt]
    started = time.time()
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
    note(f"started goal {n} pid={proc.pid} log={log}" + (" (with salvage note)" if append_prompt else ""))
    write_state(goal=n, lead_pid=proc.pid, sid=None, lead_started=started, status="running")
    sid, forced, peak, stale_alerted = None, False, 0, 0.0
    while proc.poll() is None:
        time.sleep(POLL)
        sid = sid or session_id_from_log(log)
        if not sid:
            continue
        os.makedirs(f"{REPO}/.devdata", exist_ok=True)
        open(f"{REPO}/.devdata/lead_session", "w", encoding="utf-8").write(sid)
        ctx = last_context(sid)
        peak = max(peak, ctx)
        write_state(sid=sid, lead_ctx=ctx)
        if ctx >= FORCE_AT and agents_running(sid) == 0 and not git_busy(REPO):
            note(f"forcing a stop: lead context {ctx} >= {FORCE_AT}, no agent running")
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            forced = True
            break
        idle_min = (time.time() - newest_activity(sid, log)) / 60
        if idle_min > STALE_MIN and time.time() - stale_alerted > STALE_MIN * 60:
            alert("Build lead stale", f"No activity for {idle_min:.0f} min. Lead {sid} (pid {proc.pid}) "
                                      "left running. Last log lines:\n" + log_tail(20))
            stale_alerted = time.time()
    proc.wait()
    res = final_result(log)
    note(f"lead ended sid={sid} peak_lead_ctx={peak} turns={res.get('num_turns')} "
         f"cost_usd={res.get('total_cost_usd')} exit={proc.returncode} forced={forced}")
    write_state(lead_pid=None, status="between leads")
    return sid, str(res.get("result") or ""), started, log, forced


def session_id_from_log(log: str) -> str | None:
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("session_id"):
                    return d["session_id"]
    except OSError:
        pass
    return None


def final_result(log: str) -> dict:
    try:
        lines = open(log, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return {}
    for line in reversed(lines[-50:]):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "result":
            return d
    return {}


def log_tail(n: int = 20) -> str:
    try:
        with open(os.path.join(LOGS, "supervisor.log"), encoding="utf-8") as f:
            return "\n".join(f.read().splitlines()[-n:])
    except OSError:
        return "(no supervisor log)"


# ---------------------------------------------------------------- the loop

def supervise(n: int, run=run_lead, sleep=time.sleep, repo: str = REPO, goal_done=goal1_done,
              met=goal_met_since, handed_off=handed_off_since) -> str:
    """Returns the final line it printed. `run`, `sleep` and the checks are injectable for tests."""
    failures, salvage_note = 0, ""
    while True:
        if os.path.exists(PAUSE):
            write_state(status="paused", lead_pid=None)
            sleep(60)
            continue
        if n == 1 and goal_done(repo):
            return finish("GOAL-DONE goal=1")
        sid, text, started, log, forced = run(n, salvage_note)
        salvage_note = ""
        if (n == 1 and goal_done(repo)) or (sid and met(sid, started)):
            return finish(f"GOAL-DONE goal={n}" if n == 1 else f"GOAL-MET goal={n} sid={sid} (verify by hand)")
        kind, detail = ("unrecognized", "stopped by the supervisor at its context limit") if forced \
            else classify_end(repo, sid, started, text, handed_off)
        note(f"end classified: {kind}: {detail}")
        if kind == "handoff":
            failures = 0
            continue
        if kind == "handoff-fallback":
            failures = 0
            note("WARNING: handoff accepted from the transcript only; HANDOFF.json was not valid")
            continue
        try:
            salvage_note = salvage(repo, sid)
        except RuntimeError as exc:
            alert("Build supervisor stopped", f"Could not return the checkout to the last pushed commit after "
                                              f"lead {sid}: {exc}\n" + log_tail(20))
            return finish(f"FAILING goal={n} salvage failed: {exc}")
        if salvage_note:
            note("salvaged: " + salvage_note)
        if forced:
            continue
        failures += 1
        if failures > MAX_RESTARTS:
            alert("Build supervisor gave up",
                  f"Goal {n}: {MAX_RESTARTS} fresh leads in a row ended without a handoff. Last lead {sid}, "
                  f"log {log}. Last supervisor log lines:\n" + log_tail(20))
            return finish(f"FAILING goal={n} last_sid={sid} log={log}")
        wait = BACKOFF_MIN[failures - 1]
        note(f"restart {failures} of {MAX_RESTARTS} in {wait} min (unrecognized end of {sid})")
        write_state(status=f"backoff {wait} min before restart {failures}")
        sleep(wait * 60)


def finish(line: str) -> str:
    note(line)
    write_state(status=line, lead_pid=None)
    print(line, flush=True)
    return line


def main() -> None:
    n = int(sys.argv[1])
    other = read_state().get("supervisor_pid")
    if other and other != os.getpid() and pid_alive(other):
        print(f"ANOTHER SUPERVISOR is running (pid {other}); not starting.")
        sys.exit(2)
    lead = read_state().get("lead_pid")
    if lead and pid_alive(lead):
        print(f"A LEAD is still running (pid {lead}); not starting a second one.")
        sys.exit(2)
    write_state(goal=n, status="starting", started=time.time())
    supervise(n)


if __name__ == "__main__":
    main()
