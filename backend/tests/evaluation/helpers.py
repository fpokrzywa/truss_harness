"""Shortcuts of the evaluation tests: a sealed release of an organization's build stack (the
packaging tests' stack, in project `main/sample`) whose model binding names a served model of the
same fake provider (`served-chat`, priced in USD unless asked otherwise), the dataset its version
names, content capture, evaluation requests and waiting for a scored evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any  # Any: JSON bodies

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests._support.p2util import post
from tests._support.p3util import A
from tests._support.p5util import Hdr, wait_for
from tests._support.p8util import rate_card, rate_version
from tests.packaging.helpers import (
    Stack,
    bindings,
    dataset,
    new_version,
    seal,
    solution,
    stack,
    version_body,
)

SERVED = "served-chat"
ENDED = {"passed", "failed", "error"}


def case(case_id: str, message: str, *assertions: tuple[str, Any]) -> dict[str, Any]:
    """A case with `{"message": message}` input; each assertion is `(kind, value)`."""
    return {
        "case_id": case_id,
        "input": {"message": message},
        "assertions": [
            {"id": f"a{i}", "kind": kind, "value": value}
            for i, (kind, value) in enumerate(assertions, 1)
        ],
    }


def capture(owner: Session, org_slug: str, mode: str) -> None:
    """Set the organization's `capture_content` as the schema owner."""
    owner.execute(
        text(
            "UPDATE shifde.tenants SET settings = coalesce(settings, '{}'::jsonb) || "
            "jsonb_build_object('capture_content', CAST(:m AS text)) WHERE slug = :s"
        ),
        {"m": mode, "s": org_slug},
    )
    owner.commit()


@dataclass(frozen=True)
class Release:
    h: Hdr
    stack: Stack
    served: str  # the client's own model the binding names
    dataset: dict[str, Any]
    solution: str
    version: str
    id: str
    digest: str
    org: str

    def evaluations(self) -> str:
        return f"{self.org}/releases/{self.id}/evaluations"


def served_model(client: TestClient, h: dict[str, str], s: Stack, org: str = A) -> str:
    model = post(
        client,
        f"{org}/models",
        h,
        {
            "provider_id": s.provider,
            "model_key": SERVED,
            "display_name": "Served chat",
            "capabilities": ["chat"],
            "context_window": 8192,
            "max_output_tokens": 1024,
            "lifecycle": "active",
            "rate_limits": {},
        },
    )
    return str(model["id"])


def release(
    h: Hdr,
    owner: Session,
    cases: list[dict[str, Any]],
    *,
    org: str = A,
    org_slug: str = "demo-a",
    min_accuracy: str = "0.9",
    max_p95_latency_ms: int = 600_000,
    max_total_cost_usd: str = "1",
    priced: bool = True,
    served: str = SERVED,
    name: str = "Evaluated",
    target_kind: str = "databricks.app/v1",
) -> Release:
    """A sealed release of `org`'s stack agent whose dataset holds `cases`, its model pin bound
    to `served` on the stack's provider, of a solution of `target_kind`; capture is full."""
    c = h.client
    s = stack(c, h, org)
    model = served_model(c, h, s, org)
    if priced:
        card = rate_card(c, h, model_key=SERVED, base=org)
        rate_version(c, h, card["id"], base=org, currency="USD", input_rate=1, output_rate=1)
    golden = dataset(c, h, f"{name}-cases", org=org, cases=cases)
    sid = solution(c, h, name, org, target_kind)["id"]
    body = version_body(s.agent_v1, dataset=golden["id"], min_accuracy=min_accuracy)
    body["evaluation"].update(
        max_p95_latency_ms=max_p95_latency_ms, max_total_cost_usd=max_total_cost_usd
    )
    vid = new_version(c, h, sid, body, org)["id"]
    sealed = seal(c, h, sid, vid, bindings({s.model: s.provider}, served=served), org)
    assert sealed.status_code == 201, sealed.text
    capture(owner, org_slug, "full")
    return Release(h, s, model, golden, sid, vid, sealed.json()["id"], sealed.json()["digest"], org)


def request(rel: Release) -> httpx.Response:
    return rel.h.client.post(rel.evaluations(), headers=rel.h, json={})


def get_run(rel: Release, run_id: str) -> dict[str, Any]:
    r = rel.h.client.get(f"{rel.org}/evaluation-runs/{run_id}", headers=rel.h)
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def wait_scored(rel: Release, run_id: str, timeout: float = 60.0) -> dict[str, Any]:
    def scored() -> dict[str, Any] | None:
        got = get_run(rel, run_id)
        return got if got["status"] in ENDED else None

    out: dict[str, Any] = wait_for(scored, timeout)
    return out
