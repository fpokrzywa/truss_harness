# Unit two-org-proof

Two-organization side-by-side proof on the local cluster (S-002, seed `dev-seed`) and the known-defects refresh in `STATUS.md`.

## Done

- `scripts/two_org_proof.py` (standard library): logs every `dev-seed` person in over HTTP against a running `shifde-api` and checks that each sees only their own organization and that every route of the other one answers 404; the platform administrator lists both with `?scope=all`, and nobody else may (403). Never prints a password. Tests: `scripts/tests/test_two_org_proof.py` (fake API: an isolated pair passes; one leaking route fails the proof once per person; no users file or no server stops it, exit 2).
- B-2470: `shifde-admin db upgrade` with no argument failed (`head` with one Alembic branch per module means several heads); the default is now `heads`. Test: `backend/tests/cli/test_db_commands.py`.
- Live run, 2026-11-17, this worktree's database on 127.0.0.1:55450, from `backend/` with `SHIFDE_ENV=development SHIFDE_AUTH_LOCAL_ENABLED=true`:
  `uv run shifde-admin db upgrade` -> `database upgraded to heads`; `db check` -> `status: ok`; `uv run shifde-admin dev-seed` -> `seeded 6 users`; `SHIFDE_API_PORT=8791 uv run shifde-api`; `python -I scripts/two_org_proof.py --base-url http://127.0.0.1:8791` -> `TWO-ORG PROOF PASSED: 67 checks`, exit 0.
  The first run failed one check of the script's own: the platform administrator's plain `/api/v1/orgs` lists the administrator's own memberships (none); `?scope=all` is the route that lists every client.
- The in-process test of the same claim already exists: `tests/app/test_routes.py::test_two_seeded_organizations_with_separate_groups_see_only_themselves`.

## Known-defects refresh (written to STATUS in the merge step)

- Item 12 F-02: closed (connections B-1509, runs B-2271, `start_action_run` test in `tests/runtime/test_service.py`; runtime merged).
- C1: closed (B-2122, B-2145, B-2194, B-2243, B-2295 end-to-end worker test, B-2422 pipeline write node through a `SubprocessWorker`).
- Item 19 ARCH M3: narrowed (B-2294: worker statement timeout, heartbeat stops renewing a run past its deadline, reaper settles it; the API's `Database` now has the statement timeout too, `app.py:66`). Left: a wedged thread keeps its executor slot until the process restarts.
- Item 18 J-M4: `/api/ready` mounted (observability) over a database with connect and statement timeouts; time to 503 with the database down not measured.
- Items 13, 14, 16 F-13, REQ F-10, ARCH M11: runtime ported as SETH, still open (B-2196, B-2275).
