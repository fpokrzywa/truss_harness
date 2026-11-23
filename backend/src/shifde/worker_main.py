"""`shifde-worker`: the composition root of the worker (SP5 design section 3, R1).

`runtime` sits below the modules whose run-less jobs the worker also runs (`appgen`), so it never
imports them; this top-layer module registers their handlers (`job_handlers`) and starts
`runtime.worker.run` with them. In-process workers in tests and demos pass `job_handlers()` too.
"""

from __future__ import annotations

import sys

from shifde.appgen import service as appgen
from shifde.runtime import worker
from shifde.runtime.jobkinds import JobKindRegistry


def job_handlers() -> JobKindRegistry:
    """The job kinds of the modules above `runtime`, with their handlers."""
    return JobKindRegistry.of({appgen.DRAFT_JOB_KIND: appgen.run_draft_job})


def run(argv: list[str] | None = None) -> int:
    return worker.run(argv, job_handlers=job_handlers())


if __name__ == "__main__":
    sys.exit(run())
