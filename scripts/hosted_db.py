"""Set up the owner's hosted PostgreSQL database for SETH (standard library + the backend's psycopg).

    cd backend; uv run --frozen python ../scripts/hosted_db.py check
    cd backend; uv run --frozen python ../scripts/hosted_db.py setup

The administrator connection is DATABASE_URL in .env.local (the owner's file; never printed).
This script never prints the URL, host, user, password or database name.

`check` reports which route is in use, the server version, whether TLS is in use and what is there.

`setup` is safe to repeat. It:
  1. creates or repairs the roles `shifde_owner` (schema owner) and `shifde_app` (application:
     NOSUPERUSER, NOBYPASSRLS, owns nothing, member of no role), with new random passwords on
     first run;
  2. gives the target database to `shifde_owner`. It does this only when the database is empty or
     already owned by `shifde_owner`; otherwise it refuses. PUBLIC loses access; `shifde_app` gets
     CONNECT and TEMPORARY;
  3. writes `.devdata/hosted.env` (gitignored) with SHIFDE_DATABASE_URL and
     SHIFDE_MIGRATION_DATABASE_URL for this route;
  4. runs `shifde-admin db upgrade heads` as the owner;
  5. connects as `shifde_app` through the application's own startup role check, and counts the
     row-level-security tables.

It refuses to run over a route an eavesdropper could read: a public address without TLS. It allows
TLS on any route, or a loopback, private (RFC 1918) or Tailscale/CGNAT (100.64.0.0/10) address
without TLS. The owner chose the Tailscale route on 2026-11-16.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO = Path(__file__).resolve().parents[1]
HOSTED_ENV = REPO / ".devdata" / "hosted.env"
OWNER_ROLE, APP_ROLE = "shifde_owner", "shifde_app"
CGNAT = ipaddress.ip_network("100.64.0.0/10")


class Admin:
    def __init__(self) -> None:
        url = None
        for line in (REPO / ".env.local").read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip().strip('"').strip("'")
        if not url:
            raise SystemExit("DATABASE_URL is not in .env.local")
        p = urlsplit(url)
        self.host, self.port = p.hostname or "", p.port or 5432
        self.user, self.password = unquote(p.username or ""), unquote(p.password or "")
        self.db = p.path.lstrip("/") or "postgres"
        self._secrets = [s for s in (url, self.password, self.user, self.host, self.db) if s]

    def clean(self, text: str) -> str:
        for s in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(s, "<redacted>")
        return text

    def connect(self, dbname: str | None = None):
        import psycopg
        return psycopg.connect(host=self.host, port=self.port, user=self.user, password=self.password,
                               dbname=dbname or self.db, connect_timeout=10, autocommit=True)


def route_kind(host: str) -> str:
    try:
        addrs = {ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(host, None)}
    except OSError:
        return "unresolvable"
    kinds = set()
    for ip in addrs:
        if ip.is_loopback:
            kinds.add("loopback")
        elif ip.version == 4 and ip in CGNAT:
            kinds.add("tailscale/cgnat")
        elif ip.is_private:
            kinds.add("private")
        else:
            kinds.add("public")
    return "/".join(sorted(kinds))


def server_facts(conn) -> dict:
    row = conn.execute(
        "select current_setting('server_version_num')::int, current_setting('server_version'),"
        " coalesce((select ssl from pg_stat_ssl where pid = pg_backend_pid()), false),"
        " (select rolsuper or rolcreaterole from pg_roles where rolname = current_user)").fetchone()
    return {"version_num": row[0], "version": row[1], "ssl": row[2], "can_create_roles": row[3]}


def route_ok(kind: str, ssl: bool) -> bool:
    return ssl or (kind != "unresolvable" and "public" not in kind)


def cmd_check(admin: Admin) -> int:
    kind = route_kind(admin.host)
    print("route:", kind)
    try:
        with admin.connect() as c:
            f = server_facts(c)
            tables = c.execute("select count(*) from information_schema.tables where table_schema "
                               "not in ('pg_catalog','information_schema')").fetchone()[0]
            roles = [r[0] for r in c.execute("select rolname from pg_roles where rolname in (%s, %s) "
                                             "order by 1", (OWNER_ROLE, APP_ROLE))]
            owner = c.execute("select pg_get_userbyid(datdba) from pg_database "
                              "where datname = current_database()").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        print("connect: FAILED:", type(exc).__name__, admin.clean(str(exc)).strip()[:300])
        return 1
    print(f"connect: OK; server {f['version']}; tls in use: {f['ssl']}; can create roles: {f['can_create_roles']}")
    print(f"target database: {tables} user tables; owner is {'shifde_owner' if owner == OWNER_ROLE else 'another role'}; "
          f"shifde roles present: {roles or 'none'}")
    print("route acceptable for setup:", route_ok(kind, f["ssl"]))
    return 0


def read_hosted_env() -> dict[str, str]:
    values = {}
    if HOSTED_ENV.exists():
        for line in HOSTED_ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def cmd_setup(admin: Admin) -> int:
    kind = route_kind(admin.host)
    with admin.connect() as c:
        f = server_facts(c)
        if f["version_num"] < 170000:
            print(f"REFUSED: server is {f['version']}; the design needs PostgreSQL 17")
            return 1
        if not route_ok(kind, f["ssl"]):
            print(f"REFUSED: route is {kind} without TLS; use the Tailscale address or enable TLS")
            return 1
        if not f["can_create_roles"]:
            print("REFUSED: the DATABASE_URL role cannot create roles")
            return 1
        saved = read_hosted_env()
        owner_pw = saved.get("OWNER_PASSWORD") or secrets.token_urlsafe(32)
        app_pw = saved.get("APP_PASSWORD") or secrets.token_urlsafe(32)
        # 1. Roles (same attributes as scripts/dev_db.py ensure_roles).
        for role in (OWNER_ROLE, APP_ROLE):
            if not c.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
                c.execute(f"create role {role} login")
        from psycopg import sql  # ALTER ROLE is a utility statement: no bind parameters, so quote client-side
        c.execute(sql.SQL(f"alter role {OWNER_ROLE} login nosuperuser bypassrls createrole nocreatedb password {{}}")
                  .format(sql.Literal(owner_pw)))
        c.execute(sql.SQL(f"alter role {APP_ROLE} login nosuperuser nobypassrls nocreatedb nocreaterole "
                          "noreplication password {}").format(sql.Literal(app_pw)))
        for granted, grantor in c.execute(
                "select r.rolname, g.rolname from pg_auth_members m join pg_roles r on r.oid = m.roleid "
                "join pg_roles g on g.oid = m.grantor where m.member = %s::regrole", (APP_ROLE,)).fetchall():
            c.execute(f'revoke "{granted}" from {APP_ROLE} granted by "{grantor}" cascade')
        print("roles: shifde_owner and shifde_app created or repaired")
        # 2. The database.
        owner, tables = c.execute(
            "select pg_get_userbyid(datdba), (select count(*) from information_schema.tables where "
            "table_schema not in ('pg_catalog','information_schema')) from pg_database "
            "where datname = current_database()").fetchone()
        if owner != OWNER_ROLE:
            if tables:
                print("REFUSED: the target database already holds tables and belongs to another role")
                return 1
            c.execute(f'alter database "{admin.db}" owner to {OWNER_ROLE}')
            c.execute(f"alter schema public owner to {OWNER_ROLE}")
        c.execute(f'revoke all on database "{admin.db}" from public')
        c.execute(f'grant connect, temporary on database "{admin.db}" to {APP_ROLE}')
        print("database: owned by shifde_owner; PUBLIC revoked; shifde_app has CONNECT, TEMPORARY")
    # 3. hosted.env for this route.
    base = f"postgresql+psycopg://{{user}}:{{pw}}@{admin.host}:{admin.port}/{admin.db}"
    # The instance's own API-key pepper and secret-encryption key ring. Production settings require
    # them, and they must never change for this database (stored secrets are encrypted with them),
    # so they are generated once, the same way the app does, and kept here.
    import base64
    pepper = saved.get("SHIFDE_API_KEY_PEPPER") or secrets.token_urlsafe(48)
    keyring = saved.get("SHIFDE_SECRET_KEYS") or f"hosted1:{base64.b64encode(os.urandom(32)).decode()}"
    env_lines = [
        "# Hosted database (scripts/hosted_db.py). Never commit or print.",
        f"OWNER_PASSWORD={owner_pw}",
        f"APP_PASSWORD={app_pw}",
        "SHIFDE_DATABASE_URL=" + base.format(user=APP_ROLE, pw=app_pw),
        "SHIFDE_MIGRATION_DATABASE_URL=" + base.format(user=OWNER_ROLE, pw=owner_pw),
        f"SHIFDE_API_KEY_PEPPER={pepper}",
        f"SHIFDE_SECRET_KEYS={keyring}",
    ]
    HOSTED_ENV.parent.mkdir(parents=True, exist_ok=True)
    HOSTED_ENV.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    print("wrote .devdata/hosted.env")
    # 4 and 5 run inside the backend environment, with only the hosted URLs.
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHIFDE_")}
    env["SHIFDE_DATABASE_URL"] = base.format(user=APP_ROLE, pw=app_pw)
    env["SHIFDE_MIGRATION_DATABASE_URL"] = base.format(user=OWNER_ROLE, pw=owner_pw)
    env["SHIFDE_API_KEY_PEPPER"], env["SHIFDE_SECRET_KEYS"] = pepper, keyring
    backend = REPO / "backend"
    r = subprocess.run(["uv", "run", "--frozen", "shifde-admin", "db", "upgrade", "heads"], cwd=backend, env=env,
                       capture_output=True, text=True)
    print("migrations: exit", r.returncode, admin.clean((r.stdout + r.stderr).strip().splitlines()[-1:][0]
                                                       if (r.stdout + r.stderr).strip() else ""))
    if r.returncode != 0:
        return 1
    probe = (
        "import os\n"
        "from sqlalchemy import text\n"
        "from shifde.core.db import Database\n"
        "db = Database(os.environ['SHIFDE_DATABASE_URL'])\n"   # runs the startup role check
        "with db.session() as s:\n"
        "    who = s.execute(text('select current_user')).scalar()\n"
        "    forced = s.execute(text(\"select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'shifde' and c.relkind = 'r' and c.relrowsecurity and c.relforcerowsecurity\")).scalar()\n"
        "    total = s.execute(text(\"select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'shifde' and c.relkind = 'r'\")).scalar()\n"
        "print(f'app connect: OK as {who}; startup role check passed; shifde tables {total}, forced RLS on {forced}')\n"
        "db.dispose()\n")
    r = subprocess.run(["uv", "run", "--frozen", "python", "-c", probe], cwd=backend, env=env,
                       capture_output=True, text=True)
    out = admin.clean((r.stdout + r.stderr).strip())
    print(out.splitlines()[-1] if out else f"app connect: exit {r.returncode}")
    return r.returncode


def main() -> int:
    admin = Admin()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        return cmd_check(admin)
    if cmd == "setup":
        return cmd_setup(admin)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
