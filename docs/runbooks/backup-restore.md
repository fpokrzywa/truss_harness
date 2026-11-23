# Backup and restore (runbook)

Logical backups of the SETH database with `pg_dump`/`pg_restore`, and a restore that reports
success only after it has verified the result (design
`docs/superpowers/specs/2026-11-21-subproject-4-go-live-design.md` section 6.1). Part of the
[go-live runbook](go-live.md).

## Who runs it

Every command runs as the **schema owner**: `SHIFDE_MIGRATION_DATABASE_URL` names `shifde_owner`,
the role that owns the schema and bypasses row-level security, so the dump holds every client's
rows without disabling a policy. The application role never runs a backup. `SHIFDE_DATABASE_URL`
(`shifde_app`) is still set: restore uses its credentials for the app-role check.

`pg_dump` and `pg_restore` get host, port, user and database as arguments and the password only as
`PGPASSWORD` in the child's environment (`sslmode` as `PGSSLMODE`), never in the command line.
They are found in `SHIFDE_PG_BIN_DIR` when set, otherwise on `PATH`; `pg_dump`'s major version
must be at least the server's.

## Create

    cd backend; uv run shifde-admin backup create --out <dir>

`<dir>` must be new or empty. It gets `shifde.dump` (`pg_dump -Fc --no-owner`, taken in one
exported snapshot) and `manifest.json`, both readable only by the owner. The manifest holds the app
version, the Alembic heads, the `pg_dump` and server versions, the creation time, the dump's
sha256, the **key ids** of `SHIFDE_SECRET_KEYS` (ids only, never key material), the row count of
every table (the volatile `workers`, `sessions`, `login_lockouts` and `oidc_auth_requests` are
counted but not compared), a catalog fingerprint (row-level security flags, policies, grants to
`shifde_app`, SECURITY DEFINER functions and their `search_path`) and its own digest.

**Schedule:** the host's scheduler runs `backup create` nightly into a new directory per run
(B-2724). Encryption at rest, off-site copies and retention are host storage settings (design call
6). Point-in-time recovery and WAL archiving are decided with the hosting target and wait on owner
input.

## Verify

    cd backend; uv run shifde-admin backup verify --from <dir>

Needs no database: the digests match, the revision is known to this code and not newer than its
head, and `pg_restore` is present. Prints `backup verified: ...`; exit 1 otherwise.

## Prepare an empty target

As the cluster superuser, create the target database owned by `shifde_owner` from `template0`,
and give `shifde_app` its access:

    CREATE DATABASE <name> OWNER shifde_owner TEMPLATE template0;
    REVOKE ALL ON DATABASE <name> FROM PUBLIC;
    GRANT CONNECT, TEMPORARY ON DATABASE <name> TO shifde_app;

The roles `shifde_owner` and `shifde_app` must exist on the target cluster (roles are cluster-level
and are not in the dump; create them as `scripts/hosted_db.py setup` does). The target must be
empty: no `shifde` schema and no relation outside the system schemas.

## Restore

    cd backend; uv run shifde-admin backup restore --from <dir> --target-url-env <NAME>

`<NAME>` is the name of an environment variable holding the target's owner URL (or `<NAME>_FILE`,
a file holding it), so the URL is never in the command line. Restore is refused unless verify
passes, the target is empty and owned by `shifde_owner`, and both roles exist. It runs
`pg_restore --single-transaction --exit-on-error --no-owner` as `shifde_owner`, then checks, one
line each:

- the catalog fingerprint equals the manifest's;
- `catalog rules`: the restored catalog passes the code's isolation rules (row-level security,
  app-role privileges, the SECURITY DEFINER allowlist, no object that writes the client context,
  tenant_id in foreign and unique keys between client tables, uuid tenant_id and user_id
  columns, the admin flag only on installation rows of client tables, the principal tables:
  each a table without tenant_id with a policy keyed to the principal, and the context helpers'
  definitions equal to the migrations'), read from the catalog and never from the manifest;
- the row counts equal the manifest's for every non-volatile table;
- the migration state is `ok`;
- `shifde_app` passes the startup role check against the target;
- every organization's audit hash chain verifies;
- every stored secret decrypts with the current `SHIFDE_SECRET_KEYS` (0 failed).

It prints `RESTORE VERIFIED` only when all of them pass. Otherwise it exits 1 naming what differs,
and the target is **left as it is for inspection**: never point a service at it; drop it and
restore again into a new empty database once the cause is fixed.

## Secrets check

    cd backend; uv run shifde-admin secrets check

As the owner, decrypts every stored secret with `SHIFDE_SECRET_KEYS`, discards the plaintext and
prints `ok <n>, failed <m>` and the id of each secret that failed; exit 1 if any failed. Read only.
Run it after a key rotation and after a restore.

## Keep the key ring with the backups

`SHIFDE_SECRET_KEYS` (and the API-key pepper) are **not** in the backup. Keep the key ring that was
current when each backup was taken, stored apart from the backups but kept as long as they are:
without it the restored secrets cannot be read and restore's secrets check fails. The manifest's
key ids say which ring a backup needs.

## Proof on the local cluster

`python scripts/golive_check.py --local` seeds a scratch database, runs `backup create`, restores
it into a second scratch database (`RESTORE VERIFIED`) and runs the two-organization proof against
an API on the restored database.
