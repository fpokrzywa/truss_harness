"""Source-mapping operations of ontology drafts (P7 contract section 2, amendment F1).

Mappings are the physical layer: they belong to ONE ontology version row. They are written only
against the saved draft (the database trigger freezes them when the version is published), every
write takes the draft row lock (serialising with publish) and is refused while a publish request
is pending. No data is read from the source here: the `check` endpoint compares the mapping with
the connection's schema discovery (metadata) only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any  # Any: stored JSON
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from shifde.build import ontologies, repository
from shifde.build import schemas as s
from shifde.build.kind_ops import load_version
from shifde.build.service import ArtifactService
from shifde.core.errors import (
    Conflict,
    InvalidTransition,
    NotFound,
    PublishPending,
    ValidationFailed,
)
from shifde.core.ids import uuid7

KIND = "ontologies"


def mapping_out(row: Mapping[str, Any]) -> s.MappingOut:  # Any: DB row
    return s.MappingOut(
        target_kind=row["target_kind"],
        target_key=row["target_key"],
        connection_id=row["connection_id"],
        relation=s.RelationOut.model_validate(
            {"schema": row["relation_schema"], "name": row["relation_name"]}
        ),
        column_map=dict(row["column_map"] or {}),
        key_map=dict(row["key_map"]) if row["key_map"] is not None else None,
        filters=[str(f) for f in row["filters"] or []],
        status=row["status"],
        row_version=row["row_version"],
    )


class OntologyMappingOps:
    def __init__(self, svc: ArtifactService) -> None:
        self.svc = svc

    # ------------------------------------------------------------------ helpers
    def _draft(self, art: Mapping[str, Any]) -> Mapping[str, Any]:  # Any: DB rows
        draft = repository.get_draft(self.svc.session, self.svc.sc.tenant_id, art["id"], lock=True)
        if draft is None:
            raise NotFound("this ontology has no saved draft: save the draft before mapping it")
        if (
            repository.pending_request_for(self.svc.session, self.svc.sc.tenant_id, draft["id"])
            is not None
        ):
            raise PublishPending("the draft is frozen while its publish request is pending")
        return draft

    @staticmethod
    def _target_exists(spec: Mapping[str, Any], kind: str, key: str) -> bool:  # Any: JSON
        section = "object_types" if kind == "object_type" else "relationships"
        return any(isinstance(x, dict) and x.get("key") == key for x in spec.get(section) or [])

    def _require_connection(self, connection_id: UUID, action: str) -> Mapping[str, Any]:
        conns = self.svc.conns
        row = conns.get_row(connection_id)  # NotFound for another tenant's id
        self.svc.sc.pdp.require(action, conns.ref_of(row), hide_unless="connection:read")
        return row

    # ------------------------------------------------------------------ operations
    def list(self, artifact_id: UUID, selector: str) -> list[s.MappingOut]:
        art = self.svc.artifact(KIND, artifact_id)
        version = load_version(self.svc, art, selector)
        return [
            mapping_out(r)
            for r in repository.list_mappings(
                self.svc.session, self.svc.sc.tenant_id, version["id"]
            )
        ]

    def put(self, artifact_id: UUID, kind: str, key: str, body: s.MappingIn) -> s.MappingOut:
        self.svc._active_tenant()
        art = self.svc.artifact(KIND, artifact_id, "update")
        if art["status"] == "retired":
            raise InvalidTransition("a retired ontology cannot be edited")
        if body.target_kind != kind or body.target_key != key:
            raise ValidationFailed("the body's target_kind / target_key must match the path")
        self._require_connection(body.connection_id, "connection:use")
        draft = self._draft(art)
        if not self._target_exists(draft["spec"], kind, key):
            raise ValidationFailed(
                f"the draft has no {kind.replace('_', ' ')} {key!r}: save it in the spec first"
            )
        if kind == "relationship":
            if body.key_map is None:
                raise ValidationFailed("a relationship mapping needs key_map")
        elif body.key_map is not None:
            raise ValidationFailed("an object type mapping takes no key_map")
        key_map = (
            body.key_map.model_dump(by_alias=True, exclude_none=True) if body.key_map else None
        )
        try:
            row = repository.upsert_mapping(
                self.svc.session,
                self.svc.sc.tenant_id,
                {
                    "id": uuid7(),
                    "tenant_id": self.svc.sc.tenant_id,
                    "ontology_version_id": draft["id"],
                    "target_kind": kind,
                    "target_key": key,
                    "connection_id": body.connection_id,
                    "relation_schema": body.relation.schema_,
                    "relation_name": body.relation.name,
                    "column_map": body.column_map,
                    "key_map": key_map,
                    "filters": body.filters,
                    "status": "unchecked",
                    "created_by": self.svc.sc.principal.principal_id,
                },
            )
        except IntegrityError as exc:
            self.svc.session.rollback()
            raise Conflict("the mapping could not be saved") from exc
        self.svc._audit(
            "ontology.mapping_saved",
            art,
            {
                "target_kind": kind,
                "target_key": key,
                "connection_id": str(body.connection_id),
                "relation": f"{body.relation.schema_}.{body.relation.name}".strip("."),
            },
            target_type="ontology_mapping",
            target_id=row["id"],
        )
        self.svc.session.commit()
        return mapping_out(row)

    def delete(self, artifact_id: UUID, kind: str, key: str) -> None:
        self.svc._active_tenant()
        art = self.svc.artifact(KIND, artifact_id, "update")
        draft = self._draft(art)
        if not repository.delete_mapping(
            self.svc.session, self.svc.sc.tenant_id, draft["id"], kind, key
        ):
            raise NotFound("no such mapping")
        self.svc._audit(
            "ontology.mapping_removed",
            art,
            {"target_kind": kind, "target_key": key},
            target_type="ontology_mapping",
        )
        self.svc.session.commit()

    def check(self, artifact_id: UUID, kind: str, key: str) -> s.MappingCheckOut:
        self.svc._active_tenant()
        art = self.svc.artifact(KIND, artifact_id, "update")
        draft = repository.get_draft(self.svc.session, self.svc.sc.tenant_id, art["id"])
        row = (
            repository.get_mapping(self.svc.session, self.svc.sc.tenant_id, draft["id"], kind, key)
            if draft
            else None
        )
        if draft is None or row is None:
            raise NotFound("no such draft mapping")
        self._require_connection(row["connection_id"], "connection:test")
        if self.svc.env(art).schema_discovery(row["connection_id"]) is None:
            self.svc.conns.discover(row["connection_id"])  # live discovery: metadata only
            self.svc.session.commit()
        result = ontologies.check_one_mapping(dict(draft["spec"]), row, self.svc.env(art))
        repository.set_mapping_status(
            self.svc.session, self.svc.sc.tenant_id, row["id"], "ok" if result.ok else "invalid"
        )
        self.svc.session.commit()
        return s.MappingCheckOut(
            ok=result.ok,
            issues=[s.MappingIssueOut.model_validate(i) for i in result.issues],
            sample_columns=[s.SampleColumnOut.model_validate(c) for c in result.sample_columns],
        )


def frozen_mappings(
    svc: ArtifactService, version_id: UUID
) -> tuple[list[Mapping[str, Any]], Mapping[str, Any] | None]:  # Any: DB rows
    """The mappings frozen with a version and the info row recorded at publish (None for a
    version published without one: it cannot be queried)."""
    return (
        list(repository.list_mappings(svc.session, svc.sc.tenant_id, version_id)),
        repository.get_ontology_info(svc.session, svc.sc.tenant_id, version_id),
    )
