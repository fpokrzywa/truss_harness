"""Pipeline executor (SYSTEM_ARCHITECTURE 7.3; docs/api/PHASE_6_CONTRACT.md section 4).

A pipeline run is an ordinary run (`kind = 'pipeline'`) advanced by the ordinary worker queue: the
same lease fencing (`RunContext.txn`), Execution Gate, step journal, approvals, retries and usage
events as agent and orchestration runs - there is no second engine. Like the orchestration graph
executor it is *re-entrant*: on every advance the state of every node is rebuilt from the step
journal (one `pipeline_node` step per node attempt) and whatever is ready is executed.

* Scheduling: nodes run in topological order; a node waits for all its incoming edges, is
  `skipped` when no incoming edge is live (condition branches) or a bound input was not produced,
  and otherwise runs. Inline nodes run one at a time inside the advance; `agent` nodes are CHILD
  RUNS (no worker slot is held while they run) started up to `policies.max_parallel` at a time.
* Failure: `fail_fast` cancels the open child runs and fails the run at the first failed node;
  `continue` skips the failed node's dependants, finishes the independent branches, then fails.
  Node `retry` applies to the nodes that can be repeated safely (reads, models, validation); a
  node with a side effect is NEVER retried automatically.
* Side effects (api_request with a write method, connection sink) are journaled write-ahead: the
  step row (with its idempotency key) is committed BEFORE the call, so a worker that died around
  the call leaves a started-without-outcome step that recovery turns into an `indeterminate`
  approval (an operator decides: mark succeeded / retry / fail) - never a blind second send.
* Recovery: a replacement worker rebuilds everything from the journal; succeeded nodes are never
  executed again.
* Untrusted data: node outputs (database rows, API bodies, model text) only reach models inside
  `<untrusted>` blocks in USER messages; prompt variables that a prompt declares trusted cannot be
  fed from pipeline data.

Unified-platform port (DECISIONS B-2190..): the pipeline's `Env` is the run's own (`ctx.env`, the
`run_of` contract of B-1933, never a visibility flag) and the run re-validates its spec through it,
so a node's tool, agent or prompt version is answered only when the run's pinned pipeline version
pins it, and only for a version of the node's kind; the tool policy's pinned ids come from that set
(B-2162). A connection node reaches only a connection of this client that the pinned specs name, at
the client level or the run's own workspace and project (`run_connection`, B-2161), and the session
is opened for that scope (`open_for_use(at=)`), so the row used is the row checked. Every
repository call names the client (B-2115); a child run is created in the parent's client,
workspace, project and root with a prompt from the run's pin set, and a step's child run is read
only when it is this run's own child. Model nodes pass `max_cost=` (Item 10).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any  # Any: JSON documents flowing between nodes
from uuid import UUID

from shifde.adapters.base import AdapterFailure
from shifde.authz.service import ResourceRef
from shifde.build import tool_runtime
from shifde.build.env import VersionRef
from shifde.build.issues import IDENT
from shifde.build.pipeline_nodes import BINDING, PARAM_BINDING, WRITE_METHODS, expand_refs
from shifde.build.prompts import (
    build_node,
    merged_variables,
    render_prompt,
    resolve_variables,
)
from shifde.core import safeexpr
from shifde.core.clock import utcnow
from shifde.core.errors import AppError, NotFound
from shifde.core.redaction import scrub, scrub_text
from shifde.runtime import gate, ontology_exec, state, steps
from shifde.runtime import repository as repo
from shifde.runtime.context import RunContext
from shifde.runtime.pipeline_ops import (
    OpError,
    apply_transform,
    sanitize_json,
    untrusted_envelope,
    validate_rows,
)
from shifde.runtime.settle import cancel_open_children

MAX_NODE_OUTPUT_BYTES = 2_000_000
MAX_DEPTH = 3
DEFAULT_APPROVALS = frozenset({"write_non_idempotent", "destructive"})
FINAL = ("succeeded", "skipped", "failed")
UNTRUSTED_TYPES = frozenset({"model", "prompt", "agent"})
# node type -> (the config field naming its version, the kind that version must be)
VERSION_FIELDS = {
    "agent": ("agent_version_id", "agents"),
    "prompt": ("prompt_version_id", "prompts"),
    "tool": ("tool_version_id", "tools"),
}
# node types this executor implements (the registry lists a type enabled only when it is here)
EXECUTOR_TYPES = frozenset(
    {
        "data_source",
        "api_request",
        "db_query",
        "ontology_query",
        "validate",
        "transform",
        "condition",
        "model",
        "prompt",
        "agent",
        "tool",
        "approval",
        "sink",
    }
)


class NodeError(state.RunFailed):
    """A node failure that knows whether a retry is sensible and whether a write may have been
    delivered (the two facts the retry / indeterminate rules need)."""

    def __init__(
        self, error_class: str, message: str, *, retryable: bool = False, maybe_sent: bool = False
    ) -> None:
        super().__init__(error_class, message)
        self.retryable = retryable
        self.maybe_sent = maybe_sent


@dataclass
class Resolved:
    status: str  # succeeded | skipped | failed
    output: dict[str, Any] | None  # Any: port -> value
    error_class: str | None = None


def _text_of(value: Any) -> str:  # Any: JSON value
    return value if isinstance(value, str) else json.dumps(value, default=str)


class PipelineRunner:
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self._outputs: dict[UUID, dict[str, Any] | None] = {}
        self.blocked: dict[str, str] = {}
        self._resolved_now: Mapping[str, Resolved] = {}

    # ================================================================== setup
    def run(self) -> Any:  # Any: the run's output document
        self._load()
        self._start_gate()
        self._revalidate()
        ctx = self.ctx
        while True:
            ctx.checkpoint()
            resolved, open_steps = self._state()
            if self._collect_children(open_steps):
                continue
            progressed = False
            ready_agents: list[str] = []
            for nid in self.order:
                if nid in resolved or nid in self.blocked:
                    continue
                if nid in open_steps and open_steps[nid]["child_run_id"] is not None:
                    continue  # its child run is still open
                verdict, why = self._ready(nid, resolved)
                if verdict == "wait":
                    continue
                if verdict == "skip":
                    self._record_skipped(nid, why)
                    progressed = True
                    break
                if self.nodes[nid]["type"] == "agent":
                    ready_agents.append(nid)
                    continue
                try:
                    self._run_inline(nid, resolved)
                except state.Waiting as waiting:
                    if waiting.transitioned:
                        raise
                    self.blocked[nid] = waiting.reason
                    continue
                progressed = True
                break
            if progressed:
                continue
            open_children = [n for n, s in open_steps.items() if s["child_run_id"] is not None]
            if ready_agents:
                slots = int(self.policies["max_parallel"]) - len(open_children)
                if slots > 0 and self._start_children(ready_agents[:slots], resolved):
                    continue  # every node of the batch failed before a child could start
            if self.blocked:
                raise state.Waiting(next(iter(self.blocked.values())))
            if open_children:
                raise state.Waiting("child_runs")
            break
        resolved, _ = self._state()
        unresolved = [n for n in self.order if n not in resolved]
        if unresolved:
            raise state.RunFailed(state.INTERNAL, f"nodes could not be scheduled: {unresolved}")
        failed = self._first_failure()  # from the journal: survives advances and recoveries
        if failed is not None:
            nid, cls, message = failed
            raise state.RunFailed(cls, f"node {nid} failed: {message}"[:500])
        return self._result(resolved)

    def _first_failure(self) -> tuple[str, str, str] | None:
        latest: dict[str, Mapping[str, Any]] = {}
        for st in repo.list_steps(self.ctx.session, self.ctx.tenant_id, self.ctx.run_id):
            if st["kind"] == "pipeline_node":
                latest[st["node_id"]] = st
        failed = [st for st in latest.values() if st["status"] in ("failed", "indeterminate")]
        if not failed:
            return None
        first = min(failed, key=lambda st: int(st["seq"]))
        return (
            str(first["node_id"]),
            str(first["error_class"] or state.INTERNAL),
            str(first["error_message"] or ""),
        )

    def _stored_run(self) -> Mapping[str, Any]:  # Any: run row
        """The run row re-read with the client id: the version executed and every child's scope
        come from it, never from the context's mapping (runtime isolation review 7 item 1). A run
        that is gone or no longer names the context's pipeline version is the unknown-version
        answer."""
        ctx = self.ctx
        run = repo.get_run(ctx.session, ctx.tenant_id, ctx.run_id)
        if (
            run is None
            or run["tenant_id"] != ctx.tenant_id
            or run["kind"] != "pipeline"
            or run["pipeline_id"] != ctx.run["pipeline_id"]
            or run["pipeline_version_id"] != ctx.run["pipeline_version_id"]
        ):
            raise state.RunFailed(state.VALIDATION, "the pinned pipeline version is not available")
        return run

    def _load(self) -> None:
        ctx = self.ctx
        run = self._stored_run()
        self.stored: Mapping[str, Any] = run  # Any: the stored run row
        self.art: dict[str, Any] = {  # Any: artifact row stand-in for the validators' env
            "id": run["pipeline_id"],
            "tenant_id": ctx.tenant_id,
            "workspace_id": run["workspace_id"],
            "project_id": run["project_id"],
            "kind": "pipelines",
            "key": "",
            "name": "",
            "status": "active",
        }
        try:
            # the run's own env: its pinned version and what it pins (`run_of`, B-1933), built
            # from the stored run row, never the context's mapping (runtime isolation review 8
            # item 4)
            self.env = ctx.artifacts.env(self.art, run_of=self.stored)
        except NotFound as exc:
            raise state.RunFailed(
                state.VALIDATION, "the pinned pipeline version is not available"
            ) from exc
        version = self.env.version(run["pipeline_version_id"])
        if version is None or version.kind != "pipelines":
            raise state.RunFailed(state.VALIDATION, "the pinned pipeline version is not available")
        self.version = version
        payload = repo.get_artifact(ctx.session, ctx.tenant_id, run["input_ref"]) or {}
        self.params: dict[str, Any] = dict(payload.get("params") or {})
        spec = version.spec
        snapshot = payload.get("spec_snapshot")
        # a test run executes the draft AS IT WAS when it started, even if that draft was
        # published (or changed) while the run was parked
        self.draft_run = isinstance(snapshot, dict)
        if isinstance(snapshot, dict):
            spec = snapshot
        elif version.state == "draft":
            raise state.RunFailed(state.VALIDATION, "the draft snapshot of the run is missing")
        self.spec = spec

    def _start_gate(self) -> None:
        v = self.version
        self.action = "pipeline:test" if self.draft_run else "pipeline:execute"
        self.pref = ResourceRef(
            self.ctx.tenant_id, v.workspace_id, v.project_id, "pipeline", v.artifact_id
        )
        is_draft_test = self.draft_run and bool(self.stored["is_test"])

        def resource_state() -> gate.Problem | None:
            if v.artifact_status != "active":
                return ("pipeline_not_active", "the pipeline is not active")
            if v.state not in ("published", "deprecated") and not is_draft_test:
                return ("version_not_published", "only published pipelines can run")
            return None

        gate.check(
            self.ctx,
            self.action,
            self.pref,
            label=f"pipeline:{v.artifact_key}",
            resource_state=resource_state,
        )

    def _revalidate(self) -> None:
        """Runs re-validate the pinned version before executing it (contract section 3)."""
        try:
            prepared = self.ctx.artifacts.prepare(
                self.art, self.spec, resolve=False, run_of=self.stored
            )
        except NotFound as exc:
            raise state.RunFailed(
                state.VALIDATION, "the pinned pipeline version is not available"
            ) from exc
        if prepared.report.errors:
            first = prepared.report.errors[0]
            where = f" (node {first.node_id})" if first.node_id else ""
            raise state.RunFailed(
                state.VALIDATION,
                f"the pipeline no longer validates{where}: {first.code}: {first.message}"[:500],
            )
        self.nodes: dict[str, dict[str, Any]] = {n["id"]: n for n in prepared.spec["nodes"]}
        self.edges: list[dict[str, Any]] = prepared.spec["edges"]
        self.policies: dict[str, Any] = prepared.spec["policies"]
        self.named: dict[str, Any] = prepared.spec.get("schemas") or {}
        self.order: list[str] = prepared.topo_order or []
        self.incoming: dict[str, list[dict[str, Any]]] = {n: [] for n in self.nodes}
        for e in self.edges:
            self.incoming[e["to"]].append(e)
        self.versions: dict[str, VersionRef] = {}
        for nid, node in self.nodes.items():
            fld = VERSION_FIELDS.get(node["type"])
            if fld is None:
                continue
            # only from the run's pin set, and only a version of the node's kind (B-2162)
            found = self.env.version(UUID(str(node["config"][fld[0]])))
            if found is None or found.kind != fld[1]:
                raise state.RunFailed(state.VALIDATION, f"node {nid}: its version is not available")
            self.versions[nid] = found
        parents: dict[str, set[str]] = {
            n: {e["from"] for e in self.incoming[n]} for n in self.nodes
        }
        self.ancestors: dict[str, set[str]] = {}
        for nid in self.order:  # topological: the parents are done first
            above: set[str] = set()
            for p in parents[nid]:
                above |= {p} | self.ancestors[p]
            self.ancestors[nid] = above
        self.pinned_tools = frozenset(
            v.version_id for n, v in self.versions.items() if self.nodes[n]["type"] == "tool"
        )

    @staticmethod
    def _edge_live(e: Mapping[str, Any], resolved: Mapping[str, Resolved]) -> bool:  # Any: edge
        r = resolved.get(e["from"])
        if r is None or r.status != "succeeded":
            return False
        if e["when"] == "always":
            return True
        return bool((e["when"] == "true") == bool((r.output or {}).get("result")))

    def _live_upstream(
        self, nid: str, resolved: Mapping[str, Resolved], *, through_approvals: bool
    ) -> set[str]:
        """Nodes reachable backwards from `nid` over LIVE edges only (a succeeded source and a
        matching condition: skipped nodes and dead edges are not live). An approval node is a
        guard: it is reported, and only expanded when `through_approvals`."""
        seen: set[str] = set()
        stack = [nid]
        while stack:
            cur = stack.pop()
            for e in self.incoming[cur]:
                src = e["from"]
                if src in seen or not self._edge_live(e, resolved):
                    continue
                seen.add(src)
                if self.nodes[src]["type"] == "approval" and not through_approvals:
                    continue
                stack.append(src)
        return seen

    def _unguarded_on_live_path(self, nid: str, resolved: Mapping[str, Resolved]) -> str | None:
        """Why a side-effect node may not run (rule 8 on LIVE paths), else None. Only nodes with
        model output upstream are constrained; every live path from such a source must pass a
        granted approval, and data bound from a source needs a live (approved) path to the node."""
        direct = self._live_upstream(nid, resolved, through_approvals=False)
        for src in sorted(direct):
            if self.nodes[src]["type"] in UNTRUSTED_TYPES:
                return f"model output of {src} reaches it on a live path without an approval"
        full = self._live_upstream(nid, resolved, through_approvals=True)
        # model output may reach the node through ANY pass-through node (validate, transform,
        # ...): every succeeded model / prompt / agent ancestor must be covered by a granted
        # approval on a live path, not only the ones bound directly
        for src in sorted(self.ancestors[nid]):
            got = resolved.get(src)
            if (
                self.nodes[src]["type"] in UNTRUSTED_TYPES
                and got is not None
                and got.status == "succeeded"
                and src not in full
            ):
                return f"no live approved path carries the output of {src} to it"
        return None

    def _tool_policy(self, nid: str, resolved: Mapping[str, Resolved]) -> steps.ToolPolicy:
        """A GRANTED approval on a live path to THIS node waives only the default
        write_non_idempotent per-call approval; otherwise it stays; destructive is always forced."""
        guarded = any(
            self.nodes[a]["type"] == "approval"
            for a in self._live_upstream(nid, resolved, through_approvals=True)
        )
        return steps.ToolPolicy(
            approval_classes=frozenset() if guarded else DEFAULT_APPROVALS,
            agent_id=None,
            pinned_version_ids=self.pinned_tools,
        )

    def _is_side_effect(self, nid: str) -> bool:
        node = self.nodes[nid]
        if self._is_write(node):
            return True
        if node["type"] == "tool":
            return self.versions[nid].spec.get("side_effect_class") in state.WRITE_SIDE_EFFECTS
        return False

    # ================================================================== journal
    def _node_steps(self, nid: str) -> list[Mapping[str, Any]]:
        return [
            s
            for s in repo.list_steps(self.ctx.session, self.ctx.tenant_id, self.ctx.run_id)
            if s["kind"] == "pipeline_node" and s["node_id"] == nid
        ]

    def _state(self) -> tuple[dict[str, Resolved], dict[str, Mapping[str, Any]]]:
        """(resolved nodes, open step per node) from the latest step row of every node."""
        latest: dict[str, Mapping[str, Any]] = {}
        for s in repo.list_steps(self.ctx.session, self.ctx.tenant_id, self.ctx.run_id):
            if s["kind"] == "pipeline_node":
                latest[s["node_id"]] = s  # rows come ordered by (seq, attempt)
        resolved: dict[str, Resolved] = {}
        open_steps: dict[str, Mapping[str, Any]] = {}
        for nid, s in latest.items():
            if s["status"] == "failed" and self._retries_left(nid, s):
                continue  # the worker died between two attempts: the node is not finished
            if s["status"] in FINAL:
                resolved[nid] = Resolved(
                    s["status"],
                    self._output_of(s) if s["status"] == "succeeded" else None,
                    s["error_class"],
                )
            elif s["status"] in ("running", "indeterminate"):
                open_steps[nid] = s
        return resolved, open_steps

    def _retries_left(self, nid: str, step: Mapping[str, Any]) -> bool:
        node = self.nodes[nid]
        if node["type"] == "tool" or self._is_write(node):
            return False  # the tool retries itself; side effects are never repeated
        max_attempts = int((node.get("retry") or {}).get("max_attempts", 1))
        return int(step["attempt"]) < max_attempts and step["error_class"] in state.RETRYABLE

    def _output_of(self, step: Mapping[str, Any]) -> dict[str, Any] | None:
        sid: UUID = step["id"]
        if sid not in self._outputs:
            content = (
                repo.get_artifact(self.ctx.session, self.ctx.tenant_id, step["output_ref"]) or {}
            )
            out = content.get("output")
            self._outputs[sid] = out if isinstance(out, dict) else {}
        return self._outputs[sid]

    # ================================================================== readiness / bindings
    @staticmethod
    def _bound_sources(node: Mapping[str, Any]) -> set[str]:
        values = list((node.get("inputs") or {}).values())
        args = (node.get("config") or {}).get("args")
        if isinstance(args, dict):
            values += [v for v in args.values() if isinstance(v, str)]
        out: set[str] = set()
        for value in values:
            m = BINDING.match(value)
            if m is not None:
                out.add(m.group(1))
        return out

    def _ready(self, nid: str, resolved: Mapping[str, Resolved]) -> tuple[str, str]:
        inc = self.incoming[nid]
        if any(e["from"] not in resolved for e in inc):
            return "wait", ""
        for src in self._bound_sources(self.nodes[nid]):
            r = resolved.get(src)
            if r is None:
                return "wait", ""
            if r.status != "succeeded":
                return "skip", f"input node {src} did not succeed ({r.status})"
        if inc:
            live = False
            for e in inc:
                r = resolved[e["from"]]
                if r.status != "succeeded":
                    continue
                if e["when"] == "always":
                    live = True
                    break
                result = bool((r.output or {}).get("result"))
                if (e["when"] == "true") == result:
                    live = True
                    break
            if not live:
                return "skip", "no incoming edge is live"
        # edges are OR-joined, but a side-effect node fed by model output may only run when
        # every LIVE path from that output passed a granted approval (not merely when one edge
        # woke it); skipped approvals on branches not taken do not count
        if self._is_side_effect(nid):
            why = self._unguarded_on_live_path(nid, resolved)
            if why is not None:
                return "skip", why
        return "run", ""

    def _resolve_binding(self, value: str, resolved: Mapping[str, Resolved]) -> Any:  # Any: JSON
        param = PARAM_BINDING.match(value)
        if param is not None:
            return self.params.get(param.group(1))
        m = BINDING.match(value)
        if m is None:
            raise NodeError(state.VALIDATION, f"invalid binding {value!r}")
        src = resolved.get(m.group(1))
        if src is None or src.output is None or m.group(2) not in src.output:
            raise NodeError(state.VALIDATION, f"{value} is not available")
        return src.output[m.group(2)]

    def _inputs(self, nid: str, resolved: Mapping[str, Resolved]) -> dict[str, Any]:  # Any: JSON
        node = self.nodes[nid]
        return {
            p: self._resolve_binding(b, resolved) for p, b in (node.get("inputs") or {}).items()
        }

    # ================================================================== records
    def _open_step(
        self, nid: str, *, attempt: int | None = None, idempotency_key: str | None = None
    ) -> Mapping[str, Any]:
        """The running step of the node (a recovered / waiting execution continues under it) or a
        new attempt. A write node passes the idempotency key it is about to send: a `running` step
        that carries one is a write that may have been delivered (see
        `shifde.settle_ended_run`)."""
        ctx = self.ctx
        rows = self._node_steps(nid)
        last = rows[-1] if rows else None
        if attempt is None and last is not None and last["status"] == "running":
            return last
        node = self.nodes[nid]
        with ctx.txn():
            seq = (
                last["seq"]
                if last is not None
                else repo.next_seq(ctx.session, ctx.tenant_id, ctx.run_id)
            )
            step = repo.insert_step(
                ctx.session,
                {
                    "tenant_id": ctx.tenant_id,
                    "run_id": ctx.run_id,
                    "seq": seq,
                    "attempt": attempt
                    if attempt is not None
                    else (last["attempt"] + 1 if last else 1),
                    "kind": "pipeline_node",
                    "name": f"{node['type']}:{nid}",
                    "node_id": nid,
                    "status": "running",
                    **({"idempotency_key": idempotency_key} if idempotency_key else {}),
                },
            )
            steps._step_event(ctx, "step_started", step, {"node": nid})
        return step

    def _finish(
        self,
        step: Mapping[str, Any],
        status: str,
        output: dict[str, Any] | None = None,
        *,
        error_class: str | None = None,
        message: str | None = None,
        stored_doc: Any = None,  # Any: sink document stored as a run artifact
        land: bool = False,
    ) -> Mapping[str, Any]:
        ctx = self.ctx
        with ctx.txn():
            values: dict[str, Any] = {"status": status, "finished_at": utcnow()}
            if status == "succeeded":
                body = sanitize_json(dict(output or {}))
                stored_doc = sanitize_json(stored_doc)
                if stored_doc is not None:
                    doc_id = repo.insert_artifact(
                        ctx.session,
                        ctx.tenant_id,
                        ctx.run_id,
                        "output",
                        scrub(stored_doc),
                        step["id"],
                    )
                    result = dict(body.get("result") or {})
                    result["artifact_id"] = str(doc_id)
                    body["result"] = result
                values["output_ref"] = repo.insert_artifact(
                    ctx.session,
                    ctx.tenant_id,
                    ctx.run_id,
                    "intermediate",
                    scrub({"output": body}),
                    step["id"],
                )
            if error_class:
                values["error_class"] = error_class
                values["error_message"] = scrub_text(message or "")[:500]
            elif status == "succeeded":  # a step that was unknown and is now confirmed carries none
                values["error_class"] = None
                values["error_message"] = None
            row = repo.update_step(ctx.session, ctx.tenant_id, step["id"], **values)
            if land:  # an operator's verdict covers the node's earlier deliveries: same transaction
                repo.land_verdicts(ctx.session, ctx.tenant_id, ctx.run_id)
            started = step["started_at"]
            duration = int((utcnow() - started).total_seconds() * 1000) if started else None
            steps._usage_event(
                ctx,
                "pipeline_node",
                row,
                status="succeeded" if status == "succeeded" else "failed",
                duration_ms=duration,
            )
            steps._step_event(
                ctx,
                "step_finished",
                row,
                {
                    "status": status,
                    "node": row["node_id"],
                    **({"error_class": error_class} if error_class else {}),
                },
            )
        return row

    def _record_skipped(self, nid: str, why: str) -> None:
        ctx = self.ctx
        node = self.nodes[nid]
        with ctx.txn():
            seq = repo.next_seq(ctx.session, ctx.tenant_id, ctx.run_id)
            row = repo.insert_step(
                ctx.session,
                {
                    "tenant_id": ctx.tenant_id,
                    "run_id": ctx.run_id,
                    "seq": seq,
                    "attempt": 1,
                    "kind": "pipeline_node",
                    "name": f"{node['type']}:{nid}",
                    "node_id": nid,
                    "status": "skipped",
                },
            )
            row = repo.update_step(
                ctx.session, ctx.tenant_id, row["id"], status="skipped", finished_at=utcnow()
            )
            steps._step_event(
                ctx, "step_finished", row, {"status": "skipped", "node": nid, "why": why}
            )

    def _fit(self, output: dict[str, Any]) -> dict[str, Any]:  # Any: port outputs
        """The output of a DELIVERED write: never failed for its size, truncated instead."""
        try:
            self._check_size(output)
        except NodeError:
            return {"result": {"delivered": True, "truncated": True}, "truncated": True}
        return output

    def _check_size(self, output: Mapping[str, Any]) -> None:
        if (
            len(json.dumps(scrub({"output": dict(output)}), default=str).encode())
            > MAX_NODE_OUTPUT_BYTES
        ):
            raise NodeError(
                state.VALIDATION, f"the node output is larger than {MAX_NODE_OUTPUT_BYTES} bytes"
            )

    # ================================================================== failure policy
    def _on_failure(self, nid: str, error_class: str, message: str) -> None:
        """fail_fast: cancel the open children and end the run now; continue: remember it."""
        if self.policies["on_failure"] == "fail_fast":
            self._cancel_children()
            raise state.RunFailed(error_class, f"node {nid} failed: {message}"[:500])
        # continue: the failed step is the record; the run fails when everything else is done

    def _cancel_children(self) -> None:
        """Cancel the open child runs (own transactions, the lease is checked before each)."""
        ctx = self.ctx

        def still_leader() -> None:
            ctl = repo.control_state(ctx.session, ctx.tenant_id, ctx.run_id)
            ctx.session.commit()
            if ctl is None or ctl["status"] != state.RUNNING or ctl["lease_token"] != ctx.token:
                raise state.LeaseLost()

        cancel_open_children(
            ctx.session,
            ctx.tenant_id,
            ctx.run_id,
            self.stored["initiated_by_principal_id"],
            still_leader,
        )

    # ================================================================== inline nodes
    def _node_gate(self, nid: str) -> None:
        """Every node execution passes the Execution Gate with the INITIATING principal's current
        permissions (a revocation takes effect at the next node)."""
        gate.check(
            self.ctx,
            self.action,
            self.pref,
            label=f"pipeline:{self.version.artifact_key}:{nid}",
        )

    def _is_write(self, node: Mapping[str, Any]) -> bool:
        cfg = node["config"]
        if node["type"] == "sink":
            return bool(cfg.get("target") == "connection")
        if node["type"] == "api_request":
            return str(cfg.get("method", "GET")).upper() in WRITE_METHODS
        return False

    def _run_inline(self, nid: str, resolved: Mapping[str, Resolved]) -> None:
        node = self.nodes[nid]
        self._resolved_now = resolved
        try:
            self._node_gate(nid)
        except state.GateDenied as exc:
            rows = self._node_steps(nid)
            last = rows[-1]["status"] if rows else None
            # a write step found `running` / `indeterminate` was started (write-ahead): its
            # outcome is UNKNOWN and that step stays the record (nothing is appended)
            unknown = self._is_write(node) and last in ("running", "indeterminate")
            if not (unknown and last == "indeterminate"):
                step = self._open_step(nid)
                self._finish(
                    step,
                    "indeterminate" if unknown else "failed",
                    error_class=exc.error_class,
                    message=exc.message,
                )
            self._cancel_children()
            raise
        if node["type"] == "approval":
            self._approval_node(nid, resolved)
        elif self._is_write(node):
            self._write_node(nid, resolved)
        else:
            self._read_node(nid, resolved)

    def _read_node(self, nid: str, resolved: Mapping[str, Resolved]) -> None:
        node = self.nodes[nid]
        retry_cfg = node.get("retry") or {}
        max_attempts = 1 if node["type"] == "tool" else int(retry_cfg.get("max_attempts", 1))
        while True:
            step = self._open_step(nid)
            try:
                output, stored = self._compute(nid, node, resolved)
                output = sanitize_json(output)
                self._check_size(output)
            except state.Waiting:
                raise
            except state.GateDenied as exc:
                self._finish(step, "failed", error_class=exc.error_class, message=exc.message)
                self._cancel_children()
                raise
            except state.RunFailed as exc:
                self._finish(step, "failed", error_class=exc.error_class, message=exc.message)
                retryable = (
                    exc.retryable
                    if isinstance(exc, NodeError)
                    else exc.error_class in state.RETRYABLE
                )
                if retryable and int(step["attempt"]) < max_attempts:
                    delay = steps.backoff_seconds(
                        float(retry_cfg.get("backoff_s", 0)),
                        self.ctx.settings.runtime_retry_cap_seconds,
                        int(step["attempt"]),
                    )
                    with self.ctx.txn():
                        repo.add_event(
                            self.ctx.session,
                            self.stored,
                            "retry_scheduled",
                            step_id=step["id"],
                            details={
                                "node": nid,
                                "next_attempt": int(step["attempt"]) + 1,
                                "error_class": exc.error_class,
                                "backoff_ms": int(delay * 1000),
                            },
                        )
                    self.ctx.sleep(delay)
                    continue
                self._on_failure(nid, exc.error_class, exc.message)
                return
            self._finish(step, "succeeded", output, stored_doc=stored)
            return

    def _compute(
        self,
        nid: str,
        node: Mapping[str, Any],
        resolved: Mapping[str, Resolved],
    ) -> tuple[dict[str, Any], Any]:  # Any: (port outputs, document to store)
        kind = node["type"]
        cfg = node["config"]
        inputs = self._inputs(nid, resolved)
        try:
            if kind in ("data_source", "db_query"):
                return self._sql_node(nid, node, resolved), None
            if kind == "ontology_query":
                return self._ontology_node(nid, node), None
            if kind == "api_request":
                return self._http_read(nid, node, inputs), None
            if kind == "validate":
                return self._validate_node(cfg, inputs), None
            if kind == "transform":
                return self._transform_node(cfg, inputs), None
            if kind == "condition":
                return self._condition_node(cfg, inputs), None
            if kind in ("model", "prompt"):
                return self._model_node(nid, node, inputs), None
            if kind == "tool":
                return self._tool_node(nid, inputs), None
            if kind == "sink":  # run_artifact target
                document = inputs.get("document")
                return {"result": {"mode": cfg.get("mode", "replace")}}, document
        except OpError as exc:
            raise NodeError(state.VALIDATION, str(exc)) from exc
        except safeexpr.ExprError as exc:
            raise NodeError(state.VALIDATION, str(exc)) from exc
        raise NodeError(state.INTERNAL, f"no executor for node type {kind!r}")

    # ---------------------------------------------------------------- pure nodes
    def _validate_node(self, cfg: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
        schema = expand_refs(cfg["schema"], self.named)
        mode = str(cfg.get("on_invalid", "fail"))
        try:
            valid, invalid, was_list = validate_rows(schema, inputs.get("data"))
        except Exception as exc:  # noqa: BLE001 - a malformed schema must fail the node, not the worker
            raise NodeError(
                state.VALIDATION, f"the schema could not be applied ({type(exc).__name__})"
            ) from exc
        if invalid and (mode == "fail" or not was_list):
            first = invalid[0]["errors"][0]
            raise NodeError(
                state.SCHEMA_VIOLATION,
                f"{len(invalid)} invalid item(s); first: {first}"[:400],
            )
        data: Any = valid if was_list else (valid[0] if valid else None)
        return {"data": data, "invalid": invalid if mode == "route" else []}

    def _transform_node(self, cfg: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
        return {"data": apply_transform(inputs.get("data"), cfg["operations"], self.params)}

    def _condition_node(self, cfg: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
        scope = {"inputs": dict(inputs), "params": self.params}
        return {"result": safeexpr.evaluate_condition(str(cfg["expression"]), scope)}

    # ---------------------------------------------------------------- ontology
    def _ontology_node(self, nid: str, node: Mapping[str, Any]) -> dict[str, Any]:
        """Execution Gate `ontology:query` on the ontology, then the query as the run's own
        principal (policies, row filters and property denial apply)."""
        cfg = node["config"]
        oid, vid = UUID(str(cfg["ontology_id"])), UUID(str(cfg["version_id"]))
        ontology_exec.gate_query(self.ctx, oid, vid, f"node:{nid}")
        try:
            out = ontology_exec.run_query(
                self.ctx,
                oid,
                vid,
                cfg["query"],
                channel="pipeline",
                purpose=f"pipeline:{self.ctx.run_id}:{nid}",
                consts=self.params,
            )
        except AppError as exc:
            cls, retryable, sent = steps.classify_tool_failure(exc)
            raise NodeError(
                cls, steps._short(exc.detail), retryable=retryable, maybe_sent=sent
            ) from exc
        return {"rows": out.rows, "row_count": len(out.rows), "lineage_id": str(out.lineage_id)}

    # ---------------------------------------------------------------- connections
    def _connection_gate(self, nid: str, connection_id: UUID, *, write: bool) -> None:
        ctx = self.ctx
        conns = ctx.artifacts.conns
        # a connection of this client that the run's pinned specs name (the run's env), at the
        # client level or the stored run's own workspace and project (B-2161); anything else,
        # another client's included, is the unknown-connection answer
        row = (
            ctx.artifacts.run_connection(ctx.run_id, connection_id)
            if self.env.connection(connection_id) is not None
            else None
        )
        if row is None:
            raise state.GateDenied(
                state.AUTHZ_DENIED, "the connection is not available", "connection_missing"
            )

        def resource_state() -> gate.Problem | None:
            if row["status"] != "active":
                return ("connection_inactive", f"the connection is {row['status']}")
            desc = ctx.runtime.registry.descriptor(row["adapter_key"])
            if desc is None or not desc.implemented:
                return ("adapter_planned", "the connection's adapter is not implemented")
            return None

        gate.check(
            ctx,
            "connection:use",
            conns.ref_of(row),
            label=f"connection:{connection_id}",
            also_require=["connection:write"] if write else [],
            resource_state=resource_state,
        )

    def _invoke_connection(
        self,
        nid: str,
        connection_id: UUID,
        request: dict[str, Any],
        *,
        write: bool,
    ) -> dict[str, Any]:  # Any: adapter result
        ctx = self.ctx
        timeout = min(float(ctx.settings.connection_call_deadline_seconds), ctx.remaining())
        if timeout <= 0:
            raise state.TimedOut()
        try:
            # the session re-reads the row: it must still lie at the run's scope (the row used is
            # the row checked; runtime isolation review 6 round 2)
            with ctx.artifacts.conns.open_for_use(
                connection_id,
                f"pipeline:{ctx.run_id}:{nid}",
                write=write,
                deadline=timeout,
                at=(self.stored["workspace_id"], self.stored["project_id"]),
            ) as session:
                result = session.invoke(request, write=write)
        except AdapterFailure as exc:
            cls, retryable, sent = steps.classify_tool_failure(exc)
            raise NodeError(
                cls, steps._short(exc.message), retryable=retryable, maybe_sent=sent
            ) from exc
        except AppError as exc:
            cls, retryable, sent = steps.classify_tool_failure(exc)
            raise NodeError(
                cls, steps._short(exc.detail), retryable=retryable, maybe_sent=sent
            ) from exc
        return dict(result.result)

    def _call_connection(
        self, nid: str, connection_id: UUID, request: dict[str, Any], *, write: bool
    ) -> dict[str, Any]:  # Any: adapter result
        self._connection_gate(nid, connection_id, write=write)
        return self._invoke_connection(nid, connection_id, request, write=write)

    def _sql_node(
        self, nid: str, node: Mapping[str, Any], resolved: Mapping[str, Resolved]
    ) -> dict[str, Any]:
        cfg = node["config"]
        cid = UUID(str(cfg["connection_id"]))
        if node["type"] == "data_source":
            parts = str(cfg["dataset"]).split(".")
            if not all(IDENT.match(p) for p in parts):
                raise NodeError(state.VALIDATION, "the dataset is not a table name")
            limit = int(cfg.get("limit", 1000))
            table = ".".join('"' + p + '"' for p in parts)
            sql = f"SELECT * FROM {table} LIMIT %(row_limit)s"
            params: dict[str, Any] = {"row_limit": limit}
        else:
            limit = int(cfg.get("max_rows", 1000))
            sql = str(cfg["sql"])
            params = {}
            for name, value in (cfg.get("args") or {}).items():
                if isinstance(value, str):
                    params[name] = self._resolve_binding(value, resolved)
                elif isinstance(value, dict):
                    params[name] = value.get("value")
                else:
                    params[name] = value
        result = self._call_connection(nid, cid, {"sql": sql, "params": params}, write=False)
        columns = [str(c) for c in result.get("columns") or []]
        rows = [dict(zip(columns, r, strict=False)) for r in result.get("rows") or []][:limit]
        return {"rows": rows, "row_count": len(rows)}

    def _http_read(
        self, nid: str, node: Mapping[str, Any], inputs: Mapping[str, Any]
    ) -> dict[str, Any]:
        cfg = node["config"]
        request = self._rest_request(cfg, inputs, None)
        result = self._call_connection(nid, UUID(str(cfg["connection_id"])), request, write=False)
        return self._http_output(result)

    @staticmethod
    def _rest_request(
        cfg: Mapping[str, Any], inputs: Mapping[str, Any], key: str | None
    ) -> dict[str, Any]:
        binding = {
            "method": cfg.get("method", "GET"),
            "path_template": cfg["path_template"],
            "query_template": cfg.get("query"),
            "body_template": cfg.get("body"),
        }
        try:
            return tool_runtime.rest_request(binding, dict(inputs), key)
        except AppError as exc:
            raise NodeError(state.VALIDATION, exc.detail) from exc

    @staticmethod
    def _http_output(result: Mapping[str, Any], *, strict: bool = False) -> dict[str, Any]:
        """`strict`: the node has an earlier delivery whose outcome is unknown, so only a 2xx
        answers it."""
        status = result.get("status")
        if isinstance(status, int):
            if status == 429:
                raise NodeError(
                    state.RATE_LIMITED, "the upstream rate limited the call", retryable=True
                )
            if status >= 500:
                raise NodeError(
                    state.PROVIDER_UNAVAILABLE,
                    f"the upstream answered {status}",
                    retryable=True,
                    maybe_sent=True,
                )
            if status >= 400:
                raise NodeError(state.PROVIDER_ERROR_4XX, f"the upstream answered {status}")
            if strict and not 200 <= status < 300:
                raise NodeError(
                    state.TOOL_ERROR, f"the upstream answered {status}, which is not a success"
                )
        return {"response": dict(result), "status": status}

    # ---------------------------------------------------------------- models / tools
    def _model_node(
        self, nid: str, node: Mapping[str, Any], inputs: Mapping[str, Any]
    ) -> dict[str, Any]:
        cfg = node["config"]
        ctx = self.ctx
        if node["type"] == "model":
            system = str(cfg.get("system") or "You are a careful data-processing assistant.")
            messages = [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": untrusted_envelope(f"pipeline:{nid}", inputs.get("prompt")),
                },
            ]
        else:
            messages = self._render_prompt(nid, inputs.get("variables") or {})
        model_id = UUID(str(cfg["model_id"]))
        prior = [
            s
            for s in repo.list_steps(ctx.session, ctx.tenant_id, ctx.run_id)
            if s["kind"] == "model_call" and s["node_id"] == nid
        ]
        if prior and prior[-1]["status"] == "succeeded":  # finished before the worker died
            done = repo.get_artifact(ctx.session, ctx.tenant_id, prior[-1]["output_ref"]) or {}
            return {"text": str(done.get("text") or "")}
        result = steps.call_model(
            ctx,
            messages,
            [model_id],
            f"node:{nid}",
            prior,
            lambda _t: None,
            node_id=nid,
            # Item 10: the pipeline's cost ceiling when its spec carries one (B-2192)
            max_cost=self.policies.get("max_cost"),
        )
        return {"text": result.text}

    def _render_prompt(self, nid: str, variables: Any) -> list[dict[str, str]]:  # Any: JSON
        ref = self.versions[nid]
        if not isinstance(variables, dict):
            raise NodeError(state.VALIDATION, "the prompt variables must be an object")
        try:
            node = build_node(ref.spec, self.env)
        except ValueError as exc:
            raise NodeError(state.VALIDATION, str(exc)) from exc
        declared = merged_variables(node)
        self._refuse_trusted(declared, variables)
        values, problems = resolve_variables(declared, variables)
        if problems:
            raise NodeError(
                state.VALIDATION,
                "invalid prompt variables: " + "; ".join(f"{p.name} {p.message}" for p in problems),
            )
        messages, _ = render_prompt(node, values, declared)
        return [dict(m) for m in messages]

    @staticmethod
    def _refuse_trusted(
        declared: Mapping[str, Mapping[str, Any]], supplied: Mapping[str, Any]
    ) -> None:
        """Pipeline data is untrusted: it can never fill a variable the prompt declares trusted
        (those are rendered into the system message)."""
        bad = sorted(n for n in supplied if (declared.get(n) or {}).get("trusted") is True)
        if bad:
            raise NodeError(
                state.VALIDATION,
                f"variables {bad} are declared trusted: pipeline data is untrusted "
                "and cannot fill them",
            )

    def _tool_node(self, nid: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
        args = inputs.get("args")
        if not isinstance(args, dict):
            raise NodeError(state.VALIDATION, "the tool arguments must be an object")
        output = steps.run_tool(
            self.ctx,
            steps.ToolCall(tool=self.versions[nid], args=args, call_ref=f"node:{nid}", node_id=nid),
            self._tool_policy(nid, self._resolved_now),
        )
        return {"output": output}

    # ================================================================== side effects
    def _epoch(self, call_ref: str) -> int:
        epoch = 0
        while True:
            appr = repo.find_approval(
                self.ctx.session,
                self.ctx.tenant_id,
                self.ctx.run_id,
                call_ref,
                "indeterminate_side_effect",
                epoch,
            )
            if appr is not None and appr["status"] == "approved" and appr["decision"] == "retry":
                epoch += 1
                continue
            return epoch

    def _write_node(self, nid: str, resolved: Mapping[str, Resolved]) -> None:
        ctx = self.ctx
        node = self.nodes[nid]
        cfg = node["config"]
        call_ref = f"node:{nid}"
        rows = self._node_steps(nid)
        last = rows[-1] if rows else None
        if last is not None and last["status"] in ("running", "indeterminate"):
            se = int(last["attempt"]) - 1  # the idempotency epoch the step belongs to
            if last["status"] == "running":
                # started (write-ahead) without an outcome: the call MAY have been delivered
                self._park_indeterminate(
                    nid, last, se, state.INDETERMINATE, "the worker was lost around a side effect"
                )
            if self._resolve_indeterminate(nid, last, se):
                return
        epoch = self._epoch(call_ref)
        key = repo.sha256_hex(str(ctx.run_id), call_ref, str(epoch))
        connection_id = UUID(str(cfg["connection_id"]))
        try:
            inputs = self._inputs(nid, resolved)
            if node["type"] == "sink":
                method = str(
                    cfg.get("method") or ("POST" if cfg.get("mode") == "append" else "PUT")
                )
                request: dict[str, Any] = {
                    "method": method,
                    "path": tool_runtime.fill_path(str(cfg["path_template"]), {}),
                    "body": inputs.get("document"),
                    "headers": {"Idempotency-Key": key},
                }
            else:
                request = self._rest_request(cfg, inputs, key)
            self._connection_gate(nid, connection_id, write=True)
            ctx.checkpoint()  # cancel / shutdown are honoured BEFORE anything is sent
        except state.GateDenied as exc:
            step = self._open_step(nid, attempt=epoch + 1)
            self._finish(step, "failed", error_class=exc.error_class, message=exc.message)
            self._cancel_children()
            raise
        except (NodeError, AppError) as exc:
            cls = exc.error_class if isinstance(exc, NodeError) else state.VALIDATION
            msg = exc.message if isinstance(exc, NodeError) else exc.detail
            step = self._open_step(nid, attempt=epoch + 1)
            self._finish(step, "failed", error_class=cls, message=msg)
            self._on_failure(nid, cls, msg)
            return
        # write-ahead: committed BEFORE the call
        step = self._open_step(nid, attempt=epoch + 1, idempotency_key=key)
        try:
            output = self._http_output(
                self._invoke_connection(nid, connection_id, request, write=True),
                strict=epoch > 0,
            )
            if node["type"] == "sink":
                output = {"result": {"status": output["status"], "delivered": True}}
            output = self._fit(sanitize_json(output))
        except NodeError as exc:
            if exc.maybe_sent:
                self._park_indeterminate(nid, step, epoch, exc.error_class, exc.message)
            self._finish(step, "failed", error_class=exc.error_class, message=exc.message)
            self._on_failure(nid, exc.error_class, exc.message)
            return
        self._finish(step, "succeeded", output)

    def _park_indeterminate(
        self, nid: str, step: Mapping[str, Any], epoch: int, error_class: str, message: str
    ) -> None:
        """Mark the step indeterminate, open the operator approval and stop (raises Waiting)."""
        ctx = self.ctx
        with ctx.txn():  # one transaction: no state without its decision
            row = repo.update_step(
                ctx.session,
                ctx.tenant_id,
                step["id"],
                status="indeterminate",
                error_class=error_class,
                error_message=scrub_text(message)[:500],
                finished_at=utcnow(),
            )
            steps._step_event(ctx, "step_finished", row, {"status": "indeterminate", "node": nid})
            self._insert_indeterminate_approval(nid, row, epoch)
        raise state.Waiting("indeterminate_side_effect")

    def _create_indeterminate_approval(self, nid: str, step: Mapping[str, Any], epoch: int) -> None:
        with self.ctx.txn():
            self._insert_indeterminate_approval(nid, step, epoch)

    def _insert_indeterminate_approval(self, nid: str, step: Mapping[str, Any], epoch: int) -> None:
        """In the caller's transaction."""
        ctx = self.ctx
        call_ref = f"node:{nid}"
        if (
            repo.find_approval(
                ctx.session, ctx.tenant_id, ctx.run_id, call_ref, "indeterminate_side_effect", epoch
            )
            is not None
        ):
            return
        node = self.nodes[nid]
        approval = repo.insert_approval(
            ctx.session,
            {
                "tenant_id": ctx.tenant_id,
                "run_id": ctx.run_id,
                "workspace_id": self.stored["workspace_id"],
                "project_id": self.stored["project_id"],
                "step_seq": step["seq"],
                "call_ref": call_ref,
                "epoch": epoch,
                "reason": "indeterminate_side_effect",
                "side_effect_class": "write_non_idempotent",
                "arguments": {"node": nid, "type": node["type"]},
                "context_summary": {"node": nid},
                "requested_by": self.stored["initiated_by_principal_id"],
                "expires_at": utcnow() + timedelta(hours=ctx.settings.approval_expiry_hours),
            },
        )
        repo.add_event(
            ctx.session,
            self.stored,
            "approval_requested",
            step_id=step["id"],
            details={"approval_id": str(approval["id"]), "reason": "indeterminate_side_effect"},
        )
        ctx.audit(
            "approval.requested",
            {
                "approval_id": str(approval["id"]),
                "reason": "indeterminate_side_effect",
                "node": nid,
            },
            commit=False,
        )

    def _resolve_indeterminate(self, nid: str, step: Mapping[str, Any], epoch: int) -> bool:
        """True when an operator confirmed the call happened (node finished); False to retry.
        Raises Waiting / RunFailed otherwise."""
        ctx = self.ctx
        appr = repo.find_approval(
            ctx.session,
            ctx.tenant_id,
            ctx.run_id,
            f"node:{nid}",
            "indeterminate_side_effect",
            epoch,
        )
        if appr is None:
            self._create_indeterminate_approval(nid, step, epoch)
            raise state.Waiting("indeterminate_side_effect")
        if appr["status"] == "pending":
            raise state.Waiting("indeterminate_side_effect")
        decision = appr["decision"]
        if appr["status"] != "approved" or decision == "fail":
            raise state.RunFailed(
                state.INDETERMINATE, "an operator declared the side effect failed or unverifiable"
            )
        if decision == "mark_succeeded":
            self._finish(
                step,
                "succeeded",
                {
                    "result": {
                        "status": "operator_confirmed_succeeded",
                        "note": appr["decision_note"],
                    }
                },
                land=True,  # the verdict covers every earlier delivery of the node
            )
            return True
        return False  # retry: a new idempotency epoch

    # ================================================================== approval node
    def _approval_node(self, nid: str, resolved: Mapping[str, Resolved]) -> None:
        ctx = self.ctx
        node = self.nodes[nid]
        cfg = node["config"]
        call_ref = f"node:{nid}"
        inputs = self._inputs(nid, resolved)
        appr = repo.find_approval(
            ctx.session, ctx.tenant_id, ctx.run_id, call_ref, "pipeline_node", 0
        )
        if appr is None:
            step = self._open_step(nid)
            expires = utcnow() + timedelta(seconds=int(cfg.get("expires_seconds", 86400)))
            with ctx.txn():
                approval = repo.insert_approval(
                    ctx.session,
                    {
                        "tenant_id": ctx.tenant_id,
                        "run_id": ctx.run_id,
                        "workspace_id": self.stored["workspace_id"],
                        "project_id": self.stored["project_id"],
                        "step_seq": step["seq"],
                        "call_ref": call_ref,
                        "epoch": 0,
                        "reason": "pipeline_node",
                        "arguments": scrub(
                            {"message": cfg.get("message"), "context": inputs.get("context")}
                        ),
                        "context_summary": {"node": nid, "message": cfg.get("message")},
                        "requested_by": self.stored["initiated_by_principal_id"],
                        "expires_at": expires,
                    },
                )
                repo.add_event(
                    ctx.session,
                    self.stored,
                    "approval_requested",
                    step_id=step["id"],
                    details={"approval_id": str(approval["id"]), "reason": "pipeline_node"},
                )
                ctx.audit(
                    "approval.requested",
                    {"approval_id": str(approval["id"]), "reason": "pipeline_node", "node": nid},
                    commit=False,
                )
            raise state.Waiting("approval")
        if appr["status"] == "pending":
            raise state.Waiting("approval")
        step = self._open_step(nid)
        if appr["status"] != "approved":
            self._finish(
                step,
                "failed",
                error_class=state.APPROVAL_REJECTED,
                message="the approval was not granted",
            )
            # a rejection ends the run whatever on_failure says (the approval API does the same
            # when it decides a parked run); the open children are cancelled first
            self._cancel_children()
            raise state.RunFailed(
                state.APPROVAL_REJECTED, f"node {nid} failed: the approval was not granted"
            )
        context = inputs.get("context")
        self._finish(step, "succeeded", {"approved": True if context is None else context})

    # ================================================================== agent nodes (child runs)
    def _collect_children(self, open_steps: Mapping[str, Mapping[str, Any]]) -> bool:
        ctx = self.ctx
        changed = False
        for nid, step in open_steps.items():
            child_id = step["child_run_id"]
            if child_id is None:
                continue
            child = repo.get_run(ctx.session, ctx.tenant_id, child_id)
            # only this run's own child (the client id above, the parent here)
            if child is None or child["parent_run_id"] != ctx.run_id:
                self._finish(
                    step, "failed", error_class=state.INTERNAL, message="its child run is missing"
                )
                self._on_failure(nid, state.INTERNAL, "its child run is missing")
            elif child["status"] not in state.TERMINAL:
                continue
            elif child["status"] == state.COMPLETED:
                out = repo.get_artifact(ctx.session, ctx.tenant_id, child["output_ref"]) or {}
                self._finish(step, "succeeded", {"output": out.get("output")})
            else:
                cls = str(child["error_class"] or state.INTERNAL)
                msg = f"the agent run {child['status']}: {child['error_message'] or ''}"[:500]
                self._finish(step, "failed", error_class=cls, message=msg)
                self._on_failure(nid, cls, msg)
            changed = True
        return changed

    def _start_children(self, node_ids: list[str], resolved: Mapping[str, Resolved]) -> bool:
        """Start the child runs of ready agent nodes (raises Waiting once they exist); returns
        True when nothing could be started because the nodes failed their pre-checks."""
        ctx = self.ctx
        run = self.stored
        if int(run["depth"]) + 1 > MAX_DEPTH:
            raise state.RunFailed(
                state.BUDGET_DENIED, f"pipeline limit max_depth ({MAX_DEPTH}) reached"
            )
        prepared: list[tuple[str, VersionRef, UUID, dict[str, Any]]] = []
        for nid in node_ids:
            agent = self.versions[nid]
            inputs = self._inputs(nid, resolved)
            try:
                self._node_gate(nid)
                prompt = self._pinned_prompt(agent)
                if prompt is None:
                    raise NodeError(state.VALIDATION, "the agent's prompt is not available")
                gate.check(
                    ctx,
                    "agent:execute",
                    ResourceRef(
                        ctx.tenant_id,
                        agent.workspace_id,
                        agent.project_id,
                        "agent",
                        agent.artifact_id,
                    ),
                    label=f"agent:{agent.artifact_key}",
                )
                variables = inputs.get("variables") or {}
                if not isinstance(variables, dict):
                    raise NodeError(state.VALIDATION, "the agent variables must be an object")
                self._refuse_trusted_for_agent(agent, variables)
            except state.GateDenied as exc:
                step = self._open_step(nid)
                self._finish(step, "failed", error_class=exc.error_class, message=exc.message)
                self._cancel_children()
                raise
            except NodeError as exc:
                step = self._open_step(nid)
                self._finish(step, "failed", error_class=exc.error_class, message=exc.message)
                self._on_failure(nid, exc.error_class, exc.message)
                continue
            prepared.append(
                (
                    nid,
                    agent,
                    prompt,
                    {"input": _text_of(inputs.get("input")), "variables": variables},
                )
            )
        if not prepared:
            return True
        with ctx.txn():
            # the child's scope from the stored run, re-read under the lease (runtime isolation
            # review 7 item 1)
            run = self._stored_run()
            for nid, agent, prompt, payload in prepared:
                # the parent's client, workspace, project and root only (B-2115: the id is new)
                child = repo.create_run(
                    ctx.session,
                    {
                        "tenant_id": ctx.tenant_id,
                        "workspace_id": run["workspace_id"],
                        "project_id": run["project_id"],
                        "kind": "agent",
                        "initiated_by_principal_id": run["initiated_by_principal_id"],
                        "principal_type": run["principal_type"],
                        "api_key_id": run["api_key_id"],
                        "agent_id": agent.artifact_id,
                        "agent_version_id": agent.version_id,
                        "prompt_version_id": prompt,
                        "parent_run_id": run["id"],
                        "root_run_id": run["root_run_id"],
                        "node_id": nid,
                        "depth": int(run["depth"]) + 1,
                        "is_test": run["is_test"],
                        "trace_id": run["trace_id"],
                        "correlation_id": run["correlation_id"],
                        "deadline_at": run["deadline_at"],
                    },
                    scrub(payload),
                )
                child_id = child["id"]
                rows = self._node_steps(nid)
                seq = (
                    rows[-1]["seq"]
                    if rows
                    else repo.next_seq(ctx.session, ctx.tenant_id, ctx.run_id)
                )
                step = repo.insert_step(
                    ctx.session,
                    {
                        "tenant_id": ctx.tenant_id,
                        "run_id": ctx.run_id,
                        "seq": seq,
                        "attempt": (rows[-1]["attempt"] + 1) if rows else 1,
                        "kind": "pipeline_node",
                        "name": f"agent:{nid}",
                        "node_id": nid,
                        "status": "running",
                        "child_run_id": child_id,
                    },
                )
                steps._step_event(ctx, "step_started", step, {"child_run_id": str(child_id)})
                repo.enqueue(ctx.session, ctx.tenant_id, child_id)
            moved = repo.transition(
                ctx.session,
                ctx.tenant_id,
                ctx.run_id,
                state.RUNNING,
                state.WAITING,
                token=ctx.token,
                clear_lease=True,
                waiting_reason="child_runs",
            )
            if moved is None:
                raise state.LeaseLost()
        raise state.Waiting("child_runs", transitioned=True)

    def _refuse_trusted_for_agent(self, agent: VersionRef, variables: Mapping[str, Any]) -> None:
        if not variables:
            return
        prompt_id = self._pinned_prompt(agent)
        prompt = self.env.version(prompt_id) if prompt_id is not None else None
        if prompt is None:
            raise NodeError(state.VALIDATION, "the agent's prompt is not available")
        try:
            declared = merged_variables(build_node(prompt.spec, self.env))
        except ValueError as exc:
            raise NodeError(state.VALIDATION, str(exc)) from exc
        self._refuse_trusted(declared, variables)

    def _pinned_prompt(self, agent: VersionRef) -> UUID | None:
        """The agent version's prompt when the run's pin set holds it (a prompt version of this
        client that the pipeline's agent pins); None otherwise, like an unknown prompt."""
        try:
            prompt_id = UUID(str(agent.spec.get("prompt_version_id")))
        except ValueError:
            return None
        found = self.env.version(prompt_id)
        return found.version_id if found is not None and found.kind == "prompts" else None

    # ================================================================== result
    def _result(self, resolved: Mapping[str, Resolved]) -> dict[str, Any]:
        has_out = {e["from"] for e in self.edges}
        outputs = {
            n: resolved[n].output
            for n in self.order
            if n not in has_out and resolved[n].status == "succeeded"
        }
        artifacts = {
            n: (resolved[n].output or {}).get("result", {}).get("artifact_id")
            for n in self.order
            if self.nodes[n]["type"] == "sink"
            and resolved[n].status == "succeeded"
            and self.nodes[n]["config"].get("target") == "run_artifact"
        }
        return {
            "outputs": outputs,
            "artifacts": artifacts,
            "skipped": [n for n in self.order if resolved[n].status == "skipped"],
        }
