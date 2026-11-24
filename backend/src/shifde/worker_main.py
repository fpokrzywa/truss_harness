"""`shifde-worker`: the composition root of the worker (SP5 design section 3, R1; SP7 design
section 3).

`runtime` sits below the modules whose run-less jobs the worker also runs (`appgen`) and whose
`seth.*` internal functions its runs call (`assist`), so it never imports them; this top-layer
module registers their job handlers (`job_handlers`) and internal functions (`internal_tools`) and
starts `runtime.worker.run` with both. In-process workers in tests and demos pass `job_handlers()`
where they run those jobs, and `p5util.thread_worker` passes `internal_tools()`.
"""

from __future__ import annotations

import sys

from shifde.appgen import service as appgen
from shifde.runtime import worker
from shifde.runtime.internal_tools import InternalToolRegistry
from shifde.runtime.jobkinds import JobKindRegistry


def job_handlers() -> JobKindRegistry:
    """The job kinds of the modules above `runtime`, with their handlers."""
    return JobKindRegistry.of({appgen.DRAFT_JOB_KIND: appgen.run_draft_job})


def internal_tools() -> InternalToolRegistry:
    """The `seth.*` internal functions runs may call (none yet: SP7-4 registers them)."""
    return InternalToolRegistry.of({})


def run(argv: list[str] | None = None) -> int:
    return worker.run(argv, job_handlers=job_handlers(), internal_tools=internal_tools())


if __name__ == "__main__":
    sys.exit(run())
