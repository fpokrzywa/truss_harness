"""Executor tests: unit tests of the client, broker and entry point, and runner tests against the
in-process gateway (the delivery tests' app, world, people and executor fixtures) and the fake
platform. Nothing here contacts a real Databricks endpoint."""

from __future__ import annotations

from tests.delivery.conftest import client, executor_a, flow, owner_session, people
from tests.tenancy.conftest import world

__all__ = ["client", "executor_a", "flow", "owner_session", "people", "world"]
