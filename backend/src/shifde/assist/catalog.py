"""The specialist catalog (SP7 design section 4): code, versioned by `CATALOG_VERSION`.

Each entry is installed per project as ordinary project-level `build` artifacts (Call S7-B): one
published `builtin.function` tool per internal key it uses (shared by every entry of the project
that uses the key), the prompt `assist-<key>` and the agent `assist-<key>`. The helpers here are
pure: they derive every spec install publishes, so a reinstall compares and the drift check of
SP7-3 regenerates the same documents.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any  # Any: JSON specs
from uuid import UUID

from shifde.build.internal_functions import INTERNAL_FUNCTIONS

CATALOG_VERSION = "assist.catalog/1"
VARIABLES = ("assist_request_id", "context")
TOOL_TIMEOUT_S = 30
AGENT_LIMITS = {"max_iterations": 8, "max_tool_calls": 12, "max_tokens": 100_000, "deadline_s": 120}

_FOOTER = (
    "\n\nWork only inside the project this request belongs to. You read SETH through your tools "
    "and never change anything yourself: anything to be done is a proposal a person accepts."
)
# the request's variables, in the task message: a caller may set them on a direct run, so they
# are untrusted and render inside `<untrusted>` blocks, never at system level
CONTEXT_SECTION = "Assist request: {{assist_request_id}}. Context: {{context}}."


@dataclass(frozen=True)
class SpecialistEntry:
    key: str
    title: str
    router_description: str
    instructions: str
    tools: tuple[str, ...]
    required_permissions: tuple[str, ...]
    proposal_kinds: tuple[str, ...]


_ENTRIES = (
    SpecialistEntry(
        "research",
        "Research",
        "Answers questions about the project's data and existing work: which connections and "
        "tables exist, what their columns are, and what solutions the project already has.",
        "You are SETH's research specialist. Answer the person's question about the project's "
        "data catalog and existing solutions from what your tools return; say plainly when they "
        "return nothing that answers it." + _FOOTER,
        ("seth.catalog.read", "seth.solutions.read"),
        ("connection:read",),
        (),
    ),
    SpecialistEntry(
        "solution_design",
        "Solution design",
        "Proposes the shape of a solution: the agent, the tables it reads, the screens and the "
        "target kind, based on the project's data and its existing solutions and releases.",
        "You are SETH's solution design specialist. Propose the shape of a solution (agent, "
        "tables, screens, target kind) grounded in the project's catalog, solutions and "
        "releases. Answer in text; you make no proposal objects." + _FOOTER,
        ("seth.catalog.read", "seth.solutions.read", "seth.releases.read"),
        ("connection:read", "packaging:solution:read"),
        (),
    ),
    SpecialistEntry(
        "build",
        "Build",
        "Turns a brief into an app request: picks the connection and tables from the project's "
        "catalog and proposes the request for a person to accept.",
        "You are SETH's build specialist. Turn the person's brief into one app request over a "
        "read connection and discovered tables of the project, and propose it." + _FOOTER,
        ("seth.catalog.read", "seth.propose.app_request"),
        ("connection:read", "appgen:request:write"),
        ("appgen_request",),
    ),
    SpecialistEntry(
        "evaluation",
        "Evaluation",
        "Explains evaluation results of the project's releases and proposes an evaluation run.",
        "You are SETH's evaluation specialist. Explain the evaluation runs of the project's "
        "releases, threshold by threshold, and propose an evaluation run when one is "
        "needed." + _FOOTER,
        ("seth.releases.read", "seth.evaluations.read", "seth.propose.evaluation_run"),
        ("packaging:solution:read", "evaluation:run:read", "evaluation:run:request"),
        ("evaluation_run",),
    ),
    SpecialistEntry(
        "cost",
        "Cost",
        "Answers spend and budget questions: what the project's runs and models cost and how "
        "much of each budget is left.",
        "You are SETH's cost specialist. Answer spend and budget questions from the usage and "
        "budget figures your tools return; never estimate a figure they do not give." + _FOOTER,
        ("seth.costs.read", "seth.budgets.read"),
        ("usage:read", "budget:read"),
        (),
    ),
    SpecialistEntry(
        "deployment",
        "Deployment",
        "Explains release and target status (which release runs where, what was deployed and "
        "how it went) and prepares a delivery plan request.",
        "You are SETH's deployment specialist. Explain which release runs on which target and "
        "how its deployments went, and propose a delivery plan when the person asks to "
        "deploy." + _FOOTER,
        ("seth.board.read", "seth.targets.read", "seth.propose.delivery_plan"),
        (
            "delivery:target:read",
            "delivery:deployment:read",
            "delivery:plan:read",
            "delivery:plan:request",
        ),
        ("delivery_plan",),
    ),
)
CATALOG: Mapping[str, SpecialistEntry] = MappingProxyType({e.key: e for e in _ENTRIES})
KEYS: tuple[str, ...] = tuple(CATALOG)


def artifact_key(key: str) -> str:
    """The prompt and agent artifact key of catalog entry `key`."""
    return f"assist-{key}"


def tool_artifact_key(internal_key: str) -> str:
    """The tool artifact key of an internal function: `.` -> `-`, `_` kept."""
    return internal_key.replace(".", "-")


def tool_spec(internal_key: str) -> dict[str, Any]:
    """The `builtin.function` tool spec bound to `internal_key`, from its declaration."""
    decl = INTERNAL_FUNCTIONS[internal_key]
    return {
        "executor": "builtin.function",
        "binding": {"function_key": internal_key},
        "input_schema": copy.deepcopy(decl.input_schema),
        "output_schema": copy.deepcopy(decl.output_schema),
        "side_effect_class": decl.side_effect_class,
        "required_permissions": [],
        "invocation_policy": {},
        "timeout_s": TOOL_TIMEOUT_S,
        "retry": {"max_attempts": 1, "backoff_s": 0},
        "audit_level": "metadata",
        "idempotency": "key_supported" if decl.side_effect_class == "write_idempotent" else "none",
    }


def prompt_spec(key: str) -> dict[str, Any]:
    """The prompt `assist-<key>`: the entry's instructions as the system section and the two
    variables in the context section. The handover sets them, but any caller of a direct run can
    too, so they are untrusted; they are optional so the agent needs no `variables_map`."""
    return {
        "sections": {"system": CATALOG[key].instructions, "context": CONTEXT_SECTION},
        "variables": [
            {"name": name, "type": "string", "required": False, "default": "", "trusted": False}
            for name in VARIABLES
        ],
    }


def agent_spec(
    key: str,
    model_id: UUID,
    prompt_version_id: UUID,
    tool_version_ids: Sequence[UUID],
    max_cost: float,
) -> dict[str, Any]:
    """The agent `assist-<key>` over its published prompt and tool versions."""
    assert key in CATALOG
    return {
        "prompt_version_id": str(prompt_version_id),
        "model": {"model_id": str(model_id), "fallback_model_ids": []},
        "tool_version_ids": [str(v) for v in tool_version_ids],
        "skill_version_ids": [],
        "memory": {"mode": "none", "max_turns": 0},
        "limits": {**AGENT_LIMITS, "max_cost": max_cost},
        "execution_permissions": {
            "allowed_principal_types": ["user"],
            "required_permission": "agent:execute",
        },
        "approval_rules": {"require_approval_for": []},
        "variables_map": {},
    }
