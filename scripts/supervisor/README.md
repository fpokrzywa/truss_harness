# Build supervisor

The supervisor runs the unattended build. It runs one goal from `docs/build/GOALS.md` in headless Claude Code lead sessions, one after another, until that goal is met.

## How a lead ends

| How it ends | How it's recognized | What the supervisor does |
|---|---|---|
| Handoff | `python scripts/handoff.py verify` passes: the branch's record `docs/build/handoffs/<branch>.json` is committed and pushed on a clean tree, for that session | Starts a fresh lead at once |
| Handoff, fallback | The transcript says `HANDOFF COMPLETE` and the checkout is clean and pushed, but there is no valid record | Same as a handoff, and logs a warning |
| Goal met | Goal 1's STATUS row and main == origin/main, or the goal check recorded met | Prints `GOAL-DONE` / `GOAL-MET` and exits, for a person or session to verify |
| Anything else | A crash, a forced turn end, usage limits | Salvages the checkout (stash plus a `salvage/...` branch), returns main to the last pushed commit, backs off 1, 2, 4, 8, 16 minutes, then starts a fresh lead told what was salvaged. After 5 failed restarts in a row it alerts and exits `FAILING` |

The goal condition puts the handoff outcome first, so the goal check accepts a handoff on its first evaluation. Before this change it blocked every handoff until Claude Code's cap of 9 blocks ended the turn.

## Run it

Run each in the background from a supervising session, from the repo root:

```
python -I scripts/supervisor/supervise.py 1      # goal 1
python -I scripts/supervisor/supervise.py 1 --parallel 3   # goal 1, up to 3 units at once (charter section 11)
python -I scripts/supervisor/watchdog.py         # alerts when no lead runs or no commit lands for 30 min
```

- A second supervisor refuses to start, and so does a supervisor while a lead is still running. Both use `.devdata/supervisor/state.json`.
- Alerts are a Windows toast, a line in `.devdata/supervisor/logs/ALERTS.log`, and a stdout line starting `ALERT`. Each alert includes the last lead session id and the last 20 lines of the supervisor log.

## Parallel mode

In parallel mode the supervisor runs the units listed in `docs/build/units.json` (charter section 11):
- It runs up to 3 units at once, each with its own lead, branch, worktree and database.
- A unit starts only once its dependencies are done, and never alongside another unit that declares one of the same resources.
- It does not start units marked `waiting_owner`, or the units behind them, and it alerts you once with the list.
- Handoffs, salvage and backoff work per unit. A unit that fails 5 restarts is given up, and the other units continue.
- Units merge one at a time, under the merge lock, after 13/13 gates on the branch rebased onto main.
- When every unit is done, the final lead runs alone in the main checkout with goal 1's own condition.

Locks: `python scripts/unit.py locks` lists them; they live in `.devdata/supervisor/locks/`.

## Pause it

```
echo pause > .devdata/supervisor/PAUSE     # no new lead starts; the watchdog goes quiet
del .devdata\supervisor\PAUSE              # resume
```

A pause never stops a running lead. To stop one early, ask it to hand off.

## Watch it

```
python -I scripts/supervisor/watch_agents.py            # live feed: lead + agents
python -I scripts/supervisor/watch_agents.py --text     # only what they say
```

The supervisor's own log is `.devdata/supervisor/logs/supervisor.log`, with one stream-json log per lead next to it.

## Tests

`python -m unittest discover -s scripts/tests` runs `test_supervisor.py`. It covers a clean handoff, a forced turn end with empty result text, a crash mid-unit with a dirty tree and an unpushed commit, backoff and the alert after 5 restarts, the pause file, and the watchdog.
