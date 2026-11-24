"""Run state machine, failure classification, control-flow exceptions (SYSTEM_ARCHITECTURE 6.2/6.7).

`ALLOWED` mirrors the `run_state_transitions` table seeded by migration `runtime_0001` (SETH
`p05_0001_runtime`); a unit test asserts they are equal, and the database trigger enforces the
table even for buggy code.
"""

from __future__ import annotations

PENDING = "pending"
RUNNING = "running"
WAITING = "waiting_for_approval"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
TIMED_OUT = "timed_out"

STATUSES = (PENDING, RUNNING, WAITING, COMPLETED, FAILED, CANCELLED, TIMED_OUT)
TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED, TIMED_OUT})
ALLOWED: dict[str, frozenset[str]] = {
    PENDING: frozenset({RUNNING, CANCELLED, FAILED, TIMED_OUT}),
    RUNNING: frozenset({WAITING, PENDING, COMPLETED, FAILED, CANCELLED, TIMED_OUT}),
    WAITING: frozenset({PENDING, FAILED, CANCELLED, TIMED_OUT}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
    TIMED_OUT: frozenset(),
}

# ---- failure classification (6.7)
VALIDATION = "validation"
AUTHZ_DENIED = "authz_denied"
BUDGET_DENIED = "budget_denied"
PROVIDER_UNAVAILABLE = "provider_unavailable"
RATE_LIMITED = "rate_limited"
TIMEOUT = "timeout"
TRANSIENT_NETWORK = "transient_network"
PROVIDER_ERROR_4XX = "provider_error_4xx"
TOOL_ERROR = "tool_error"
SCHEMA_VIOLATION = "schema_violation"
APPROVAL_REJECTED = "approval_rejected"
CANCELLED_CLASS = "cancelled"
INDETERMINATE = "indeterminate_side_effect"
INTERNAL = "internal"
# a `seth.*` tool whose function the worker's registry does not provide (never retried)
INTERNAL_FUNCTION_UNAVAILABLE = "internal_function_unavailable"

RETRYABLE = frozenset({PROVIDER_UNAVAILABLE, RATE_LIMITED, TIMEOUT, TRANSIENT_NETWORK})
# classes after which the next model in the fallback chain is tried
FALLBACK = frozenset({PROVIDER_UNAVAILABLE, RATE_LIMITED, TIMEOUT, TRANSIENT_NETWORK})

# ---- tool side effects
NO_RETRY_SIDE_EFFECTS = frozenset({"write_non_idempotent", "destructive"})
WRITE_SIDE_EFFECTS = frozenset({"write_idempotent", "write_non_idempotent", "destructive"})


class RunInterrupt(Exception):
    """Base of the control-flow exceptions that end (or suspend) one advance of a run."""


class LeaseLost(RunInterrupt):
    """Another worker (or the reaper) owns the run now: write nothing further."""


class Cancelled(RunInterrupt):
    """Cooperative cancellation was requested."""


class TimedOut(RunInterrupt):
    def __init__(self, message: str = "the run deadline passed") -> None:
        super().__init__(message)
        self.message = message


class Shutdown(RunInterrupt):
    """The worker is stopping: give the run back (running -> pending)."""


class Waiting(RunInterrupt):
    """The run must wait (approval, indeterminate side effect, child runs)."""

    def __init__(self, reason: str, *, transitioned: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        # True when the raiser already moved the run to waiting inside its own transaction
        # (child runs: the parent must be `waiting` before any child can finish).
        self.transitioned = transitioned


class RunFailed(RunInterrupt):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.message = message


class GateDenied(RunFailed):
    """The Execution Gate refused an action (non-retryable, recorded in policy_decisions)."""

    def __init__(self, error_class: str, message: str, reason: str) -> None:
        super().__init__(error_class, message)
        self.reason = reason
