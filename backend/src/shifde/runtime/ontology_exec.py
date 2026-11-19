"""Ontology access from inside runs: the `ontology_query` pipeline node and the `ontology.query`
builtin tool (P7 contract section 5, FR-AUTHZ-009).

Both reach the ontology only through `OntologyService.query` with the run's own principal
(re-derived at every gate), after an Execution Gate check of `ontology:query` on the ontology
resource. The service then applies the ontology's default-deny policies, property denial and
row filters, so a pipeline or an agent can never read what its initiator could not read through
the API. Results are DATA: agent tool results are wrapped in `<untrusted>` blocks whose source is
`ontology:<object type>`, and pipeline data reaches models only through the pipeline untrusted
envelope.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any  # Any: tool arguments / query documents
from uuid import UUID

from pydantic import ValidationError

from shifde.authz.service import ResourceRef
from shifde.build.executors import ONTOLOGY_QUERY
from shifde.core.errors import ValidationFailed
from shifde.ontology import schemas as ont
from shifde.ontology.service import OntologyService
from shifde.runtime import gate
from shifde.runtime.context import RunContext


def is_ontology_tool(spec: Mapping[str, Any]) -> bool:  # Any: tool spec
    binding = spec.get("binding") or {}
    return spec.get("executor") == "builtin.function" and binding.get("function_key") == (
        ONTOLOGY_QUERY
    )


def provenance(spec: Mapping[str, Any], output: Any) -> str | None:  # Any: tool output
    """The `<untrusted source=...>` label of an ontology tool result."""
    if is_ontology_tool(spec) and isinstance(output, dict) and "object_type" in output:
        return f"ontology:{str(output['object_type'])[:200]}"
    return None


def _uuid(value: Any, what: str) -> UUID:  # Any: JSON value
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise ValidationFailed(f"{what} must be a UUID") from exc


def gate_query(ctx: RunContext, ontology_id: UUID, version_id: UUID | None, label: str) -> None:
    """Execution Gate for one ontology query (identity, current `ontology:query` grant on the
    ontology, ontology active, version published)."""
    art = ctx.artifacts.artifact_row("ontologies", ontology_id)
    ref = ResourceRef(ctx.tenant_id, art["workspace_id"], art["project_id"], "ontology", art["id"])

    def resource_state() -> gate.Problem | None:
        if art["status"] != "active":
            return ("ontology_not_active", f"the ontology is {art['status']}")
        vid = version_id or art["latest_published_version_id"]
        if vid is None:
            return ("version_not_published", "the ontology has no published version")
        version = ctx.artifacts.version_of(art, vid)
        if version["state"] not in ("published", "deprecated"):
            return ("version_not_published", "only a published ontology version can be queried")
        return None

    def visibility() -> gate.Problem | None:
        # the model chooses `ontology_id` at runtime: an ontology the principal cannot see through
        # the API (e.g. a private one) must not be readable through a tool either
        if not ctx.artifacts.visible(art):
            return ("ontology_not_visible", "the ontology is not available to this principal")
        return None

    gate.check(
        ctx,
        "ontology:query",
        ref,
        label=f"ontology:{art['key']}:{label}"[:200],
        resource_state=resource_state,
        policy=visibility,
    )


def query_in(query: Mapping[str, Any], version_id: UUID | None) -> ont.QueryIn:  # Any: JSON
    try:
        return ont.QueryIn.model_validate(
            {**dict(query), **({"version_id": str(version_id)} if version_id else {})}
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ValidationFailed(
            f"invalid ontology query: {'.'.join(map(str, first['loc']))}: {first['msg']}"
        ) from exc


def run_query(
    ctx: RunContext,
    ontology_id: UUID,
    version_id: UUID | None,
    query: Mapping[str, Any],  # Any: JSON query
    *,
    channel: str,
    purpose: str,
    consts: Mapping[str, Any] | None = None,  # Any: pipeline params
) -> ont.QueryOut:
    body = query_in(query, version_id)
    service = OntologyService(ctx.artifacts)
    out = service.query(
        ontology_id,
        body,
        channel=channel,
        run_id=ctx.run_id,
        api=False,  # the Execution Gate decided visibility and permission
        consts=consts,
        purpose=purpose,
    )
    ctx.record_classification(service.read_classification)  # ARCH M11: before any model sees it
    return out


# ---------------------------------------------------------------------------- agent tool
def tool_gate(ctx: RunContext, args: Mapping[str, Any]) -> None:  # Any: tool arguments
    version = _uuid(args["version_id"], "version_id") if args.get("version_id") else None
    gate_query(ctx, _uuid(args["ontology_id"], "ontology_id"), version, "tool")


def tool_query(ctx: RunContext, args: Mapping[str, Any], purpose: str) -> dict[str, Any]:  # Any
    version = _uuid(args["version_id"], "version_id") if args.get("version_id") else None
    out = run_query(
        ctx,
        _uuid(args["ontology_id"], "ontology_id"),
        version,
        args["query"],
        channel="agent",
        purpose=purpose,
    )
    return {
        "object_type": str(args["query"]["object_type"]),
        "rows": out.rows,
        "row_count": len(out.rows),
        "lineage_id": str(out.lineage_id),
        **({"truncated": True} if out.truncated or out.next_cursor else {}),
    }
