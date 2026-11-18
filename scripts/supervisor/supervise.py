"""Runs one build goal in headless Claude Code lead sessions, one after another, until the goal is met.

  python -I scripts/supervisor/supervise.py <1|2>

Each lead gets goal N from docs/build/GOALS.md. A lead stops in one of three ways:

- **Handoff.** `scripts/handoff.py verify` passes for that lead: the branch's handoff record is committed and
  pushed on top of a clean tree. A fresh lead starts at once.
  - Fallback: if the repo record is missing but the transcript shows `HANDOFF COMPLETE` and the
    checkout is clean and pushed, that also counts as a handoff, and a warning is logged.
- **Goal met.** Goal 1's STATUS check passes, or the goal check recorded met=true for a lead that
  did not hand off (outcome A also records met=true, so a handoff never counts). The supervisor
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
CLAUDE = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~/AppData/Roaming")),
    "npm", "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe",
)
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
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(**kw) -> None:
    os.makedirs(SUPDIR, exist_ok=True)
    state = read_state()
    state.update(kw, supervisor_pid=os.getpid(), heartbeat=time.time())
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    # On Windows a reader (watchdog, watch_agents) holding state.json open makes the rename fail for
    # a moment. Retry; a state file one tick old is harmless, a crashed supervisor is not.
    for attempt in range(50):
        try:
            os.replace(tmp, STATE)
            return
        except PermissionError:
            time.sleep(0.1)
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} state.json locked by a reader; skipped this update", flush=True)


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


def api_error_since(sid: str, since: float) -> str:
    """The error kind of the lead's last API error message after `since` ("" if none)."""
    err = ""
    for d in _entries_since(sid, since):
        if d.get("type") == "assistant" and d.get("isApiErrorMessage"):
            err = str(d.get("error") or "")
    return err


def goal_met_since(sid: str, since: float) -> bool:
    """True only for a judged met. When Claude Code clears a goal after an unrecoverable error (an
    expired sign-in, 2026-11-18) it records goal_status met=true with sentinel=true: not a result."""
    met = False
    for d in _entries_since(sid, since):
        a = d.get("attachment")
        if isinstance(a, dict) and a.get("type") == "goal_status":
            met = bool(a.get("met")) and not a.get("sentinel")
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
            f"the checkout {repo}: " + "; ".join(notes) + ". Agent worktrees under .worktrees/ were not touched "
            "and may hold uncommitted work. Inspect the salvage and the worktrees, decide what to keep, and "
            "record what you decided in docs/build/DECISIONS.md before continuing from docs/build/HANDOFF.md.")


# ---------------------------------------------------------------- running a lead

class Lead:
    """One headless lead, started at once and watched by repeated step() calls."""

    def __init__(self, prompt: str, cwd: str, label: str, model: str = "opus", append_prompt: str = ""):
        os.makedirs(LOGS, exist_ok=True)
        self.cwd, self.label = cwd, label
        self.log = os.path.join(LOGS, f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.log")
        env = {k: v for k, v in os.environ.items() if k not in SCRUB}
        env["DISABLE_AUTO_COMPACT"] = "1"   # the owner wants handoffs, never compaction
        env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = str(8 * 3600 * 1000)  # default 10 min cuts agents off
        cmd = [CLAUDE, "-p", prompt, "--permission-mode", "auto", "--permission-prompts", "none",
               "--model", model, "--output-format", "stream-json", "--verbose", *EXTRA_FLAGS]
        if append_prompt:
            cmd += ["--append-system-prompt", append_prompt]
        self.started = time.time()
        with open(self.log, "w", encoding="utf-8") as out:
            self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL)
        self.pid = self.proc.pid
        self.sid, self.forced, self.peak, self.ctx, self._stale_alerted = None, False, 0, 0, 0.0
        note(f"started {label} model={model} pid={self.pid} cwd={cwd} log={self.log}"
             + (" (with salvage note)" if append_prompt else ""))

    def step(self) -> bool:
        """Check on the lead once. True while it is still running."""
        if self.proc.poll() is not None:
            return False
        self.sid = self.sid or session_id_from_log(self.log)
        if not self.sid:
            return True
        os.makedirs(os.path.join(self.cwd, ".devdata"), exist_ok=True)
        with open(os.path.join(self.cwd, ".devdata", "lead_session"), "w", encoding="utf-8") as f:
            f.write(self.sid)
        self.ctx = last_context(self.sid)
        self.peak = max(self.peak, self.ctx)
        if self.ctx >= FORCE_AT and agents_running(self.sid) == 0 and not git_busy(self.cwd):
            note(f"{self.label}: forcing a stop, lead context {self.ctx} >= {FORCE_AT}, no agent running")
            subprocess.run(["taskkill", "/PID", str(self.pid), "/T", "/F"], capture_output=True)
            self.forced = True
            self.proc.wait()
            return False
        idle_min = (time.time() - newest_activity(self.sid, self.log)) / 60
        if idle_min > STALE_MIN and time.time() - self._stale_alerted > STALE_MIN * 60:
            alert("Build lead stale", f"{self.label}: no activity for {idle_min:.0f} min. Lead {self.sid} "
                                      f"(pid {self.pid}) left running. Last log lines:\n" + log_tail(20))
            self._stale_alerted = time.time()
        return True

    def outcome(self) -> tuple[str | None, str, float, str, bool]:
        self.proc.wait()
        res = final_result(self.log)
        note(f"{self.label} ended sid={self.sid} peak_lead_ctx={self.peak} turns={res.get('num_turns')} "
             f"cost_usd={res.get('total_cost_usd')} exit={self.proc.returncode} forced={self.forced}")
        return self.sid, str(res.get("result") or ""), self.started, self.log, self.forced


def run_lead(n: int, append_prompt: str = "") -> tuple[str | None, str, float, str, bool]:
    """Start the goal-n lead in the main checkout and wait for it. Returns (sid, text, started, log, forced)."""
    lead = Lead(goal_text(n), REPO, f"goal{n}", "opus", append_prompt)
    write_state(goal=n, lead_pid=lead.pid, sid=None, lead_started=lead.started, status="running")
    while lead.step():
        write_state(sid=lead.sid, lead_ctx=lead.ctx)
        time.sleep(POLL)
    out = lead.outcome()
    write_state(lead_pid=None, status="between leads")
    return out


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
        if n == 1 and goal_done(repo):
            return finish("GOAL-DONE goal=1")
        kind, detail = ("unrecognized", "stopped by the supervisor at its context limit") if forced \
            else classify_end(repo, sid, started, text, handed_off)
        # The goal check also records met=true for outcome A (a handoff), so it proves the goal only
        # when the lead did not hand off (2026-11-17: goal 2's first handoff was read as GOAL-MET).
        if not kind.startswith("handoff") and sid and met(sid, started):
            return finish(f"GOAL-DONE goal={n}" if n == 1 else f"GOAL-MET goal={n} sid={sid} (verify by hand)")
        note(f"end classified: {kind}: {detail}")
        if not kind.startswith("handoff") and sid and api_error_since(sid, started) == "authentication_failed":
            alert("Build lead could not sign in",
                  f"Goal {n}: lead {sid} stopped because its Claude sign-in expired. Run `claude auth login` "
                  "(or `claude setup-token` and set CLAUDE_CODE_OAUTH_TOKEN for unattended runs). The "
                  "supervisor keeps restarting with backoff.")
        if kind == "handoff":
            failures = 0
            continue
        if kind == "handoff-fallback":
            failures = 0
            note("WARNING: handoff accepted from the transcript only; the handoff record was not valid")
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


# ---------------------------------------------------------------- parallel units (charter section 11)

def load_units(repo: str = REPO) -> list[dict]:
    """The unit list as origin/main has it (leads mark their unit done there when they merge)."""
    git(repo, "fetch", "--quiet", "origin")
    shown = git(repo, "show", "origin/main:docs/build/units.json")
    return json.loads(shown.stdout).get("units", []) if shown.returncode == 0 else []


def unit_prompt(u: dict, worktree: str, repo: str = REPO) -> str:
    text = goal_text(3, repo)
    for key, value in (("<<id>>", u["id"]), ("<<title>>", u["title"]), ("<<branch>>", u["branch"]),
                       ("<<worktree>>", worktree)):
        text = text.replace(key, value)
    return text


def _has_ref(repo: str, ref: str) -> bool:
    return bool(git_out(repo, "rev-parse", "--verify", "--quiet", ref))


def _worktree_branch(path: str) -> str:
    """The branch checked out in a worktree, or "" if the path is not one (or HEAD is detached)."""
    top = git_out(path, "rev-parse", "--show-toplevel")
    try:
        own = bool(top) and os.path.samefile(top, path)
    except OSError:
        own = False
    if not own:
        return ""  # not a worktree of its own (a stray folder inside the main checkout, say)
    r = git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else ""


def unit_worktree(u: dict, repo: str = REPO) -> str:
    """The worktree holding the unit's branch, created (with the branch, pushed) if needed."""
    branch, current, found = u["branch"], None, None
    for line in git_out(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            found = current
    if found:
        return found.replace("\\", "/")
    # A lead may have moved its worktree to another branch (app-2 -> app-2-merge-wip, 2026-11-17).
    # Its handoff names that branch, so the next lead continues there rather than failing to add
    # a second worktree at the same path.
    path = f"{repo}/.worktrees/{u['id']}"
    if os.path.isdir(path) and _worktree_branch(path):
        return path
    if not _has_ref(repo, f"refs/heads/{branch}"):
        start = f"origin/{branch}" if _has_ref(repo, f"refs/remotes/origin/{branch}") else "origin/main"
        if git(repo, "branch", branch, start).returncode != 0:
            raise RuntimeError(f"cannot create branch {branch} from {start}")
    if not _has_ref(repo, f"refs/remotes/origin/{branch}"):
        r = git(repo, "push", "--quiet", "-u", "origin", branch)
        if r.returncode != 0:
            raise RuntimeError(f"cannot push new branch {branch}: {r.stderr.strip()}")
    path = f"{repo}/.worktrees/{u['id']}"
    r = git(repo, "worktree", "add", "--quiet", path, branch)
    if r.returncode != 0:
        raise RuntimeError(f"cannot add worktree {path}: {r.stderr.strip()}")
    git(path, "branch", "--quiet", "--set-upstream-to", f"origin/{branch}")
    # The worktree's own database settings; fails harmlessly while the cluster is down.
    subprocess.run([sys.executable, "scripts/dev_db.py", "env"], cwd=path, capture_output=True)
    return path


def take_resources(u: dict, repo: str = REPO) -> bool:
    import unit as unitlib
    from pathlib import Path
    for r in u.get("resources", []):
        h = unitlib.holder(Path(repo), r)
        if h and h.get("unit") != u["id"]:
            return False
    return all(unitlib.lock(Path(repo), r, u["id"])[0] for r in u.get("resources", []))


def free_resources(uid: str, repo: str = REPO) -> list[str]:
    import unit as unitlib
    from pathlib import Path
    return unitlib.unlock_all(Path(repo), uid)


def start_unit(u: dict, salvage_note: str):
    path = unit_worktree(u)
    branch = _worktree_branch(path) or u["branch"]
    if branch != u["branch"]:
        note(f"unit {u['id']}: worktree is on branch {branch} (left there by the last lead); continuing on it")
    return Lead(unit_prompt({**u, "branch": branch}, path), path, f"unit-{u['id']}", u.get("lead_model", "opus"), salvage_note)


def end_unit(u: dict, lead, outcome) -> tuple[str, str]:
    sid, text, started, _log, forced = outcome
    if not forced:
        kind, detail = classify_end(lead.cwd, sid, started, text)
        note(f"unit {u['id']} end classified: {kind}: {detail}")
        if kind.startswith("handoff"):
            return kind, ""
    return "unrecognized", salvage(lead.cwd, sid, u["branch"])


def run_final() -> str:
    if git_out(REPO, "status", "--porcelain") == "":
        git(REPO, "merge", "--quiet", "--ff-only", "origin/main")
    note("all units done: starting the goal 1 final lead in the main checkout")
    return supervise(1)


def supervise_parallel(slots_max: int, *, units=load_units, start=start_unit, end=end_unit,
                       take=take_resources, free=free_resources, final=run_final,
                       sleep=time.sleep, now=time.time) -> str:
    """Run ready units, at most slots_max at a time. Every collaborator is injectable for tests."""
    running: dict = {}
    failures: dict = {}
    wait_until: dict = {}
    notes_for: dict = {}
    gave_up: set = set()
    owner_alerted = ""
    while True:
        # 1. Check on running leads.
        for uid, (u, lead) in list(running.items()):
            if lead.step():
                continue
            del running[uid]
            outcome = lead.outcome()
            if {x["id"]: x.get("status") for x in units()}.get(uid) == "done":
                freed = free(uid)
                failures[uid] = 0
                note(f"unit {uid} done; freed {freed}")
                continue
            kind, salvage_note = end(u, lead, outcome)
            if kind.startswith("handoff"):
                failures[uid] = 0
                continue
            notes_for[uid] = salvage_note
            if outcome[4]:          # stopped by the supervisor at its context limit
                continue
            failures[uid] = failures.get(uid, 0) + 1
            if failures[uid] > MAX_RESTARTS:
                gave_up.add(uid)
                free(uid)
                alert("Build unit failing", f"Unit {uid}: {MAX_RESTARTS} fresh leads in a row ended without a "
                                            f"handoff. Last lead {outcome[0]}, log {outcome[3]}. Other units go on."
                                            "\nLast supervisor log lines:\n" + log_tail(20))
                continue
            wait_until[uid] = now() + BACKOFF_MIN[failures[uid] - 1] * 60
            note(f"unit {uid}: restart {failures[uid]} of {MAX_RESTARTS} after {BACKOFF_MIN[failures[uid] - 1]} min")
        # 2. Everything done except the final unit: run it alone.
        us = units()
        rest = [u for u in us if not u.get("final")]
        status = {u["id"]: u.get("status") for u in us}
        if rest and not running and all(u.get("status") == "done" for u in rest):
            return final()
        # 3. Start what is ready.
        launched = False
        if not os.path.exists(PAUSE):
            for u in rest:
                if len(running) >= slots_max:
                    break
                uid = u["id"]
                if uid in running or uid in gave_up or u.get("status") in ("done", "waiting_owner"):
                    continue
                if any(status.get(d) != "done" for d in u.get("depends_on", [])):
                    continue
                if wait_until.get(uid, 0) > now():
                    continue
                if not take(u):
                    continue
                try:
                    running[uid] = (u, start(u, notes_for.pop(uid, "")))
                    launched = True
                except RuntimeError as exc:
                    free(uid)
                    failures[uid] = failures.get(uid, 0) + 1
                    wait_until[uid] = now() + BACKOFF_MIN[min(failures[uid], MAX_RESTARTS) - 1] * 60
                    alert("Build unit could not start", f"Unit {uid}: {exc}")
        # 4. Nothing running and nothing about to run: say why.
        if not running and not launched and not os.path.exists(PAUSE) and \
                not any(t > now() for t in wait_until.values()):
            pending = [u["id"] for u in rest if u.get("status") != "done"]
            if gave_up:
                alert("Build supervisor stopped", f"No unit can run. Failing: {sorted(gave_up)}. "
                                                  f"Not done: {pending}.\n" + log_tail(20))
                return finish(f"FAILING parallel failing={sorted(gave_up)} pending={pending}")
            waiting = sorted(u["id"] for u in rest if u.get("status") == "waiting_owner")
            msg = f"waiting on the owner: {waiting}; blocked behind them: {sorted(set(pending) - set(waiting))}"
            if msg != owner_alerted:
                alert("Build waiting on you", msg)
                owner_alerted = msg
        write_state(slots={uid: {"pid": lead.pid, "sid": lead.sid} for uid, (_, lead) in running.items()},
                    status=f"parallel: running {sorted(running)}", lead_pid=None)
        sleep(POLL)


def finish(line: str) -> str:
    note(line)
    write_state(status=line, lead_pid=None)
    print(line, flush=True)
    return line


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Run one build goal in headless lead sessions.")
    ap.add_argument("goal", type=int, choices=(1, 2))
    ap.add_argument("--parallel", type=int, default=0, metavar="N",
                    help="goal 1 only: run up to N units from docs/build/units.json at once (max 3)")
    args = ap.parse_args()
    state = read_state()
    other = state.get("supervisor_pid")
    if other and other != os.getpid() and pid_alive(other):
        print(f"ANOTHER SUPERVISOR is running (pid {other}); not starting.")
        sys.exit(2)
    leads = [state.get("lead_pid")] + [s.get("pid") for s in (state.get("slots") or {}).values()]
    alive = [p for p in leads if p and pid_alive(p)]
    if alive:
        print(f"A LEAD is still running (pid {alive}); not starting another.")
        sys.exit(2)
    write_state(goal=args.goal, status="starting", started=time.time(), slots={}, lead_pid=None)
    if args.parallel and args.goal == 1:
        supervise_parallel(min(args.parallel, 3))
    else:
        supervise(args.goal)


if __name__ == "__main__":
    main()
