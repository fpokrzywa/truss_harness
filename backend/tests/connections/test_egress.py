"""`shifde.connections.egress`: the effective guard for a connection and rule validation.

SETH's egress unit tests (`test_p3_egress.py`, `shifde.core.egress`) already live in
`tests/adapters/` (B-305); SETH's egress API tests (`test_p3_connections.py`) move with the routes
in part 2. These pin the data-layer half: a connection's guard is built from the installation
default, its own client's default and its own client's policy only (B-1503).
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text

from shifde.adapters.registry import AdapterRegistry
from shifde.adapters.runtime import IntegrationRuntime
from shifde.connections import egress
from shifde.connections import schemas as s
from shifde.core.db import Database
from shifde.core.egress import EgressPolicy
from shifde.core.errors import ValidationFailed
from shifde.core.settings import Settings
from tests.connections.conftest import Pair, owner_policy
from tests.tenancy.conftest import app_session

DENY_ALL = EgressPolicy(allowed_schemes=frozenset({"__none__"}))


def _runtime(settings: Settings) -> IntegrationRuntime:
    return IntegrationRuntime(settings=settings, registry=AdapterRegistry.load(include_fake=True))


def _row(app_db: Database, tenant: UUID, policy: UUID) -> EgressPolicy:
    with app_session(app_db, tenant) as sess:
        row = (
            sess.execute(text("SELECT * FROM shifde.egress_policies WHERE id = :i"), {"i": policy})
            .mappings()
            .one()
        )
        return egress.policy_from_row(row)


def test_a_connection_guard_uses_only_its_own_clients_policies(
    settings: Settings, app_db: Database, owner_db: Database, pair: Pair
) -> None:
    a, b = pair.a.tenant, pair.b.tenant
    with owner_db.session() as o:
        b_default = owner_policy(o, b, is_default=True)
        o.commit()
    runtime = _runtime(settings)
    with app_session(app_db, a) as sess:
        no_policy = egress.build_guard(sess, runtime, a, None).layers
        own = egress.build_guard(sess, runtime, a, pair.a.policy).layers
        theirs = egress.build_guard(sess, runtime, a, pair.b.policy).layers
        installation = egress.build_guard(sess, runtime, a, pair.installation_policy).layers
        # the key names B while the context is A's: refused outright (review 1 item 1)
        with pytest.raises(RuntimeError, match="own client's context"):
            egress.build_guard(sess, runtime, b, pair.b.policy)
    assert len(no_policy) == 1  # installation default only: B's client default is not A's
    assert own == (*no_policy, _row(app_db, a, pair.a.policy))
    assert theirs == (*no_policy, DENY_ALL)  # a dangling or foreign reference denies everything
    assert installation == (*no_policy, DENY_ALL)
    with app_session(app_db, b) as sess:
        b_layers = egress.build_guard(sess, runtime, b, None).layers
    assert b_layers == (*no_policy, _row(app_db, b, b_default))


def test_rule_values_are_validated_and_normalised() -> None:
    rules = s.EgressRules(
        allowed_schemes=["HTTPS", "https"],
        allowed_ports=[443, 443, 8443],
        allowed_hosts=["API.Example.com", "*.example.org"],
        allowed_cidrs=["10.1.2.3/8"],
    )
    assert egress._rule_values(rules) == {
        "allowed_schemes": ["https"],
        "allowed_ports": [443, 8443],
        "allowed_hosts": ["api.example.com", "*.example.org"],
        "allowed_cidrs": ["10.0.0.0/8"],
    }
    for bad in (
        {"allowed_schemes": ["gopher"]},
        {"allowed_ports": [0]},
        {"allowed_hosts": ["bad host"]},
        {"allowed_cidrs": ["not-a-cidr"]},
    ):
        with pytest.raises(ValidationFailed):
            egress._rule_values(s.EgressRules.model_validate(bad))


def test_a_guard_is_built_only_in_its_own_clients_context(
    settings: Settings, app_db: Database, owner_db: Database, pair: Pair
) -> None:
    """Connections review 1 item 1: with no client context, or another client's, RLS would hide
    the client's own restrictive default and the guard would silently leave it out."""
    a = pair.a.tenant
    with owner_db.session() as o:
        a_default = owner_policy(o, a, is_default=True)
        o.commit()
    runtime = _runtime(settings)
    with app_session(app_db, a) as sess:  # own context first: A's default is a layer
        own = egress.build_guard(sess, runtime, a, None).layers
    assert own[-1] == _row(app_db, a, a_default) and len(own) == 2
    with app_session(app_db, None) as sess, pytest.raises(RuntimeError):
        egress.build_guard(sess, runtime, a, None)
    with app_session(app_db, pair.b.tenant) as sess, pytest.raises(RuntimeError):
        egress.build_guard(sess, runtime, a, None)
