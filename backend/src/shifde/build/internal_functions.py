"""Internal function declarations: the `seth.*` keys a `builtin.function` tool may bind (SP7 design
sections 3 and 4).

Declarations only: key, description, input and output schema and side-effect class. The functions
themselves are injected into the worker as a `runtime.internal_tools.InternalToolRegistry` by the
composition root (`worker_main`) and run only inside a run (`runtime/steps.py`), under the run
principal and in the step's session; outside a run (`tool_runtime.run_builtin`, the test panel) a
`seth.*` key is refused.

Output schemas are permissive objects that declare `truncated` and `error` without requiring them,
so a function's `{"error": "not_found"}` answer is a valid output.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any  # Any: JSON Schema documents

_DRAFT = "https://json-schema.org/draft/2020-12/schema"
_UUID: dict[str, Any] = {"type": "string", "format": "uuid"}


@dataclass(frozen=True)
class InternalFunctionDecl:
    key: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    side_effect_class: str


def _input(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "$schema": _DRAFT,
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _output() -> dict[str, Any]:
    return {
        "$schema": _DRAFT,
        "type": "object",
        "properties": {"truncated": {"type": "boolean"}, "error": {"type": "string"}},
    }


_DECLS = (
    InternalFunctionDecl(
        "seth.catalog.read",
        "Read the data catalog of the project's connections (one connection when given).",
        _input({"connection_id": _UUID}),
        _output(),
        "read",
    ),
    InternalFunctionDecl(
        "seth.solutions.read",
        "Read the project's solutions (one solution when given).",
        _input({"solution_id": _UUID}),
        _output(),
        "read",
    ),
    InternalFunctionDecl(
        "seth.releases.read",
        "Read the releases of the project's solutions (filtered by solution or release).",
        _input({"solution_id": _UUID, "release_id": _UUID}),
        _output(),
        "read",
    ),
    InternalFunctionDecl(
        "seth.evaluations.read",
        "Read the evaluation runs of a release (one evaluation run when given).",
        _input({"release_id": _UUID, "evaluation_run_id": _UUID}, ("release_id",)),
        _output(),
        "read",
    ),
    InternalFunctionDecl(
        "seth.costs.read",
        "Read the project's costs (one run, or the last `days` days).",
        _input({"run_id": _UUID, "days": {"type": "integer", "minimum": 1, "maximum": 90}}),
        _output(),
        "read",
    ),
    InternalFunctionDecl(
        "seth.budgets.read",
        "Read the budgets that apply to the project.",
        _input({}),
        _output(),
        "read",
    ),
    InternalFunctionDecl(
        "seth.board.read",
        "Read the project's board.",
        _input({}),
        _output(),
        "read",
    ),
    InternalFunctionDecl(
        "seth.targets.read",
        "Read the delivery targets the project may deliver to.",
        _input({}),
        _output(),
        "read",
    ),
    InternalFunctionDecl(
        "seth.propose.app_request",
        "Propose an application request (a person accepts it on the board).",
        _input(
            {
                "connection_id": _UUID,
                "tables": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {"type": "string", "minLength": 1, "maxLength": 256},
                },
                "brief": {"type": "string", "minLength": 1, "maxLength": 4000},
                "model_id": _UUID,
            },
            ("connection_id", "tables", "brief", "model_id"),
        ),
        _output(),
        "write_idempotent",
    ),
    InternalFunctionDecl(
        "seth.propose.evaluation_run",
        "Propose an evaluation run of a release (a person accepts it on the board).",
        _input({"release_id": _UUID}, ("release_id",)),
        _output(),
        "write_idempotent",
    ),
    InternalFunctionDecl(
        "seth.propose.delivery_plan",
        "Propose a delivery plan of a release to a target (a person accepts it on the board).",
        _input(
            {
                "target_id": _UUID,
                "release_id": _UUID,
                "application_key": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,62}$"},
                "display_name": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            ("target_id", "release_id", "application_key", "display_name"),
        ),
        _output(),
        "write_idempotent",
    ),
)
INTERNAL_FUNCTIONS: Mapping[str, InternalFunctionDecl] = MappingProxyType(
    {d.key: d for d in _DECLS}
)
