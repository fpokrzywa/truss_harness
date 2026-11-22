"""Go-live preflight (SP4-1): the go-live register and `shifde-admin config preflight`.

The register (`docs/build/golive-register.md`) is one markdown table `| id | state | evidence |`.
Preflight validates the configuration as `config validate` does, then prints one `ok:` /
`refused:` line per go-live check (and `pending:` per `pending-owner` register row). No line
carries a configuration value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from shifde.core.configtools import validate_environment
from shifde.core.db import Database
from shifde.core.dbrole import UnsafeDatabaseRole, check_database_url
from shifde.core.health import migration_state
from shifde.core.settings import REPO_ROOT

DEFAULT_REGISTER = REPO_ROOT / "docs" / "build" / "golive-register.md"
REGISTER_STATES = frozenset({"closed", "accepted", "pending-owner", "open"})
REGISTER_HEADER = ["id", "state", "evidence"]
HSTS_MIN_SECONDS = 15_552_000  # 180 days
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_SEPARATOR_CELL = re.compile(r":?-+:?")


class RegisterError(ValueError):
    """The register is invalid; the message names the line and the reason."""


@dataclass(frozen=True)
class RegisterRow:
    id: str
    state: str
    evidence: str
    line: int


def _cells(line: str) -> list[str]:
    inner = line.strip().removeprefix("|").removesuffix("|")
    return [cell.strip() for cell in _CELL_SPLIT.split(inner)]


def parse_register(text: str) -> list[RegisterRow]:
    """The rows of the first table whose header is `id | state | evidence`."""
    lines = text.splitlines()
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if line.lstrip().startswith("|") and _cells(line) == REGISTER_HEADER
        ),
        None,
    )
    if start is None:
        raise RegisterError("no table with the header | id | state | evidence |")
    sep = start + 1
    if sep >= len(lines) or not (
        len(cells := _cells(lines[sep])) == 3 and all(_SEPARATOR_CELL.fullmatch(c) for c in cells)
    ):
        raise RegisterError(f"line {sep + 1}: the header is not followed by a separator row")
    rows: list[RegisterRow] = []
    seen: set[str] = set()
    for index in range(sep + 1, len(lines)):
        line = lines[index]
        if not line.lstrip().startswith("|"):
            break
        number = index + 1
        cells = _cells(line)
        if len(cells) != 3:
            raise RegisterError(f"line {number}: expected 3 cells, found {len(cells)}")
        row_id, state, evidence = cells
        if not row_id:
            raise RegisterError(f"line {number}: empty id")
        if row_id in seen:
            raise RegisterError(f"line {number}: duplicate id {row_id!r}")
        if state not in REGISTER_STATES:
            raise RegisterError(
                f"line {number}: state must be one of {', '.join(sorted(REGISTER_STATES))}"
            )
        if not evidence:
            raise RegisterError(f"line {number}: empty evidence")
        seen.add(row_id)
        rows.append(RegisterRow(row_id, state, evidence, number))
    return rows


def read_register(path: Path) -> list[RegisterRow]:
    """Raises OSError if the file cannot be read, `RegisterError` if it is invalid."""
    return parse_register(path.read_text(encoding="utf-8-sig"))


@dataclass
class PreflightResult:
    lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(line.startswith("refused:") for line in self.lines)


def run_preflight(environ: Mapping[str, str], register: Path) -> PreflightResult:
    """The go-live checks of design SP4 4.8 on the SHIFDE_* configuration in `environ`."""
    result = PreflightResult()
    out = result.lines
    report = validate_environment(environ)
    if not report.ok or report.settings is None:
        out += report.render().splitlines()
        out.append("refused: configuration invalid")
        return result
    settings = report.settings

    rows: list[RegisterRow] | None = None
    register_line: str
    try:
        rows = read_register(register)
        register_line = "ok: register"
    except OSError as exc:
        register_line = f"refused: register unreadable ({exc.__class__.__name__})"
    except RegisterError as exc:
        register_line = f"refused: register invalid: {exc}"
    states = {row.id: row.state for row in rows or []}

    if settings.trusted_proxies or settings.trust_loopback_proxy:
        out.append("ok: trusted proxies")
    else:
        out.append(
            "refused: trusted proxies: SHIFDE_TRUSTED_PROXIES is empty and "
            "SHIFDE_TRUST_LOOPBACK_PROXY is not true"
        )
    if settings.hsts_max_age_seconds >= HSTS_MIN_SECONDS:
        out.append("ok: hsts")
    else:
        out.append(f"refused: hsts: SHIFDE_HSTS_MAX_AGE_SECONDS is below {HSTS_MIN_SECONDS}")
    if settings.api_docs_enabled:
        out.append("refused: api docs: SHIFDE_API_DOCS_ENABLED is not false")
    else:
        out.append("ok: api docs")
    if not settings.auth_local_enabled:
        out.append("ok: local login")
    elif states.get("REQ F-22") == "accepted":
        out.append("ok: local login (REQ F-22 accepted)")
    else:
        out.append(
            "refused: local login: SHIFDE_AUTH_LOCAL_ENABLED is true and the register row "
            "REQ F-22 is not accepted"
        )
    if settings.metrics_token and settings.metrics_token.get_secret_value():
        out.append("ok: metrics token")
    else:
        out.append("refused: metrics token: SHIFDE_METRICS_TOKEN is not set")

    reachable = True
    try:
        check_database_url(settings.database_url)
        out.append("ok: database role")
    except UnsafeDatabaseRole as exc:
        out.append(f"refused: database role: {exc.reason}")
    except (SQLAlchemyError, OSError):
        out.append("refused: database unreachable")
        reachable = False
    if reachable:
        # the role is checked above; a refused role still gets its migration line
        database = Database(
            settings.database_url,
            pool_size=1,
            max_overflow=0,
            verify_role=False,
            connect_timeout=settings.db_connect_timeout_seconds,
        )
        try:
            with database.session() as session:
                state = migration_state(session)
        except (SQLAlchemyError, OSError):
            out.append("refused: database unreachable")
        else:
            if state.status == "ok":
                out.append("ok: migrations")
            else:
                out.append(f"refused: migrations: status {state.status}")
        finally:
            database.dispose()

    out.append(register_line)
    for row in rows or []:
        if row.state == "pending-owner":
            out.append(f"pending: {row.id}")
        elif row.state == "open":
            out.append(f"refused: open: {row.id}")
    return result
