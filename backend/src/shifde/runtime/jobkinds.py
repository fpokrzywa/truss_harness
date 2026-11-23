"""The worker's job-kind registry (SP5 design section 3, R1).

A module above `runtime` (the solution generator, `appgen`) runs run-less jobs on the worker
without `runtime` importing it: the composition root (`worker_main`, the `shifde-worker` entry
point) builds a `JobKindRegistry` of kind -> handler and hands it to `runtime.worker.Worker`. A
handler has the signature of `connections.service.run_discover_job`: it runs the job of its
client and returns None (the job ends `done`) or an error code (the job ends `dead` with that
code). A kind of the worker's own (`run.advance`, `connection.*`, `model.*`) cannot be registered.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from types import MappingProxyType
from uuid import UUID

from shifde.adapters.runtime import IntegrationRuntime
from shifde.core.db import Database

JobHandler = Callable[[Database, IntegrationRuntime, UUID, UUID], str | None]
BUILT_IN_KIND = "run.advance"
BUILT_IN_PREFIXES = ("connection.", "model.")


class JobKindRegistry(Mapping[str, JobHandler]):
    """A frozen mapping of job kind to handler."""

    __slots__ = ("_handlers",)

    def __init__(self, handlers: Mapping[str, JobHandler]) -> None:
        self._handlers: Mapping[str, JobHandler] = MappingProxyType(dict(handlers))

    @classmethod
    def of(cls, handlers: Mapping[str, JobHandler]) -> JobKindRegistry:
        """The registry of `handlers`; `ValueError` for a kind that collides with a built-in."""
        for kind in handlers:
            if kind == BUILT_IN_KIND or kind.startswith(BUILT_IN_PREFIXES):
                raise ValueError(f"job kind {kind!r} collides with a built-in kind")
        return cls(handlers)

    def __getitem__(self, kind: str) -> JobHandler:
        return self._handlers[kind]

    def __iter__(self) -> Iterator[str]:
        return iter(self._handlers)

    def __len__(self) -> int:
        return len(self._handlers)
