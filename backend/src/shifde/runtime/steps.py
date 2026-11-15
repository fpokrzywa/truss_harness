"""Journaled execution steps shared by the agent loop and the graph executor: governed model calls
(retry, backoff, fallback chain, usage events) and tool calls (gate, approvals, write-ahead
`tool_invocations`, at-most-once side effects, indeterminate outcomes).

Reliability rules implemented here (SYSTEM_ARCHITECTURE 6.7):
* a retry is a NEW step attempt (same seq, attempt + 1) - never silent;
* `write_non_idempotent` / `destructive` tools are NEVER retried automatically; when the outcome of
  such a call is unknown (worker died after the write-ahead row, or the call failed in a way that
  may have reached the target) the call becomes `indeterminate` and an operator decides;
* `write_idempotent` retries reuse the same idempotency key;
* usage events are keyed by step attempt, so a retried call is recorded once per attempt.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any  # Any: tool arguments / results, JSON
from uuid import UUID

from shifde import ledger
from shifde.adapters.base import AdapterFailure, ErrorClass, Usage
from shifde.authz.service import ResourceRef
from shifde.build import tool_runtime
from shifde.build.env import VersionRef
from shifde.build.issues import schema_errors
from shifde.core.clock import utcnow
from shifde.core.errors import (
    AppError,
    EgressDenied,
    Forbidden,
    ModelNotAllowed,
    ModelRateLimited,
    ModelUnavailable,
    NotFound,
    RateLimited,
    UpstreamError,
    ValidationFailed,
)
from shifde.core.redaction import scrub, scrub_text
from shifde.ledger import enforcement, estimate, pricing
from shifde.models_admin import schemas as ms
from shifde.observability import tracing
from shifde.runtime import gate, ontology_exec, policies, state
from shifde.runtime import repository as repo
from shifde.runtime.context import RunContext
from shifde.runtime.governance import Reservation, ReserveDetail

MAX_OUTPUT_BYTES = 1_000_000


def json_safe(value: Any) -> Any:  # Any: tool result
    return json.loads(json.dumps(scrub(value), default=str))


def latest_attempts(steps: Sequence[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    """seq -> its latest attempt row (rows come ordered by seq, attempt)."""
    out: dict[int, Mapping[str, Any]] = {}
    for s in steps:
        out[s["seq"]] = s
    return out


def backoff_seconds(base: float, cap: float, attempt: int) -> float:
    """Exponential backoff with full jitter: random(0, min(cap, base * 2**(attempt-1)))."""
    if base <= 0:
        return 0.0
    return random.uniform(0, min(cap, base * 2 ** max(attempt - 1, 0)))  # noqa: S311


def _short(message: str) -> str:
    return scrub_text(message)[:500]


# ====================================================================== model calls
def classify_model_error(exc: AppError) -> tuple[str, bool, bool]:
    """(error_class, retry on the same model, try the next model)."""
    if isinstance(exc, ModelRateLimited | RateLimited):
        return state.RATE_LIMITED, True, True
    if isinstance(exc, ModelUnavailable):
        return state.PROVIDER_UNAVAILABLE, False, True
    if isinstance(exc, ModelNotAllowed | Forbidden | NotFound | EgressDenied):
        return state.AUTHZ_DENIED, False, False
    if isinstance(exc, ValidationFailed):
        return state.PROVIDER_ERROR_4XX, False, False
    if isinstance(exc, UpstreamError):
        text = exc.detail.lower()
        if text.startswith("timeout"):
            return state.TIMEOUT, True, True
        if text.startswith(("network", "dns")):
            return state.TRANSIENT_NETWORK, True, True
        if text.startswith("upstream"):
            return state.PROVIDER_UNAVAILABLE, True, True
        return state.PROVIDER_ERROR_4XX, False, False
    return state.INTERNAL, False, False


@dataclass
class ModelResult:
    text: str
    calls: list[dict[str, Any]] | None
    malformed: str | None
    step_id: UUID
    seq: int


def _event_values(
    ctx: RunContext,
    event_type: str,
    step: Mapping[str, Any],
    *,
    status: str,
    duration_ms: int | None = None,
    model_id: UUID | None = None,
) -> dict[str, Any]:  # Any: usage event columns
    """The run's attribution of one usage event (`event_key` = step id: one per attempt, a replay
    inserts nothing). Token counts, cost and the model's keys are set by the ledger."""
    run = ctx.run
    return {
        "tenant_id": ctx.tenant_id,
        "event_key": f"{step['id']}",
        "event_type": event_type,
        "status": status,
        "workspace_id": run["workspace_id"],
        "project_id": run["project_id"],
        "principal_id": run["initiated_by_principal_id"],
        "run_id": run["id"],
        "root_run_id": run["root_run_id"],
        "step_id": step["id"],
        "attempt": step["attempt"],
        "agent_id": run["agent_id"],
        "agent_version_id": run["agent_version_id"],
        "prompt_version_id": run["prompt_version_id"],
        "model_id": model_id,
        "pipeline_id": run["pipeline_id"],
        "pipeline_version_id": run["pipeline_version_id"],
        "orchestration_id": run["orchestration_id"],
        "node_id": step["node_id"],
        "is_test": run["is_test"],
        "call_count": 1,
        "duration_ms": duration_ms,
    }


def _usage_event(
    ctx: RunContext,
    event_type: str,
    step: Mapping[str, Any],
    *,
    status: str,
    duration_ms: int | None = None,
    model_id: UUID | None = None,
) -> ledger.EventResult:
    """The usage event of an attempt that consumed no tokens: a tool call, or a model call that
    failed (`status="failed"`, 0 counts, priced at 0 usage from the rate card in force, as
    SETH). The model's `provider_key` / `model_key` come from the ledger's lookup of the model,
    never from the run (B-2118, B-2141)."""
    values = _event_values(
        ctx, event_type, step, status=status, duration_ms=duration_ms, model_id=model_id
    )
    costed = pricing.UNMEASURED
    if event_type == "model_call" and model_id is not None:
        costed, provider_key, model_key = ledger.price_model_usage(
            ctx.session,
            ctx.tenant_id,
            model_id,
            pricing.Usage(input_tokens=0, output_tokens=0, calls=0, duration_ms=duration_ms),
        )
        values.update(provider_key=provider_key, model_key=model_key)
    return ledger.record(
        ctx.session,
        {
            **values,
            "input_tokens": 0,
            "output_tokens": 0,
            "provider_reported_usage": {"input": 0, "output": 0},
            "costed": costed,
        },
    )


def _model_usage(
    ctx: RunContext,
    step: Mapping[str, Any],
    reported: Usage,
    *,
    est_input: int,
    output: str,
    duration_ms: int,
    model_id: UUID,
    durable: bool,
) -> ledger.EventResult:
    """The usage event of a model call that answered (carry_forward Item 11): the counts the
    provider reported, each one it did not report estimated (never 0) and the event priced as
    `estimated`, not `calculated` (`ledger.record_model_call`). `durable` (with the worker's
    database): written in its own committed transaction (`record_model_call_durably`), so the row
    survives a crash or a lost lease after the provider answered; the step's own write of the
    same `event_key` then inserts nothing."""
    values = _event_values(
        ctx, "model_call", step, status="succeeded", duration_ms=duration_ms, model_id=model_id
    )
    est_output = estimate.estimate_output_tokens(output)
    if durable and ctx.db is not None:
        return ledger.record_model_call_durably(
            ctx.db,
            ctx.tenant_id,
            values,
            reported=reported,
            estimated_input=est_input,
            estimated_output=est_output,
        )
    return ledger.record_model_call(
        ctx.session,
        values,
        reported=reported,
        estimated_input=est_input,
        estimated_output=est_output,
    )


def _step_event(
    ctx: RunContext, kind: str, step: Mapping[str, Any], extra: Mapping[str, Any] | None = None
) -> None:
    repo.add_event(
        ctx.session,
        ctx.run,
        kind,
        step_id=step["id"],
        details={
            "seq": step["seq"],
            "attempt": step["attempt"],
            "kind": step["kind"],
            "name": step["name"],
            **dict(extra or {}),
        },
    )


def _cost_limit(value: object) -> Decimal | None:
    """`limits.max_cost` of the executing spec (validated non-negative by the build module)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _check_max_cost(
    ctx: RunContext, model_id: UUID, est_input: int, max_tokens: int, max_cost: Decimal | None
) -> None:
    """carry_forward Item 10: before each call, the run's known cost (its own usage events, in
    the call's currency) plus the call's worst case at the rate in force (`estimate.call_cost`:
    the input estimate and the whole output ceiling) must stay within `limits.max_cost`. A model
    with no rate follows the client's `unknown_cost_policy`: `deny` refuses the call,
    `allow_unmeasured` lets it run with its cost unknown. A refusal is a deny decision of the run
    and ends it as `budget_denied` before anything is held or sent."""
    if max_cost is None:
        return
    cost = estimate.call_cost(ctx.session, ctx.tenant_id, model_id, est_input, max_tokens)
    if cost is None:
        if enforcement.unknown_cost_policy(ctx.session, ctx.tenant_id) != "deny":
            return
        message = "run limit max_cost: the model has no rate and unpriced calls are denied"
    else:
        spent = repo.run_cost(ctx.session, ctx.tenant_id, ctx.run_id, cost.currency)
        if spent + cost.amount <= max_cost:
            return
        message = (
            f"run limit max_cost ({max_cost}) would be exceeded: {spent} spent, this call up to "
            f"{cost.amount} {cost.currency}"
        )
    with ctx.txn():
        gate.record_decision(
            ctx, "model:invoke", ctx.run_ref(), False, ["max_cost"], label=f"model:{model_id}"
        )
    raise state.RunFailed(state.BUDGET_DENIED, message)


def call_model(
    ctx: RunContext,
    messages: list[dict[str, str]],
    chain: Sequence[UUID],
    name: str,
    prior: Sequence[Mapping[str, Any]],
    parse: Any,  # Any: callable(text) -> list[dict] | None, raising ValueError when malformed
    node_id: str | None = None,  # pipeline model / prompt nodes
    *,
    max_cost: object = None,  # the executing spec's `limits.max_cost` (None = no cost limit)
) -> ModelResult:
    """One logical model step (seq) with its attempts. `prior` = attempt rows of this seq.

    Every attempt sends an output ceiling (`max_tokens`, carry_forward Item 9) and holds
    `estimate.call_hold_tokens(input estimate, max_tokens)` at the gate, so the hold covers the
    whole call; `max_cost` is checked before each attempt (Item 10); the usage of an answer is
    recorded durably and labelled estimated where the provider reported nothing (Item 11)."""
    settings = ctx.settings
    cost_limit = _cost_limit(max_cost)
    seq: int | None = prior[-1]["seq"] if prior else None
    attempt_no = prior[-1]["attempt"] if prior else 0
    if prior and prior[-1]["status"] == "running":  # the previous worker died mid-call
        with ctx.txn():
            row = repo.update_step(
                ctx.session,
                ctx.tenant_id,
                prior[-1]["id"],
                status="failed",
                error_class=state.INTERNAL,
                error_message="the worker was lost during this attempt",
                finished_at=utcnow(),
            )
            _step_event(ctx, "step_finished", row, {"status": "failed", "recovered": True})
    last_class, last_msg = state.INTERNAL, "no model was available"
    ref = ctx.run_ref()
    est_input = estimate.estimate_input_tokens(messages)
    max_tokens = int(settings.runtime_output_reserve_tokens)
    for model_index, model_id in enumerate(chain):
        for n in range(settings.runtime_model_attempts):
            attempt_no += 1
            _check_max_cost(ctx, model_id, est_input, max_tokens, cost_limit)
            res = gate.check(
                ctx,
                "model:invoke",
                ref,
                label=f"model:{model_id}",
                reserve=(f"{name}:{attempt_no}", estimate.call_hold_tokens(est_input, max_tokens)),
                reserve_detail=ReserveDetail(
                    model_id=model_id,
                    est_input_tokens=est_input,
                    est_output_tokens=max_tokens,
                ),
            ).reservation
            with ctx.txn():
                if seq is None:
                    seq = repo.next_seq(ctx.session, ctx.tenant_id, ctx.run_id)
                step = repo.insert_step(
                    ctx.session,
                    {
                        "tenant_id": ctx.tenant_id,
                        "run_id": ctx.run_id,
                        "seq": seq,
                        "attempt": attempt_no,
                        "kind": "model_call",
                        "name": name,
                        "node_id": node_id,
                        "model_id": model_id,
                        "status": "running",
                    },
                )
                _step_event(ctx, "step_started", step, {"model_id": str(model_id)})
            started = time.monotonic()
            try:
                with tracing.start_span(
                    "adapter.model_call",
                    {"model_id": str(model_id), "attempt": attempt_no, "step": name},
                ):
                    resp = ctx.artifacts.models.invoke_for_run(
                        ref,
                        model_id,
                        ms.InvokeIn(
                            messages=[ms.ChatMessageIn(**m) for m in messages],
                            max_tokens=max_tokens,
                        ),
                        run_id=ctx.run_id,  # on `model.invoked` (Item 15)
                        max_deadline=max(ctx.remaining(), 0.001),
                    )
            except AppError as exc:
                ctx.session.rollback()
                cls, same_model, fallback = classify_model_error(exc)
                msg = _short(exc.detail)
                with ctx.txn():
                    _release(ctx, res)
                    done = repo.update_step(
                        ctx.session,
                        ctx.tenant_id,
                        step["id"],
                        status="failed",
                        error_class=cls,
                        error_message=msg,
                        finished_at=utcnow(),
                    )
                    _usage_event(ctx, "model_call", done, status="failed", model_id=model_id)
                    _step_event(
                        ctx, "step_finished", done, {"status": "failed", "error_class": cls}
                    )
                last_class, last_msg = cls, msg
                if not (same_model or fallback):
                    raise state.RunFailed(cls, msg) from exc
                last_attempt_of_model = n + 1 >= settings.runtime_model_attempts
                if same_model and not last_attempt_of_model:
                    delay = backoff_seconds(
                        settings.runtime_retry_base_seconds,
                        settings.runtime_retry_cap_seconds,
                        n + 1,
                    )
                    with ctx.txn():
                        repo.add_event(
                            ctx.session,
                            ctx.run,
                            "retry_scheduled",
                            step_id=done["id"],
                            details={
                                "seq": done["seq"],
                                "next_attempt": attempt_no + 1,
                                "error_class": cls,
                                "backoff_ms": int(delay * 1000),
                            },
                        )
                    ctx.sleep(delay)
                    continue
                if model_index + 1 < len(chain):
                    with ctx.txn():
                        repo.add_event(
                            ctx.session,
                            ctx.run,
                            "retry_scheduled",
                            step_id=done["id"],
                            details={
                                "seq": done["seq"],
                                "error_class": cls,
                                "backoff_ms": 0,
                                "fallback_model_id": str(chain[model_index + 1]),
                            },
                        )
                break
            latency = int((time.monotonic() - started) * 1000)
            text = resp.out.output
            # the usage row first, in its own committed transaction, while nothing of this
            # session is pending (`invoke_for_run` committed): it survives a lost lease below
            durable = _model_usage(
                ctx,
                step,
                resp.reported,
                est_input=est_input,
                output=text,
                duration_ms=latency,
                model_id=model_id,
                durable=True,
            )
            used = estimate.resolve_usage(
                resp.reported,
                estimated_input=est_input,
                estimated_output=estimate.estimate_output_tokens(text),
            )
            malformed: str | None = None
            calls: list[dict[str, Any]] | None = None
            try:
                calls = parse(text)
            except ValueError as exc:
                malformed = str(exc)
            tokens = used.input_tokens + used.output_tokens
            with ctx.txn():
                out_ref = repo.insert_artifact(
                    ctx.session,
                    ctx.tenant_id,
                    ctx.run_id,
                    "intermediate",
                    scrub({"text": text, "calls": calls, "malformed": malformed}),
                    step["id"],
                )
                done = repo.update_step(
                    ctx.session,
                    ctx.tenant_id,
                    step["id"],
                    usage={
                        "input_tokens": used.input_tokens,
                        "output_tokens": used.output_tokens,
                        "latency_ms": latency,
                        "basis": "estimated" if used.estimated else "reported",
                    },
                    status="succeeded",
                    output_ref=out_ref,
                    finished_at=utcnow(),
                )
                # without the worker's database this is the only write; with it, the same
                # `event_key` inserts nothing
                event = _model_usage(
                    ctx,
                    done,
                    resp.reported,
                    est_input=est_input,
                    output=text,
                    duration_ms=latency,
                    model_id=model_id,
                    durable=False,
                )
                if ctx.db is not None:
                    event = durable
                repo.add_usage(
                    ctx.session,
                    ctx.tenant_id,
                    ctx.run_id,
                    input_tokens=used.input_tokens,
                    output_tokens=used.output_tokens,
                    model_calls=1,
                )
                if res is not None:
                    for alert in ctx.hooks.settle(
                        ctx.session, res, tokens, event.costed.amount, event.costed.currency
                    ):
                        ctx.audit(
                            "governance.budget_alert",
                            {
                                "budget_id": str(alert.budget_id),
                                "scope_type": alert.scope_type,
                                "scope_id": str(alert.scope_id),
                                "metric": alert.metric,
                                "threshold": str(alert.threshold),
                                "utilization": str(alert.utilization),
                            },
                            commit=False,
                        )
                _step_event(ctx, "step_finished", done, {"status": "succeeded"})
            policies.after_model_call(ctx, event)  # cost-threshold policies (own transactions)
            return ModelResult(text, calls, malformed, done["id"], int(done["seq"]))
    raise state.RunFailed(last_class, f"every model failed: {last_msg}")


def _release(ctx: RunContext, res: Reservation | None) -> None:
    if res is not None:
        ctx.hooks.release(ctx.session, res)


# ====================================================================== tool calls
@dataclass
class ToolCall:
    tool: VersionRef
    args: dict[str, Any]
    call_ref: str  # "<model seq>:<index>" or "node:<id>"
    parent_seq: int | None = None
    call_index: int | None = None
    node_id: str | None = None


@dataclass
class ToolPolicy:
    approval_classes: frozenset[str]
    agent_id: UUID | None  # the calling agent artifact (invocation_policy.allowed_agent_ids)
    pinned_version_ids: frozenset[UUID]  # tools this agent / node may call
    force_approval: bool = False  # orchestration approval node already required one


def _no_nul(value: Any) -> Any:  # Any: JSON value
    """jsonb cannot hold U+0000: the journal copy of the arguments (display only; the call itself
    is sent unchanged) shows U+FFFD instead, so recording a call can never fail it."""
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, dict):
        return {_no_nul(k): _no_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_nul(v) for v in value]
    return value


def _journal_arguments(args: Mapping[str, Any]) -> Any:  # Any: redacted JSON
    return _no_nul(scrub(args))


def classify_tool_failure(exc: Exception) -> tuple[str, bool, bool]:
    """(error_class, retryable, may_have_reached_the_target)."""
    if isinstance(exc, AdapterFailure):
        ec = exc.error_class
        if ec is ErrorClass.TIMEOUT:
            return state.TIMEOUT, True, True
        if ec is ErrorClass.NETWORK:
            return state.TRANSIENT_NETWORK, True, True
        if ec is ErrorClass.UPSTREAM:
            return state.PROVIDER_UNAVAILABLE, True, True
        if ec is ErrorClass.RATE_LIMIT:
            return state.RATE_LIMITED, True, False
        if ec is ErrorClass.DNS:
            return state.TRANSIENT_NETWORK, True, False
        if ec is ErrorClass.EGRESS:
            return state.AUTHZ_DENIED, False, False
        return state.TOOL_ERROR, False, False
    if isinstance(exc, Forbidden | NotFound | EgressDenied):
        return state.AUTHZ_DENIED, False, False
    if isinstance(exc, ValidationFailed):
        return state.VALIDATION, False, False
    return state.TOOL_ERROR, False, False


def _steps_of(
    steps: Sequence[Mapping[str, Any]], kind: str, call: ToolCall
) -> list[Mapping[str, Any]]:
    return [
        s
        for s in steps
        if s["kind"] == kind
        and s["parent_seq"] == call.parent_seq
        and s["call_index"] == call.call_index
        and s["node_id"] == call.node_id
    ]


def _epoch(ctx: RunContext, call: ToolCall) -> int:
    epoch = 0
    while True:
        inv = repo.get_invocation(ctx.session, ctx.tenant_id, _key(ctx, call, epoch))
        if inv is None or inv["status"] != "resolved_retry":
            return epoch
        epoch += 1


def _key(ctx: RunContext, call: ToolCall, epoch: int) -> str:
    return repo.sha256_hex(str(ctx.run_id), call.call_ref, str(epoch))


def tool_resource(ctx: RunContext, tool: VersionRef) -> ResourceRef:
    return ResourceRef(ctx.tenant_id, tool.workspace_id, tool.project_id, "tool", tool.artifact_id)


def _arg_problems(spec: Mapping[str, Any], args: dict[str, Any]) -> list[str]:
    problems = [m for _, m in schema_errors(spec["input_schema"], args)]
    binding = spec.get("binding") or {}
    snap = binding.get("schema_snapshot") if spec.get("executor") == "mcp.tool" else None
    if isinstance(snap, dict) and isinstance(snap.get("input_schema"), dict):
        problems += [m for _, m in schema_errors(snap["input_schema"], args)]
    return problems


def _resource_state(ctx: RunContext, tool: VersionRef) -> gate.Check:
    spec = tool.spec

    def check() -> gate.Problem | None:
        if tool.state not in ("published", "deprecated"):
            return ("tool_version_not_published", "the tool version is not published")
        if tool.artifact_status != "active":
            return ("tool_disabled", "the tool is disabled or retired")
        cid = (spec.get("binding") or {}).get("connection_id")
        if spec.get("executor") != "builtin.function" and cid:
            try:
                row = ctx.artifacts.conns.get_row(UUID(str(cid)))
            except NotFound:
                return ("connection_missing", "the tool's connection does not exist")
            if row["status"] != "active":
                return ("connection_inactive", f"the tool's connection is {row['status']}")
            desc = ctx.runtime.registry.descriptor(row["adapter_key"])
            if desc is None or not desc.implemented:
                return ("adapter_planned", "the connection's adapter is not implemented")
        return None

    return check


def _policy(ctx: RunContext, call: ToolCall, policy: ToolPolicy) -> gate.Check:
    tool = call.tool
    inv = tool.spec.get("invocation_policy") or {}

    def check() -> gate.Problem | None:
        if tool.version_id not in policy.pinned_version_ids:
            return ("tool_not_pinned", "the tool is not pinned by this agent version")
        allowed = inv.get("allowed_agent_ids")
        if (
            isinstance(allowed, list)
            and policy.agent_id is not None
            and str(policy.agent_id) not in {str(a) for a in allowed}
        ):
            return ("agent_not_allowed", "the tool does not allow this agent")
        cap = inv.get("max_calls_per_run")
        if isinstance(cap, int):
            others = {
                (s["parent_seq"], s["call_index"], s["node_id"])
                for s in repo.list_steps(ctx.session, ctx.tenant_id, ctx.run_id)
                if s["kind"] == "tool_call"
                and s["tool_version_id"] == tool.version_id
                and (s["parent_seq"], s["call_index"], s["node_id"])
                != (call.parent_seq, call.call_index, call.node_id)
            }
            if len(others) >= cap:
                return ("max_calls_per_run", f"the tool allows {cap} call(s) per run")
        return None

    return check


def _approval_needed(side: str, policy: ToolPolicy) -> tuple[bool, str]:
    if side == "destructive":  # always, cannot be disabled
        return True, "tool_side_effect"
    if side in policy.approval_classes:
        return True, "tool_side_effect" if side == "write_non_idempotent" else "approval_rule"
    return False, ""


def _insert_approval(
    ctx: RunContext,
    call: ToolCall,
    reason: str,
    epoch: int,
    side: str,
    *,
    step_kind: str = "approval",
    required_permission: str | None = None,
    policy_ids: Sequence[UUID] = (),
) -> None:
    """Insert the approval, its step, event and audit in the CALLER's transaction."""
    spec = call.tool.spec
    seq = repo.next_seq(ctx.session, ctx.tenant_id, ctx.run_id)
    step = repo.insert_step(
        ctx.session,
        {
            "tenant_id": ctx.tenant_id,
            "run_id": ctx.run_id,
            "seq": seq,
            "attempt": 1 + epoch,
            "kind": step_kind,
            "name": f"approval:{call.tool.artifact_key}",
            "node_id": call.node_id,
            "parent_seq": call.parent_seq,
            "call_index": call.call_index,
            "tool_version_id": call.tool.version_id,
            "status": "running",
        },
    )
    approval = repo.insert_approval(
        ctx.session,
        {
            "tenant_id": ctx.tenant_id,
            "run_id": ctx.run_id,
            "workspace_id": ctx.run["workspace_id"],
            "project_id": ctx.run["project_id"],
            "step_seq": seq,
            "call_ref": call.call_ref,
            "epoch": epoch,
            "reason": reason,
            "tool_version_id": call.tool.version_id,
            "tool_key": call.tool.artifact_key,
            "tool_version_no": call.tool.version_no,
            "side_effect_class": side,
            "arguments": scrub(call.args),
            "context_summary": {"executor": spec.get("executor")},
            "requested_by": ctx.run["initiated_by_principal_id"],
            "expires_at": utcnow() + timedelta(hours=ctx.settings.approval_expiry_hours),
            "required_permission": required_permission,
        },
    )
    repo.add_event(
        ctx.session,
        ctx.run,
        "approval_requested",
        step_id=step["id"],
        details={"approval_id": str(approval["id"]), "reason": reason},
    )
    ctx.audit(
        "approval.requested",
        {
            "approval_id": str(approval["id"]),
            "reason": reason,
            "tool": call.tool.artifact_key,
            "side_effect_class": side,
            **({"policy_ids": [str(p) for p in policy_ids]} if policy_ids else {}),
        },
        commit=False,
    )
    if policy_ids:  # a governance rule asked for this approval: leave a decision record
        policies.record_rule_decision(ctx, "approval_required", policy_ids, "approval_rule")


def _create_approval(
    ctx: RunContext,
    call: ToolCall,
    reason: str,
    epoch: int,
    side: str,
    *,
    step_kind: str = "approval",
    required_permission: str | None = None,
    policy_ids: Sequence[UUID] = (),
) -> None:
    with ctx.txn():
        _insert_approval(
            ctx,
            call,
            reason,
            epoch,
            side,
            step_kind=step_kind,
            required_permission=required_permission,
            policy_ids=policy_ids,
        )


def _close_approval_steps(ctx: RunContext, call: ToolCall, ok: bool) -> None:
    """In the caller's transaction."""
    for s in _steps_of(repo.list_steps(ctx.session, ctx.tenant_id, ctx.run_id), "approval", call):
        if s["status"] == "running":
            extra: dict[str, Any] = {} if ok else {"error_class": state.APPROVAL_REJECTED}
            repo.update_step(
                ctx.session,
                ctx.tenant_id,
                s["id"],
                status="succeeded" if ok else "failed",
                finished_at=utcnow(),
                **extra,
            )


def _finish_approval_step(ctx: RunContext, call: ToolCall, ok: bool) -> None:
    if any(
        s["status"] == "running"
        for s in _steps_of(
            repo.list_steps(ctx.session, ctx.tenant_id, ctx.run_id), "approval", call
        )
    ):
        with ctx.txn():
            _close_approval_steps(ctx, call, ok)


def run_tool(ctx: RunContext, call: ToolCall, policy: ToolPolicy) -> Any:  # Any: tool result
    """Execute (or replay) one tool call of the run; returns the stored/valid output."""
    tool, spec = call.tool, call.tool.spec
    side = str(spec["side_effect_class"])
    all_steps = repo.list_steps(ctx.session, ctx.tenant_id, ctx.run_id)
    mine = _steps_of(all_steps, "tool_call", call)
    last = mine[-1] if mine else None
    if last is not None and last["status"] == "succeeded":
        return repo.get_artifact(ctx.session, ctx.tenant_id, last["output_ref"])

    problems = _arg_problems(spec, call.args)
    if problems:
        raise state.RunFailed(state.VALIDATION, f"invalid arguments: {problems[0]}")

    epoch = _epoch(ctx, call)
    inv = repo.get_invocation(ctx.session, ctx.tenant_id, _key(ctx, call, epoch))
    retry_authorized = False

    # ---- recovery of an unfinished earlier attempt
    if last is not None and last["status"] == "running":
        if inv is not None and inv["status"] == "started" and side in state.NO_RETRY_SIDE_EFFECTS:
            _make_indeterminate(ctx, call, last, inv, epoch, side)  # raises Waiting
        with ctx.txn():
            row = repo.update_step(
                ctx.session,
                ctx.tenant_id,
                last["id"],
                status="failed",
                error_class=state.INTERNAL,
                error_message="the worker was lost during this attempt",
                finished_at=utcnow(),
            )
            if (
                inv is not None
                and inv["status"] == "started"
                and side not in state.WRITE_SIDE_EFFECTS
            ):
                # a write's earlier delivery stays `started` until the retry really begins: if
                # the gate, a cancel or the deadline stops it first, the run ends with the
                # outcome unknown instead of a journal that says the call failed
                repo.update_invocation(
                    ctx.session, ctx.tenant_id, inv["id"], "failed", error_class=state.INTERNAL
                )
            _step_event(ctx, "step_finished", row, {"status": "failed", "recovered": True})
        last = row
    if last is not None and last["status"] == "indeterminate" and inv is not None:
        decision = _resolve_indeterminate(ctx, call, last, inv, epoch)  # may raise Waiting/Failed
        if decision is not None:  # operator confirmed the call happened
            return decision
        epoch += 1
        inv = None
        retry_authorized = True

    # ---- gate (identity, permissions, resource state, policy)
    gate.check(
        ctx,
        "tool:invoke",
        tool_resource(ctx, tool),
        label=f"tool:{tool.artifact_key}",
        also_require=[str(p) for p in spec.get("required_permissions") or []],
        resource_state=_resource_state(ctx, tool),
        policy=_policy(ctx, call, policy),
    )

    if ontology_exec.is_ontology_tool(spec):
        ontology_exec.tool_gate(ctx, call.args)  # FR-AUTHZ-009: the ontology gate

    # ---- approval
    if not retry_authorized:
        needed, reason = _approval_needed(side, policy)
        needed = needed or policy.force_approval
        # configurable governance rules can only ADD an approval (never waive one)
        rule = policies.tool_approval(ctx, call, side)
        if rule is not None and not needed:
            needed, reason = True, "approval_rule"
        if needed:
            reason = reason or "approval_rule"
            appr = repo.find_approval(
                ctx.session, ctx.tenant_id, ctx.run_id, call.call_ref, reason, epoch
            )
            if appr is None:
                _create_approval(
                    ctx,
                    call,
                    reason,
                    epoch,
                    side,
                    required_permission=rule.approver_permission if rule else None,
                    policy_ids=rule.policy_ids if rule else (),
                )
                raise state.Waiting("approval")
            if appr["status"] == "pending":
                raise state.Waiting("approval")
            if appr["status"] != "approved":
                _finish_approval_step(ctx, call, False)
                raise state.RunFailed(state.APPROVAL_REJECTED, "the approval was not granted")
            _finish_approval_step(ctx, call, True)

    return _execute_attempts(ctx, call, policy, side, epoch, inv, last)


def _make_indeterminate(
    ctx: RunContext,
    call: ToolCall,
    step: Mapping[str, Any],
    inv: Mapping[str, Any],
    epoch: int,
    side: str,
) -> None:
    with ctx.txn():
        repo.update_invocation(ctx.session, ctx.tenant_id, inv["id"], "indeterminate")
        row = repo.update_step(
            ctx.session,
            ctx.tenant_id,
            step["id"],
            status="indeterminate",
            error_class=state.INDETERMINATE,
            finished_at=utcnow(),
        )
        _step_event(ctx, "step_finished", row, {"status": "indeterminate"})
        # in the same transaction: a death in between must not leave the state without a decision
        _insert_approval(ctx, call, "indeterminate_side_effect", epoch, side)
    raise state.Waiting("indeterminate_side_effect")


def _resolve_indeterminate(
    ctx: RunContext,
    call: ToolCall,
    step: Mapping[str, Any],
    inv: Mapping[str, Any],
    epoch: int,
) -> Any:  # Any: confirmed output, or None to retry
    appr = repo.find_approval(
        ctx.session, ctx.tenant_id, ctx.run_id, call.call_ref, "indeterminate_side_effect", epoch
    )
    if appr is None:
        _create_approval(
            ctx, call, "indeterminate_side_effect", epoch, str(inv["side_effect_class"])
        )
        raise state.Waiting("indeterminate_side_effect")
    if appr["status"] == "pending":
        raise state.Waiting("indeterminate_side_effect")
    decision = appr["decision"]
    if appr["status"] != "approved" or decision == "fail":
        raise state.RunFailed(
            state.INDETERMINATE, "an operator declared the side effect failed or unverifiable"
        )
    if decision == "mark_succeeded":
        output = {"status": "operator_confirmed_succeeded", "note": appr["decision_note"]}
        with ctx.txn():
            out_ref = repo.insert_artifact(
                ctx.session, ctx.tenant_id, ctx.run_id, "intermediate", output, step["id"]
            )
            repo.update_invocation(
                ctx.session,
                ctx.tenant_id,
                inv["id"],
                "resolved_succeeded",
                response_ref=out_ref,
                finished_at=utcnow(),
            )
            row = repo.update_step(
                ctx.session,
                ctx.tenant_id,
                step["id"],
                status="succeeded",
                output_ref=out_ref,
                error_class=None,
                error_message=None,
                finished_at=utcnow(),
            )
            _step_event(ctx, "step_finished", row, {"status": "succeeded", "operator": True})
            # the decision is applied and its step closed in ONE transaction: a kill in between
            # must not leave an approval step `running` on a completed run
            _close_approval_steps(ctx, call, True)
            repo.land_verdicts(
                ctx.session, ctx.tenant_id, ctx.run_id
            )  # on every earlier delivery of the call
        return output
    with ctx.txn():  # retry: the operator authorized a new execution (new idempotency epoch)
        repo.update_invocation(
            ctx.session, ctx.tenant_id, inv["id"], "resolved_retry", finished_at=utcnow()
        )
        _close_approval_steps(ctx, call, True)
    return None


def _park_budget_spent(
    ctx: RunContext,
    call: ToolCall,
    inv: Mapping[str, Any],
    step: Mapping[str, Any],
    epoch: int,
    side: str,
) -> None:
    with ctx.txn():
        repo.update_invocation(
            ctx.session, ctx.tenant_id, inv["id"], "indeterminate", error_class=state.INDETERMINATE
        )
        row = repo.update_step(
            ctx.session,
            ctx.tenant_id,
            step["id"],
            status="indeterminate",
            error_class=state.INDETERMINATE,
            error_message=(
                "the delivery budget (max_attempts) was spent across worker restarts; "
                "whether a delivery landed is unknown"
            ),
            finished_at=utcnow(),
        )
        _step_event(ctx, "step_finished", row, {"status": "indeterminate"})
        _insert_approval(ctx, call, "indeterminate_side_effect", epoch, side)
    raise state.Waiting("indeterminate_side_effect")


def _execute_attempts(
    ctx: RunContext,
    call: ToolCall,
    policy: ToolPolicy,
    side: str,
    epoch: int,
    inv: Mapping[str, Any] | None,
    last: Mapping[str, Any] | None,
) -> Any:  # Any: tool result
    tool, spec = call.tool, call.tool.spec
    retry_cfg = spec.get("retry") or {}
    max_attempts = (
        1 if side in state.NO_RETRY_SIDE_EFFECTS else int(retry_cfg.get("max_attempts", 1))
    )
    base = float(retry_cfg.get("backoff_s", 0))
    key = _key(ctx, call, epoch)
    journaled = not (spec["executor"] == "builtin.function" and side == "none")
    seq: int | None = last["seq"] if last is not None else None
    attempt_no = last["attempt"] if last is not None else 0
    # `max_attempts` limits the deliveries of a write ACROSS worker restarts, counted from the
    # journal (the `tool_call` steps carrying this key), not per process: once the budget is
    # spent and an earlier delivery may have landed, park with a decision instead of sending
    tries = 0
    if side in state.WRITE_SIDE_EFFECTS and journaled:
        keyed = [
            s
            for s in repo.list_steps(ctx.session, ctx.tenant_id, ctx.run_id)
            if s["kind"] == "tool_call" and s["idempotency_key"] == key
        ]
        tries = len(keyed)
        if inv is not None and inv["status"] == "started" and tries >= max_attempts and keyed:
            _park_budget_spent(ctx, call, inv, keyed[-1], epoch, side)  # raises Waiting
        if inv is not None and last is not None and last["status"] == "failed":
            # The previous worker recorded this attempt's answer and died before the run ended.
            # A recovering worker never delivers a call whose answer is on record, and never
            # exceeds `max_attempts` for the key: it ends the run as the dead worker would have
            # (settle then flags it when the call is still open). Only an answer that was
            # definite AND may be retried within the budget goes on to another delivery.
            recorded_cls = str(last["error_class"] or state.INTERNAL)
            recorded_msg = str(last["error_message"] or "the recorded attempt failed")
            if inv["status"] == "succeeded":  # a 2xx whose output failed the schema
                raise state.RunFailed(recorded_cls, recorded_msg)
            if inv["status"] == "failed" and (tries >= max_attempts):
                raise state.RunFailed(recorded_cls, recorded_msg)
    # a write whose earlier delivery (a recovered `started` row, or an earlier attempt of this
    # call) may have reached the target stays unknown, whatever a later attempt reports
    delivery_unknown = bool(
        inv is not None and inv["status"] == "started" and side in state.WRITE_SIDE_EFFECTS
    )
    while True:
        tries += 1
        attempt_no += 1
        ctx.checkpoint()
        timeout = min(float(spec["timeout_s"]), ctx.remaining())
        if timeout <= 0:
            raise state.TimedOut()
        with ctx.txn():
            if seq is None:
                seq = repo.next_seq(ctx.session, ctx.tenant_id, ctx.run_id)
            step = repo.insert_step(
                ctx.session,
                {
                    "tenant_id": ctx.tenant_id,
                    "run_id": ctx.run_id,
                    "seq": seq,
                    "attempt": attempt_no,
                    "kind": "tool_call",
                    "name": tool.artifact_key,
                    "node_id": call.node_id,
                    "parent_seq": call.parent_seq,
                    "call_index": call.call_index,
                    "tool_version_id": tool.version_id,
                    "idempotency_key": key,
                    "status": "running",
                },
            )
            inv_row = inv
            if journaled:  # write-ahead: committed BEFORE the call is made
                if inv_row is None:
                    inv_row = repo.insert_invocation(
                        ctx.session,
                        {
                            "tenant_id": ctx.tenant_id,
                            "run_id": ctx.run_id,
                            "call_ref": call.call_ref,
                            "epoch": epoch,
                            "tool_version_id": tool.version_id,
                            "side_effect_class": side,
                            "idempotency_key": key,
                            "request_hash": repo.sha256_hex(
                                json.dumps(call.args, sort_keys=True, default=str)
                            ),
                            # the same redaction the approval path applies; writes only, so the
                            # decision opened after a crash can say which call it is about
                            "arguments": _journal_arguments(call.args)
                            if side in state.WRITE_SIDE_EFFECTS
                            else None,
                        },
                    )
                else:
                    repo.update_invocation(
                        ctx.session,
                        ctx.tenant_id,
                        inv_row["id"],
                        "started",
                        attempts=inv_row["attempts"] + 1,
                        started_at=utcnow(),
                    )
                step = repo.update_step(
                    ctx.session,
                    ctx.tenant_id,
                    step["id"],
                    tool_invocation_id=inv_row["id"],
                    status="running",
                )
            _step_event(ctx, "step_started", step, {"tool": tool.artifact_key})
        inv = inv_row
        try:
            with tracing.start_span(
                "adapter.tool_call", {"tool": tool.artifact_key, "side_effect_class": side}
            ):
                if ontology_exec.is_ontology_tool(spec):
                    output = ontology_exec.tool_query(
                        ctx, call.args, purpose=f"tool:{tool.artifact_key}:ontology"
                    )
                else:
                    output = tool_runtime.invoke_once(
                        ctx.artifacts.conns,
                        dict(spec),
                        call.args,
                        purpose=f"tool:{tool.artifact_key}:run",
                        idempotency_key=key,
                        deadline=timeout,
                    )
        except (AdapterFailure, AppError) as exc:
            ctx.session.rollback()
            cls, retryable, maybe_sent = classify_tool_failure(exc)
            msg = _short(exc.message if isinstance(exc, AdapterFailure) else exc.detail)
            will_retry = retryable and tries < max_attempts
            # a write that may have reached its target and will not be retried is unknown, of
            # any write class; one that IS retried keeps its invocation `started` (below)
            delivery_unknown = delivery_unknown or maybe_sent
            unknown = side in state.WRITE_SIDE_EFFECTS and delivery_unknown and inv is not None
            if unknown and not will_retry and inv is not None:
                with ctx.txn():
                    repo.update_invocation(
                        ctx.session, ctx.tenant_id, inv["id"], "indeterminate", error_class=cls
                    )
                    row = repo.update_step(
                        ctx.session,
                        ctx.tenant_id,
                        step["id"],
                        status="indeterminate",
                        error_class=cls,
                        error_message=msg,
                        finished_at=utcnow(),
                    )
                    _step_event(ctx, "step_finished", row, {"status": "indeterminate"})
                    _insert_approval(ctx, call, "indeterminate_side_effect", epoch, side)
                raise state.Waiting("indeterminate_side_effect") from exc
            with ctx.txn():
                if inv is not None and not unknown:
                    repo.update_invocation(
                        ctx.session,
                        ctx.tenant_id,
                        inv["id"],
                        "failed",
                        error_class=cls,
                        finished_at=utcnow(),
                    )
                row = repo.update_step(
                    ctx.session,
                    ctx.tenant_id,
                    step["id"],
                    status="failed",
                    error_class=cls,
                    error_message=msg,
                    finished_at=utcnow(),
                )
                repo.add_usage(ctx.session, ctx.tenant_id, ctx.run_id, tool_calls=1)
                _usage_event(ctx, "tool_call", row, status="failed")
                _step_event(ctx, "step_finished", row, {"status": "failed", "error_class": cls})
            if will_retry:
                delay = backoff_seconds(base, ctx.settings.runtime_retry_cap_seconds, tries)
                with ctx.txn():
                    repo.add_event(
                        ctx.session,
                        ctx.run,
                        "retry_scheduled",
                        step_id=row["id"],
                        details={
                            "seq": row["seq"],
                            "next_attempt": attempt_no + 1,
                            "error_class": cls,
                            "backoff_ms": int(delay * 1000),
                        },
                    )
                ctx.sleep(delay)
                continue
            raise state.RunFailed(cls, msg) from exc
        refused = (
            tool_runtime.refusal(str(spec["executor"]), output)
            if (epoch > 0 or delivery_unknown) and side in state.WRITE_SIDE_EFFECTS
            else None
        )
        if refused is not None:
            # The call has an earlier delivery that may have landed (an operator-authorized
            # retry, or an automatic resend / a recovery after a cut, a 5xx or a crash) and the
            # target answered WITHOUT doing the work (anything but a 2xx). That answer says nothing
            # about the earlier delivery, so it cannot close the question.
            msg = _short(f"the resend was refused: {refused}")
            if delivery_unknown and inv is not None:
                # this epoch's own delivery is unknown: park with a decision, state and decision
                # in one transaction
                with ctx.txn():
                    repo.update_invocation(
                        ctx.session,
                        ctx.tenant_id,
                        inv["id"],
                        "indeterminate",
                        error_class=state.TOOL_ERROR,
                    )
                    row = repo.update_step(
                        ctx.session,
                        ctx.tenant_id,
                        step["id"],
                        status="indeterminate",
                        error_class=state.TOOL_ERROR,
                        error_message=msg,
                        finished_at=utcnow(),
                    )
                    _step_event(ctx, "step_finished", row, {"status": "indeterminate"})
                    _insert_approval(ctx, call, "indeterminate_side_effect", epoch, side)
                raise state.Waiting("indeterminate_side_effect")
            # a fresh epoch after an operator's `retry`: a definite failure of THIS attempt; the
            # run ends and settle opens the decision for the call
            with ctx.txn():
                if inv is not None:
                    repo.update_invocation(
                        ctx.session,
                        ctx.tenant_id,
                        inv["id"],
                        "failed",
                        error_class=state.TOOL_ERROR,
                        finished_at=utcnow(),
                    )
                row = repo.update_step(
                    ctx.session,
                    ctx.tenant_id,
                    step["id"],
                    status="failed",
                    error_class=state.TOOL_ERROR,
                    error_message=msg,
                    finished_at=utcnow(),
                )
                repo.add_usage(ctx.session, ctx.tenant_id, ctx.run_id, tool_calls=1)
                _usage_event(ctx, "tool_call", row, status="failed")
                _step_event(
                    ctx, "step_finished", row, {"status": "failed", "error_class": state.TOOL_ERROR}
                )
            raise state.RunFailed(state.TOOL_ERROR, msg)
        safe = json_safe(output)
        raw = json.dumps(safe, default=str)
        if len(raw.encode()) > MAX_OUTPUT_BYTES:
            safe = {"truncated": True, "preview": raw[:100_000]}
        violation = schema_errors(spec["output_schema"], safe) if "output_schema" in spec else []
        violation_extra: dict[str, Any] = (
            {
                "error_class": state.SCHEMA_VIOLATION,
                "error_message": _short(f"the tool output violates its schema: {violation[0][1]}"),
            }
            if violation
            else {}
        )
        with ctx.txn():
            out_ref = repo.insert_artifact(
                ctx.session, ctx.tenant_id, ctx.run_id, "intermediate", safe, step["id"]
            )
            if inv is not None:
                repo.update_invocation(
                    ctx.session,
                    ctx.tenant_id,
                    inv["id"],
                    "succeeded",
                    response_ref=out_ref,
                    finished_at=utcnow(),
                )
            row = repo.update_step(
                ctx.session,
                ctx.tenant_id,
                step["id"],
                status="failed" if violation else "succeeded",
                output_ref=out_ref,
                finished_at=utcnow(),
                **violation_extra,
            )
            repo.add_usage(ctx.session, ctx.tenant_id, ctx.run_id, tool_calls=1)
            _usage_event(ctx, "tool_call", row, status="failed" if violation else "succeeded")
            _step_event(
                ctx, "step_finished", row, {"status": "failed" if violation else "succeeded"}
            )
        if violation:
            raise state.RunFailed(
                state.SCHEMA_VIOLATION, f"the tool output violates its schema: {violation[0][1]}"
            )
        return safe
