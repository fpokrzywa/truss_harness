# 140 (#293, PR #303) isolation review 1: BLOCKED (2026-11-23, isolation-reviewer opus)

Sent back as `factory/bin/job feedback 293` (feedback-1, round 3). Findings:
1. (blocker) `docker/source.py:272` (+ :87, :93): compose project, image tag and `/etc/shifde-apps/{project}.env` come from the client-chosen project name; client B deploying first with A's project name loads A's env file. Fix: names from the target id; test B-deploys-first.
2. `docker/fake.py:71`, `:232`: fake runs bundle `app/app.py` in-process with no dev/test guard.
3. `source.py:228`, `runtime.py:56-57,:146`: refuse `$` in compose.yaml; set `DOCKER_CONFIG` to an adapter-owned empty dir.
4. `adapter.py:367-371`: observe must check daemon and project binding; fake `http_get_local` keyed by project.
Accepted: `artifacts` keyword (SP8-9: adapter per job, tenant-checked store), Compose major >= 2, pinned BASE_IMAGE digest.
