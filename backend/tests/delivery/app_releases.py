"""Sealed `/2` releases for the generated-app delivery tests (SP5-6), inserted as the schema owner
like `releases.make_release`: a model provider (an `openai_compatible` one on `base_url`, or a
`fake` one) and its connection, the published prompt, the two `connection.sql` tools of
`SAMPLE_DRAFT` and the agent pinning them (specs as accept makes them), a solution version with
the draft's screens, a dataset and the release, whose manifest is the packaging `/2` manifest with
one model binding. Each release gets a project of its own (artifact keys are unique per scope).
`kind=DOCKER_KIND` (SP8-9) is the Databricks one for `docker.compose/v1`.
`kind=SNOWFLAKE_KIND` (SP6-6) is the `snowflake.native-app/v1` release of `SNOWFLAKE_SAMPLE_DRAFT`,
its data reads every table the tools read (`SNOWFLAKE_SAMPLE_TABLES`), bound to `CORTEX_MODEL`."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any  # Any: manifest JSON
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from shifde.appgen.render import DATABRICKS_KIND, GENERATOR_VERSIONS
from shifde.appgen.render_docker import TARGET_KIND as DOCKER_KIND
from shifde.appgen.schemas import DraftTool
from shifde.appgen.service import AGENT_LIMITS, tool_spec
from shifde.core.db import Database, set_tenant
from shifde.core.ids import uuid7
from shifde.core.sqlrules import Dialect
from shifde.packaging.canonical import digest
from shifde.packaging.service import COMPILER_VERSION_APP
from tests._support.appgen_fixtures import (
    SAMPLE_DRAFT,
    SAMPLE_TABLES,
    SNOWFLAKE_SAMPLE_DRAFT,
    SNOWFLAKE_SAMPLE_TABLES,
)
from tests.appgen.rendered import CORTEX_MODEL, ENDPOINT, SNOWFLAKE_KIND, input_schema
from tests.delivery.releases import TARGET_SCOPE_SQL, SealedRelease, insert_run


@dataclass(frozen=True)
class Kind:
    """What a release of one target kind is made of."""

    draft: dict[str, Any]
    tables: tuple[str, ...]
    model: str
    dialect: Dialect


KINDS = {
    DATABRICKS_KIND: Kind(SAMPLE_DRAFT, SAMPLE_TABLES, ENDPOINT, "databricks"),
    DOCKER_KIND: Kind(SAMPLE_DRAFT, SAMPLE_TABLES, ENDPOINT, "databricks"),
    SNOWFLAKE_KIND: Kind(
        SNOWFLAKE_SAMPLE_DRAFT, SNOWFLAKE_SAMPLE_TABLES, CORTEX_MODEL, "snowflake"
    ),
}


@dataclass(frozen=True)
class AppRelease:
    release: SealedRelease
    provider_id: UUID
    agent: tuple[UUID, str]  # (version id, spec hash)
    prompt: tuple[UUID, str]
    tools: dict[str, tuple[UUID, str]]  # by tool name

    @property
    def id(self) -> UUID:
        return self.release.id

    def resolved(self) -> dict[str, Any]:  # Any: a `Resolved` document
        """The `Resolved` the plan request must gather for this release (its provider apart)."""
        draft = KINDS[self.release.manifest["target_kind"]].draft
        agent = draft["agent"]
        return {
            "agent": {
                "version_id": str(self.agent[0]),
                "spec_hash": self.agent[1],
                "name": agent["name"],
                "instructions": agent["instructions"],
                "limits": dict(AGENT_LIMITS),
            },
            "tools": [
                {
                    "version_id": str(self.tools[t["name"]][0]),
                    "spec_hash": self.tools[t["name"]][1],
                    "name": t["name"],
                    "description": t["description"],
                    "sql": t["sql"],
                    "parameters": sorted(t["parameters"], key=lambda p: p["name"]),
                    "input_schema": input_schema(t["parameters"]),
                    "max_rows": t["max_rows"],
                }
                for t in sorted(draft["tools"], key=lambda t: t["name"])
            ],
            "model": {
                "serving_endpoint": self.release.manifest["bindings"]["model_bindings"]["default"][
                    "model"
                ]
            },
            "screens": draft["screens"],
        }


def add_provider(owner_db: Database, tenant: UUID, adapter_key: str, base_url: str | None) -> UUID:
    """A model provider of `tenant` and its connection (`base_url` in its config, if any)."""
    connection, provider = uuid7(), uuid7()
    with owner_db.session() as o:
        set_tenant(o, tenant)
        o.execute(
            text(
                "INSERT INTO shifde.connections (id, tenant_id, name, connection_type, "
                "adapter_key, config) VALUES (:c, :t, :n, 'model_api', :a, CAST(:cfg AS jsonb))"
            ),
            {
                "c": connection,
                "t": tenant,
                "n": f"conn-{connection.hex}",
                "a": adapter_key,
                "cfg": json.dumps({"base_url": base_url} if base_url else {}),
            },
        )
        o.execute(
            text(
                "INSERT INTO shifde.model_providers (id, tenant_id, name, adapter_key, "
                "connection_id) VALUES (:p, :t, :n, :a, :c)"
            ),
            {
                "p": provider,
                "t": tenant,
                "n": f"prov-{provider.hex}",
                "a": adapter_key,
                "c": connection,
            },
        )
        o.commit()
    return provider


def _version(
    o: Session,
    values: dict[str, Any],
    kind: str,
    key: str,
    spec: dict[str, Any],
    description: str | None = None,
) -> tuple[UUID, str]:
    """A published version of a new artifact of `kind` (`key`) with `spec`: (id, spec hash)."""
    artifact, version, spec_hash = uuid7(), uuid7(), digest(spec)
    o.execute(
        text(
            "INSERT INTO shifde.artifacts (id, tenant_id, kind, workspace_id, project_id, key, "
            "name, description, owner_principal_id) VALUES (:a, :t, :kind, :w, :p, :k, :k, :d, :u)"
        ),
        {**values, "a": artifact, "kind": kind, "k": key, "d": description},
    )
    o.execute(
        text(
            "INSERT INTO shifde.artifact_versions (id, tenant_id, artifact_id, kind, version_no, "
            "state, spec, spec_hash, published_at) VALUES (:v, :t, :a, :kind, 1, 'published', "
            "CAST(:s AS jsonb), :h, now())"
        ),
        {
            **values,
            "v": version,
            "a": artifact,
            "kind": kind,
            "s": json.dumps(spec),
            "h": spec_hash,
        },
    )
    return version, spec_hash


def make_app_release(
    owner_db: Database,
    tenant: UUID,
    workspace_id: UUID,
    *,
    provider_id: UUID,
    kind: str = DATABRICKS_KIND,
    model: str | None = None,
    edit: Callable[[dict[str, Any]], None] | None = None,
) -> AppRelease:
    """A sealed `/2` release of `kind` and `tenant` in a new project of `workspace_id`, bound to
    `provider_id`'s `model` (the kind's own unless given), with a passed evaluation run; `edit` changes the manifest before it is
    digested (a tampered pin, another generator version)."""
    project, solution, sv = uuid7(), uuid7(), uuid7()
    dataset_id, release_id, by, connection = uuid7(), uuid7(), uuid7(), uuid7()
    made = KINDS[kind]
    draft, model = made.draft, model or made.model
    cases = draft["evaluation_cases"]
    dataset_digest = digest(cases)
    values = {"t": tenant, "w": workspace_id, "p": project, "u": by}
    with owner_db.session() as o:
        set_tenant(o, tenant)
        o.execute(
            text(
                "INSERT INTO shifde.projects (id, tenant_id, workspace_id, slug, name) "
                "VALUES (:p, :t, :w, :s, :s)"
            ),
            {**values, "s": f"p-{project.hex}"},
        )
        agent = draft["agent"]
        prompt = _version(
            o, values, "prompts", agent["name"], {"sections": {"system": agent["instructions"]}}
        )
        tools = {
            t["name"]: _version(
                o,
                values,
                "tools",
                t["name"],
                tool_spec(DraftTool.model_validate(t), connection, made.dialect),
                t["description"],
            )
            for t in draft["tools"]
        }
        agent_spec = {
            "prompt_version_id": str(prompt[0]),
            "model": {"model_id": str(uuid7())},
            "tool_version_ids": [str(tools[t["name"]][0]) for t in draft["tools"]],
            "limits": {**AGENT_LIMITS, "max_cost": 1},
        }
        agent_version = _version(o, values, "agents", agent["name"], agent_spec)
        o.execute(
            text(
                "INSERT INTO shifde.solutions (tenant_id, id, workspace_id, project_id, name, "
                "target_kind, created_by) VALUES (:t, :s, :w, :p, :n, :k, :u)"
            ),
            {**values, "k": kind, "s": solution, "n": f"solution-{solution.hex[-12:]}"},
        )
        o.execute(
            text(
                "INSERT INTO shifde.solution_versions (tenant_id, id, solution_id, version_no, "
                "entrypoint_kind, entrypoint_version_id, spec, spec_hash, created_by) VALUES "
                "(:t, :v, :s, 1, 'agent', :av, '{}'::jsonb, :h, :u)"
            ),
            {**values, "v": sv, "s": solution, "av": agent_version[0], "h": "b" * 64},
        )
        o.execute(
            text(
                "INSERT INTO shifde.evaluation_datasets (tenant_id, id, workspace_id, project_id, "
                "name, version_no, cases, digest, created_by) VALUES (:t, :d, :w, :p, :n, 1, "
                "CAST(:c AS jsonb), :dd, :u)"
            ),
            {
                **values,
                "d": dataset_id,
                "n": f"golden-{dataset_id.hex[-12:]}",
                "c": json.dumps(cases),
                "dd": dataset_digest,
            },
        )
        pins = sorted(
            [{"role": "prompt", "version_id": str(prompt[0]), "spec_hash": prompt[1]}]
            + [{"role": "tool", "version_id": str(v), "spec_hash": h} for v, h in tools.values()],
            key=lambda p: (p["role"], p["version_id"]),
        )
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "solution_id": str(solution),
            "solution_version_id": str(sv),
            "version_no": 1,
            "target_kind": kind,
            "entrypoint": {
                "kind": "agent",
                "version_id": str(agent_version[0]),
                "spec_hash": agent_version[1],
            },
            "pins": pins,
            "data_reads": [{"connection_id": str(connection), "tables": list(made.tables)}],
            "bindings": {
                "model_bindings": {"default": {"provider_id": str(provider_id), "model": model}},
                "connection_bindings": {},
            },
            "evaluation": {
                "dataset_id": str(dataset_id),
                "dataset_digest": dataset_digest,
                "thresholds": {"min_accuracy": "0.9"},
            },
            "compiler_version": COMPILER_VERSION_APP,
            "screens": draft["screens"],
            "app": {"generator_version": GENERATOR_VERSIONS[kind]},
        }
        if edit is not None:
            edit(manifest)
        release_digest = digest(manifest)
        o.execute(
            text(
                "INSERT INTO shifde.releases (tenant_id, id, solution_version_id, digest, "
                "manifest, compatibility, status, sealed_by) VALUES (:t, :i, :v, :rd, "
                "CAST(:m AS jsonb), '{}'::jsonb, 'sealed', :u)"
            ),
            {**values, "i": release_id, "v": sv, "rd": release_digest, "m": json.dumps(manifest)},
        )
        o.commit()
    rel = SealedRelease(
        tenant, release_id, release_digest, manifest, dataset_id, dataset_digest, None
    )
    run_id = insert_run(owner_db, rel, "passed", dataset_digest)
    return AppRelease(
        SealedRelease(
            tenant, release_id, release_digest, manifest, dataset_id, dataset_digest, run_id
        ),
        provider_id,
        agent_version,
        prompt,
        tools,
    )


def app_release_for_target(owner_db: Database, target_id: object, **options: Any) -> AppRelease:
    """A `/2` release in the target's workspace (`make_app_release` options)."""
    with owner_db.session() as o:
        tenant, workspace_id, _ = o.execute(text(TARGET_SCOPE_SQL), {"g": target_id}).one()
    return make_app_release(owner_db, tenant, workspace_id, **options)
