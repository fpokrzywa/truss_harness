"""The Marketplace deployment descriptor, the toolchain pin and the target bindings (Call S8-H).

The product version, object type, frontend asset digest and every RID are target bindings: a new
product version is a target change followed by a new plan, never part of the sealed release.

A delivered descriptor (`delivery_files`, SP8-7) also holds the release digest and the digest of
the target as the executor sees it: it is inside the artifact's digest, so the plan an approver
quotes covers it, and the executor refuses a plan whose target changed (`plan_stale`). Nothing here
makes a request; the API imports only this module of the package.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from shifde.adapters.native.addresses import public_https_origin
from shifde.adapters.native.contracts import (
    ApplicationSpec,
    FoundryMarketplaceSpec,
    Target,
    canonical_bytes,
    digest,
)

ADAPTER_VERSION = "0.1.0"
PALANTIR_PROFILE: Final = {
    "adapter": "0.1.0",
    "api": "v3-preview",
    "packager": "palantir-approved-product/v1",
}
TOOLCHAIN_DIGEST = digest(PALANTIR_PROFILE)
TERMINAL = "palantir-finalize"
# the spec of every delivered release: the bindings name the frontend origin and the asset's digest
DELIVERY_SPEC: Final = FoundryMarketplaceSpec(frontend_asset_path="index.html")
DESCRIPTOR: Final = "deployment.json"

OBJECT_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def palantir_spec(spec: ApplicationSpec | None) -> FoundryMarketplaceSpec:
    """`spec`, refused with `ValueError` unless it is a Marketplace spec."""
    if not isinstance(spec, FoundryMarketplaceSpec):
        raise ValueError("Marketplace delivery requires a palantir.marketplace/v1 spec")
    return spec


def rid(value: object, kind: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"ri\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\." + re.escape(kind) + r"\.[a-fA-F0-9-]{36}", value
    ):
        raise ValueError("invalid Foundry resource RID")
    try:
        UUID(value.rsplit(".", 1)[1])
    except ValueError:
        raise ValueError("invalid Foundry resource RID") from None
    return value


def object_type(value: object) -> str:
    if not isinstance(value, str) or not OBJECT_TYPE.fullmatch(value):
        raise ValueError("invalid Foundry object type")
    return value


@dataclass(frozen=True, kw_only=True)
class Bindings:
    marketplace_rid: str
    product_id: str
    version_id: str
    namespace_rid: str
    parent_folder_rid: str
    ontology_rid: str
    object_type: str
    frontend_origin: str
    frontend_asset_sha256: str
    allow_preview_apis: bool


def _uuid(value: str) -> str:
    return str(UUID(value))


def _sha256(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("invalid digest")
    return value


def _flag(value: str) -> bool:
    if value not in ("true", "false"):
        raise ValueError("invalid flag")
    return value == "true"


def bindings(target: Target) -> Bindings:
    """The target's Marketplace bindings, each validated; an error never echoes the value."""
    raw = target.bindings

    def read[T](key: str, parse: Callable[[str], T]) -> T:
        value = raw.get(key)
        try:
            if not value:
                raise ValueError("missing")
            return parse(value)
        except ValueError:
            raise ValueError(f"Foundry binding {key} is missing or invalid") from None

    return Bindings(
        marketplace_rid=read("marketplace_rid", lambda v: rid(v, "local")),
        product_id=read("product_id", _uuid),
        version_id=read("version_id", _uuid),
        namespace_rid=read("namespace_rid", lambda v: rid(v, "namespace")),
        parent_folder_rid=read("parent_folder_rid", lambda v: rid(v, "folder")),
        ontology_rid=read("ontology_rid", lambda v: rid(v, "ontology")),
        object_type=read("object_type", object_type),
        frontend_origin=read("frontend_origin", public_https_origin),
        frontend_asset_sha256=read("frontend_asset_sha256", _sha256),
        allow_preview_apis=read("allow_preview_apis", _flag),
    )


def files_for(spec: FoundryMarketplaceSpec) -> dict[str, bytes]:
    """The whole source: one immutable descriptor holding the canonical spec JSON."""
    spec = palantir_spec(spec)
    return {
        DESCRIPTOR: (
            json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    }


@dataclass(frozen=True, kw_only=True)
class Delivery:
    """What a delivered descriptor binds a plan to."""

    bindings_digest: str
    release_digest: str


def delivery_files(
    *, bindings_digest: str, release_digest: str, spec: FoundryMarketplaceSpec = DELIVERY_SPEC
) -> dict[str, bytes]:
    """The delivered source: one descriptor of exactly the spec, the digest of the target the plan
    is for and the release's digest, as canonical JSON."""
    value = {
        "spec": palantir_spec(spec).model_dump(mode="json"),
        "bindings_digest": _sha256(bindings_digest),
        "release_digest": _sha256(release_digest),
    }
    return {DESCRIPTOR: canonical_bytes(value) + b"\n"}


def read_delivery(
    files: dict[str, bytes], spec: FoundryMarketplaceSpec = DELIVERY_SPEC
) -> Delivery:
    """The digests of a delivered source of `spec`; `ValueError` for any other source (a spec-only
    descriptor included): the files must be byte for byte what `delivery_files` writes."""
    try:
        value = json.loads(files[DESCRIPTOR])
        found = Delivery(
            bindings_digest=value["bindings_digest"], release_digest=value["release_digest"]
        )
        expected = delivery_files(
            bindings_digest=found.bindings_digest, release_digest=found.release_digest, spec=spec
        )
    except (KeyError, TypeError, ValueError):
        raise ValueError("Marketplace source is not a delivery descriptor") from None
    if files != expected:
        raise ValueError("Marketplace source is not a delivery descriptor")
    return found


def validate_source(files: dict[str, bytes], spec: FoundryMarketplaceSpec) -> None:
    """The source is the spec-only descriptor or a delivered one of `spec`, byte for byte."""
    if files == files_for(spec):
        return
    try:
        read_delivery(files, spec)
    except ValueError:
        raise ValueError(
            "Marketplace source must be the immutable approved-product descriptor"
        ) from None


def native_id(host: str, installation: str) -> str:
    return "palantir:" + digest(host) + ":" + rid(installation, "block-set-installation")


def installation_suffix(application_id: UUID, target_id: UUID) -> str:
    return "shifde-" + digest({"app": str(application_id), "target": str(target_id)})[:16]
