"""Helpers for the P8 tests: rate cards, priced stacks, budgets, usage rows and local servers."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.p2util import post, tenant_id
from tests._support.p3util import A
from tests._support.p5util import Stack

CURRENCY = "XTS"  # ISO 4217 code reserved for testing: never a real currency
PAST = "2000-01-01T00:00:00Z"
IN_RATE = 1000.0  # per 1M tokens: 1 input token = 0.001
OUT_RATE = 2000.0  # per 1M tokens: 1 output token = 0.002
SCRIPT = [{"match": "weather", "reply": "sunny and warm today"}]


def rate_card(
    client: TestClient,
    h: dict[str, str],
    model_key: str = "chat-a",
    provider_key: str = "fake",
    *,
    base: str = A,
) -> dict[str, Any]:
    out: dict[str, Any] = post(
        client,
        f"{base}/rate-cards",
        h,
        {"provider_key": provider_key, "model_key": model_key, "name": f"TEST {model_key}"},
    )
    return out


def rate_version(
    client: TestClient,
    h: dict[str, str],
    card_id: str,
    *,
    base: str = A,
    input_rate: float = IN_RATE,
    output_rate: float = OUT_RATE,
    effective_from: str = PAST,
    effective_to: str | None = None,
    status: int = 201,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "effective_from": effective_from,
        "currency": CURRENCY,
        "unit": "per_1m_tokens",
        "input_rate": input_rate,
        "output_rate": output_rate,
        "source": "TEST rate card - not vendor pricing",
        "revision": "t-1",
        **extra,
    }
    if effective_to:
        body["effective_to"] = effective_to
    out: dict[str, Any] = post(client, f"{base}/rate-cards/{card_id}/versions", h, body, status)
    return out


def priced_stack(
    client: TestClient,
    h: dict[str, str],
    owner: Session,
    *,
    script: list[dict[str, str]] | None = None,
    priced: bool = True,
    **kwargs: Any,
) -> Stack:
    """A Stack on provider `fake` / model `chat-a`, with a tenant rate card in force."""
    stack = Stack(client, h, owner, script=SCRIPT if script is None else script, **kwargs)
    if priced:
        card = rate_card(client, h)
        rate_version(client, h, card["id"])
    return stack


def budget(
    client: TestClient,
    h: dict[str, str],
    scope: dict[str, str],
    *,
    metric: str = "tokens",
    amount: float = 100000,
    period: str = "daily",
    base: str = A,
    status: int = 201,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "scope": scope,
        "period": period,
        "amount": amount,
        "metric": metric,
        **({"currency": CURRENCY} if metric == "cost" else {}),
        **extra,
    }
    out: dict[str, Any] = post(client, f"{base}/budgets", h, body, status)
    return out


def project_scope(owner: Session, slug: str = "sample", org: str = "demo-a") -> dict[str, str]:
    pid = owner.execute(
        text("SELECT id FROM shifde.projects WHERE slug = :s AND tenant_id = :t"),
        {"s": slug, "t": tenant_id(owner, org)},
    ).scalar_one()
    return {"type": "project", "id": str(pid)}


def agent_scope(agent_id: str) -> dict[str, str]:
    return {"type": "agent", "id": agent_id}


def events_of(owner: Session, run_id: str) -> list[dict[str, Any]]:
    rows = owner.execute(
        text(
            "SELECT id, event_type, status, event_key, input_tokens, output_tokens, cost_kind, "
            "cost_amount, currency, rate_card_version_id, rate_snapshot, project_id, run_id, "
            "root_run_id, attempt FROM shifde.usage_events WHERE root_run_id = :r "
            "ORDER BY occurred_at, id"
        ),
        {"r": run_id},
    ).mappings()
    return [dict(r) for r in rows]


def fast_settings(settings: Settings, **over: Any) -> Settings:
    return settings.model_copy(update={"runtime_retry_base_seconds": 0.01, **over})


class CaptureServer:
    """A local HTTP server that records POST bodies (an OTLP/HTTP collector stand-in)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: Any) -> None:
                return None

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> CaptureServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


@contextmanager
def tampered(owner: Session, sql: str, **params: Any) -> Any:
    """Run owner SQL that simulates tampering/drift (the app role cannot do this)."""
    owner.execute(text(sql), params)
    owner.commit()
    yield
