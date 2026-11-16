# Build supervisor

The supervisor runs the unattended build. It runs one goal from `docs/build/GOALS.md` in headless Claude Code lead sessions, one after another, until that goal is done. When a lead reaches the 300k context line in charter section 10, it hands off and the supervisor starts a fresh lead.

## Run it

```
python -I scripts/supervisor/supervise.py 1           # goal 1
python -I scripts/supervisor/supervise.py 2           # goal 2, after goal 1 is verified
python -I scripts/supervisor/supervise.py 1 <sid>     # resume a lead that was cut off
```

- Run it in the background from a supervising Claude session that reacts to the one line the supervisor prints when it exits: `GOAL-DONE`, `GOAL-ENDED`, `STALE` or `FAILING`. The docstring in `supervise.py` explains each one.
- Never run two supervisors at once.
- Never run the supervisor while another lead is running.

## Watch it

```
python -I scripts/supervisor/watch_agents.py            # live feed: lead + agents
python -I scripts/supervisor/watch_agents.py --text     # only what they say
```

- The supervisor's own log is `.devdata/supervisor/logs/supervisor.log`.
- Each lead gets one stream-json log in `.devdata/supervisor/logs/`.

## Pause it

1. Stop the supervisor's python process only. Do not stop its child process tree.
2. Let the running lead finish its handoff.

`docs/build/HANDOFF-69788680-c14f-42b4-b596-e6677a8d7b47.md` records the last pause and how to restart from it.
