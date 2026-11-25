# Process control (runbook)

SETH runs as three processes, `shifde-api`, `shifde-worker` and `shifde-executor`, plus the Next
server for the front end. This runbook is the contract a supervisor needs to run them: exit codes,
health endpoints, stop signals and graces, and the restart policy. `scripts/procctl.py` is a
reference supervisor that follows it on one machine (standard library only, not product code). A
production host maps the same contract onto its own supervisor once the host is known (design
`docs/superpowers/specs/2026-11-21-subproject-4-go-live-design.md` section 6.2).

`scripts/supervisor/` is the build supervisor of this repository and has nothing to do with this.

## Exit codes

The same codes for all three processes:

| Code | Meaning | What a supervisor does |
|---|---|---|
| 0 | stopped on request | nothing; if nobody asked for the stop, find out who stopped it before starting it again |
| 1 | crashed (also: worker at the abandoned-thread cap, executor grace passed with a job in flight) | restart, with backoff |
| 2 | configuration refused (a setting failed validation, a health port that cannot be bound) | do not restart; fix the configuration |
| 3 | unsafe database role (`SHIFDE_DATABASE_URL` is privileged or cannot be checked) | do not restart; fix the role |
| 4 | schema revision mismatch (worker: database not at the code's head) | do not restart; run the migrations or deploy the matching code |
| 5 | cannot authenticate (executor: `whoami` answered 401 or 403) | do not restart; issue a new executor credential |

`shifde-api` exits 0 on SIGTERM, SIGINT or Windows `CTRL_BREAK_EVENT` once it has drained (a stop
received during start-up exits 0 too); its 3 comes only from the start-up role check.

Any other code (a signal, a hard kill: -9 on POSIX, 15 left by TerminateProcess on Windows) is a
crash and is treated like 1.

## Per process

| | `shifde-api` | `shifde-worker` | `shifde-executor` | web (Next) |
|---|---|---|---|---|
| Liveness | `GET /healthz` on `SHIFDE_API_HOST:SHIFDE_API_PORT` (default `127.0.0.1:8710`), no database | `GET /healthz` on `SHIFDE_WORKER_HEALTH_HOST:SHIFDE_WORKER_HEALTH_PORT` (default host 127.0.0.1, port 0 = off): 200 while the heartbeat loop ticks | `GET /healthz` on `SHIFDE_EXECUTOR_HEALTH_HOST:SHIFDE_EXECUTOR_HEALTH_PORT` (port 0 = off): 200 while the run loop ticks | the process is running |
| Readiness | `GET /api/ready` (503 when the database or schema is not ready) | `GET /readyz`: database reachable, a recent claim tick, no stop requested | `GET /readyz`: a recent successful gateway call, no stop requested | `GET /` answers below 400 (a redirect to sign-in counts) |
| Stop signal | SIGTERM / SIGINT; Windows `CTRL_BREAK_EVENT` | SIGTERM / SIGINT; Windows `CTRL_BREAK_EVENT` | SIGTERM / SIGINT; Windows `CTRL_BREAK_EVENT` | SIGTERM; Windows `CTRL_BREAK_EVENT` |
| Grace | `SHIFDE_API_SHUTDOWN_GRACE_SECONDS` (30): open requests, passed to uvicorn | `SHIFDE_WORKER_SHUTDOWN_GRACE_SECONDS` (30): running runs are handed back | `SHIFDE_EXECUTOR_SHUTDOWN_GRACE_SECONDS` (60): the step in flight finishes | 10 s (procctl) |
| Start checks | settings (2), database role (3) | settings (2), database role (3), schema at head (4), health port bound (2) | settings (2), health port bound (2), `whoami` 401/403 (5); other gateway errors are retried | `pnpm build` must succeed |

A health probe is ready only on a 2xx answer within a short timeout (procctl: 2 s). SETH never
cuts a platform write mid-call: an executor that passes its grace with a write in flight exits 1
and the write is left to the unknown-outcome path.

A `worker.sweep_still_running` warning (`worker_id`, `elapsed_seconds`) with a "stale"
`reaper.status` in platform health (`GET /api/v1/platform/health-details`) means the client sweep of
that worker is stuck: no client finished for twice the lease, at least 60 s (`elapsed_seconds`
counts from the last finished one). A long sweep that progresses does not log it. The worker stays
ready and keeps claiming and reaping leases. It only stops recording its reaps, until the sweep
progresses or ends. With several workers a healthy one keeps the status "ok", so the log line is the
per-worker signal.

Only one worker sweeps at a time (a PostgreSQL session advisory lock). A worker whose sweep comes
due meanwhile skips it, logs nothing and tries again at its next reap. A sweeping worker uses one
extra database connection, outside its pool, while it sweeps (another worker opens one for a moment
to try the lock); like LISTEN, it needs `SHIFDE_DATABASE_URL` to be a direct or session-pooled
connection. A `worker.sweep_lock_lost` warning (`worker_id`) means the server ended that connection
during the sweep: it was idle for the bound above because a client's step was stuck, it was
terminated, or the database restarted. The worker ends its sweep after the step in progress; another
worker, or its own next reap, sweeps again. When the connection cannot be opened, nothing is swept,
`worker.sweep_failed` is logged and the next reap tries again.

## Restart policy

- Restart on 1 (and any code outside 0 and 2-5) after `min(2**n, 60)` seconds for the n-th
  consecutive crash, n from 0: 1, 2, 4, 8, 16, 32, 60, 60, ... seconds.
- n starts again at 0 when the process had been up at least 60 s before it exited.
- Never restart on 2, 3, 4 or 5: a restart cannot fix them. procctl stops the other processes and
  exits with that code.
- Do not restart an exit 0 that the supervisor did not ask for: someone stopped the process on
  purpose. procctl stops the others and exits 1.
- A process not ready within 120 s of its start stops procctl with exit 1.

## procctl

Needs the backend venv (`uv sync` in `backend`), and for `--with-web` pnpm with the front end
installed (`pnpm install` in `frontend`). Commands, from the repository root:

    python scripts/procctl.py up --env-file F [--with-executor] [--with-web] [--state-dir D]
    python scripts/procctl.py down [--state-dir D]
    python scripts/procctl.py status [--state-dir D]

- **`up`** runs in the foreground. It starts api, worker, executor (with `--with-executor`) and
  web (with `--with-web`: `pnpm build`, then `pnpm exec next start -p <SHIFDE_WEB_PORT> -H
  127.0.0.1`) in that order, each once the previous one is ready, and supervises them by the
  policy above. It prints one line per event (started, ready, exited, restarting in N s,
  stopping, stopped, killed after grace) and never a value from the env file. It stops on
  `down`, Ctrl+C, SIGTERM or SIGBREAK: it signals every child gracefully (newest first), waits up
  to each child's grace plus 5 s and hard-kills what is left.
  Exit codes: 0 stopped on request; 2 refused to start (bad or missing env file, missing health
  port, missing install, already running) or a child exited 2; 3, 4, 5 a child exited with that
  code; 1 otherwise (a child not ready in time, an unrequested exit 0, a failed `pnpm build`).
- **`down`** writes the stop file and waits for `up` to exit, up to the largest grace plus 15 s.
  If `up` is still running after that, or is gone and left children behind, `down` hard-kills
  them and says which. Exit 0 when nothing recorded is still running (also `procctl: not
  running`), 1 otherwise.
- **`status`** prints `<name> pid=<pid|-> live=<yes|no> ready=<yes|no> restarts=<n>` per child,
  probing the recorded URLs itself. Exit 0 when every child is live and ready, 1 otherwise or
  when `up` is not running.

**The env file.** UTF-8 (a BOM is fine), one `KEY=VALUE` per line; blank lines and `#` lines are
skipped and one pair of quotes around a value is removed. The children get procctl's environment
overlaid with these values. `up` refuses without:

- `SHIFDE_WORKER_HEALTH_PORT`, not 0 (readiness is probed there);
- `SHIFDE_EXECUTOR_HEALTH_PORT`, not 0, with `--with-executor`.

`SHIFDE_WEB_PORT` (default 3710, the port of `pnpm start` and of the default
`SHIFDE_ALLOWED_ORIGINS`) is read by procctl only. For a local run, `SHIFDE_ENV=development`
lets the processes fall back to this checkout's `.devdata/dev.env`.

**The state directory** (default `.devdata/procctl/`, gitignored) holds:

- `state.json`: procctl's PID and, per child, its PID, health URLs, start time, restart count,
  last exit code and grace; written atomically, removed when `up` ends;
- `stop`: written by `down`, removed by `up`;
- `<name>.log`: each child's stdout and stderr, appended (no rotation).

A second `up` on the same state directory refuses while the first is running.

## Mapping onto a host supervisor

procctl is a reference, not a deployment. When the host is chosen, its own supervisor (a service
manager, a container orchestrator) runs the same contract. Nothing here ships unit files, service
definitions or templates; write them for the host from this table.

| Contract | What the host supervisor is set to do |
|---|---|
| Restart | restart on exit 1 (and on a signal or kill) only, with backoff up to about 60 s |
| No restart | treat 2, 3, 4 and 5 as permanent failures: stop restarting and alert; a clean 0 is a normal stop |
| Liveness | probe the liveness endpoint; restart the process when it fails repeatedly |
| Readiness | send traffic (api, web) or count the process as up (worker, executor) only while readiness answers 2xx (web: below 400) |
| Stop | send the stop signal, wait the process's grace (plus a margin), then hard-kill |
| Windows | start each process in its own process group and stop it with `CTRL_BREAK_EVENT`; Ctrl+C does not reach a new process group |
| Health ports | set `SHIFDE_WORKER_HEALTH_PORT` and `SHIFDE_EXECUTOR_HEALTH_PORT`; keep them on loopback or a private interface |
| Order | start the API before the executor (the executor's readiness needs the API); stop in the reverse order |

A hard kill must reach the real process. On Windows the venv console scripts are launchers that
start the interpreter as a child; they end it with themselves, so killing the recorded PID is
enough. A host that wraps the processes in another launcher checks the same.
