"""Per-kind operations on top of the shared artifact lifecycle: prompt render/test, tool test, agent
assembly test / status / resolved view (contract sections 2, 3 and 5)."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any  # Any: specs / variable values / tool arguments
from uuid import UUID

from shifde.build import repository, tool_runtime
from shifde.build import schemas as s
from shifde.build.executors import WRITE_CLASSES
from shifde.build.issues import Report, schema_errors
from shifde.build.prompts import (
    CompositionError,
    PromptNode,
    build_node,
    escape_untrusted,
    evaluate,
    merged_variables,
    render_prompt,
    resolve_variables,
    untrusted_in_system,
    validate_prompt,
)
from shifde.build.service import SINGULAR, ArtifactService, issues_as_errors
from shifde.build.skills import dependency_closure
from shifde.core.clock import utcnow
from shifde.core.errors import (
    AppError,
    Forbidden,
    InvalidTransition,
    NotFound,
    SideEffectsNotAcknowledged,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.core.redaction import scrub
from shifde.models_admin import schemas as ms

INPUT_REF = re.compile(r"\{\{\s*input((?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}")


def _uuid_or_422(value: str, what: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationFailed(f"{what} must be a version id or 'draft'") from exc


def _json_safe(value: Any) -> Any:  # Any: tool result
    return json.loads(json.dumps(scrub(value), default=str))


def load_version(svc: ArtifactService, art: Mapping[str, Any], selector: str) -> Mapping[str, Any]:
    """`version_id` of a request body: a version id of this artifact, or the saved draft."""
    if selector == "draft":
        draft = repository.get_draft(svc.session, svc.sc.tenant_id, art["id"])
        if draft is None:
            raise NotFound("this artifact has no draft")
        return draft
    return svc.version_of(art, _uuid_or_422(selector, "version_id"))


# ====================================================================== prompts
class PromptOps:
    def __init__(self, svc: ArtifactService) -> None:
        self.svc = svc

    def _render(
        self, art: Mapping[str, Any], ver: Mapping[str, Any], variables: dict[str, Any]
    ) -> tuple[list[dict[str, str]], list[str], dict[str, Any], dict[str, Any], PromptNode]:
        spec = dict(ver["spec"])
        env = self.svc.env(art)
        if ver["state"] == "draft":  # only a saved draft can be malformed
            report = Report()
            validate_prompt(spec, env, report)
            if report.errors:
                raise ValidationFailed(
                    "the draft prompt is invalid", errors=issues_as_errors(report.errors)
                )
        try:
            node = build_node(spec, env)
        except CompositionError as exc:
            raise ValidationFailed(str(exc)) from exc
        offenders = untrusted_in_system(node)
        if offenders:
            raise ValidationFailed(
                "untrusted variables cannot be rendered into the system message",
                errors=[
                    {
                        "loc": ["spec", "sections", "system"],
                        "msg": f"variable {name!r} is untrusted",
                        "type": "untrusted_in_system",
                        "path": "sections.system",
                        "code": "untrusted_in_system",
                        "message": f"variable {name!r} is untrusted",
                    }
                    for name in offenders
                ],
            )
        declared = merged_variables(node)
        values, problems = resolve_variables(declared, variables)
        if problems:
            raise ValidationFailed(
                "the variables are invalid",
                errors=[
                    {
                        "loc": ["variables", p.name],
                        "msg": f"{p.name} {p.message}",
                        "type": p.code,
                        "path": f"variables.{p.name}",
                        "code": p.code,
                        "message": f"{p.name} {p.message}",
                    }
                    for p in problems
                ],
            )
        messages, warnings = render_prompt(node, values, declared)
        return messages, warnings, values, spec, node

    def render(self, artifact_id: UUID, body: s.RenderIn) -> s.RenderOut:
        art = self.svc.artifact("prompts", artifact_id)
        ver = load_version(self.svc, art, body.version_id)
        messages, warnings, *_ = self._render(art, ver, body.variables)
        return s.RenderOut(messages=[s.RenderedMessage(**m) for m in messages], warnings=warnings)

    def test(self, artifact_id: UUID, body: s.PromptTestIn) -> s.PromptTestRunOut:
        svc = self.svc
        art = svc.artifact("prompts", artifact_id, "test")
        ver = load_version(svc, art, body.version_id)
        messages, _warnings, values, spec, _ = self._render(art, ver, body.variables)
        model_id = body.model_id
        if model_id is None and isinstance(spec.get("default_model_id"), str):
            try:
                model_id = UUID(spec["default_model_id"])
            except ValueError:
                model_id = None
        if model_id is None:
            raise ValidationFailed("a model is required: pass model_id or set default_model_id")
        invoked = svc.models.invoke_at(
            svc.scope_ref_of(art),
            model_id,
            ms.InvokeIn(messages=[ms.ChatMessageIn(**m) for m in messages]),
            usage_event_type="prompt_test",
            is_test=True,
        )
        criteria = spec.get("evaluation_criteria")
        results = evaluate(criteria if isinstance(criteria, list) else [], invoked.output)
        verdict = "pass" if all(r.passed for r in results) else "fail"
        row = repository.insert_run(
            svc.session,
            {
                "id": uuid7(),
                "tenant_id": svc.sc.tenant_id,
                "artifact_id": art["id"],
                "version_id": ver["id"],
                "model_id": model_id,
                "inputs": scrub(values),
                "output": invoked.output,
                "usage": {
                    "input_tokens": invoked.usage.input_tokens,
                    "output_tokens": invoked.usage.output_tokens,
                },
                "latency_ms": invoked.latency_ms,
                "evaluation": [
                    {"name": r.name, "passed": r.passed, "detail": r.detail} for r in results
                ],
                "verdict": verdict,
                "created_by": svc.sc.principal.principal_id,
            },
        )
        svc._audit(
            "prompt.tested",
            art,
            {
                "version_no": ver["version_no"],
                "state": ver["state"],
                "model_id": str(model_id),
                "verdict": verdict,
                "criteria": len(results),
                "latency_ms": invoked.latency_ms,
            },
        )
        svc.session.commit()
        return self._run_out(row)

    @staticmethod
    def _run_out(row: Mapping[str, Any]) -> s.PromptTestRunOut:
        return s.PromptTestRunOut(
            id=row["id"],
            output=row["output"],
            usage=s.UsageOut(**row["usage"]),
            latency_ms=row["latency_ms"],
            evaluation=[s.CriterionOut(**e) for e in row["evaluation"]],
            verdict=row["verdict"],
            created_at=row["created_at"],
            version_id=row["version_id"],
            model_id=row["model_id"],
        )

    def test_runs(
        self, artifact_id: UUID, limit: int, cursor: str | None
    ) -> Page[s.PromptTestRunOut]:
        art = self.svc.artifact("prompts", artifact_id)
        rows = repository.list_runs(
            self.svc.session, self.svc.sc.tenant_id, art["id"], decode_cursor(cursor), limit + 1
        )
        page, more = paginate(rows, limit)
        return Page(
            items=[self._run_out(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )


# ====================================================================== tools
class ToolOps:
    def __init__(self, svc: ArtifactService) -> None:
        self.svc = svc

    def test(self, artifact_id: UUID, body: s.ToolTestIn) -> s.ToolTestOut:
        svc = self.svc
        art = svc.artifact("tools", artifact_id, "test")
        ver = load_version(svc, art, body.version_id)
        spec = dict(ver["spec"])
        if ver["state"] == "draft":
            prepared = svc.prepare(art, spec)
            if prepared.report.errors:
                raise ValidationFailed(
                    "the draft tool is invalid", errors=issues_as_errors(prepared.report.errors)
                )
            spec = prepared.spec
        side = spec["side_effect_class"]
        if side in WRITE_CLASSES:
            svc.sc.pdp.require("tool:test_side_effects", svc.ref_of(art))
            if not body.acknowledge_side_effects:
                raise SideEffectsNotAcknowledged(
                    f"this tool is {side}: send acknowledge_side_effects=true to run it"
                )
        problems = schema_errors(spec["input_schema"], body.args)
        snapshot = (
            spec["binding"].get("schema_snapshot") if spec["executor"] == "mcp.tool" else None
        )
        if isinstance(snapshot, dict) and isinstance(snapshot.get("input_schema"), dict):
            problems += schema_errors(snapshot["input_schema"], body.args)
        if problems:
            raise ValidationFailed(
                "the arguments do not match the input schema",
                errors=[
                    {"loc": ["args", *path], "msg": message, "type": "schema"}
                    for path, message in problems
                ],
            )
        key = uuid7().hex  # fresh per test call; reused only across retries inside execute()
        started = time.monotonic()
        try:
            result = tool_runtime.execute(
                svc.conns,
                spec,
                body.args,
                purpose=f"tool:{art['key']}:test",
                idempotency_key=key,
            )
        except AppError as exc:
            self._audit(art, ver, spec, body, started, error=type(exc).__name__)
            svc.session.commit()
            raise
        output = _json_safe(result.output)
        valid_output = not schema_errors(spec["output_schema"], output)
        self._audit(art, ver, spec, body, started, attempts=result.attempts, output=output)
        svc.session.commit()
        return s.ToolTestOut(
            output=output,
            valid_output=valid_output,
            duration_ms=int((time.monotonic() - started) * 1000),
            attempts=result.attempts,
        )

    def _audit(
        self,
        art: Mapping[str, Any],
        ver: Mapping[str, Any],
        spec: Mapping[str, Any],
        body: s.ToolTestIn,
        started: float,
        *,
        attempts: int | None = None,
        output: Any = None,  # Any: tool result
        error: str | None = None,
    ) -> None:
        details: dict[str, object] = {
            "version_no": ver["version_no"],
            "state": ver["state"],
            "executor": spec["executor"],
            "side_effect_class": spec["side_effect_class"],
            "arg_keys": sorted(body.args),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if attempts is not None:
            details["attempts"] = attempts
        if error:
            details["error_class"] = error
        if spec.get("audit_level") == "full":
            details["args"] = scrub(body.args)
            if output is not None:
                details["output"] = scrub(output)
        self.svc._audit("tool.tested", art, details, outcome="failure" if error else "success")


# ====================================================================== agents
class AgentOps:
    def __init__(self, svc: ArtifactService) -> None:
        self.svc = svc

    def set_status(self, artifact_id: UUID, body: s.AgentStatusIn) -> s.ArtifactOut:
        svc = self.svc
        svc._active_tenant()
        art = svc.artifact("agents", artifact_id, "activate")
        if art["status"] == "retired":
            raise InvalidTransition("a retired agent is terminal")
        if art["status"] != body.status:
            updated = repository.update_artifact(
                svc.session, svc.sc.tenant_id, art["id"], None, {"status": body.status}
            )
            assert updated is not None
            svc._audit("agent.status_changed", updated, {"from": art["status"], "to": body.status})
            svc.session.commit()
            return svc.artifact_out(updated)
        return svc.artifact_out(art)

    # ---------------------------------------------------------------- resolved view
    def resolved(self, artifact_id: UUID, version_id: UUID) -> s.ResolvedOut:
        svc = self.svc
        art = svc.artifact("agents", artifact_id)
        ver = svc.version_of(art, version_id)
        if ver["state"] == "draft":
            raise ValidationFailed("only published versions have a resolved view")
        spec = ver["spec"]
        env = svc.env(art)

        def component(vid: str | None) -> dict[str, Any] | None:
            ref = env.version(UUID(vid)) if vid else None
            if ref is None:
                return None
            return {
                "id": str(ref.version_id),  # P5 D1.1: clients read prompt_version.id
                "version_id": str(ref.version_id),
                "version_no": ref.version_no,
                "state": ref.state,
                "spec_hash": ref.spec_hash,
                "artifact": {
                    "id": str(ref.artifact_id),
                    "key": ref.artifact_key,
                    "name": ref.artifact_name,
                },
                "spec": ref.spec,
            }

        prompt = component(spec.get("prompt_version_id"))
        composed: list[dict[str, Any]] = []
        if prompt is not None:
            for item in prompt["spec"].get("composed_of") or []:
                found = component(item.get("prompt_version_id"))
                if found is not None:
                    composed.append({**found, "placement": item.get("placement")})
            prompt["composed"] = composed
        tools = [c for c in (component(t) for t in spec.get("tool_version_ids") or []) if c]
        direct = [UUID(v) for v in spec.get("skill_version_ids") or []]
        skills = []
        for vid in dependency_closure(env, direct):
            comp = component(str(vid))
            if comp is not None:
                skills.append({**comp, "direct": vid in direct})
        return s.ResolvedOut(
            agent_version_id=ver["id"],
            version_no=ver["version_no"],
            spec_hash=ver["spec_hash"],
            spec=spec,
            prompt_version=prompt,
            tools=tools,
            skills=skills,
            models=self._models(spec.get("model")),
        )

    def _models(self, model: Any) -> list[dict[str, Any]]:  # Any: spec fragment
        if not isinstance(model, dict):
            return []
        ids: list[UUID] = []
        if "route_id" in model:
            ids = self.svc.models.route_model_ids(UUID(model["route_id"])) or []
        elif "model_id" in model:
            ids = [
                UUID(model["model_id"]),
                *[UUID(f) for f in model.get("fallback_model_ids") or []],
            ]
        out: list[dict[str, Any]] = []
        for index, mid in enumerate(ids):
            entry: dict[str, Any] = {
                "id": str(mid),
                "role": "primary" if index == 0 else "fallback",
            }
            try:
                found = self.svc.models.get_model(mid)
                entry.update(
                    model_key=found.model_key,
                    display_name=found.display_name,
                    provider_id=str(found.provider_id),
                    lifecycle=found.lifecycle,
                )
            except (Forbidden, NotFound):
                pass
            out.append(entry)
        if isinstance(model, dict) and "route_id" in model:
            out.insert(0, {"route_id": str(model["route_id"])})
        return out

    # ---------------------------------------------------------------- assembly test
    def test(self, artifact_id: UUID, body: s.AgentTestIn) -> s.AgentTestOut:
        svc = self.svc
        art = svc.artifact("agents", artifact_id, "test")
        ver = load_version(svc, art, body.version_id)
        spec = dict(ver["spec"])
        if ver["state"] == "draft":
            prepared = svc.prepare(art, spec)
            if prepared.report.errors:
                raise ValidationFailed(
                    "the draft agent is invalid", errors=issues_as_errors(prepared.report.errors)
                )
            spec = prepared.spec
        messages, tools = self.assemble(art, spec, body.input, body.variables)
        model_id = self._primary_model(spec)
        invoked = svc.models.invoke_at(
            svc.scope_ref_of(art),
            model_id,
            ms.InvokeIn(messages=[ms.ChatMessageIn(**m) for m in messages]),
            is_test=True,
            agent_id=artifact_id,
        )
        svc._audit(
            "agent.tested",
            art,
            {
                "version_no": ver["version_no"],
                "state": ver["state"],
                "model_id": str(model_id),
                "tools": len(tools),
                "latency_ms": invoked.latency_ms,
            },
        )
        svc.session.commit()
        return s.AgentTestOut(
            assembled=s.AssembledOut(
                messages=[s.RenderedMessage(**m) for m in messages], tools=tools
            ),
            output=invoked.output,
            usage=s.UsageOut(
                input_tokens=invoked.usage.input_tokens, output_tokens=invoked.usage.output_tokens
            ),
            latency_ms=invoked.latency_ms,
            model_id=model_id,
        )

    def assemble(
        self,
        art: Mapping[str, Any],
        spec: Mapping[str, Any],
        input_text: str,
        variables: Mapping[str, Any],  # Any: caller-supplied variable values
        *,
        run_of: Mapping[str, Any] | None = None,  # Any: the run row the runtime executes
    ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        """Context assembly shared by the test panel and the P5 runtime: rendered prompt, skills
        as instructional text, untrusted run input in a delimited block, tool declarations.

        The test panel reads through the caller's env (visibility and use checks); the runtime
        passes its run row (`run_of`, B-1933): the run's own env, which answers only for the
        run's pinned agent version and what it pins (build review 3 F1, B-2160)."""
        svc = self.svc
        env = svc.env(art, run_of=run_of)
        prompt = env.version(UUID(spec["prompt_version_id"]))
        if prompt is None:
            raise ValidationFailed("the pinned prompt version is not available to you")
        try:
            node = build_node(prompt.spec, env)
        except CompositionError as exc:
            raise ValidationFailed(str(exc)) from exc
        if untrusted_in_system(node):
            raise ValidationFailed("the prompt puts untrusted variables in the system message")
        declared = merged_variables(node)
        mapped = {
            name: self._coerce_mapped(declared.get(name), self._map_value(template, input_text))
            for name, template in (spec.get("variables_map") or {}).items()
        }
        values, problems = resolve_variables(declared, {**mapped, **variables})
        if problems:
            raise ValidationFailed(
                "the variables are invalid",
                errors=[
                    {"loc": ["variables", p.name], "msg": f"{p.name} {p.message}", "type": p.code}
                    for p in problems
                ],
            )
        messages, _ = render_prompt(node, values, declared)
        messages = self._add_skills(messages, env, spec)
        if input_text.strip():
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f'<untrusted source="input">{escape_untrusted(input_text)}</untrusted>'
                    ),
                }
            )
        tools = self._tool_declarations(env, spec)
        return messages, tools

    def _primary_model(self, spec: Mapping[str, Any]) -> UUID:
        model = spec["model"]
        if "route_id" in model:
            ids = self.svc.models.route_model_ids(UUID(model["route_id"]))
            if not ids:
                raise ValidationFailed("the model route is not available")
            return ids[0]
        return UUID(model["model_id"])

    @staticmethod
    def _map_value(template: str, input_text: str) -> str:
        parsed: Any = None  # Any: the run input as JSON when it is an object

        def repl(match: re.Match[str]) -> str:
            nonlocal parsed
            path = [p for p in match.group(1).split(".") if p]
            if not path:
                return input_text
            if parsed is None:
                try:
                    parsed = json.loads(input_text)
                except ValueError as exc:
                    raise ValidationFailed(
                        "the input must be a JSON object to use {{input.field}}"
                    ) from exc
            node = parsed
            for part in path:
                if not isinstance(node, dict) or part not in node:
                    raise ValidationFailed(f"the input has no field {'.'.join(path)!r}")
                node = node[part]
            return node if isinstance(node, str) else json.dumps(node, separators=(",", ":"))

        return INPUT_REF.sub(repl, template)

    @staticmethod
    def _coerce_mapped(decl: Mapping[str, Any] | None, text: str) -> Any:  # Any: typed value
        vtype = (decl or {}).get("type", "string")
        try:
            if vtype == "number":
                return float(text) if "." in text or "e" in text.lower() else int(text)
            if vtype == "boolean":
                return {"true": True, "false": False}.get(text.strip().lower(), text)
            if vtype == "json":
                return json.loads(text)
        except ValueError:
            return text
        return text

    @staticmethod
    def _add_skills(
        messages: list[dict[str, str]],
        env: Any,
        spec: Mapping[str, Any],  # Any: build.service._Env
    ) -> list[dict[str, str]]:
        ids = [UUID(v) for v in spec.get("skill_version_ids") or []]
        blocks = []
        for vid in dependency_closure(env, ids):
            ref = env.version(vid)
            if ref is not None:
                blocks.append(
                    f"## Skill: {ref.artifact_name}\n{ref.spec.get('instructions_md', '')}"
                )
        if not blocks:
            return messages
        skills_text = "# Skills\n" + "\n\n".join(blocks)
        if messages and messages[0]["role"] == "system":
            first = {"role": "system", "content": f"{messages[0]['content']}\n\n{skills_text}"}
            return [first, *messages[1:]]
        return [{"role": "system", "content": skills_text}, *messages]

    @staticmethod
    def _tool_declarations(env: Any, spec: Mapping[str, Any]) -> list[dict[str, Any]]:  # Any: _Env
        approval = set((spec.get("approval_rules") or {}).get("require_approval_for") or [])
        approval.add("destructive")
        out: list[dict[str, Any]] = []
        for vid in spec.get("tool_version_ids") or []:
            ref = env.version(UUID(vid))
            if ref is None:
                continue
            side = ref.spec.get("side_effect_class")
            out.append(
                {
                    "name": ref.artifact_key,
                    "description": ref.artifact_name,
                    "version_id": str(ref.version_id),
                    "input_schema": ref.spec.get("input_schema"),
                    "side_effect_class": side,
                    "requires_approval": side in approval,
                }
            )
        return out


__all__ = ["SINGULAR", "AgentOps", "PromptOps", "ToolOps", "utcnow"]
