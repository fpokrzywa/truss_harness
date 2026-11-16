"""Runs one build goal in headless Claude Code sessions, one after another, until the goal ends.

  python -I scripts/supervisor/supervise.py <1|2> [session id to resume]

Each session gets the goal from docs/build/GOALS.md plus a clause that lets it end early by
handing off (charter section 10). This script:
  - starts a fresh session whenever the last one handed off or crashed;
  - forces a handoff (stops the session at a safe moment) if the lead's context passes HARD_AT;
  - prints ONE final line and exits when the supervising session must look:
      GOAL-DONE     goal 1's deterministic check passes
      GOAL-ENDED    the session ended normally without a handoff (verify before moving on)
      STALE         nothing has happened for STALE_MIN minutes (the session is left running)
      FAILING       three sessions in a row died within five minutes
Never run two of these at once, or alongside another live build session.
"""
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE)).replace("\\", "/")
PROJ = "C:/Users/FreddiePokrzywa/.claude/projects/s--Development-2026-shi-work-agt-trus-harness"
CLAUDE = "C:/Users/FreddiePokrzywa/AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
LOGS = os.path.join(REPO, ".devdata", "supervisor", "logs")  # gitignored
HARD_AT = 450_000      # lead context, tokens: force a handoff at the next safe moment
STALE_MIN = 120
POLL = 30
# Load no MCP connectors: the build needs none, and they cost ~35k tokens of context per lead.
EXTRA_FLAGS = ["--strict-mcp-config", "--mcp-config", os.path.join(HERE, "empty-mcp.json")]
HANDOFF_CLAUSE = (" This session also ends correctly, and the goal counts as met for this session,"
                  " when you have completed a handoff exactly as charter section 10 describes and"
                  " your final message begins with the line HANDOFF COMPLETE.")
SCRUB = ["CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_CHILD_SESSION",
         "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET",
         "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED",
         "CLAUDE_CODE_QUESTION_PREVIEW_FORMAT", "CLAUDE_CODE_STARTUP_FAILURE_RESULTS",
         "CLAUDE_CODE_EMIT_STARTUP_TIMING", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
         "CLAUDE_CODE_ENABLE_TASKS"]


def note(msg):
    os.makedirs(LOGS, exist_ok=True)
    with open(os.path.join(LOGS, "supervisor.log"), "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def goal_text(n):
    import re
    text = open(f"{REPO}/docs/build/GOALS.md", encoding="utf-8").read()
    goals = re.findall(r"```\n(/goal .*?)\n```", text, flags=re.S)
    return goals[n - 1] + HANDOFF_CLAUSE


def last_context(path, tail_bytes=600_000):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message")
        if d.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict):
            u = m["usage"]
            return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (
                u.get("cache_creation_input_tokens") or 0)
    return 0


def agent_files(sid):
    return glob.glob(f"{PROJ}/{sid}/subagents/*.meta.json")


def running_agents(sid, fresh_min=180):
    n, now = 0, time.time()
    for meta in agent_files(sid):
        try:
            finished = json.load(open(meta, encoding="utf-8")).get("finished")
        except (OSError, ValueError):
            finished = None
        log = meta.replace(".meta.json", ".jsonl")
        if finished is not True and os.path.exists(log) and now - os.path.getmtime(log) < fresh_min * 60:
            n += 1
    return n


def newest_activity(sid, log):
    paths = [f"{PROJ}/{sid}.jsonl", log] + glob.glob(f"{PROJ}/{sid}/subagents/*.jsonl")
    times = [os.path.getmtime(p) for p in paths if os.path.exists(p)]
    return max(times) if times else time.time()


def git(*args):
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True).stdout.strip()


def git_busy():
    return any(os.path.exists(f"{REPO}/.git/{n}") for n in ("index.lock", "MERGE_HEAD", "rebase-merge"))


def goal1_done():
    try:
        status = open(f"{REPO}/docs/build/STATUS.md", encoding="utf-8").read().lower()
    except OSError:
        return False
    row = any(l.startswith("| software factory install") and l.split("|")[2].strip() == "done"
              for l in status.splitlines())
    main = git("rev-parse", "main")
    return bool(row and main and main == git("rev-parse", "origin/main"))


def session_id(log):
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


def final_result(log):
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


def agent_summary(sid):
    rows = []
    for meta in agent_files(sid):
        try:
            md = json.load(open(meta, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        models, peak = set(), 0
        try:
            with open(meta.replace(".meta.json", ".jsonl"), encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    m = d.get("message")
                    if d.get("type") == "assistant" and isinstance(m, dict) and isinstance(m.get("usage"), dict):
                        u = m["usage"]
                        models.add(m.get("model"))
                        peak = max(peak, (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                                   + (u.get("cache_creation_input_tokens") or 0))
        except OSError:
            pass
        rows.append(f"{md.get('agentType')} | {','.join(sorted(x for x in models if x))} | peak_ctx={peak} | "
                    f"{str(md.get('description'))[:70]}")
    return rows


def handed_off_since(sid, since, tail_bytes=3_000_000):
    """True if the lead wrote a message starting HANDOFF COMPLETE after `since` (epoch seconds).

    The final result text is not enough: when the goal's stop check keeps blocking a handoff,
    Claude Code ends the turn itself after 9 blocks and the result comes back empty.
    """
    from datetime import datetime
    path = f"{PROJ}/{sid}.jsonl"
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            d = json.loads(line)
            m = d.get("message")
            if d.get("type") != "assistant" or not isinstance(m, dict):
                continue
            ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
        except (ValueError, KeyError, AttributeError, TypeError):
            continue
        if ts < since:
            continue
        for c in m.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text" and \
                    c.get("text", "").lstrip().startswith("HANDOFF COMPLETE"):
                return True
    return False


RESUME_PROMPT = (
    "This headless session stopped and has been resumed by the supervising process; the goal is still"
    " active. Any agent that was running when it stopped was cut off, possibly leaving uncommitted work"
    " in its worktree. Check what it left with git, then continue toward the goal as the charter"
    " describes (a fresh agent on the same branch, given what is already there).")


def run_once(n, resume_sid=None):
    os.makedirs(LOGS, exist_ok=True)
    log = os.path.join(LOGS, f"goal{n}-{time.strftime('%Y%m%d-%H%M%S')}.log")
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    # The owner does not want automatic compaction. Sessions hand off instead (charter section 10);
    # if one ever reached the limit it would stop with an error and a fresh session would start.
    env["DISABLE_AUTO_COMPACT"] = "1"
    # Headless runs stop waiting for background agents after 10 minutes by default and exit,
    # cutting the agent off. Builders and test suites run far longer than that.
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = str(8 * 3600 * 1000)
    prompt = RESUME_PROMPT if resume_sid else goal_text(n)
    cmd = [CLAUDE, "-p", prompt, "--permission-mode", "auto", "--permission-prompts", "none",
           "--model", "opus", "--output-format", "stream-json", "--verbose", *EXTRA_FLAGS]
    if resume_sid:
        cmd += ["--resume", resume_sid]
    started = time.time()
    with open(log, "w", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
    note(f"started goal {n} pid={proc.pid} resume={resume_sid} log={log}")
    sid, reason, peak = resume_sid, "ended", 0
    while proc.poll() is None:
        time.sleep(POLL)
        sid = sid or session_id(log)
        if not sid:
            continue
        os.makedirs(f"{REPO}/.devdata", exist_ok=True)
        open(f"{REPO}/.devdata/lead_session", "w", encoding="utf-8").write(sid)
        ctx = last_context(f"{PROJ}/{sid}.jsonl")
        peak = max(peak, ctx)
        if ctx >= HARD_AT and running_agents(sid) == 0 and not git_busy():
            note(f"forcing handoff: lead context {ctx} >= {HARD_AT}, no agent running")
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            reason = "forced"
            break
        if (time.time() - newest_activity(sid, log)) / 60 > STALE_MIN:
            return "stale", f"pid={proc.pid} sid={sid} lead_ctx={ctx}", time.time() - started, log
    proc.wait()
    res = final_result(log)
    text = str(res.get("result") or "")
    if reason != "forced":
        if "HANDOFF COMPLETE" in text[:400] or (sid and handed_off_since(sid, started)):
            reason = "handoff"
        elif res.get("is_error") or not res:
            reason = "error"
    note(f"session ended reason={reason} sid={sid} peak_lead_ctx={peak} turns={res.get('num_turns')} "
         f"cost_usd={res.get('total_cost_usd')} exit={proc.returncode}")
    for row in agent_summary(sid) if sid else []:
        note("  agent: " + row)
    return reason, text, time.time() - started, log, sid


def main():
    n = int(sys.argv[1])
    resume_sid = sys.argv[2] if len(sys.argv) > 2 else None
    quick, resumes = 0, 0
    while True:
        if n == 1 and goal1_done():
            print("GOAL-DONE goal=1")
            return
        reason, text, seconds, log, sid = run_once(n, resume_sid)
        resume_sid = None
        if reason == "stale":
            print(f"STALE goal={n} {text} log={log}")
            return
        if reason in ("handoff", "forced"):
            quick, resumes = 0, 0
            continue
        if reason == "ended":
            if n == 1 and goal1_done():
                print("GOAL-DONE goal=1")
                return
            # Ended without meeting the goal or handing off: resume the same session up to twice,
            # so the lead keeps its context, before asking the supervising session to look.
            # Never resume a lead that is already at the charter's handoff line: start fresh.
            if sid and last_context(f"{PROJ}/{sid}.jsonl") >= 300_000:
                note(f"{sid} ended at its handoff line without a handoff message; starting fresh")
                quick, resumes = 0, 0
                continue
            if sid and resumes < 2:
                resumes += 1
                resume_sid = sid
                note(f"resuming {sid} (resume {resumes} of 2)")
                continue
            print(f"GOAL-ENDED goal={n} log={log} :: {text[:1500]}")
            return
        quick = quick + 1 if seconds < 300 else 0
        if quick >= 3:
            print(f"FAILING goal={n} log={log} :: {text[:800]}")
            return


if __name__ == "__main__":
    main()
