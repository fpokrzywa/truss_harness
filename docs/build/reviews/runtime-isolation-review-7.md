# runtime isolation review 7 (RT-8 `graph.py`; `git diff 3199791 be6061a`)

Reviewer: `isolation-reviewer` (opus, fresh), 2026-11-15. Read-only; saved by the lead.

**Verdict: APPROVED.** No way for A's orchestration run to reach B's runs, versions, prompts, steps, artifacts, approvals or jobs. `tests/runtime -n 8` at be6061a in verify3: 368 passed (check-removal proofs not re-run).

Checked: (1) every repository call in `runtime/graph.py` passes `ctx.tenant_id`; `ctx.env(art)` -> `_run_env` re-reads the run with the client id, ties the stored artifact to the stored run's version, limits answers to `pin_closure`; B's or an unknown run gets "pinned orchestration not available"; `graph.py` never wakes, cancels or settles another run; `transition`/`fence` touch only its own run with its own lease token. (2) Child runs: every column from the parent; workspace, project, parent, root keys and `fk_run_steps__child_run` include `tenant_id`. (3) Node versions only from `only_versions`; A cannot pin B's version (`fk_pins__pinned`); kind checked; prompt must be a pinned `prompts` version. (4) Defence in depth for the two unproven removals: agent kind via `_pinned_prompt` then AgentRunner's `_run_env` (stored kind `agents`); B's ids via pin membership, client-keyed `get_version`, the pins key and RLS. (6) Tests non-vacuous (both modes; B's state compared before/after; scope-swap meaningful). (7) No oracle.

## Findings

1. **Low, inside one client.** `graph.py:91` runs the version from in-memory `ctx.run["orchestration_version_id"]`, not the stored run `_run_env` re-read; child scope (`graph.py:497`+) also from `ctx.run`. Fix: `self.env.run_version_id` (or require equality) and `get_run(tenant, run_id)` before `create_run`; RT-12: `RunContext.run` is the claimed row re-read with the client id.
2. **Low, test gap.** No test fails without the agent kind check at `graph.py:121`. Fix: a pinned non-agent version with a pinned `prompt_version_id` used as an agent node: refused, no child.
3. **Info.** Builder's claim (5) inaccurate but nothing leaks: `ontology/service.py:370` `_run_reads` opens the agent-tool rule only for `kind == "agent"`, so a graph tool node's `ontology.query` is refused unless the pin closure holds that ontology version; `ontology_id` from `input_map` is bounded by `gate_query`. Fix (RT-9): record and test fail-closed, or allow orchestration runs explicitly with the connection narrowing.

## Follow-ups

- RT-10: wake, settle, cancel of graph children; inherited `depth`/`deadline_at`; no second transition after `Waiting(transitioned=True)`.
- RT-12: `RunContext` built only from the claimed row.
- RT-9: ontology connection narrowing and finding 3.
- Item 16: `ToolPolicy(agent_id=None)`: `allowed_agent_ids` not enforced for graph tool nodes (known defect, inside one client).

Findings 1-3 handed to the RT-9 builder.
