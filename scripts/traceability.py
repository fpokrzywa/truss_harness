"""Traceability matrix: design section 5.3 (client isolation) -> backend test files.

Standard library only. The mapping below is written by hand; the script turns it into
`docs/requirements/TRACEABILITY.md` and checks that every listed test file exists and that the
committed matrix is up to date.

    python scripts/traceability.py           write docs/requirements/TRACEABILITY.md
    python scripts/traceability.py --check   write nothing; exit 1 naming each problem
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX = "docs/requirements/TRACEABILITY.md"
SOURCE = "docs/superpowers/specs/2026-11-13-seth-unified-platform-design.md"


@dataclass(frozen=True)
class Entry:
    id: str
    text: str
    status: str  # "covered" or "deferred"
    tests: tuple[str, ...] = ()
    note: str = ""


_T = "backend/tests/"
CROSS_CLIENT = tuple(
    _T + p
    for p in (
        "admin/test_cross_client.py",
        "app/test_cross_client.py",
        "app/test_oidc_cross_client.py",
        "audit/test_cross_client.py",
        "authz/test_cross_client.py",
        "build/test_cross_client.py",
        "build/test_routes_cross_client.py",
        "connections/test_cross_client.py",
        "connections/test_routes_cross_client.py",
        "core/test_cross_client.py",
        "delivery/test_routes_cross_client.py",
        "governance/test_cross_client.py",
        "governance/test_routes_cross_client.py",
        "identity/test_cross_client.py",
        "ledger/test_cross_client.py",
        "models_admin/test_cross_client.py",
        "models_admin/test_routes_cross_client.py",
        "ontology/test_cross_client.py",
        "ontology/test_routes_cross_client.py",
        "runtime/test_cross_client.py",
        "runtime/test_routes_cross_client.py",
        "secrets/test_cross_client.py",
        "tenancy/test_cross_client.py",
        "vendors/test_cross_client.py",
    )
)

ENTRIES: tuple[Entry, ...] = (
    Entry(
        "F-5.3-01",
        "One organization per client: every client-owned row carries the organization id.",
        "covered",
        (_T + "schema/test_rls_schema.py", _T + "tenancy/test_cross_client.py"),
    ),
    Entry(
        "F-5.3-02",
        "The database enforces it: forced row-level security, app role neither owns nor bypasses.",
        "covered",
        (_T + "schema/test_rls_schema.py", _T + "core/test_startup_role_check.py"),
    ),
    Entry(
        "F-5.3-03",
        "The application refuses to start under a privileged database role.",
        "covered",
        (_T + "core/test_startup_role_check.py",),
    ),
    Entry(
        "F-5.3-04",
        "A schema test fails the build if a table lacks a row-level security policy.",
        "covered",
        (_T + "schema/test_rls_schema.py",),
    ),
    Entry(
        "F-5.3-05",
        "A cross-client test exists for every module.",
        "covered",
        CROSS_CLIENT,
    ),
    Entry(
        "F-5.3-06",
        "Access is through SHI-wide groups bound to a role inside a client.",
        "covered",
        (
            _T + "identity/test_platform_groups.py",
            _T + "tenancy/test_bindings.py",
            _T + "app/test_oidc_cross_client.py",
        ),
    ),
    Entry(
        "F-5.3-07",
        "Who may grant access: platform administrator or client owner; self-add is audited.",
        "covered",
        (_T + "tenancy/test_owner_rules.py",),
    ),
    Entry(
        "F-5.3-08",
        "A client platform belongs to one organization.",
        "deferred",
        note="sub-project 2 (deployment targets)",
    ),
    Entry(
        "F-5.3-09",
        "An executor belongs to one organization.",
        "deferred",
        note="sub-project 2 (executors)",
    ),
    Entry(
        "F-5.3-10",
        "Artifacts are stored under the organization and checked on every read.",
        "covered",
        (
            _T + "runtime/test_cross_client.py",
            _T + "runtime/test_routes_cross_client.py",
            _T + "runtime/test_repository.py",
            _T + "runtime/test_pipeline.py",
        ),
    ),
    Entry(
        "F-5.3-11",
        "Agent memory and run history never cross organizations.",
        "covered",
        (
            _T + "runtime/test_cross_client.py",
            _T + "runtime/test_routes_cross_client.py",
            _T + "runtime/test_repository.py",
            _T + "runtime/test_lifecycle.py",
            _T + "governance/test_cross_client.py",
            _T + "app/test_r1_round2.py",
        ),
    ),
    Entry(
        "F-5.3-12",
        "Client data and model providers: SHI's model keys are off per client by default.",
        "covered",
        (_T + "models_admin/test_shi_gate.py",),
    ),
    Entry(
        "F-5.3-13",
        "Client credentials: stored encrypted per organization, confirmed read-only.",
        "covered",
        (
            _T + "secrets/test_cross_client.py",
            _T + "connections/test_regressions.py",
            _T + "connections/test_host_change.py",
        ),
    ),
)


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def generate(entries: tuple[Entry, ...] = ENTRIES) -> str:
    """The matrix as Markdown; the same entries always give the same text."""
    lines = [
        "<!-- Generated by scripts/traceability.py: do not edit by hand. "
        "Regenerate: python scripts/traceability.py. "
        "Check: python scripts/traceability.py --check -->",
        "",
        "# Traceability matrix: client isolation",
        "",
        f"Source: `{SOURCE}`, section 5.3 (Client isolation).",
        "",
        "| id | requirement | status | tests |",
        "| --- | --- | --- | --- |",
    ]
    for e in entries:
        if e.tests:
            tests = "<br>".join(f"`{t}`" for t in e.tests)
        else:
            tests = f"none: arrives in {e.note}" if e.note else "none"
        lines.append(f"| {e.id} | {_cell(e.text)} | {e.status} | {_cell(tests)} |")
    lines += [
        "",
        "SETH PRD requirement ids are not yet included (follow-up of issue #14).",
        "",
    ]
    return "\n".join(lines)


def write(root: Path = ROOT, entries: tuple[Entry, ...] = ENTRIES) -> Path:
    path = root / MATRIX
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(generate(entries).encode("utf-8"))
    return path


def check(root: Path = ROOT, entries: tuple[Entry, ...] = ENTRIES) -> list[str]:
    """One message per problem; an empty list means the matrix is sound."""
    problems = [
        f"missing test file: {t} ({e.id})"
        for e in entries
        for t in e.tests
        if not (root / t).is_file()
    ]
    path = root / MATRIX
    if not path.is_file():
        problems.append(f"{MATRIX} is missing: run python scripts/traceability.py")
    elif path.read_bytes() != generate(entries).encode("utf-8"):
        problems.append(f"{MATRIX} is out of date: run python scripts/traceability.py")
    return problems


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description="Design 5.3 traceability matrix")
    parser.add_argument("--check", action="store_true", help="check only, write nothing")
    args = parser.parse_args(argv)
    if args.check:
        problems = check(root)
        for p in problems:
            print(p, file=sys.stderr)
        return 1 if problems else 0
    print(f"wrote {write(root).relative_to(root).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
