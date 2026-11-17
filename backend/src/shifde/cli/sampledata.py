"""Generic sample database (`shifde_sample`) for connection demos, conformance and later phases.

Created with superuser access (SHIFDE_*_ADMIN_DATABASE_URL) from the dev command and the tests only.
Tables: employees, customers (with a restricted-classification column `tax_id`), accounts,
service_requests (region, status, priority, assigned_employee_id). The reader role is NOSUPERUSER,
read-only (SELECT grants only, default_transaction_read_only) and owns nothing.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any  # Any: libpq connection parameters

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

REGIONS = ["north", "south", "east", "west"]
STATUSES = ["open", "in_progress", "resolved", "closed"]
PRIORITIES = ["low", "medium", "high", "urgent"]
SEGMENTS = ["consumer", "small_business", "enterprise"]
DEPARTMENTS = ["support", "billing", "field", "success"]

SCHEMA_SQL = [
    """CREATE TABLE employees (
        id integer PRIMARY KEY,
        name text NOT NULL,
        email text NOT NULL UNIQUE,
        department text NOT NULL,
        region text NOT NULL,
        title text NOT NULL
    )""",
    """CREATE TABLE customers (
        id integer PRIMARY KEY,
        name text NOT NULL,
        email text NOT NULL,
        region text NOT NULL,
        segment text NOT NULL,
        tax_id text,
        created_at timestamptz NOT NULL
    )""",
    """CREATE TABLE accounts (
        id integer PRIMARY KEY,
        customer_id integer NOT NULL REFERENCES customers (id),
        account_number text NOT NULL UNIQUE,
        status text NOT NULL,
        balance numeric(12, 2) NOT NULL,
        opened_at timestamptz NOT NULL
    )""",
    """CREATE TABLE service_requests (
        id integer PRIMARY KEY,
        account_id integer NOT NULL REFERENCES accounts (id),
        customer_id integer NOT NULL REFERENCES customers (id),
        region text NOT NULL,
        status text NOT NULL,
        priority text NOT NULL,
        assigned_employee_id integer REFERENCES employees (id),
        summary text NOT NULL,
        opened_at timestamptz NOT NULL,
        closed_at timestamptz
    )""",
    "COMMENT ON COLUMN customers.tax_id IS 'classification: restricted'",
    "COMMENT ON TABLE customers IS 'Sample customers (synthetic data)'",
    "CREATE INDEX ix_service_requests__region_status ON service_requests (region, status)",
]


def _connect(
    url: str, dbname: str | None = None, **extra: Any
) -> psycopg.Connection[Any]:  # Any: kwargs
    u = make_url(url)
    return psycopg.connect(
        host=u.host,
        port=u.port or 5432,
        user=u.username,
        password=u.password,
        dbname=dbname or u.database,
        **extra,
    )


def _rows(rng: random.Random) -> dict[str, list[tuple[Any, ...]]]:  # Any: row values
    base = datetime(2025, 1, 1, tzinfo=UTC)
    employees = [
        (
            i,
            f"Employee {i:02d}",
            f"employee{i:02d}@example.test",
            DEPARTMENTS[i % 4],
            REGIONS[i % 4],
            ["Agent", "Senior Agent", "Lead"][i % 3],
        )
        for i in range(1, 21)
    ]
    customers = [
        (
            i,
            f"Customer {i:03d}",
            f"customer{i:03d}@example.test",
            REGIONS[i % 4],
            SEGMENTS[i % 3],
            f"TAX-{rng.randrange(10**8, 10**9)}",
            base + timedelta(days=i),
        )
        for i in range(1, 51)
    ]
    accounts = [
        (
            i,
            1 + (i - 1) % 50,
            f"AC-{100000 + i}",
            rng.choice(["active", "active", "suspended"]),
            round(rng.uniform(-500, 20000), 2),
            base + timedelta(days=i * 2),
        )
        for i in range(1, 81)
    ]
    requests = []
    for i in range(1, 201):
        account = rng.choice(accounts)
        status = rng.choice(STATUSES)
        opened = base + timedelta(days=rng.randrange(0, 300), hours=rng.randrange(24))
        closed = (
            opened + timedelta(days=rng.randrange(1, 10))
            if status in ("resolved", "closed")
            else None
        )
        requests.append(
            (
                i,
                account[0],
                account[1],
                customers[account[1] - 1][3],
                status,
                rng.choice(PRIORITIES),
                rng.choice(employees)[0] if status != "open" or rng.random() < 0.5 else None,
                f"Synthetic request {i}",
                opened,
                closed,
            )
        )
    return {
        "employees": employees,
        "customers": customers,
        "accounts": accounts,
        "service_requests": requests,
    }


def build_sample_database(
    admin_url: str, db_name: str, reader_role: str, reader_password: str, *, recreate: bool = False
) -> None:
    """Create (or refresh) the sample database and the read-only reader role."""
    with _connect(admin_url, "postgres", autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
        ).fetchone()
        if exists and recreate:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db_name)))
            exists = None
        if not exists:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
        role_exists = admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (reader_role,)
        ).fetchone()
        verb = "ALTER" if role_exists else "CREATE"
        admin.execute(
            sql.SQL(
                verb + " ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(reader_role), sql.Literal(reader_password))
        )
        admin.execute(
            sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(
                sql.Identifier(reader_role)
            )
        )
        admin.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(db_name))
        )
        admin.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(db_name), sql.Identifier(reader_role)
            )
        )
    rng = random.Random(20260101)  # deterministic data
    with _connect(admin_url, db_name) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        for stmt in SCHEMA_SQL:
            conn.execute(stmt)
        for table, rows in _rows(rng).items():
            width = len(rows[0])
            with conn.cursor() as cur:
                cur.executemany(
                    sql.SQL("INSERT INTO {} VALUES ({})").format(
                        sql.Identifier(table), sql.SQL(", ").join([sql.Placeholder()] * width)
                    ),
                    rows,
                )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(reader_role))
        )
        conn.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(
                sql.Identifier(reader_role)
            )
        )
        conn.commit()


def drop_sample_database(admin_url: str, db_name: str, reader_role: str | None = None) -> None:
    with _connect(admin_url, "postgres", autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db_name))
        )
        if reader_role:
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(reader_role)))
