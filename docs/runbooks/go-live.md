# Go-live (runbook)

What a host needs before SETH may hold real client data, the supported edge-proxy topology, the
production settings, and the procedure that ends with `python scripts/golive_check.py --final`.
It is hosting-neutral: the host is not chosen yet (design
`docs/superpowers/specs/2026-11-21-subproject-4-go-live-design.md` section 1). The Ubuntu host
files (systemd units, edge proxy configuration, `docs/runbooks/ubuntu-host.md`) come in SP4-10
(#157).

Two states (design section 1):

- **Met locally**: `python scripts/golive_check.py --local` ends `GO-LIVE CHECK PASSED (local)` on
  the local cluster and loopback, listing the register's `pending-owner` rows.
- **Go-live** (real client data allowed): the hosting target, TLS on the hosted PostgreSQL and the
  owner's written acceptances are recorded in `docs/build/golive-register.md`, and
  `python scripts/golive_check.py --final` ends `GO-LIVE CHECK PASSED (final)`.

Related runbooks: [backup and restore](backup-restore.md) and
[process control](process-control.md).

## Host prerequisites

- **PostgreSQL 17**, with the roles `shifde_owner` (schema owner, BYPASSRLS) and `shifde_app`
  (NOSUPERUSER, NOBYPASSRLS, owns nothing, member of no role) created as `scripts/hosted_db.py
  setup` does: the database owned by `shifde_owner`, PUBLIC without access, `shifde_app` with
  CONNECT and TEMPORARY, then `shifde-admin db upgrade` as the owner.
- **TLS to the hosted database.** The application never runs over cleartext to a non-private
  address. The hosted server does not offer TLS today: waiting on owner input (B-009).
- **`pg_dump` and `pg_restore`** of at least the server's major version, on `PATH` or in the
  directory named by `SHIFDE_PG_BIN_DIR`.
- **Backup storage**: encryption at rest, off-site copies and how long backups are kept are
  settings of the host's storage, not of SETH (design call 6). SETH does not encrypt backup files.
- **A nightly `shifde-admin backup create`** from the host's scheduler (B-2724), into a new
  directory per run; see [backup and restore](backup-restore.md).
- **Python 3.13 with uv** for the backend (`cd backend; uv sync --frozen`).
- **Node with pnpm** for the front end (`cd frontend; pnpm install --frozen-lockfile; pnpm build`).
- A supervisor for `shifde-api`, `shifde-worker`, `shifde-executor` and the Next server that
  follows the contract in [process control](process-control.md) (exit codes 2-5 are never
  restarted).

## Edge proxy

The supported topology (design section 5, call 2):

- One edge reverse proxy terminates TLS for the SETH host name.
- It routes `/api/*` straight to `shifde-api` and everything else to the Next server.
- It **overwrites** `X-Forwarded-For` with the real client address (it never appends to a value
  the client sent).
- `SHIFDE_TRUSTED_PROXIES` lists exactly the edge's addresses, nothing broader; loopback trust
  (`SHIFDE_TRUST_LOOPBACK_PROXY`) stays off.
- `SHIFDE_WEB_API_PROXY=off` on the Next server: its own `/api` proxy is for development only and
  answers 404 when off.
- HSTS comes from the API (`SHIFDE_HSTS_MAX_AGE_SECONDS`) or from the edge, never from Next.
- Running without an edge proxy (all web traffic sharing one limiter key and one audited address)
  is a degraded mode. It is allowed only as a `pending-owner` register row the owner accepts
  (B-2725), never silently.

`golive_check.py --local` proves this on loopback: an in-script edge proxy in front of
`shifde-api` in production, the security headers and HSTS on `/api/health`, 404 on `/api/docs`
and `/api/openapi.json`, and a spoofed `X-Forwarded-For` ignored while the edge's peer address is
audited.

## Env checklist

Production settings (`SHIFDE_ENV=production`, the default). Values are never written here; the
rule is what `shifde-api`, `shifde-worker`, `shifde-executor` and `shifde-admin config preflight`
enforce. A startup refusal exits 2.

| Variable | Rule |
|---|---|
| `SHIFDE_ENV` | `production` (the default when unset). |
| `SHIFDE_DATABASE_URL` | The `shifde_app` role over TLS. A privileged role is refused at startup (exit 3) and in preflight (`refused: database role`); an unreachable database is `refused: database unreachable`. |
| `SHIFDE_MIGRATION_DATABASE_URL` | The `shifde_owner` role: migrations, backup and restore only. Never given to a running service that does not need it. |
| `SHIFDE_API_KEY_PEPPER` | Required, at least 32 characters. Keep it with the deployment's secrets; losing it invalidates every API key. |
| `SHIFDE_SECRET_KEYS` | Required: the key ring that encrypts stored secrets (`KeyRing.generate_entry` format). Keep it with the backups (see [backup and restore](backup-restore.md)). |
| `SHIFDE_METRICS_TOKEN` | Required; `/api/metrics` needs it as a bearer token. |
| `SHIFDE_PUBLIC_BASE_URL` | Required, `https://` (plain http only for loopback). |
| `SHIFDE_ALLOWED_ORIGINS` | Comma-separated, each `https://` (plain http only for loopback). |
| `SHIFDE_TRUSTED_PROXIES` | Exactly the edge proxy's addresses; an entry broader than the minimum prefix is refused; empty is `refused: trusted proxies` unless loopback trust is on. |
| `SHIFDE_TRUST_LOOPBACK_PROXY` | `false` (the default). |
| `SHIFDE_HSTS_MAX_AGE_SECONDS` | At least 15552000 (180 days); the default is 31536000. Below is `refused: hsts`. |
| `SHIFDE_API_DOCS_ENABLED` | `false` (the default in production); `true` is `refused: api docs`. |
| `SHIFDE_AUTH_LOCAL_ENABLED` | `false`, unless the register row `REQ F-22` is `accepted`; otherwise `refused: local login`. |
| `SHIFDE_AUTH_LOCAL_ALLOW_IN_PRODUCTION` | Must be `true` for local login to start at all in production; set it only with `REQ F-22` accepted. |
| `SHIFDE_LOG_FORMAT` | `json` (the default); `console` is refused at startup. |
| `SHIFDE_WEB_API_PROXY` | `off` on the Next server (front end). |
| `SHIFDE_API_HOST`, `SHIFDE_API_PORT` | Where the edge reaches the API (default `127.0.0.1:8710`). |
| `SHIFDE_API_SHUTDOWN_GRACE_SECONDS` | Grace for open requests on a stop (default 30). |
| `SHIFDE_WORKER_HEALTH_HOST`, `SHIFDE_WORKER_HEALTH_PORT` | The worker's `/healthz` and `/readyz` (port 0 = off; set it for the supervisor). |
| `SHIFDE_WORKER_SHUTDOWN_GRACE_SECONDS` | Grace for running jobs on a stop (default 30). |
| `SHIFDE_EXECUTOR_HEALTH_HOST`, `SHIFDE_EXECUTOR_HEALTH_PORT` | The executor's `/healthz` and `/readyz` (port 0 = off). |
| `SHIFDE_EXECUTOR_SHUTDOWN_GRACE_SECONDS` | Grace for the step in flight on a stop (default 60). |
| `SHIFDE_PG_BIN_DIR` | Directory of `pg_dump`/`pg_restore` when they are not on `PATH`. |
| `SHIFDE_MCP_ENABLED` | `false` (the default): `/mcp` is not mounted. `true` mounts the MCP endpoint (bearer `shf_` keys with `mcp:access`, each organization switched on in its MCP settings); preflight prints `ok: mcp access` when it is off, or on with allowed hosts. |
| `SHIFDE_MCP_ALLOWED_HOSTS` | Comma-separated `Host` values `/mcp` answers (`host:*` admits any port); any other host gets 421. Empty while `SHIFDE_MCP_ENABLED` is true is `refused: mcp access`. |
| `SHIFDE_MCP_MAX_RATE_PER_MINUTE` | Installation ceiling of one key's MCP tool calls per minute (default 600); an organization's own rate is capped by it, and its client ceiling is 4x the key rate. |
| `SHIFDE_MCP_PUBLIC_URL` | The MCP endpoint URL shown on the MCP settings screen (unset: "not configured"). |

Secrets can also be given as `SHIFDE_<NAME>_FILE` (a path to a file holding the value), never both
forms.

## Go-live procedure

1. On the host, with the production env file: `cd backend; uv run shifde-admin config preflight
   --env-file <file>`. Fix every `refused:` line; exit 0 only when there is none. Each
   `pending: <id>` line is a register row waiting on the owner.
2. On the local cluster (cluster running, port 8792 free): `python scripts/golive_check.py
   --local`. It must end `GO-LIVE CHECK PASSED (local)`.
3. Record the owner's decisions in `docs/build/golive-register.md`: each `pending-owner` row
   becomes `accepted` (the evidence quotes the owner's own words, the date and where they were
   given; the supervising session's delegated approval does not count) or `closed` (the evidence
   names the issue, PR or test). Add the row `| Hosting target | closed | <host, database
   location, domain and TLS, and where this was decided> |` once the host is set up.
4. `python scripts/golive_check.py --final`. It runs every `--local` check, then refuses any
   `pending-owner` row and a missing or not-yet-closed `Hosting target` row. Only when it ends
   `GO-LIVE CHECK PASSED (final)` may real client data enter SETH.
