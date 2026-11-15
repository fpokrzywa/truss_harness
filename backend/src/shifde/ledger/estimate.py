"""What a model call may use and cost, before and after it ran (R3 cost: Items 9, 10, 11).

* Item 9: a hard budget only holds if what is reserved for a call covers what the call can use.
  `call_hold_tokens` is input estimate + output ceiling (the `max_tokens` the caller sends), and
  refuses to hold anything when there is no output ceiling. `estimate_input_tokens` is
  deliberately high (one token per three UTF-8 bytes plus a per-message overhead): English
  averages about four characters per token, code about three, CJK one token per three-byte
  character, so it does not undercount ordinary text.
* Item 10: `call_cost` is the worst-case cost of one call (input estimate, full output ceiling) at
  the rate in force now; `limits.max_cost` compares against it before the call.
* Item 11: `resolve_usage` turns what the provider reported (a count, or nothing: `None`) into the
  counts the ledger stores. A count the provider did not report is replaced by the platform's own
  estimate, never by 0, and the result says which fields were estimated.

Pure functions apart from `call_cost`, which reads rate cards on the caller's session.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol  # Any: JSON document
from uuid import UUID

from sqlalchemy.orm import Session

from shifde.core.clock import utcnow
from shifde.ledger import pricing

MESSAGE_OVERHEAD_TOKENS = 4  # role and framing tokens each chat message costs
BYTES_PER_TOKEN = 3  # deliberately below the usual ~4 characters per token


class MissingOutputCeiling(ValueError):
    """The call has no output ceiling (`max_tokens` absent or below 1), so no hold can cover it."""


def estimate_text_tokens(text: str) -> int:
    """High estimate of the tokens in `text`: UTF-8 bytes / 3, rounded up (0 for empty text)."""
    size = len(text.encode("utf-8"))
    return -(-size // BYTES_PER_TOKEN)


def estimate_input_tokens(messages: Sequence[Mapping[str, str]]) -> int:
    return sum(estimate_text_tokens(m["content"]) + MESSAGE_OVERHEAD_TOKENS for m in messages)


def estimate_output_tokens(text: str) -> int:
    """Estimate of an answer's tokens; an answer that exists is at least one token."""
    return max(1, estimate_text_tokens(text))


def call_hold_tokens(est_input_tokens: int, max_output_tokens: int | None) -> int:
    """Tokens to hold for one call: input estimate + the output ceiling sent to the provider."""
    if max_output_tokens is None or max_output_tokens < 1:
        raise MissingOutputCeiling(
            "a call needs an output ceiling (max_tokens >= 1) before a budget can cover it"
        )
    return est_input_tokens + max_output_tokens


@dataclass(frozen=True)
class CostEstimate:
    amount: Decimal
    currency: str


def call_cost(
    session: Session,
    tenant_id: UUID,
    model_id: UUID | None,
    est_input_tokens: int,
    max_output_tokens: int,
    at: datetime | None = None,
) -> CostEstimate | None:
    """Worst-case cost of one call at the rate in force, or None when the model has no rate
    (unknown, never 0)."""
    if model_id is None:
        return None
    keys = pricing.rate_keys(session, tenant_id, model_id)
    if keys is None:
        return None
    version = pricing.resolve(
        session, tenant_id, keys[0], keys[1], at or utcnow(), global_only=keys[2]
    )
    if version is None:
        return None
    usage = pricing.Usage(input_tokens=est_input_tokens, output_tokens=max_output_tokens, calls=1)
    return CostEstimate(pricing.calculate(version, usage), version.currency)


class ReportedUsage(Protocol):
    """What a provider reported (`shifde.adapters.base.Usage` satisfies this): `None` = not
    reported."""

    @property
    def input_tokens(self) -> int | None: ...

    @property
    def output_tokens(self) -> int | None: ...


@dataclass(frozen=True)
class ResolvedUsage:
    input_tokens: int
    output_tokens: int
    estimated_fields: tuple[str, ...]
    reported_input: int | None
    reported_output: int | None

    @property
    def estimated(self) -> bool:
        return bool(self.estimated_fields)

    def as_json(self) -> dict[str, Any]:  # Any: JSON document
        """Stored in `usage_events.provider_reported_usage`: what the provider said (null where
        it said nothing) and whether the stored counts are the provider's or an estimate."""
        return {
            "basis": "estimated" if self.estimated else "reported",
            "input": self.reported_input,
            "output": self.reported_output,
            "estimated_fields": list(self.estimated_fields),
        }


def resolve_usage(
    reported: ReportedUsage | None, *, estimated_input: int, estimated_output: int
) -> ResolvedUsage:
    """The counts to store: the provider's where it reported them (a reported 0 stays 0), the
    estimate (at least 1) where it did not."""
    rep_in = None if reported is None else reported.input_tokens
    rep_out = None if reported is None else reported.output_tokens
    fields: list[str] = []
    if rep_in is None:
        fields.append("input_tokens")
    if rep_out is None:
        fields.append("output_tokens")
    return ResolvedUsage(
        input_tokens=max(1, estimated_input) if rep_in is None else rep_in,
        output_tokens=max(1, estimated_output) if rep_out is None else rep_out,
        estimated_fields=tuple(fields),
        reported_input=rep_in,
        reported_output=rep_out,
    )
