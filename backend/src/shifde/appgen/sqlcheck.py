"""The diagnostics of a draft (SP5 design 5.4): stored with every revision, never raised.

Each tool's SQL goes through `build.sqlrules.check` against the request's tables and the tool's
declared parameters (`tools.<i>.sql`). The other rules: a table screen names an existing tool
(`screen_tool_unknown`) and only its parameters (`screen_input_unknown`); tool names, screen
titles and case ids are unique (`duplicate_name`); a text assertion with an empty value is a
warning (`case_trivial`). Everything else is an error.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from shifde.appgen.schemas import Diagnostic, Draft
from shifde.build import sqlrules
from shifde.packaging.canonical import digest

TEXT_KINDS = frozenset({"equals", "contains", "not_contains"})


def _error(path: str, code: str, message: str) -> Diagnostic:
    return Diagnostic(path=path, code=code, message=message, severity="error")


def _duplicates(field: str, attr: str, names: Iterable[str], what: str) -> list[Diagnostic]:
    seen: set[str] = set()
    out: list[Diagnostic] = []
    for i, name in enumerate(names):
        if name in seen:
            out.append(
                _error(f"{field}.{i}.{attr}", "duplicate_name", f"{what} {name!r} is repeated")
            )
        seen.add(name)
    return out


def validate(draft: Draft, tables: Sequence[str]) -> list[Diagnostic]:
    """Every diagnostic of `draft` over the request's `tables`."""
    out: list[Diagnostic] = []
    allowed = [t.lower() for t in tables]
    tools = {t.name: t for t in draft.tools}
    for i, tool in enumerate(draft.tools):
        declared = [p.name for p in tool.parameters]
        for issue in sqlrules.check(tool.sql, allowed, declared):
            out.append(_error(f"tools.{i}.sql", issue.code, issue.message))
    for i, screen in enumerate(draft.screens):
        if screen.kind != "table":
            continue
        named = tools.get(screen.tool)
        if named is None:
            out.append(
                _error(f"screens.{i}.tool", "screen_tool_unknown", f"no tool {screen.tool!r}")
            )
            continue
        params = {p.name for p in named.parameters}
        for j, name in enumerate(screen.inputs):
            if name not in params:
                out.append(
                    _error(
                        f"screens.{i}.inputs.{j}",
                        "screen_input_unknown",
                        f"{name!r} is not a parameter of tool {named.name!r}",
                    )
                )
    out += _duplicates("tools", "name", (t.name for t in draft.tools), "tool name")
    out += _duplicates("screens", "title", (s.title for s in draft.screens), "screen title")
    out += _duplicates(
        "evaluation_cases", "case_id", (c.case_id for c in draft.evaluation_cases), "case id"
    )
    for i, case in enumerate(draft.evaluation_cases):
        for j, assertion in enumerate(case.assertions):
            if assertion.kind in TEXT_KINDS and assertion.value == "":
                out.append(
                    Diagnostic(
                        path=f"evaluation_cases.{i}.assertions.{j}.value",
                        code="case_trivial",
                        message="the expected text is empty",
                        severity="warning",
                    )
                )
    return out


def draft_hash(draft: Draft) -> str:
    """sha256 of the draft's canonical JSON (`packaging.canonical`)."""
    return digest(draft.model_dump(mode="json"))
