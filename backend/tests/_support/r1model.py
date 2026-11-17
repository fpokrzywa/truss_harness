"""Helpers of SETH 3ba0e06 `backend/tests/test_r1_model.py` (`DONE`, `drive`, `pipeline`,
`drive_pipeline`), copied unchanged except for the imports. `tests/app/test_r1_round6.py` and
`test_r1_round7.py` import them from here because `test_r1_model.py` itself is not ported yet; a
later port of that file should import them from here instead of redefining them."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shifde.core.settings import Settings
from tests._support.fullapp import World
from tests._support.p5util import thread_worker, wait_run
from tests._support.p6util import World6
from tests.app.test_p6_exec2 import hang_pipeline
from tests.app.test_r1_round3 import PARKED, Scripted
from tests.app.test_r1_unknown_outcome import UNKNOWN, decide, pending_unknown

DONE = {"completed", "failed", "cancelled", "timed_out"}


def drive(
    client: TestClient,
    h: dict[str, str],
    ah: Any,
    settings: Settings,
    run: dict[str, Any],
    decisions: list[str],
) -> dict[str, Any]:
    """Park, decide, park again ... and wait for the run to end (or park once more)."""
    with thread_worker(settings, lease=5, reap=1000):
        for d in decisions:
            wait_run(client, h, run["id"], PARKED, timeout=90)
            [appr] = pending_unknown(ah, run["id"])
            assert decide(ah, appr["id"], d, "looked at the target").status_code == 200
        return wait_run(client, h, run["id"], DONE | PARKED, timeout=90)


def pipeline(
    client: TestClient, world: World, owner: Session, rest: Any, db: str, srv: Scripted
) -> tuple[World6, dict[str, Any]]:
    w = World6(client, world, owner, rest, db)
    # SETH's ignore: hang_pipeline is typed for p6util.HangServer; Scripted is duck-typed (url)
    pipe = hang_pipeline(w, srv, write=True)  # type: ignore[arg-type]
    return w, w.run(pipe["id"], {"doc": {"a": 1}})


def drive_pipeline(
    w: World6, settings: Settings, run: dict[str, Any], decisions: list[str]
) -> dict[str, Any]:
    with thread_worker(settings, lease=5, reap=1000):
        for d in decisions:
            w.wait_parked(run["id"], UNKNOWN, timeout=90)
            [appr] = [a for a in w.pending_approvals() if a["reason"] == UNKNOWN]
            assert w.decide(appr["id"], d, "looked at the target").status_code == 200
        return wait_run(w.client, w.h, run["id"], DONE | PARKED, timeout=90)
