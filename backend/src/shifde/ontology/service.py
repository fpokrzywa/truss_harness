"""The ontology service: query and traversal, object resolution, access policies, principal
attributes and lineage (P7 contract sections 3-5).

Authorization is decided on the server, in this order, and every step fails closed:

1. visibility (`ontology:read` / ownership) -> 404, so other tenants' and hidden ontologies do not
   exist; 2. `ontology:query` -> 403; 3. the version must be published (never a draft) and the
   ontology active; 4. the mappings frozen with the version must still match the hash recorded at
   publish; 5. default-deny policies per object type (no policy -> 404), property denial (explicit
   request of a denied property -> 403, nothing is silently dropped), row predicates compiled into
   the source query as bound parameters. Denials are audited.

The connection's own identity is the ceiling: SHIFDE never asks a source for more than its service
identity may read. Ontology access is authorized by the ontology (permission + policy), not by
`connection:use` of the caller - the author who mapped the connection held that permission at
publish.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
from collections.abc import Mapping
from contextlib import ExitStack
from decimal import Decimal
from typing import Any  # Any: rows / specs / attribute values
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from shifde import ledger
from shifde.adapters.base import AdapterFailure, ConnectionSession, SourceQueryPlan
from shifde.authz import service as authz
from shifde.build import ontologies, ontology_ops
from shifde.build import ontology_expr as expr
from shifde.build.service import ArtifactService
from shifde.connections.service import failure_to_error
from shifde.core.errors import (
    AppError,
    Conflict,
    Forbidden,
    NotFound,
    ValidationFailed,
)
from shifde.core.ids import uuid7
from shifde.core.pagination import Page, decode_cursor, encode_cursor, paginate
from shifde.identity import service as identity
from shifde.ontology import engine, repository
from shifde.ontology import policy as pol
from shifde.ontology import schemas as s

KIND = "ontologies"
ATTRIBUTE_MAX_BYTES = 8000
MAX_SUMMARIES = 24


def spec_type(spec: Mapping[str, Any], key: str) -> Mapping[str, Any]:  # Any: spec
    return next(t for t in spec["object_types"] if t["key"] == key)


class VersionNotPublished(Conflict):
    code = "version_not_published"


class OntologyNotActive(Conflict):
    code = "ontology_not_active"


class MaterializationNotEnabled(AppError):
    status, code, title = 501, "materialization_not_enabled", "Not Implemented"


def _fingerprint(version_id: UUID, body: s.QueryIn) -> str:
    doc = body.model_dump(mode="json", exclude={"cursor", "limit"})
    doc["version_id"] = str(version_id)
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]


def encode_query_cursor(offset: int, fingerprint: str) -> str:
    raw = json.dumps({"o": offset, "h": fingerprint}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_query_cursor(cursor: str | None, fingerprint: str) -> int:
    if not cursor:
        return 0
    try:
        doc = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        offset, digest = int(doc["o"]), str(doc["h"])
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise ValidationFailed("invalid cursor") from exc
    if digest != fingerprint or offset < 0 or offset > 1_000_000:
        raise ValidationFailed("the cursor does not belong to this query")
    return offset


class OntologyService:
    def __init__(self, artifacts: ArtifactService) -> None:
        self.arts = artifacts
        self.sc = artifacts.sc
        self.session = artifacts.session
        self.runtime = artifacts.runtime
        self.conns = artifacts.conns
        self._rows: dict[UUID, Mapping[str, Any]] = {}  # Any: connection rows

    # ================================================================== loading
    def _artifact(
        self, ontology_id: UUID, *, api: bool, run_id: UUID | None = None
    ) -> Mapping[str, Any]:  # Any: row
        self._anchor_run(run_id, api=api)
        if api:
            return self.arts.artifact(KIND, ontology_id)  # 404 unless visible
        return self.arts.artifact_row(KIND, ontology_id)

    def _version(self, art: Mapping[str, Any], version_id: UUID | None) -> Mapping[str, Any]:
        if art["status"] != "active":
            raise OntologyNotActive(f"the ontology is {art['status']}")
        vid = version_id or art["latest_published_version_id"]
        if vid is None:
            raise VersionNotPublished("the ontology has no published version")
        version = self.arts.version_of(art, vid)  # 404 for another artifact's version
        if version["state"] not in ("published", "deprecated"):
            raise VersionNotPublished("only a published ontology version can be queried")
        return version

    def resolve_version(
        self,
        art: Mapping[str, Any],
        version_id: UUID | None,  # Any: artifact row
    ) -> Mapping[str, Any]:
        """The published / deprecated version a request addresses (default: the latest)."""
        return self._version(art, version_id)

    def _frozen(
        self, version: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[tuple[str, str], Mapping[str, Any]]]:  # Any: spec / rows
        # only a version `_version` has checked (this client's, of the addressed artifact)
        if version["tenant_id"] != self.sc.tenant_id or version["kind"] != KIND:
            raise NotFound("version not found")
        rows, info = ontology_ops.frozen_mappings(self.arts, version["id"])
        if info is None or info["mappings_hash"] != ontologies.mappings_digest(rows):
            raise engine.MappingUnavailable(
                "the mappings of this version do not match what was published"
            )
        return dict(version["spec"]), {(r["target_kind"], r["target_key"]): r for r in rows}

    def _subjects(self, art: Mapping[str, Any], principal_attributes: bool = True) -> pol.Subjects:
        principal = self.sc.principal
        roles, groups = authz.subject_ids_covering(
            self.session,
            tenant_id=self.sc.tenant_id,
            principal=principal,
            resource=self.arts.ref_of(art),
        )
        row = repository.get_attributes(
            self.session, self.sc.tenant_id, principal.principal_type, principal.principal_id
        )
        attrs = dict(row["attributes"]) if row is not None and principal_attributes else {}
        return pol.Subjects(
            principal.principal_id,
            principal.principal_type,
            frozenset(roles),
            frozenset(groups),
            attrs,
        )

    def _conn_row(self, connection_id: UUID) -> Mapping[str, Any]:  # Any: row
        # `get_row` reads with the client id (NotFound otherwise); `open_trusted` re-checks it
        if connection_id not in self._rows:
            self._rows[connection_id] = self.conns.get_row(connection_id)
        return self._rows[connection_id]

    def _schema_discovery(self, mapping: Mapping[str, Any]) -> dict[str, Any] | None:  # Any: JSON
        """The latest schema discovery of the connection a frozen mapping names. The mapping is
        this client's (it came from `_frozen`, a version `_version` checked) and `discovery_of`
        reads with the client id; the ontology's policies, not the caller's `connection:read`,
        authorize this use (no env, no visibility flag: build isolation review 3 F1)."""
        if mapping["tenant_id"] != self.sc.tenant_id:
            return None
        found = self.conns.discovery_of(mapping["connection_id"])
        if found is None or found.kind != "schema" or not isinstance(found.payload, dict):
            return None
        return dict(found.payload)

    def _context(
        self,
        art: Mapping[str, Any],
        spec: dict[str, Any],
        mappings: dict[tuple[str, str], Mapping[str, Any]],
        consts: Mapping[str, Any] | None,
    ) -> engine.EngineContext:
        def capabilities(connection_id: UUID) -> frozenset[str]:
            row = self._conn_row(connection_id)
            desc = self.runtime.registry.descriptor(row["adapter_key"])
            return frozenset(str(c) for c in desc.capabilities) if desc else frozenset()

        def edge_operands(mapping: Mapping[str, Any]) -> dict[str, expr.Operand]:
            payload = self._schema_discovery(mapping)
            if payload is None:
                raise engine.MappingUnavailable("the connection has no schema discovery")
            schema, name = str(mapping["relation_schema"]), str(mapping["relation_name"])
            for table in payload.get("tables") or []:
                if table.get("name") == name and (not schema or table.get("schema") == schema):
                    return {
                        c["name"]: expr.Operand(c["name"], ptype, c["name"])
                        for c in table.get("columns") or []
                        if (ptype := expr.ptype_of_db_type(str(c.get("type", "")))) is not None
                    }
            raise engine.MappingUnavailable("the mapped relation is not in the schema discovery")

        return engine.EngineContext(
            spec=spec,
            mappings=mappings,
            policies=repository.list_policies(self.session, self.sc.tenant_id, art["id"]),
            subjects=self._subjects(art),
            capabilities=capabilities,
            edge_operands=edge_operands,
            consts=consts,
        )

    # ================================================================== source access
    def _source(self, stack: ExitStack, purpose: str) -> engine.Source:
        def open_session(connection_id: UUID, remaining: float) -> ConnectionSession:
            row = self._conn_row(connection_id)
            return stack.enter_context(self.conns.open_trusted(row, purpose, deadline=remaining))

        def compile_for(connection_id: UUID, plan: SourceQueryPlan) -> tuple[str, list[Any]]:
            row = self._conn_row(connection_id)
            adapter = self.runtime.registry.get(row["adapter_key"])
            if adapter is None or not callable(getattr(adapter, "compile", None)):
                raise engine.MappingUnavailable("the source adapter cannot compile queries")
            try:
                compiled = adapter.compile(plan)
            except AdapterFailure as exc:
                raise failure_to_error(exc) from exc
            return str(compiled.sql), list(compiled.params)

        return engine.Source(open_session, compile_for)

    # ================================================================== audit / lineage
    def _denied(
        self,
        art: Mapping[str, Any],
        reason: str,
        details: Mapping[str, Any],  # Any: audit details
    ) -> None:
        self.arts._audit(
            "ontology.query_denied",
            art,
            {"reason": reason, **dict(details)},
            outcome="denied",
            target_type="ontology",
        )
        self.session.commit()

    def _lineage(
        self,
        art: Mapping[str, Any],
        version: Mapping[str, Any],
        source: engine.Source,
        counts: Mapping[str, int],
        started: float,
        *,
        channel: str,
        run_id: UUID | None,
        outcome: str,
        error_code: str | None,
        object_types: list[str],
    ) -> UUID:
        lineage_id = uuid7()
        principal = self.sc.principal
        repository.insert_lineage(
            self.session,
            self.sc.tenant_id,
            {
                "id": lineage_id,
                "principal_id": principal.principal_id,
                "principal_type": principal.principal_type,
                "ontology_id": art["id"],
                "ontology_version_id": version["id"],
                "run_id": run_id,
                "channel": channel,
                "object_types": object_types,
                "connection_ids": list(source.connections),
                "compiled_query_hashes": list(source.hashes),
                "row_counts": dict(counts),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "outcome": outcome,
                "error_code": error_code,
            },
        )
        self._usage(art, lineage_id, run_id, outcome, int((time.monotonic() - started) * 1000))
        self.arts._audit(
            "ontology.queried",
            art,
            {
                "lineage_id": str(lineage_id),
                "version_no": version["version_no"],
                "object_types": object_types,
                "channel": channel,
                "row_counts": dict(counts),
                "statements": len(source.hashes),
                "outcome": outcome,
            },
            outcome="success" if outcome == "success" else "failure",
            target_type="ontology",
        )
        self.session.commit()
        return lineage_id

    def _has_runs_table(self) -> bool:
        """`runs` belongs to the runtime module (not ported yet, B-2007): until its migration
        exists no run does either. Remove with the guard in `governance` (B-1812)."""
        return bool(
            self.session.execute(text("SELECT to_regclass('shifde.runs') IS NOT NULL")).scalar_one()
        )

    def _anchor_run(self, run_id: UUID | None, *, api: bool) -> None:
        """A run id a caller names must be a run of this client (it ends up in the lineage row
        and the usage event; ontology review 1 F4). The unchecked path (`api=False`: the
        Execution Gate authorized the caller before the runtime called in) exists only for a run
        of this client, so it is anchored to one the way build's `approval_of` is anchored to a
        request: no run row of the scope's client, no unchecked call (B-1933, B-2008)."""
        if run_id is None:
            if api:
                return
            raise NotFound("run not found")
        if not self._has_runs_table():
            raise NotFound("run not found")
        found = self.session.execute(
            text("SELECT 1 FROM shifde.runs WHERE id = :r AND tenant_id = :t"),
            {"r": run_id, "t": self.sc.tenant_id},
        ).first()
        if found is None:
            raise NotFound("run not found")

    def _usage(
        self,
        art: Mapping[str, Any],
        lineage_id: UUID,
        run_id: UUID | None,
        outcome: str,
        duration_ms: int,
    ) -> None:
        """One `ontology_query` usage event per query (keyed by its lineage row, so a replay
        counts once). Queries cost nothing a rate card prices: the event carries attribution,
        duration and outcome, with cost kind `unmeasured`."""
        root_run_id: UUID | None = None
        agent_id: UUID | None = None
        if run_id is not None and self._has_runs_table():
            row = self.session.execute(
                text(
                    "SELECT root_run_id, agent_id FROM shifde.runs WHERE id = :r AND tenant_id = :t"
                ),
                {"r": run_id, "t": self.sc.tenant_id},
            ).first()
            if row is not None:
                root_run_id, agent_id = row[0], row[1]
        ledger.record(
            self.session,
            {
                "tenant_id": self.sc.tenant_id,
                "event_key": f"ontology_query:{lineage_id}",
                "event_type": "ontology_query",
                "status": "succeeded" if outcome == "success" else "failed",
                "workspace_id": art["workspace_id"],
                "project_id": art["project_id"],
                "principal_id": self.sc.principal.principal_id,
                "run_id": run_id,
                "root_run_id": root_run_id,
                "agent_id": agent_id,
                "call_count": 1,
                "duration_ms": duration_ms,
                "provider_reported_usage": {"lineage_id": str(lineage_id)},
            },
        )

    @staticmethod
    def _object_types(root: engine.NodePlan) -> list[str]:
        out = [root.object_type]
        for hop in root.hops:
            out.extend(OntologyService._object_types(hop.target))
        return list(dict.fromkeys(out))

    def _authorize_call(self, art: Mapping[str, Any], *, api: bool) -> None:
        ref = self.arts.ref_of(art)
        if api:
            self.sc.pdp.require("ontology:query", ref)
        elif not self.sc.pdp.authorize("ontology:query", ref).allowed:
            raise Forbidden("the principal lacks permission ontology:query")

    # ================================================================== query
    def query(
        self,
        ontology_id: UUID,
        body: s.QueryIn,
        *,
        channel: str = "api",
        run_id: UUID | None = None,
        api: bool = True,
        consts: Mapping[str, Any] | None = None,  # Any: pipeline `params.*` constants
        purpose: str | None = None,
    ) -> s.QueryOut:
        art = self._artifact(ontology_id, api=api, run_id=run_id)
        planner: engine.Planner | None = None
        try:
            self._authorize_call(art, api=api)
            version = self._version(art, body.version_id)
            spec, mappings = self._frozen(version)
            planner = engine.Planner(self._context(art, spec, mappings, consts))
            fingerprint = _fingerprint(version["id"], body)
            offset = decode_query_cursor(body.cursor, fingerprint)
            root = planner.node(body.object_type, body.select, body.filter, body.traverse, 0)
        except (NotFound, Forbidden) as exc:
            reason = (
                planner.denied[-1][1]
                if planner is not None and planner.denied
                else type(exc).__name__.lower()
            )
            self._denied(art, reason, {"object_type": body.object_type[:200], "channel": channel})
            raise
        result = self._execute(
            art,
            version,
            root,
            body.limit,
            offset,
            channel=channel,
            run_id=run_id,
            purpose=purpose or f"ontology:{art['key']}:query",
        )
        rows, executed, lineage_id = result
        more = executed.has_more
        return s.QueryOut(
            rows=rows,
            next_cursor=encode_query_cursor(offset + body.limit, fingerprint) if more else None,
            lineage_id=lineage_id,
            truncated=executed.truncated,
        )

    def _execute(
        self,
        art: Mapping[str, Any],
        version: Mapping[str, Any],
        root: engine.NodePlan,
        limit: int,
        offset: int,
        *,
        channel: str,
        run_id: UUID | None,
        purpose: str,
    ) -> tuple[list[dict[str, Any]], engine.Result, UUID]:  # Any: output rows
        started = time.monotonic()
        object_types = self._object_types(root)
        with ExitStack() as stack:
            source = self._source(stack, purpose)
            runner = engine.Executor(source)
            try:
                result = runner.run(root, limit, offset)
            except AppError as exc:
                self.session.rollback()
                self._lineage(
                    art,
                    version,
                    source,
                    runner.counts,
                    started,
                    channel=channel,
                    run_id=run_id,
                    outcome="failed",
                    error_code=exc.code,
                    object_types=object_types,
                )
                raise
        lineage_id = self._lineage(
            art,
            version,
            source,
            result.row_counts,
            started,
            channel=channel,
            run_id=run_id,
            outcome="success",
            error_code=None,
            object_types=object_types,
        )
        return result.rows, result, lineage_id

    # ================================================================== objects
    def _typed_pk(self, ot: Mapping[str, Any], raw: str) -> list[Any]:  # Any: typed values
        pk = list(ot["primary_key"])
        parts = raw.split(",")
        if len(parts) != len(pk):
            raise ValidationFailed(f"the identifier has {len(pk)} part(s), separated by ','")
        props = {p["key"]: p for p in ot["properties"]}
        out: list[Any] = []
        for key, part in zip(pk, parts, strict=True):
            ptype = props[key]["type"]
            try:
                if ptype == "integer":
                    value: Any = int(part)
                elif ptype == "number":
                    value = engine.exact_number(part)  # not float(): keys above 2**53 (F-04)
                    if not isinstance(value, int | float | Decimal):
                        raise ValueError(part)
                elif ptype == "boolean":
                    if part not in ("true", "false"):
                        raise ValueError(part)
                    value = part == "true"
                else:
                    value = expr.coerce(ptype, part)
            except (ValueError, expr.FilterError) as exc:
                raise ValidationFailed(f"identifier part {key!r} is not a valid {ptype}") from exc
            out.append(value)
        return out

    def get_object(
        self,
        ontology_id: UUID,
        object_type: str,
        pk: str,
        *,
        version_id: UUID | None = None,
        channel: str = "api",
        run_id: UUID | None = None,
        api: bool = True,
        properties: list[str] | None = None,
        purpose: str | None = None,
    ) -> s.ObjectOut:
        """Resolve one identifier to the object (permitted properties) and its relationships."""
        art = self._artifact(ontology_id, api=api, run_id=run_id)
        planner: engine.Planner | None = None
        try:
            self._authorize_call(art, api=api)
            version = self._version(art, version_id)
            spec, mappings = self._frozen(version)
            planner = engine.Planner(self._context(art, spec, mappings, None))
            auth = planner.authorize(object_type)
            values = self._typed_pk(auth.spec, pk)
            pk_props = list(auth.spec["primary_key"])
            pk_filters = engine.pk_equality_for(auth, values)
            plans: list[engine.NodePlan]
            if properties:
                # an action asks for exact properties: ALL of them must be visible on this row
                root = planner.node(
                    object_type, properties, None, [], 0,
                    extra_filters=pk_filters, touched_extra=pk_props,
                )  # fmt: skip
                plans = [root]
                hops: list[engine.HopPlan] = []
            else:
                # the object exists for the principal when its identifier is admitted; each
                # property then appears only if one of ITS admitting grants accepts this row
                root = planner.node(
                    object_type, [], None, [], 0, extra_filters=pk_filters, touched_extra=pk_props
                )
                readable = pol.readable(auth.grants)
                wanted = [
                    p["key"]
                    for p in auth.spec["properties"]
                    if p["key"] in readable and p["key"] in auth.operands
                ]
                plans = [
                    planner.node(
                        object_type, group, None, [], 0,
                        extra_filters=pk_filters, touched_extra=pk_props,
                    )
                    for group in pol.property_groups(auth.grants, wanted)
                ]  # fmt: skip
                hops = self._summary_hops(planner, spec, object_type)
        except (NotFound, Forbidden) as exc:
            reason = (
                planner.denied[-1][1]
                if planner is not None and planner.denied
                else type(exc).__name__.lower()
            )
            self._denied(art, reason, {"object_type": object_type[:200], "channel": channel})
            raise
        started = time.monotonic()
        purpose = purpose or f"ontology:{art['key']}:object"
        with ExitStack() as stack:
            source = self._source(stack, purpose)
            runner = engine.Executor(source)
            try:
                rows = runner.first(root)
                if not rows:
                    raise NotFound("object not found")
                shaped: dict[str, Any] = {}
                if properties:
                    shaped = runner.shape(rows[0], root)
                else:
                    for plan in plans:
                        found = runner.first(plan)
                        if found:
                            shaped.update(runner.shape(found[0], plan))
                    order = [p["key"] for p in spec_type(spec, object_type)["properties"]]
                    shaped = {k: shaped[k] for k in order if k in shaped}
                summaries: list[s.RelationshipSummaryOut] = []
                for hop in hops:
                    count, capped = runner.count_links(root, hop, rows[0], engine.PER_PARENT_LINKS)
                    summaries.append(
                        s.RelationshipSummaryOut(
                            key=hop.relationship,
                            label=hop.label,
                            direction=hop.direction,
                            count=count,
                            capped=capped,
                        )
                    )
            except AppError as exc:
                self.session.rollback()
                self._lineage(
                    art,
                    version,
                    source,
                    runner.counts,
                    started,
                    channel=channel,
                    run_id=run_id,
                    outcome="failed",
                    error_code=exc.code,
                    object_types=self._object_types(root),
                )
                raise
        lineage_id = self._lineage(
            art,
            version,
            source,
            runner.counts,
            started,
            channel=channel,
            run_id=run_id,
            outcome="success",
            error_code=None,
            object_types=list(dict.fromkeys([object_type, *(h.target.object_type for h in hops)])),
        )
        return s.ObjectOut(object=shaped, relationships=summaries, lineage_id=lineage_id)

    def _summary_hops(
        self, planner: engine.Planner, spec: Mapping[str, Any], object_type: str
    ) -> list[engine.HopPlan]:
        """One count-only hop per relationship the object type takes part in, skipping those
        whose other end the principal may not see (no oracle)."""
        out: list[engine.HopPlan] = []
        for rel in spec.get("relationships") or []:
            for direction, origin in (("forward", rel["from"]), ("reverse", rel["to"])):
                if origin != object_type:
                    continue
                if len(out) >= MAX_SUMMARIES:  # a busy type shows the first ones, never an error
                    return out
                try:
                    out.append(planner.count_hop(object_type, rel["key"], direction))
                except NotFound:
                    continue
        return out

    # ================================================================== policies
    def _policy_art(self, ontology_id: UUID) -> Mapping[str, Any]:  # Any: row
        art = self.arts.artifact(KIND, ontology_id)
        self.sc.pdp.require("ontology:manage_policies", self.arts.ref_of(art))
        return art

    def _model_of(self, art: Mapping[str, Any]) -> dict[str, Any]:  # Any: spec
        draft = self.arts.draft_of(art)
        if draft is not None:
            return dict(draft["spec"])
        if art["latest_published_version_id"] is not None:
            return dict(self.arts.version_of(art, art["latest_published_version_id"])["spec"])
        return {}

    def _validate_policy(self, art: Mapping[str, Any], body: s.PolicyIn) -> None:
        spec = self._model_of(art)
        ot = next(
            (t for t in spec.get("object_types") or [] if t.get("key") == body.object_type_key),
            None,
        )
        if ot is None:
            raise ValidationFailed(f"the ontology has no object type {body.object_type_key!r}")
        known = {p["key"] for p in ot.get("properties") or []}
        for key in body.property_keys or []:
            if key not in known:
                raise ValidationFailed(f"{body.object_type_key} has no property {key!r}")
        if len(set(body.property_keys or [])) != len(body.property_keys or []):
            raise ValidationFailed("property_keys must be distinct")
        if body.row_filter:
            try:
                paths = expr.referenced_fields(body.row_filter)  # syntax
            except expr.FilterError as exc:
                raise ValidationFailed(f"row_filter: {exc}") from exc
            for path in paths:
                if path[0] == "principal":
                    continue
                if not (len(path) == 2 and path[0] == "row" and path[1] in known):
                    raise ValidationFailed(
                        "row_filter reads row.<property> or principal.<attribute> only"
                    )
        subject = body.subject
        if subject.type == "role":
            if authz.get_role(self.session, self.sc.tenant_id, subject.id) is None:
                raise ValidationFailed("no such role")
        elif subject.type == "group":
            # groups are platform-level (B-928): only a group bound in this client is known;
            # one bound only elsewhere answers exactly like a missing one (B-930, B-2013)
            if all(g.id != subject.id for g in authz.bound_groups(self.session)):
                raise ValidationFailed("no such group")
        elif not identity.is_member(self.session, self.sc.tenant_id, subject.id):
            raise ValidationFailed("no such organization member")

    @staticmethod
    def _policy_out(row: Mapping[str, Any]) -> s.PolicyOut:  # Any: row
        return s.PolicyOut(
            id=row["id"],
            object_type_key=row["object_type_key"],
            property_keys=list(row["property_keys"]) if row["property_keys"] is not None else None,
            subject=s.PolicySubject(type=row["subject_type"], id=row["subject_id"]),
            effect=row["effect"],
            row_filter=row["row_filter"],
            row_version=row["row_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_policies(self, ontology_id: UUID) -> list[s.PolicyOut]:
        art = self._policy_art(ontology_id)
        return [
            self._policy_out(r)
            for r in repository.list_policies(self.session, self.sc.tenant_id, art["id"])
        ]

    def _policy_audit(
        self,
        action: str,
        art: Mapping[str, Any],
        row: Mapping[str, Any],  # Any: row
    ) -> None:
        self.arts._audit(
            action,
            art,
            {
                "policy_id": str(row["id"]),
                "object_type": row["object_type_key"],
                "subject": f"{row['subject_type']}:{row['subject_id']}",
                "properties": "all" if row["property_keys"] is None else len(row["property_keys"]),
                "row_filter": bool(row["row_filter"]),
            },
            target_type="ontology_policy",
            target_id=row["id"],
        )

    def create_policy(self, ontology_id: UUID, body: s.PolicyIn) -> s.PolicyOut:
        self.arts._active_tenant()
        art = self._policy_art(ontology_id)
        self._validate_policy(art, body)
        row = repository.insert_policy(
            self.session,
            self.sc.tenant_id,
            {
                "id": uuid7(),
                "ontology_id": art["id"],
                "object_type_key": body.object_type_key,
                "property_keys": body.property_keys,
                "subject_type": body.subject.type,
                "subject_id": body.subject.id,
                "row_filter": (body.row_filter or "").strip() or None,
                "created_by": self.sc.principal.principal_id,
            },
        )
        self._policy_audit("ontology.policy_created", art, row)
        self.session.commit()
        return self._policy_out(row)

    def update_policy(self, ontology_id: UUID, policy_id: UUID, body: s.PolicyIn) -> s.PolicyOut:
        self.arts._active_tenant()
        art = self._policy_art(ontology_id)
        # a caller allowed on this ontology may only rewrite a policy of this ontology (F1)
        if repository.get_policy(self.session, self.sc.tenant_id, art["id"], policy_id) is None:
            raise NotFound("policy not found")
        self._validate_policy(art, body)
        row = repository.update_policy(
            self.session,
            self.sc.tenant_id,
            art["id"],
            policy_id,
            body.row_version,
            {
                "object_type_key": body.object_type_key,
                "subject_type": body.subject.type,
                "subject_id": body.subject.id,
                "row_filter": (body.row_filter or "").strip() or None,
            },
            body.property_keys,
        )
        if row is None:
            self.session.rollback()
            raise Conflict("row_version mismatch: the policy was modified; reload and retry")
        self._policy_audit("ontology.policy_updated", art, row)
        self.session.commit()
        return self._policy_out(row)

    def delete_policy(self, ontology_id: UUID, policy_id: UUID) -> None:
        self.arts._active_tenant()
        art = self._policy_art(ontology_id)
        row = repository.get_policy(self.session, self.sc.tenant_id, art["id"], policy_id)
        if row is None or not repository.delete_policy(
            self.session, self.sc.tenant_id, art["id"], policy_id
        ):
            raise NotFound("policy not found")
        self._policy_audit("ontology.policy_deleted", art, row)
        self.session.commit()

    # ================================================================== principal attributes
    def _attr_check(self) -> None:
        self.sc.pdp.require("ontology:manage_policies", self.sc.tenant_ref)

    @staticmethod
    def _attr_out(row: Mapping[str, Any]) -> s.AttributesOut:  # Any: row
        return s.AttributesOut(
            principal_type=row["principal_type"],
            principal_id=row["principal_id"],
            attributes=dict(row["attributes"]),
            row_version=row["row_version"],
            updated_at=row["updated_at"],
        )

    def list_attributes(self) -> list[s.AttributesOut]:
        self._attr_check()
        return [
            self._attr_out(r) for r in repository.list_attributes(self.session, self.sc.tenant_id)
        ]

    def get_attributes(self, principal_type: str, principal_id: UUID) -> s.AttributesOut:
        self._attr_check()
        row = repository.get_attributes(
            self.session, self.sc.tenant_id, principal_type, principal_id
        )
        if row is None:
            raise NotFound("no attributes are set for this principal")
        return self._attr_out(row)

    def put_attributes(
        self, principal_type: str, principal_id: UUID, body: s.AttributesIn
    ) -> s.AttributesOut:
        self.arts._active_tenant()
        self._attr_check()
        exists = (
            identity.is_member(self.session, self.sc.tenant_id, principal_id)
            if principal_type == "user"
            else identity.service_account_exists(self.session, self.sc.tenant_id, principal_id)
        )
        if not exists:
            raise NotFound("principal not found")
        for key, value in body.attributes.items():
            scalar = isinstance(value, str | int | float | bool)
            listed = isinstance(value, list) and all(
                isinstance(v, str | int | float | bool) for v in value
            )
            if not key.replace("_", "").isalnum() or not (scalar or listed):
                raise ValidationFailed(
                    "attribute names are letters, digits or _; values are scalars or lists"
                )
        if len(json.dumps(body.attributes)) > ATTRIBUTE_MAX_BYTES:
            raise ValidationFailed("the attributes are too large")
        try:
            row = repository.put_attributes(
                self.session,
                self.sc.tenant_id,
                principal_type,
                principal_id,
                body.attributes,
                body.row_version,
                self.sc.principal.principal_id,
            )
        except IntegrityError as exc:
            self.session.rollback()
            raise Conflict("the attributes could not be saved") from exc
        if row is None:
            self.session.rollback()
            raise Conflict("row_version mismatch: the attributes were modified; reload and retry")
        self.arts._audit(
            "ontology.principal_attributes_set",
            {
                "kind": KIND,
                "id": principal_id,
                "key": f"{principal_type}:{principal_id}",
                "workspace_id": None,
                "project_id": None,
            },
            {"principal": f"{principal_type}:{principal_id}", "names": sorted(body.attributes)},
            target_type="principal",
            target_id=principal_id,
        )
        self.session.commit()
        return self._attr_out(row)

    # ================================================================== lineage
    def list_lineage(
        self, ontology_id: UUID, run_id: UUID | None, limit: int, cursor: str | None
    ) -> Page[s.LineageOut]:
        art = self.arts.artifact(KIND, ontology_id)
        self.sc.pdp.require("ontology:read", self.arts.ref_of(art))
        rows = [
            r
            for r in repository.list_lineage(
                self.session,
                self.sc.tenant_id,
                art["id"],
                run_id,
                decode_cursor(cursor),
                limit + 1,
            )
            # the row is this client's and belongs to the ontology the caller may read
            if r["tenant_id"] == self.sc.tenant_id and r["ontology_id"] == art["id"]
        ]
        page, more = paginate(rows, limit)
        labels = {r["principal_id"]: self.arts.label(r["principal_id"]) for r in page}
        items = []
        for r in page:
            label = labels[r["principal_id"]]
            assert label is not None
            items.append(
                s.LineageOut(
                    id=r["id"],
                    at=r["at"],
                    principal=s.PrincipalOut(id=label.id, display=label.display),
                    run_id=r["run_id"],
                    ontology_id=r["ontology_id"],
                    ontology_version_id=r["ontology_version_id"],
                    channel=r["channel"],
                    object_types=list(r["object_types"]),
                    connections=list(r["connection_ids"]),
                    compiled_query_hashes=list(r["compiled_query_hashes"]),
                    row_counts={k: int(v) for k, v in (r["row_counts"] or {}).items()},
                    duration_ms=r["duration_ms"],
                    outcome=r["outcome"],
                    error_code=r["error_code"],
                )
            )
        return Page(
            items=items, next_cursor=encode_cursor(page[-1]["id"]) if more and page else None
        )

    # ================================================================== materialization
    def materialize(self, ontology_id: UUID) -> None:
        self.arts.artifact(KIND, ontology_id, "update")
        raise MaterializationNotEnabled(
            "materialization is not enabled: ontology data is queried at the source"
        )

    # ================================================================== actions
    def action_inputs(
        self,
        ontology_id: UUID,
        version_id: UUID,
        action_key: str,
        object_pk: str,
        *,
        api: bool,
        run_id: UUID | None = None,
    ) -> tuple[dict[str, Any], Mapping[str, Any], dict[str, Any]]:  # Any: action / object JSON
        """(action spec, the version's object type, the object's permitted properties the
        action maps) for one object, resolved as the calling principal: policies and row filters
        apply, a property the principal may not read cannot feed an action parameter."""
        art = self._artifact(ontology_id, api=api, run_id=run_id)
        self._authorize_call(art, api=api)
        version = self._version(art, version_id)
        spec, _ = self._frozen(version)
        action = next((a for a in spec.get("actions") or [] if a.get("key") == action_key), None)
        if action is None:
            raise NotFound("action not found")
        ot = next(t for t in spec["object_types"] if t["key"] == action["object_type"])
        wanted = sorted(
            {
                str(v)[len(ontologies.OBJECT_REF) :]
                for v in action["param_mapping"].values()
                if isinstance(v, str) and v.startswith(ontologies.OBJECT_REF)
            }
            | set(ot["primary_key"])
        )
        out = self.get_object(
            ontology_id,
            action["object_type"],
            object_pk,
            version_id=version["id"],
            channel="action",
            run_id=run_id,
            api=api,
            properties=wanted,
            purpose=f"ontology:{art['key']}:action",
        )
        return dict(action), ot, out.object
