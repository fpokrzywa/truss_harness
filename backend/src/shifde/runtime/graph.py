"""Graph executor for published orchestrations (SYSTEM_ARCHITECTURE 6.6; reused by P6 pipelines).

The executor is *re-entrant*: on every advance it rebuilds the state of every node from the step
journal and runs whatever is ready. Agent nodes are CHILD RUNS (`parent_run_id`) executed by the
ordinary queue / worker path; the parent moves to `waiting_for_approval(child_runs)` in the same
transaction that creates the children and is woken (waiting -> pending + job) by the last child
that finishes - no worker slot is held while children run, and a crashed worker loses nothing.
`parallel` nodes create up to `max_parallel` agent children at once. Unbounded autonomy is
prevented by hard caps: nodes executed, sub-agent depth, total tokens, run deadline.

Unified-platform port (DECISIONS B-2180..): the orchestration's `Env` is the run's own (`ctx.env`,
the `run_of` contract of B-1933, never a visibility flag), so a node's tool or agent version is
answered only when the run's pinned orchestration version pins it (transitively), and only for a
version of the node's kind; the tool policy's pinned ids come from that same set (B-2162). Every
repository call names the client (B-2115). A child run is created in the parent's client,
workspace and project, under the parent's root, and its agent's prompt must be in the run's pin
set too; a step's child run is read only when it is this run's own child. The executor makes no
model call itself (agent nodes are child runs, whose runner passes `max_cost` and reads routes
through its own env).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Any  # Any: JSON documents
from uuid import UUID

from shifde.authz.service import ResourceRef
from shifde.build.env import VersionRef
from shifde.core import safeexpr
from shifde.core.clock import utcnow
from shifde.core.errors import NotFound
from shifde.core.redaction import scrub
from shifde.runtime import gate, state, steps
from shifde.runtime import repository as repo
from shifde.runtime.context import RunContext

TEMPLATE = re.compile(r"\{\{(.*?)\}\}", re.S)
WHOLE = re.compile(r"\{\{((?:(?!\}\}).)*)\}\}", re.S)  # exactly one reference
DEFAULT_APPROVALS = frozenset({"write_non_idempotent", "destructive"})


def render(value: Any, scope: dict[str, Any]) -> Any:  # Any: JSON template
    """Resolve `{{ expr }}` references of an input map: a string that is exactly one reference
    keeps the value's type, embedded references are substituted as text."""
    if isinstance(value, str):
        whole = WHOLE.fullmatch(value.strip())
        if whole:
            return safeexpr.evaluate(safeexpr.parse(whole.group(1).strip()), scope)

        def sub(m: re.Match[str]) -> str:
            got = safeexpr.evaluate(safeexpr.parse(m.group(1).strip()), scope)
            return got if isinstance(got, str) else json.dumps(got, default=str)

        return TEMPLATE.sub(sub, value)
    if isinstance(value, dict):
        return {k: render(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, scope) for v in value]
    return value


class GraphRunner:
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        run = ctx.run
        self.art = {
            "id": run["orchestration_id"],
            "tenant_id": ctx.tenant_id,
            "workspace_id": run["workspace_id"],
            "project_id": run["project_id"],
            "kind": "orchestrations",
            "key": "",
            "name": "",
            "status": "active",
        }
        try:
            # the run's own env: its pinned version and what it pins (`run_of`, B-1933)
            self.env = ctx.env(self.art)
        except NotFound as exc:
            raise state.RunFailed(
                state.VALIDATION, "the pinned orchestration is not available"
            ) from exc

    # ------------------------------------------------------------------ setup
    def run(self) -> Any:  # Any: orchestration output
        ctx = self.ctx
        orch = self.env.version(ctx.run["orchestration_version_id"])
        if orch is None or orch.kind != "orchestrations":
            raise state.RunFailed(state.VALIDATION, "the pinned orchestration is not available")
        self._start_gate(orch)
        spec = orch.spec
        self.limits = spec["limits"]
        self.nodes: dict[str, dict[str, Any]] = {n["id"]: n for n in spec["nodes"]}
        self.edges: list[dict[str, Any]] = spec["edges"]
        payload = repo.get_artifact(ctx.session, ctx.tenant_id, ctx.run["input_ref"]) or {}
        raw_input = str(payload.get("input", ""))
        try:
            parsed: Any = json.loads(raw_input)
            input_value: Any = parsed if isinstance(parsed, dict | list) else raw_input
        except ValueError:
            input_value = raw_input
        self.input_value = input_value
        branch_ids = {b for n in spec["nodes"] if n["type"] == "parallel" for b in n["branches"]}
        self.branch_ids = branch_ids
        order = self._topological(branch_ids)
        self.tool_refs: dict[str, VersionRef] = {}
        self.agent_refs: dict[str, VersionRef] = {}
        for n in spec["nodes"]:
            ref = n.get("ref") or {}
            # only from the run's pin set, and only a version of the node's kind (B-2162)
            if n["type"] == "tool":
                found = self.env.version(UUID(ref["tool_version_id"]))
                if found is not None and found.kind == "tools":
                    self.tool_refs[n["id"]] = found
            if n["type"] == "agent":
                found = self.env.version(UUID(ref["agent_version_id"]))
                if found is not None and found.kind == "agents":
                    self.agent_refs[n["id"]] = found
        self.policy = steps.ToolPolicy(
            approval_classes=DEFAULT_APPROVALS,
            agent_id=None,
            pinned_version_ids=frozenset(t.version_id for t in self.tool_refs.values()),
        )

        while True:
            journal = self._journal()
            progressed = False
            for nid in order:
                if nid in journal:
                    continue
                verdict = self._ready(nid, journal)
                if verdict == "wait":
                    continue
                if verdict == "skip":
                    self._record_node(nid, "skipped", None)
                    progressed = True
                    break
                self._check_caps(journal)
                self._execute(nid, journal)  # may raise Waiting
                progressed = True
                break
            if not progressed:
                break
        journal = self._journal()
        unresolved = [n for n in order if n not in journal]
        if unresolved:
            raise state.RunFailed(state.INTERNAL, f"nodes could not be scheduled: {unresolved}")
        return self._result(journal, order)

    def _start_gate(self, orch: VersionRef) -> None:
        ref = ResourceRef(
            self.ctx.tenant_id,
            orch.workspace_id,
            orch.project_id,
            "orchestration",
            orch.artifact_id,
        )

        def resource_state() -> gate.Problem | None:
            if orch.artifact_status != "active":
                return ("orchestration_not_active", "the orchestration is not active")
            if orch.state not in ("published", "deprecated"):
                return ("version_not_published", "only published orchestrations can run")
            return None

        gate.check(
            self.ctx,
            "orchestration:execute",
            ref,
            label=f"orchestration:{orch.artifact_key}",
            resource_state=resource_state,
        )

    def _topological(self, branch_ids: set[str]) -> list[str]:
        ids = [n for n in self.nodes if n not in branch_ids]
        indeg = {i: 0 for i in ids}
        for e in self.edges:
            if e["to"] in indeg and e["from"] in indeg:
                indeg[e["to"]] += 1
        queue = [i for i in ids if indeg[i] == 0]
        out: list[str] = []
        while queue:
            cur = queue.pop(0)
            out.append(cur)
            for e in self.edges:
                if e["from"] == cur and e["to"] in indeg:
                    indeg[e["to"]] -= 1
                    if indeg[e["to"]] == 0:
                        queue.append(e["to"])
        return out

    # ------------------------------------------------------------------ journal
    def _journal(self) -> dict[str, dict[str, Any]]:
        """node id -> {"status": succeeded|skipped, "output": value} for resolved nodes."""
        out: dict[str, dict[str, Any]] = {}
        ctx = self.ctx
        for s in repo.list_steps(ctx.session, ctx.tenant_id, ctx.run_id):
            if s["kind"] != "orchestration_node" or s["status"] not in ("succeeded", "skipped"):
                continue
            content = repo.get_artifact(ctx.session, ctx.tenant_id, s["output_ref"]) or {}
            out[s["node_id"]] = {"status": s["status"], "output": content.get("output")}
        return out

    def _scope(self, journal: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        return {
            "input": self.input_value,
            "nodes": {
                nid: {"output": j["output"]}
                for nid, j in journal.items()
                if j["status"] == "succeeded"
            },
        }

    def _ready(self, nid: str, journal: Mapping[str, Mapping[str, Any]]) -> str:
        incoming = [e for e in self.edges if e["to"] == nid]
        if not incoming:
            return "run"
        if any(e["from"] not in journal for e in incoming):
            return "wait"
        for e in incoming:
            src = journal[e["from"]]
            if src["status"] != "succeeded":
                continue
            if e["when"] == "always":
                return "run"
            result = (
                bool((src["output"] or {}).get("result"))
                if isinstance(src["output"], dict)
                else False
            )
            if (e["when"] == "true") == result:
                return "run"
        return "skip"

    def _record_node(self, nid: str, status: str, output: Any, **extra: Any) -> None:  # Any: JSON
        ctx = self.ctx
        with ctx.txn():
            seq = repo.next_seq(ctx.session, ctx.tenant_id, ctx.run_id)
            art = (
                repo.insert_artifact(
                    ctx.session,
                    ctx.tenant_id,
                    ctx.run_id,
                    "intermediate",
                    scrub({"output": output}),
                )
                if status == "succeeded"
                else None
            )
            existing = [
                s
                for s in repo.list_steps(ctx.session, ctx.tenant_id, ctx.run_id)
                if s["kind"] == "orchestration_node"
                and s["node_id"] == nid
                and s["status"] == "running"
            ]
            if existing:
                row = repo.update_step(
                    ctx.session,
                    ctx.tenant_id,
                    existing[-1]["id"],
                    status=status,
                    output_ref=art,
                    finished_at=utcnow(),
                )
            else:
                row = repo.insert_step(
                    ctx.session,
                    {
                        "tenant_id": ctx.tenant_id,
                        "run_id": ctx.run_id,
                        "seq": seq,
                        "attempt": 1,
                        "kind": "orchestration_node",
                        "name": f"{self.nodes[nid]['type']}:{nid}",
                        "node_id": nid,
                        "status": status,
                        **extra,
                    },
                )
                if art is not None or status == "skipped":
                    row = repo.update_step(
                        ctx.session,
                        ctx.tenant_id,
                        row["id"],
                        status=status,
                        output_ref=art,
                        finished_at=utcnow(),
                    )
            steps._step_event(ctx, "step_finished", row, {"status": status, "node": nid})

    # ------------------------------------------------------------------ caps
    def _check_caps(self, journal: Mapping[str, Mapping[str, Any]]) -> None:
        executed = sum(1 for j in journal.values() if j["status"] == "succeeded")
        if executed >= int(self.limits["max_nodes_executed"]):
            raise state.RunFailed(
                state.BUDGET_DENIED,
                "orchestration limit max_nodes_executed "
                f"({self.limits['max_nodes_executed']}) reached",
            )
        root = self.ctx.run["root_run_id"]
        family = repo.run_totals(self.ctx.session, self.ctx.tenant_id, [root]).get(root)
        if family is not None:
            used = int(family["input_tokens"]) + int(family["output_tokens"])
            if used >= int(self.limits["max_total_tokens"]):
                raise state.RunFailed(
                    state.BUDGET_DENIED,
                    "orchestration limit max_total_tokens "
                    f"({self.limits['max_total_tokens']}) reached",
                )

    # ------------------------------------------------------------------ nodes
    def _execute(self, nid: str, journal: Mapping[str, Mapping[str, Any]]) -> None:
        node = self.nodes[nid]
        scope = self._scope(journal)
        try:
            kind = node["type"]
            if kind == "condition":
                result = safeexpr.evaluate_condition(node["condition"], scope)
                self._record_node(nid, "succeeded", {"result": result})
            elif kind == "tool":
                args = render(node.get("input_map") or {}, scope)
                tool = self.tool_refs.get(nid)
                if tool is None:
                    raise state.RunFailed(
                        state.VALIDATION, f"node {nid}: the tool is not available"
                    )
                if not isinstance(args, dict):
                    raise state.RunFailed(state.VALIDATION, f"node {nid}: invalid tool arguments")
                output = steps.run_tool(
                    self.ctx,
                    steps.ToolCall(tool=tool, args=args, call_ref=f"node:{nid}", node_id=nid),
                    self.policy,
                )
                self._record_node(nid, "succeeded", output)
            elif kind == "approval":
                self._approval_node(nid, render(node.get("input_map") or {}, scope))
            elif kind == "agent":
                self._agent_nodes([nid], scope)
            else:
                self._agent_nodes(list(node["branches"]), scope, parallel_id=nid)
        except safeexpr.ExprError as exc:
            raise state.RunFailed(state.VALIDATION, f"node {nid}: {exc}") from exc

    def _approval_node(self, nid: str, context: Any) -> None:  # Any: rendered input map
        ctx = self.ctx
        call_ref = f"node:{nid}"
        appr = repo.find_approval(
            ctx.session, ctx.tenant_id, ctx.run_id, call_ref, "orchestration_node", 0
        )
        if appr is None:
            with ctx.txn():
                seq = repo.next_seq(ctx.session, ctx.tenant_id, ctx.run_id)
                step = repo.insert_step(
                    ctx.session,
                    {
                        "tenant_id": ctx.tenant_id,
                        "run_id": ctx.run_id,
                        "seq": seq,
                        "attempt": 1,
                        "kind": "approval",
                        "name": f"approval:{nid}",
                        "node_id": nid,
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
                        "call_ref": call_ref,
                        "epoch": 0,
                        "reason": "orchestration_node",
                        "arguments": scrub(
                            context if isinstance(context, dict) else {"value": context}
                        ),
                        "context_summary": {"node": nid},
                        "requested_by": ctx.run["initiated_by_principal_id"],
                        "expires_at": utcnow()
                        + timedelta(hours=ctx.settings.approval_expiry_hours),
                    },
                )
                repo.add_event(
                    ctx.session,
                    ctx.run,
                    "approval_requested",
                    step_id=step["id"],
                    details={"approval_id": str(approval["id"]), "reason": "orchestration_node"},
                )
                ctx.audit(
                    "approval.requested",
                    {
                        "approval_id": str(approval["id"]),
                        "reason": "orchestration_node",
                        "node": nid,
                    },
                    commit=False,
                )
            raise state.Waiting("approval")
        if appr["status"] == "pending":
            raise state.Waiting("approval")
        if appr["status"] != "approved":
            raise state.RunFailed(state.APPROVAL_REJECTED, f"approval node {nid} was not granted")
        for s in repo.list_steps(ctx.session, ctx.tenant_id, ctx.run_id):
            if s["kind"] == "approval" and s["node_id"] == nid and s["status"] == "running":
                with ctx.txn():
                    repo.update_step(
                        ctx.session,
                        ctx.tenant_id,
                        s["id"],
                        status="succeeded",
                        finished_at=utcnow(),
                    )
        self._record_node(nid, "succeeded", {"approved": True, "comment": appr["decision_note"]})

    def _agent_nodes(
        self, node_ids: list[str], scope: dict[str, Any], parallel_id: str | None = None
    ) -> None:
        """Start (or collect) the child runs of agent nodes; raises Waiting while any is open."""
        ctx = self.ctx
        steps_by_node = {
            s["node_id"]: s
            for s in repo.list_steps(ctx.session, ctx.tenant_id, ctx.run_id)
            if s["kind"] == "orchestration_node" and s["status"] == "running"
        }
        to_start = [n for n in node_ids if n not in steps_by_node]
        if to_start:
            self._start_children(to_start, scope)  # raises Waiting(transitioned)
        outputs: dict[str, Any] = {}
        for nid in node_ids:
            step = steps_by_node[nid]
            child = repo.get_run(ctx.session, ctx.tenant_id, step["child_run_id"])
            # only this run's own child (the client id above, the parent here)
            if child is None or child["parent_run_id"] != ctx.run_id:
                raise state.RunFailed(state.INTERNAL, f"node {nid}: its child run is missing")
            if child["status"] not in state.TERMINAL:
                raise state.Waiting("child_runs")
            if child["status"] != state.COMPLETED:
                raise state.RunFailed(
                    str(child["error_class"] or state.INTERNAL),
                    f"node {nid} failed ({child['status']}): {child['error_message'] or ''}"[:500],
                )
            out = repo.get_artifact(ctx.session, ctx.tenant_id, child["output_ref"]) or {}
            outputs[nid] = out.get("output")
        for nid in node_ids:
            self._record_node(nid, "succeeded", outputs[nid])
        if parallel_id is not None:
            self._record_node(parallel_id, "succeeded", outputs)

    def _start_children(self, node_ids: list[str], scope: dict[str, Any]) -> None:
        ctx = self.ctx
        run = ctx.run
        if int(run["depth"]) + 1 > int(self.limits["max_depth"]):
            raise state.RunFailed(
                state.BUDGET_DENIED,
                f"orchestration limit max_depth ({self.limits['max_depth']}) reached",
            )
        prepared: list[tuple[str, VersionRef, UUID, dict[str, Any]]] = []
        for nid in node_ids:
            agent = self.agent_refs.get(nid)
            prompt = self._pinned_prompt(agent) if agent is not None else None
            if agent is None or prompt is None:
                raise state.RunFailed(state.VALIDATION, f"node {nid}: the agent is not available")
            gate.check(
                ctx,
                "agent:execute",
                ResourceRef(
                    ctx.tenant_id, agent.workspace_id, agent.project_id, "agent", agent.artifact_id
                ),
                label=f"agent:{agent.artifact_key}",
            )
            mapped = render(self.nodes[nid].get("input_map") or {}, scope)
            text = mapped.get("input") if isinstance(mapped, dict) else None
            variables = mapped.get("variables") if isinstance(mapped, dict) else None
            if text is None:
                raise state.RunFailed(state.VALIDATION, f"node {nid}: `input` resolved to nothing")
            payload = {
                "input": text if isinstance(text, str) else json.dumps(text, default=str),
                "variables": variables if isinstance(variables, dict) else {},
            }
            prepared.append((nid, agent, prompt, payload))
        with ctx.txn():
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
                seq = repo.next_seq(ctx.session, ctx.tenant_id, ctx.run_id)
                step = repo.insert_step(
                    ctx.session,
                    {
                        "tenant_id": ctx.tenant_id,
                        "run_id": ctx.run_id,
                        "seq": seq,
                        "attempt": 1,
                        "kind": "orchestration_node",
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

    def _pinned_prompt(self, agent: VersionRef) -> UUID | None:
        """The agent version's prompt when the run's pin set holds it (a prompt version of this
        client that the orchestration's agent pins); None otherwise, like an unknown agent."""
        try:
            prompt_id = UUID(str(agent.spec.get("prompt_version_id")))
        except ValueError:
            return None
        found = self.env.version(prompt_id)
        return found.version_id if found is not None and found.kind == "prompts" else None

    # ------------------------------------------------------------------ result
    def _result(
        self, journal: Mapping[str, Mapping[str, Any]], order: list[str]
    ) -> Any:  # Any: JSON
        has_out = {e["from"] for e in self.edges}
        sinks = [n for n in order if n not in has_out and journal[n]["status"] == "succeeded"]
        if len(sinks) == 1:
            return journal[sinks[0]]["output"]
        return {n: journal[n]["output"] for n in sinks}
