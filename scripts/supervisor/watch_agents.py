"""Live view of the build: the lead session and every agent it starts, as they work.

  python -I scripts/supervisor/watch_agents.py            follow from now (shows the last few steps of each first)
  python -I scripts/supervisor/watch_agents.py --text     only what they say, no tool calls
  python -I scripts/supervisor/watch_agents.py --back 20  show more history at start

Read-only: it only reads Claude Code's session records. Ctrl+C to stop.
Follows a handoff automatically (the supervisor rewrites .devdata/lead_session).
"""
import glob
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJ = os.path.expanduser("~/.claude/projects/s--Development-2026-shi-work-agt-trus-harness")
TEXT_ONLY = "--text" in sys.argv
BACK = int(sys.argv[sys.argv.index("--back") + 1]) if "--back" in sys.argv else 4
WIDTH = 160
COLORS = ["36", "33", "35", "32", "34", "91", "96", "93", "95", "92"]
os.system("")  # turns on ANSI colours in the Windows console


def lead_sid():
    try:
        return open(f"{REPO}/.devdata/lead_session", encoding="utf-8").read().strip()
    except OSError:
        return None


def one_line(s, n=WIDTH):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def local_time(stamp):
    from datetime import datetime
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except (AttributeError, ValueError):
        return "        "


def tool_summary(name, inp):
    if not isinstance(inp, dict):
        return one_line(inp)
    for key in ("command", "file_path", "pattern", "description", "to", "url", "query", "prompt"):
        if inp.get(key):
            return one_line(inp[key])
    return one_line(json.dumps(inp))


def events(line):
    try:
        d = json.loads(line)
    except ValueError:
        return
    m = d.get("message")
    if not isinstance(m, dict) or not isinstance(m.get("content"), list):
        return
    ts = local_time(d.get("timestamp"))
    for c in m["content"]:
        t = c.get("type")
        if d.get("type") == "assistant" and t == "text" and c.get("text", "").strip():
            yield ts, "says", one_line(c["text"], 400)
        elif TEXT_ONLY:
            continue
        elif t == "tool_use":
            yield ts, c.get("name"), tool_summary(c.get("name"), c.get("input"))
        elif t == "tool_result" and c.get("is_error"):
            body = c.get("content")
            if isinstance(body, list):
                body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
            yield ts, "ERROR", one_line(body)


class Tail:
    def __init__(self, path, label, color, back):
        self.path, self.label, self.color, self.pos = path, label, color, 0
        lines = self.read_new()
        shown = [e for ln in lines for e in events(ln)][-back:] if back else []
        for e in shown:
            self.show(*e)

    def read_new(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return []
        cut = data.rfind(b"\n") + 1  # leave a half-written last line for next time
        self.pos += cut
        return data[:cut].decode("utf-8", "replace").splitlines()

    def show(self, ts, kind, text):
        tag = f"\x1b[{self.color}m[{self.label}]\x1b[0m"
        if kind == "says":
            print(f"{ts} {tag} \x1b[1m{text}\x1b[0m")
        elif kind == "ERROR":
            print(f"{ts} {tag} \x1b[31merror: {text}\x1b[0m")
        else:
            print(f"{ts} {tag} \x1b[2m{kind}:\x1b[0m {text}")

    def poll(self):
        for ln in self.read_new():
            for e in events(ln):
                self.show(*e)


def label_for(meta_path, n):
    try:
        md = json.load(open(meta_path, encoding="utf-8"))
    except (OSError, ValueError):
        md = {}
    desc = str(md.get("description") or "").replace("[", "").replace("]", "")
    return f"{md.get('agentType') or 'agent'} {n}: {desc[:40]}"


def main():
    sid, tails = None, {}
    while True:
        new_sid = lead_sid()
        if new_sid and new_sid != sid:
            if sid:
                print(f"\n\x1b[1m=== handoff: new lead session {new_sid[:8]} ===\x1b[0m\n")
            sid, tails = new_sid, {}
            lead = f"{PROJ}/{sid}.jsonl"
            tails[lead] = Tail(lead, f"lead {sid[:8]}", "1;37", BACK)
        if sid:
            for meta in sorted(glob.glob(f"{PROJ}/{sid}/subagents/*.meta.json"), key=os.path.getmtime):
                log = meta.replace(".meta.json", ".jsonl")
                if log not in tails and os.path.exists(log):
                    n = len(tails)
                    fresh = time.time() - os.path.getmtime(log) < 600
                    tails[log] = Tail(log, label_for(meta, n), COLORS[n % len(COLORS)], BACK if fresh else 0)
            for t in tails.values():
                t.poll()
        sys.stdout.flush()
        time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
