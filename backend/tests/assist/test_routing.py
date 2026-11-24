"""SP7-3 (design 5.2): the deterministic router and its golden corpus.

Every corpus entry routes to its expected `RouteResult`. The corpus holds at least 60 entries, at
least 8 rule-decided ones per specialist, the six ambiguous requests of the design verbatim and at
least 6 with no candidate. Normalization folds width (NFKC), case and punctuation; a context adds
its key; an explicit pick wins; an unknown pick is `specialist_unknown`.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any  # Any: corpus JSON

import pytest

from shifde.assist import catalog, routing
from shifde.assist.routing import RouteResult, SpecialistUnknown

CORPUS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "routing_corpus.json").read_text(encoding="utf-8")
)
AMBIGUOUS = (
    "what will it cost to deploy release 3",
    "evaluate the cost of this agent",
    "design an evaluation for the churn agent",
    "research which release is live",
    "build an app and deploy it",
    "what is the budget for the evaluation run",
)


def test_the_corpus_routes_as_expected() -> None:
    wrong = []
    for entry in CORPUS:
        got = routing.route(entry["text"], entry.get("specialist"), entry.get("context_kind"))
        want = entry["expected"]
        if got != RouteResult(
            want["specialist"], want["method"], want["rule_id"], tuple(want["candidates"])
        ):
            wrong.append((entry["text"], got, want))
    assert wrong == []


def test_the_corpus_meets_its_minimums() -> None:
    assert len(CORPUS) >= 60
    decided = Counter(
        e["expected"]["specialist"]
        for e in CORPUS
        if e["expected"]["method"] == "rule" and not e.get("context_kind")
    )
    assert set(decided) == set(catalog.KEYS)
    assert min(decided.values()) >= 8, decided
    undecided = {
        e["text"]: e["expected"]["candidates"]
        for e in CORPUS
        if e["expected"]["method"] == "undecided" and not e.get("context_kind")
    }
    for text in AMBIGUOUS:
        assert len(undecided[text]) >= 2, text
    assert sum(1 for e in CORPUS if e["expected"]["candidates"] == []) >= 6


def test_normalization_folds_width_case_and_punctuation() -> None:
    assert routing.normalize("ＤＥＰＬＯＹ  the App!!") == "deploy the app"
    assert routing.normalize("Which—Tables?\n") == "which tables"
    assert routing.normalize("cost-centre's") == "cost centre s"
    assert routing.normalize("?!?") == ""
    assert routing.route("WHICH   TABLES?!", None, None).rule_id == "research"


def test_the_context_adds_its_key() -> None:
    for kind in ("release", "plan", "target", "deployment"):
        assert routing.route("hello", None, kind) == RouteResult(
            "deployment", "rule", f"context:{kind}", ("deployment",)
        )
    assert routing.route("hello", None, "evaluation_run").specialist == "evaluation"
    assert routing.route("hello", None, "solution").method == "undecided"
    # a rule's key and the same context key: one candidate, decided by the rule
    assert routing.route("deploy it", None, "release").rule_id == "deploy"
    # a rule's key and another context key: two candidates
    assert routing.route("what does it cost", None, "plan").candidates == ("cost", "deployment")


def test_an_explicit_pick_wins_and_an_unknown_one_is_refused() -> None:
    assert routing.route("deploy release 3", "research", "release") == RouteResult(
        "research", "explicit", None, ("research",)
    )
    with pytest.raises(SpecialistUnknown) as refused:
        routing.route("anything", "astrologer", None)
    assert refused.value.code == "specialist_unknown" and refused.value.status == 422
    assert routing.ROUTER_VERSION == "assist.router/1"
