"""SETH 3ba0e06 `test_p7_units.py`: the dev-sample-ontology test (needs the full app and the
sample database). The other ten tests of that file are in `tests/ontology/test_units.py` and
`tests/build/test_ontology_expr.py`."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p2util import login
from tests._support.p3util import ADMIN, A
from tests._support.p7util import CUSTOMER, R_ACCOUNTS, REQUEST, q, rows_of


# ================================================================== dev-sample-ontology
def test_dev_sample_ontology_seeds_a_working_idempotent_example(
    client: TestClient, world: World, owner_session: Session, settings: Settings, sample_db_url: str
) -> None:
    from shifde.cli.sample_ontology import seed_sample_ontology

    seeded = seed_sample_ontology(
        settings,
        org="demo-a",
        admin_email=ADMIN,
        admin_password=world.passwords[ADMIN],
        sample_url=sample_db_url,
    )
    assert seeded.created and seeded.policies_created == 9
    again = seed_sample_ontology(
        settings,
        org="demo-a",
        admin_email=ADMIN,
        admin_password=world.passwords[ADMIN],
        sample_url=sample_db_url,
    )
    assert not again.created and again.policies_created == 0
    assert again.ontology_id == seeded.ontology_id and again.version_id == seeded.version_id
    h = login(client, world.passwords, ADMIN)
    body = q(
        CUSTOMER,
        ["id", "name", "tax_id"],
        filter="id <= 3",
        traverse=[{"relationship": R_ACCOUNTS, "select": ["account_number"]}],
    )
    out = client.post(f"{A}/ontologies/{seeded.ontology_id}/query", json=body, headers=h)
    assert out.status_code == 200, out.text
    rows = out.json()["rows"]
    want = rows_of(sample_db_url, "SELECT id, tax_id FROM customers WHERE id <= 3 ORDER BY id")
    assert [(r["id"], r["tax_id"]) for r in rows] == [tuple(w) for w in want]
    # the action targets the builtin echo tool; the ontology lists it
    version = client.get(
        f"{A}/ontologies/{seeded.ontology_id}/versions/{seeded.version_id}", headers=h
    ).json()
    assert [a["key"] for a in version["spec"]["actions"]] == ["cs.show_request_summary"]
    # nothing was copied: only metadata exists on the SHIFDE side
    assert REQUEST in str(version["spec"]) and "Customer 001" not in str(version["spec"])
