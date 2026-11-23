"""No credential of a `databricks` connection appears in any output (CM-3, issue #161): for bearer
and OAuth M2M, across passing and failing tests, discovery, invoke results and failures, and the
repr of the adapter, the session and its tokens. Fakes only."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any  # Any: collected outputs

import pytest

from shifde.adapters import oauth2
from shifde.adapters.base import AdapterFailure
from shifde.adapters.dataplatforms.databricks import DatabricksAdapter
from shifde.adapters.dataplatforms.fakes import FAULTS, SELECT_ONE, FakeDatabricksWorkspace
from shifde.adapters.oauth2 import AUTH_METHOD as OAUTH2
from shifde.core.ids import uuid7
from tests.adapters.dataplatforms.helpers import CLIENT_SECRET, PAT, conn, ctx_for, make_fake

ADAPTER = DatabricksAdapter()


def _outputs(fake: FakeDatabricksWorkspace, auth: str, secret: str) -> list[str]:
    """Every text the adapter hands back for one connection, with and without faults."""
    tenant = uuid7()
    connection_id = uuid7()
    texts: list[str] = [repr(ADAPTER)]

    def c(**config: Any) -> Any:  # Any: ConnectionConfig
        return conn(auth, secret, connection_id=connection_id, **config)

    out = ADAPTER.test(ctx_for(fake, tenant=tenant), c())
    texts.append(json.dumps([asdict(x) for x in out.checks]) + str(out.message))
    texts.append(json.dumps(ADAPTER.discover(ctx_for(fake, tenant=tenant), c()).payload))
    session = ADAPTER.open(ctx_for(fake, tenant=tenant), c())
    texts.append(repr(session) + repr(getattr(session, "_auth", None)))
    texts.append(json.dumps(session.invoke({"sql": SELECT_ONE}).result))
    session.close()
    texts.append(repr(oauth2.TOKEN_CACHE))
    if auth == OAUTH2:
        tokens = oauth2.OAuth2Tokens(
            ctx_for(fake, tenant=tenant),
            conn(OAUTH2, secret, connection_id=connection_id, oauth2_url="https://x/oidc/v1/token"),
        )
        texts.append(repr(tokens))
    for fault in sorted(FAULTS):
        fake.faults = {fault}
        fake.retry_after = "0"
        out = ADAPTER.test(ctx_for(fake, tenant=tenant, deadline=3), c())
        texts.append(json.dumps([asdict(x) for x in out.checks]) + str(out.message))
        for call in (
            lambda: ADAPTER.discover(ctx_for(fake, tenant=tenant, deadline=3), c()),
            lambda: ADAPTER.open(ctx_for(fake, tenant=tenant, deadline=2.5), c()).invoke(
                {"sql": SELECT_ONE}
            ),
        ):
            try:
                texts.append(json.dumps(call().__dict__, default=str))  # type: ignore[attr-defined]
            except AdapterFailure as exc:
                texts.append(exc.message + str(exc) + repr(exc))
    fake.faults = set()
    with pytest.raises(AdapterFailure) as raised:
        ADAPTER.open(ctx_for(fake, tenant=tenant), c()).invoke({"sql": "DROP TABLE t"})
    texts.append(raised.value.message)
    return texts


@pytest.mark.parametrize("auth", ["bearer", OAUTH2])
def test_no_credential_or_token_in_any_output(auth: str) -> None:
    fake = make_fake()
    secret = PAT if auth == "bearer" else CLIENT_SECRET
    texts = _outputs(fake, auth, secret)
    leaked = {secret, PAT, CLIENT_SECRET, *fake.issued}
    if auth == OAUTH2:
        assert fake.issued, "the OAuth flow ran"
    for text in texts:
        for value in leaked:
            assert value not in text, (value, text[:200])


def test_a_wrong_secret_is_never_echoed() -> None:
    fake = make_fake()
    wrong = "dbx-wrong-secret-0009"
    out = ADAPTER.test(ctx_for(fake, tenant=uuid7()), conn(OAUTH2, wrong))
    rendered = json.dumps([asdict(x) for x in out.checks]) + str(out.message)
    assert wrong not in rendered and CLIENT_SECRET not in rendered
