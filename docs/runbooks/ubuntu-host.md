# Ubuntu host: install, upgrade, backup and operate SETH

How to run SETH on one Ubuntu server with systemd and an nginx edge. The files are in
`deploy/ubuntu/` of the repository:

| File | Installed as |
|---|---|
| `deploy/ubuntu/systemd/seth-api.service` | `/etc/systemd/system/seth-api.service` |
| `deploy/ubuntu/systemd/seth-worker.service` | `/etc/systemd/system/seth-worker.service` |
| `deploy/ubuntu/systemd/seth-executor@.service` | `/etc/systemd/system/seth-executor@.service` (one instance per organization) |
| `deploy/ubuntu/systemd/seth-web.service` | `/etc/systemd/system/seth-web.service` |
| `deploy/ubuntu/systemd/seth-backup.service`, `seth-backup.timer` | `/etc/systemd/system/` |
| `deploy/ubuntu/nginx/seth.conf` | `/etc/nginx/sites-available/seth.conf`, linked into `sites-enabled` |
| `deploy/ubuntu/env/seth.env.example` | `/etc/seth/seth.env` (API and worker) |
| `deploy/ubuntu/env/admin.env.example` | `/etc/seth/admin.env` (schema owner credential) |
| `deploy/ubuntu/env/web.env.example` | `/etc/seth/web.env` (Next) |
| `deploy/ubuntu/env/executor.env.example` | `/etc/seth/executor/<org>.env` |
| `deploy/ubuntu/seth-admin` | `/usr/local/sbin/seth-admin` |

Fixed paths: the code is a checkout of a release tag at `/opt/seth`, configuration is in
`/etc/seth/`, backups are in `/var/backups/seth`. The exit and health contract the units follow is
in section (j).

## (a) Owner inputs

| Input | Default in these files | State |
|---|---|---|
| Operating system | Ubuntu 24.04 LTS (assumed; systemd 255, nginx 1.24) | owner to confirm |
| Database location | A separate PostgreSQL 17 host, reached with TLS (`sslmode=verify-full`) | owner to confirm |
| Domain | `<domain>` placeholder in `seth.conf`, `seth.env` and the executor env files | owner to confirm |
| TLS certificate source | Placeholder paths `/etc/ssl/seth/fullchain.pem` and `/etc/ssl/seth/privkey.pem` (an internal CA, a purchased certificate or certbot) | owner to confirm |
| SSH admin network | The address range allowed to reach SSH in section (h) | owner to confirm |

## (b) Host prerequisites

Packages from Ubuntu: `nginx`, `curl`, `ufw` and `git`.

```bash
sudo apt-get update
sudo apt-get install -y nginx curl ufw git ca-certificates gnupg
```

PostgreSQL client tools 17 from the PGDG apt repository (<https://www.postgresql.org/download/linux/ubuntu/>).
Ubuntu 24.04 ships version 16, and `pg_dump` must be at least the server's version. The binaries
land in `/usr/lib/postgresql/17/bin` (`SHIFDE_PG_BIN_DIR` in `admin.env`).

```bash
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main" | sudo tee /etc/apt/sources.list.d/pgdg.list
sudo apt-get update
sudo apt-get install -y postgresql-client-17
```

Node 22 LTS from NodeSource (<https://github.com/nodesource/distributions>); Ubuntu 24.04's own
Node is too old for Next 16. It installs `/usr/bin/node`, which `seth-web.service` runs. pnpm 10
comes through `corepack`, which ships with Node 22.

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x -o /tmp/nodesource_setup.sh
sudo bash /tmp/nodesource_setup.sh
sudo apt-get install -y nodejs
sudo corepack enable
```

uv from Astral (<https://docs.astral.sh/uv/>), installed system-wide into `/usr/local/bin`.
Python 3.13 is installed by uv later, in section (d), into `/opt/seth/.python`
(`UV_PYTHON_INSTALL_DIR`), so the services' `ProtectHome=yes` never hides the interpreter.

```bash
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh /tmp/uv-install.sh
```

Storage (SP4 design call 6): put `/var/backups/seth` (and preferably the whole disk) on encrypted
storage, for example a LUKS volume or the cloud provider's encrypted disk, and copy
`/var/backups/seth` off the host on a schedule (provider snapshots, `rsync` to another site or
object storage). The backups are not encrypted by SETH; the host's storage settings protect them.

## (c) Database

The default is a separate PostgreSQL 17 server. The roles `shifde_owner` (schema owner) and
`shifde_app` (application: NOSUPERUSER, NOBYPASSRLS, owns nothing) and the migration are made by
the repository's `scripts/hosted_db.py setup`; do not write role SQL by hand. It is safe to repeat.

1. On the database host: PostgreSQL 17 with TLS on, a server certificate whose name matches the
   host name SETH connects to, and a firewall that allows 5432 only from the SETH host. With ufw
   there (`<seth host address>`: owner to confirm):

   ```bash
   sudo ufw allow from <seth host address> to any port 5432 proto tcp
   ```

2. From a checkout of the same tag (section (d) makes `/opt/seth`; use a scratch copy owned by
   root so the generated secrets never sit in `/opt/seth`), put the database administrator's
   connection as `DATABASE_URL=...` in that copy's `.env.local`, then run:

   ```bash
   sudo -i
   git clone --branch <tag> https://github.com/<owner>/<repo>.git /root/seth-dbsetup
   install -m 0600 /dev/null /root/seth-dbsetup/.env.local
   editor /root/seth-dbsetup/.env.local
   cd /root/seth-dbsetup/backend
   uv sync --frozen
   uv run --frozen python ../scripts/hosted_db.py check
   uv run --frozen python ../scripts/hosted_db.py setup
   ```

   `setup` creates or repairs both roles, gives the database to `shifde_owner`, runs the migration
   and checks that `shifde_app` passes the startup role check. It writes the two role URLs, the
   API-key pepper, the key ring (`SHIFDE_SECRET_KEYS`) and the metrics token
   (`SHIFDE_METRICS_TOKEN`) into that copy's `.devdata/hosted.env`. Copy them into
   `/etc/seth/seth.env` (app URL, pepper, ring, metrics token) and `/etc/seth/admin.env` (owner
   URL), add `?sslmode=verify-full&sslrootcert=/etc/seth/db-ca.pem` to both URLs, store a copy of
   the key ring off the host (section (g)), then delete `.env.local` and `.devdata/hosted.env`
   from the scratch copy.

3. Put the database server's CA certificate at `/etc/seth/db-ca.pem` (0644 root:root).

Same-host variant: install `postgresql-17` from PGDG on the SETH host, listening on loopback only
(`listen_addresses = 'localhost'`). `scripts/hosted_db.py setup` still makes the roles (a loopback
route without TLS is allowed). Use `127.0.0.1` as the database host in both URLs; `sslmode` may then
be dropped from them.

## (d) Install

Users: non-login system users, each with only its own secrets. The service users can read the
code but not write it.

| User | Runs | Secrets it gets |
|---|---|---|
| `seth-deploy` | Nothing; owns and builds `/opt/seth` | None |
| `seth` | `seth-api`, `seth-worker` | `seth.env` (app role URL, pepper, key ring, metrics token) |
| `seth-web` | `seth-web` | `web.env` (no secrets) |
| `seth-exec-<org>` | `seth-executor@<org>`, one user per organization (section (e)) | That organization's executor env and credentials file only |
| `seth-ops` | `seth-backup.service` and every `sudo seth-admin` run; no long-running service | `seth.env` and `admin.env` (the schema owner URL); owns the dumps |

Because `seth-ops` runs no long-running service, a compromised API or worker (user `seth`) can read
neither the owner credential from an admin process's `/proc/<pid>/environ` nor the dumps in
`/var/backups/seth`. Every unit also sets `ProtectProc=invisible`, so a service sees only its own
processes.

```bash
sudo useradd --system --shell /usr/sbin/nologin --home-dir /opt/seth --no-create-home seth-deploy
sudo useradd --system --shell /usr/sbin/nologin --home-dir /nonexistent --no-create-home seth
sudo useradd --system --shell /usr/sbin/nologin --home-dir /nonexistent --no-create-home seth-web
sudo useradd --system --shell /usr/sbin/nologin --home-dir /nonexistent --no-create-home seth-ops
```

Directories and permissions:

```bash
sudo install -d -o seth-deploy -g seth-deploy -m 0755 /opt/seth
sudo install -d -o root -g root -m 0755 /etc/seth
sudo install -d -o root -g root -m 0711 /etc/seth/executor
sudo install -d -o seth-ops -g seth-ops -m 0700 /var/backups/seth
```

Checkout of a release tag (`<tag>`: the release being installed) and the build, as `seth-deploy`.
`UV_PYTHON_INSTALL_DIR` and `UV_CACHE_DIR` keep the interpreter and caches under `/opt/seth`:

```bash
sudo -u seth-deploy git clone --branch <tag> https://github.com/<owner>/<repo>.git /opt/seth
cd /opt/seth/backend
sudo -u seth-deploy env HOME=/opt/seth UV_PYTHON_INSTALL_DIR=/opt/seth/.python UV_CACHE_DIR=/opt/seth/.cache/uv uv python install 3.13
sudo -u seth-deploy env HOME=/opt/seth UV_PYTHON_INSTALL_DIR=/opt/seth/.python UV_CACHE_DIR=/opt/seth/.cache/uv UV_PYTHON_PREFERENCE=only-managed uv sync --frozen --no-dev
cd /opt/seth/frontend
sudo -u seth-deploy env HOME=/opt/seth COREPACK_HOME=/opt/seth/.cache/corepack pnpm install --frozen-lockfile
sudo -u seth-deploy env HOME=/opt/seth COREPACK_HOME=/opt/seth/.cache/corepack NEXT_TELEMETRY_DISABLED=1 pnpm build
```

Env files from the examples. Fill every `<...>` placeholder; values that come from
`scripts/hosted_db.py setup` are in section (c). Any secret may instead be given as
`SHIFDE_<NAME>_FILE`, the path of a file holding it.

```bash
sudo install -o root -g seth -m 0640 /opt/seth/deploy/ubuntu/env/seth.env.example /etc/seth/seth.env
sudo install -o root -g root -m 0600 /opt/seth/deploy/ubuntu/env/admin.env.example /etc/seth/admin.env
sudo install -o root -g seth-web -m 0640 /opt/seth/deploy/ubuntu/env/web.env.example /etc/seth/web.env
sudo editor /etc/seth/seth.env /etc/seth/admin.env /etc/seth/web.env
```

| File | Mode | Owner |
|---|---|---|
| `/etc/seth/seth.env` | 0640 | `root:seth` |
| `/etc/seth/web.env` | 0640 | `root:seth-web` |
| `/etc/seth/admin.env` | 0600 | `root:root` |
| `/etc/seth/executor/` | 0711 | `root:root` |
| `/etc/seth/executor/<org>.env` | 0600 | `root:root` |
| `/etc/seth/executor/<org>.credentials.json` | 0600 | `seth-exec-<org>:seth-exec-<org>` |
| `/var/backups/seth` | 0700 | `seth-ops:seth-ops` |

PID 1 reads every env file (`EnvironmentFile=`), so `admin.env` and the executor env files stay
`root:root`.

The admin wrapper. `seth-admin` runs `shifde-admin` as `seth-ops` through `systemd-run`, with both
`seth.env` and `admin.env`. PID 1 reads the env files, so the owner credential is never readable by
a service user and never reaches the long-running services:

```bash
sudo install -o root -g root -m 0755 /opt/seth/deploy/ubuntu/seth-admin /usr/local/sbin/seth-admin
```

Check the configuration and the database, in this order. `config preflight` must print no
`refused:` line; its `pending:` lines are the open rows of the go-live register.

```bash
sudo seth-admin config validate
sudo seth-admin db upgrade
sudo seth-admin db check
sudo seth-admin config preflight
```

The units, then the services and the backup timer:

```bash
sudo install -o root -g root -m 0644 /opt/seth/deploy/ubuntu/systemd/seth-api.service /opt/seth/deploy/ubuntu/systemd/seth-worker.service /opt/seth/deploy/ubuntu/systemd/seth-executor@.service /opt/seth/deploy/ubuntu/systemd/seth-web.service /opt/seth/deploy/ubuntu/systemd/seth-backup.service /opt/seth/deploy/ubuntu/systemd/seth-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now seth-api seth-worker seth-web
sudo systemctl enable --now seth-backup.timer
```

Each service unit waits at start (`ExecStartPost`, `curl --fail` with retries) for its liveness
endpoint on loopback; a unit that never answers fails to start.

The nginx edge. Replace `<domain>` in the site and install the certificate and key at the paths in
the file first:

```bash
sudo install -o root -g root -m 0644 /opt/seth/deploy/ubuntu/nginx/seth.conf /etc/nginx/sites-available/seth.conf
sudo editor /etc/nginx/sites-available/seth.conf
sudo ln -s /etc/nginx/sites-available/seth.conf /etc/nginx/sites-enabled/seth.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

The edge terminates TLS, sends `/api` and `/api/*` straight to the API on `127.0.0.1:8710` and
everything else to Next on `127.0.0.1:3710`, answers 404 for `/api/metrics`, and overwrites
`X-Forwarded-For` with the client's address. It adds no security headers: the API sets them and
HSTS, Next sets its CSP. The API trusts `X-Forwarded-For` only from `127.0.0.1/32`
(`SHIFDE_TRUSTED_PROXIES`). That means any local process on this host could set it, which is
acceptable only because this host runs nothing else that faces the network.

Readiness checks, after the install and after every upgrade. Each must answer 200:

```bash
curl --fail --silent --show-error http://127.0.0.1:8710/api/ready
curl --fail --silent --show-error http://127.0.0.1:8711/readyz
curl --fail --silent --show-error --output /dev/null https://<domain>/
curl --fail --silent --show-error https://<domain>/api/health
```

and, per executor instance, its own health port (8720 in the example):

```bash
curl --fail --silent --show-error http://127.0.0.1:8720/readyz
```

## (e) Executors

One `seth-executor@<org>` instance per organization (`<org>` is the organization slug). A
credentials file belongs to one organization, so organizations never share an executor or a uid.
Each instance runs as its own user `seth-exec-<org>` (`User=seth-exec-%i`), so no instance can read
another organization's credentials file or its process environment (which holds the `shx_` token).
User names are at most 32 characters, so for a slug longer than 22 characters pick a shorter
instance name of your own (for example `org7`) and use it everywhere `<org>` appears below; the
instance name is only a local label.

1. In SETH, on that organization's Executors screen, create an executor credential (`shx_...`).
2. Create `/etc/seth/executor/<org>.env` from `executor.env.example`:
   `SHIFDE_EXECUTOR_API_URL` is the public `https://<domain>` (through the edge),
   `SHIFDE_EXECUTOR_TOKEN` is the credential from step 1,
   `SHIFDE_EXECUTOR_CREDENTIALS_FILE=/etc/seth/executor/<org>.credentials.json`, and
   `SHIFDE_EXECUTOR_HEALTH_PORT` is a loopback port of its own (8720, 8721, ...).
3. Create the credentials file: JSON `credential_ref -> {tenant_id, host, token,
   service_principal}`, every entry carrying this organization's id as `tenant_id`.
4. Create the instance's user, set the modes and start the instance. It is never started with
   `--fake-platform`.

```bash
sudo useradd --system --shell /usr/sbin/nologin --home-dir /nonexistent --no-create-home seth-exec-<org>
sudo install -o root -g root -m 0600 /opt/seth/deploy/ubuntu/env/executor.env.example /etc/seth/executor/<org>.env
sudo editor /etc/seth/executor/<org>.env
sudo install -o seth-exec-<org> -g seth-exec-<org> -m 0600 /dev/null /etc/seth/executor/<org>.credentials.json
sudo editor /etc/seth/executor/<org>.credentials.json
sudo systemctl enable --now seth-executor@<org>
```

## (f) Upgrade

1. Back up (section (g)) and check the backup.
2. Stop the worker, the executors and the API, in that order. Each stops on SIGTERM within its
   grace (worker 30 s, executor 60 s, API 30 s by default); systemd waits 10 s longer before it
   kills.
3. Check out the new tag and rebuild, as in section (d).
4. Migrate and check the revision.
5. Start the services, then run the readiness checks of section (d).

```bash
sudo systemctl start seth-backup.service
sudo systemctl stop seth-worker
sudo systemctl stop 'seth-executor@*'
sudo systemctl stop seth-api
cd /opt/seth
sudo -u seth-deploy git fetch --tags
sudo -u seth-deploy git checkout <new tag>
cd /opt/seth/backend
sudo -u seth-deploy env HOME=/opt/seth UV_PYTHON_INSTALL_DIR=/opt/seth/.python UV_CACHE_DIR=/opt/seth/.cache/uv UV_PYTHON_PREFERENCE=only-managed uv sync --frozen --no-dev
cd /opt/seth/frontend
sudo -u seth-deploy env HOME=/opt/seth COREPACK_HOME=/opt/seth/.cache/corepack pnpm install --frozen-lockfile
sudo -u seth-deploy env HOME=/opt/seth COREPACK_HOME=/opt/seth/.cache/corepack NEXT_TELEMETRY_DISABLED=1 pnpm build
sudo seth-admin db upgrade
sudo seth-admin db check
sudo systemctl start seth-api seth-worker
sudo systemctl restart seth-web
sudo systemctl start seth-executor@<org>
```

If a unit file changed in the new tag, install it again as in section (d) and run
`sudo systemctl daemon-reload` before starting.

6. The API and the worker run the startup role check themselves, and the worker also checks the
   schema revision. A refusal shows in `systemctl status seth-worker` (`status=4`, for example)
   and in `journalctl -u seth-worker`. systemd restarts a unit only on exit 1 (crash), with backoff
   1, 2, 4 ... 60 s and no limit. It never restarts on exits 2 to 5, because a restart cannot fix
   them:

   | Exit | Meaning | What to do |
   |---|---|---|
   | 2 | Configuration refused (or the health port could not be bound) | Fix the env file named in the log, then `systemctl start` |
   | 3 | Unsafe database role: the app URL's role is privileged | Use `shifde_app` in `SHIFDE_DATABASE_URL` (section (c)) |
   | 4 | Schema revision mismatch (worker) | `sudo seth-admin db upgrade`, then `db check` |
   | 5 | The executor's token was refused (`whoami` 401 or 403) | A new credential from the Executors screen in `<org>.env` |

## (g) Backup and restore

`seth-backup.timer` starts `seth-backup.service` daily (`Persistent=true`, so a missed run happens
at the next boot). The service runs as user `seth-ops`. It runs `shifde-admin backup create` as
the schema owner into a new `/var/backups/seth/<UTC timestamp>` and then `backup verify` of that
directory; it may write only `/var/backups/seth` (0700 `seth-ops`, so the API's user cannot read
the dumps). A failure shows in `systemctl status seth-backup` and
`journalctl -u seth-backup`. Manual backup and check:

```bash
sudo systemctl list-timers seth-backup.timer
sudo seth-admin backup create --out /var/backups/seth/manual-<date>
sudo seth-admin backup verify --from /var/backups/seth/manual-<date>
```

Restore (SP4-5, issue #128). `backup restore --from DIR --target-url-env NAME` restores a verified
backup into an **empty** database owned by `shifde_owner`, on a cluster where `shifde_owner` and
`shifde_app` already exist (roles are cluster-level, never in the dump). It reads the target's
owner URL from the variable `NAME` (or from the file named by `NAME_FILE`) and never prints it.
It then runs its checks (catalog fingerprint, row counts, migration state, the app role's startup
check, every organization's audit chain, and every stored secret against the key ring) and prints
`RESTORE VERIFIED` only when all pass; otherwise it exits 1 and leaves the target for inspection.
The services keep running on the current database throughout.

1. As the database administrator, on the database host, create the empty target (`seth_restore`
   here) owned by `shifde_owner`, the same grants `scripts/hosted_db.py setup` gives:

   ```bash
   sudo -u postgres psql -v ON_ERROR_STOP=1 -c "create database seth_restore owner shifde_owner template template0"
   sudo -u postgres psql -v ON_ERROR_STOP=1 -c "revoke all on database seth_restore from public"
   sudo -u postgres psql -v ON_ERROR_STOP=1 -c "grant connect, temporary on database seth_restore to shifde_app"
   ```

2. `seth-admin` passes no variables from your shell (PID 1 builds the environment from the env
   files), so put the target's owner URL into the root-only `admin.env` for the restore: add a
   line `SETH_RESTORE_TARGET_URL=` followed by the `SHIFDE_MIGRATION_DATABASE_URL` value with the
   database name changed to `seth_restore`. Then restore and remove the line again:

   ```bash
   sudo editor /etc/seth/admin.env
   sudo seth-admin backup verify --from /var/backups/seth/<backup>
   sudo seth-admin backup restore --from /var/backups/seth/<backup> --target-url-env SETH_RESTORE_TARGET_URL
   sudo editor /etc/seth/admin.env
   ```

3. To switch to the restored database: stop the services as in section (f), change the database
   name in `SHIFDE_DATABASE_URL` (`seth.env`) and `SHIFDE_MIGRATION_DATABASE_URL` (`admin.env`) to
   `seth_restore`, then check that the configured key ring decrypts every stored secret, check the
   revision, start the services and run the readiness checks of section (d):

   ```bash
   sudo systemctl stop seth-worker
   sudo systemctl stop 'seth-executor@*'
   sudo systemctl stop seth-api
   sudo editor /etc/seth/seth.env /etc/seth/admin.env
   sudo seth-admin secrets check
   sudo seth-admin db check
   sudo systemctl start seth-api seth-worker
   sudo systemctl start seth-executor@<org>
   ```

   `secrets check` prints `ok <n>, failed <m>` and the ids of failed secrets, and exits 1 if any
   failed: a failure means the key ring in `seth.env` is not the one the backup's secrets were
   encrypted with. It refuses (exit 1, no `ok` line) when the database role cannot see every
   row, i.e. when it lacks BYPASSRLS: nothing was checked then. Likewise, without BYPASSRLS the restore's
   audit-chain check fails (`FAILED audit chains`) and `secrets rewrap` refuses (exit 1).

The key ring (`SHIFDE_SECRET_KEYS`) and the API-key pepper are **not** in the backup. Keep a copy
of both apart from the backups (for example in the owner's password vault): without the ring the
stored secrets of a restored database cannot be decrypted.

## (h) Firewall

Only SSH, HTTP and HTTPS are open. SSH is limited to the admin network (`<admin network>`: owner
to confirm). The API (8710), Next (3710) and the health ports (8711, 8720 and up) bind to
`127.0.0.1` only and are never opened.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from <admin network> to any port 22 proto tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

## (i) Logs

Every unit logs to the journal under its own identifier (`seth-api`, `seth-worker`,
`seth-executor-<org>`, `seth-web`, `seth-backup`). The API and the worker log JSON
(`SHIFDE_LOG_FORMAT=json`).

```bash
journalctl -u seth-api -f
journalctl -u seth-worker --since today
journalctl -u seth-executor@<org> -n 200
journalctl -u seth-web -n 200
journalctl -u seth-backup --since yesterday
systemctl status seth-api seth-worker seth-web 'seth-executor@*'
```

## (j) Contract source

The exit codes, stop signals, graces and health endpoints the units follow are the process control
contract of the SP4 go-live design, section 6.2
(`docs/superpowers/specs/2026-11-21-subproject-4-go-live-design.md`): exit 0 stopped on request,
1 crashed (restart with backoff), 2 configuration refused, 3 unsafe database role, 4 schema
revision mismatch, 5 cannot authenticate (never restarted). Liveness is `/healthz` on each
process; readiness is `/api/ready` (API) and `/readyz` (worker, executor). The edge topology is
section 5, call 2, of the same design.
