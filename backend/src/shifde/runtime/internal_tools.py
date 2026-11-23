"""The worker's internal tool registry (SP7 design section 3).

A `builtin.function` tool may bind a `seth.*` key declared in `build.internal_functions`. The
functions live in modules above `runtime` (`assist`), so `runtime` never imports them: the
composition root (`worker_main.internal_tools`) builds an `InternalToolRegistry` of key -> function
and hands it to `runtime.worker.Worker`, which passes it through `lifecycle.advance` to the run's
`RunContext`. `steps.py` calls the function inside the run, in the step's session (the run's
client context) and under the run principal, after the same arguments check, gate, approval and
write-ahead journal as any tool. The runtime never commits inside the call: the function's writes
become durable with the step result, and a raise rolls them back. A run whose context has no
registry, or whose registry lacks the key, fails the call with `internal_function_unavailable`.

In-process workers: `tests/_support/p5util.thread_worker` passes `worker_main.internal_tools()`;
`p5util.SubprocessWorker` (`python -m shifde.runtime.worker`) does not.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any  # Any: JSON arguments / results
from uuid import UUID

from sqlalchemy.orm import Session

from shifde.build.internal_functions import INTERNAL_FUNCTIONS
from shifde.tenancy.scope import TenantScope


@dataclass(frozen=True)
class InternalToolCall:
    """One call of an internal function inside a run."""

    session: Session  # the run's step session (client context of the run)
    scope: TenantScope  # the run context's scope (the run principal)
    tenant_id: UUID
    workspace_id: UUID
    project_id: UUID
    run_id: UUID
    step_key: str  # the step's idempotency key
    args: dict[str, Any]  # already checked against the tool's input schema


InternalToolFn = Callable[[InternalToolCall], Any]  # Any: the function's JSON result


class InternalToolRegistry(Mapping[str, InternalToolFn]):
    """A frozen mapping of internal function key to function."""

    __slots__ = ("_functions",)

    def __init__(self, functions: Mapping[str, InternalToolFn]) -> None:
        self._functions: Mapping[str, InternalToolFn] = MappingProxyType(dict(functions))

    @classmethod
    def of(cls, functions: Mapping[str, InternalToolFn]) -> InternalToolRegistry:
        """The registry of `functions`; `ValueError` for a key not in `INTERNAL_FUNCTIONS`."""
        for key in functions:
            if key not in INTERNAL_FUNCTIONS:
                raise ValueError(f"internal function {key!r} is not declared")
        return cls(functions)

    def __getitem__(self, key: str) -> InternalToolFn:
        return self._functions[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._functions)

    def __len__(self) -> int:
        return len(self._functions)
