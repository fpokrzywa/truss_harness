"""The deterministic router of an assist request (SP7 design section 5.2): pure, no I/O.

An explicit specialist wins. Otherwise the text is normalized (NFKC, lowercase, `[a-z0-9]+`
tokens joined by single spaces) and the rules run in table order, each `\\b(?:...)\\b` searched in
the normalized text; a request context adds its key. Exactly one candidate decides (`rule`, by
the first matching rule or `context:<kind>`); none or several leave it `undecided`, for the model
fallback or a refusal.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from shifde.assist.catalog import CATALOG
from shifde.core.errors import AppError

ROUTER_VERSION = "assist.router/1"
Method = Literal["explicit", "rule", "undecided"]
CONTEXT_KINDS = ("solution", "release", "target", "plan", "deployment", "evaluation_run")

# (rule_id, key, alternatives), in table order
RULES: tuple[tuple[str, str, str], ...] = (
    ("deploy", "deployment", r"deploy\w*|rollout|roll out|promote|release to"),
    (
        "status",
        "deployment",
        r"which release|what release|where is|target status|is live|is running",
    ),
    ("cost", "cost", r"cost\w*|spend\w*|spent|budget\w*|tokens?|price\w*|bill\w*"),
    ("eval", "evaluation", r"evaluat\w*|test cases?|accuracy|thresholds?"),
    (
        "build",
        "build",
        r"(?:build|draft|generate|create|make)(?: an?)? (?:agent|tool|prompt|screen|app)",
    ),
    (
        "design",
        "solution_design",
        r"design\w*|architect\w*|approach|how should (?:we|i) (?:build|structure)",
    ),
    (
        "research",
        "research",
        r"research\w*|look up|find out|which tables|what data|summari[sz]e"
        r"|review (?:the|this) document",
    ),
)
_COMPILED = tuple((rule_id, key, re.compile(rf"\b(?:{alt})\b")) for rule_id, key, alt in RULES)
CONTEXT_KEYS: dict[str, str | None] = {
    "release": "deployment",
    "plan": "deployment",
    "target": "deployment",
    "deployment": "deployment",
    "evaluation_run": "evaluation",
    "solution": None,
}
_TOKEN = re.compile(r"[a-z0-9]+")


class SpecialistUnknown(AppError):
    status, code, title = 422, "specialist_unknown", "Unprocessable Entity"


@dataclass(frozen=True)
class RouteResult:
    specialist: str | None
    method: Method
    rule_id: str | None
    candidates: tuple[str, ...]


def normalize(text: str) -> str:
    """NFKC, lowercase, the `[a-z0-9]+` tokens joined by single spaces."""
    return " ".join(_TOKEN.findall(unicodedata.normalize("NFKC", text).lower()))


def route(text: str, specialist: str | None, context_kind: str | None) -> RouteResult:
    """The routing decision for `text` (and the user's pick and the context's kind, when given);
    an unknown `specialist` raises `SpecialistUnknown`."""
    if specialist is not None:
        if specialist not in CATALOG:
            raise SpecialistUnknown(f"{specialist} is not a catalog key")
        return RouteResult(specialist, "explicit", None, (specialist,))
    normalized = normalize(text)
    candidates: list[str] = []
    first: str | None = None
    for rule_id, key, pattern in _COMPILED:
        if pattern.search(normalized):
            first = first or rule_id
            if key not in candidates:
                candidates.append(key)
    by_context = CONTEXT_KEYS.get(context_kind) if context_kind is not None else None
    if by_context is not None and by_context not in candidates:
        candidates.append(by_context)
    if len(candidates) == 1:
        rule_id = first if first is not None else f"context:{context_kind}"
        return RouteResult(candidates[0], "rule", rule_id, tuple(candidates))
    return RouteResult(None, "undecided", None, tuple(candidates))
