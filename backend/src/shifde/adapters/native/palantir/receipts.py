"""Proof checks for the Marketplace profile: the pinned job payload, checkpoints and receipts.

A receipt or checkpoint from the in-process fake also carries the handle `platform: fake`; a
receipt for the real platform never does.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal  # Any: JSON job payloads and journal receipts
from uuid import UUID

from shifde.adapters.native.contracts import (
    ApplyReceipt,
    DeploymentPlan,
    FoundryMarketplaceSpec,
    NativeStepReceipt,
    Target,
    digest,
)
from shifde.adapters.native.palantir.source import bindings

CREATE = "palantir-installation-create"
SUBMIT = "palantir-job-submit"
RECEIPT_HANDLES = frozenset(
    {"installation", "job_id", "request_digest", "schema_digest", "spec_digest"}
)
_SHA256 = re.compile(r"[a-f0-9]{64}")


def job_payload(installation: str, version_id: str) -> dict[str, Any]:
    """The job request: always pinned to the target's `version_id`, never a "latest" version."""
    return {
        "installations": [
            {
                "installationRid": installation,
                "targetVersion": {"type": "versionId", "versionId": str(UUID(version_id))},
                "inputs": {"values": {}, "explicitlyUnset": []},
            }
        ],
        "recommendations": [],
        "submissionMode": {"type": "submit", "errorTolerance": "NONE", "errorHandling": "ABORT"},
    }


def _marketplace_rid(value: object, kind: str, message: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"ri\.marketplace\.[A-Za-z0-9_-]+\." + re.escape(kind) + r"\.[a-fA-F0-9-]{36}", value
    ):
        raise ValueError(message)
    try:
        UUID(value.rsplit(".", 1)[1])
    except ValueError:
        raise ValueError(message) from None
    return value


def installation_rid(value: object) -> str:
    return _marketplace_rid(value, "block-set-installation", "invalid Marketplace installation RID")


def job_rid(value: object) -> str:
    return _marketplace_rid(value, "installation-job", "invalid installation job RID")


def _with_platform(allowed: set[str], handles: dict[str, str], platform: str) -> set[str]:
    if platform == "fake":
        if handles.get("platform") != "fake":
            raise ValueError("a fake Marketplace proof must carry platform: fake")
        return allowed | {"platform"}
    return allowed


def validate_palantir_step(
    step: NativeStepReceipt, target: Target, *, platform: Literal["palantir", "fake"] = "palantir"
) -> None:
    if step.step_key not in (CREATE, SUBMIT):
        raise ValueError("unexpected Marketplace checkpoint")
    allowed = {"installation"} if step.step_key == CREATE else {"installation", "job_id"}
    if step.step_key == SUBMIT:
        allowed.add("request_digest")
    if set(step.handles) != _with_platform(allowed, step.handles, platform):
        raise ValueError("unexpected Marketplace checkpoint handles")
    for key, value in step.handles.items():
        if key == "installation":
            installation_rid(value)
        elif key == "job_id":
            job_rid(value)
        elif key == "request_digest" and not _SHA256.fullmatch(value):
            raise ValueError("invalid native request digest")
    if len(step.resources) != 1:
        raise ValueError("Marketplace checkpoint requires exactly one installation")
    binding = step.resources[0]
    prefix = "palantir:" + digest(target.host) + ":"
    if (
        binding.key != "installation"
        or binding.resource_type != "palantir.marketplace-installation"
        or not binding.native_id.startswith(prefix)
    ):
        raise ValueError("Marketplace resource binding mismatch")
    identity = installation_rid(binding.native_id.removeprefix(prefix))
    if step.handles.get("installation") != identity:
        raise ValueError("Marketplace installation handle mismatch")


def validate_palantir_receipt(
    receipt: ApplyReceipt,
    spec: FoundryMarketplaceSpec,
    target: Target,
    *,
    platform: Literal["palantir", "fake"] = "palantir",
    completed: Callable[[str], dict[str, Any] | None] | None = None,
    plan: DeploymentPlan | None = None,
) -> None:
    if not isinstance(spec, FoundryMarketplaceSpec):
        raise ValueError("Marketplace receipt profile mismatch")
    handles = receipt.operation_handles
    if set(handles) != _with_platform(set(RECEIPT_HANDLES), handles, platform):
        raise ValueError("Marketplace receipt handle mismatch")
    if handles["spec_digest"] != digest(spec) or not _SHA256.fullmatch(handles["schema_digest"]):
        raise ValueError("Marketplace receipt specification mismatch")
    step_handles = {key: handles[key] for key in ("installation", "job_id", "request_digest")}
    if platform == "fake":
        step_handles["platform"] = "fake"
    step = NativeStepReceipt(
        step_key=SUBMIT, plan_digest="0" * 64, resources=receipt.resources, handles=step_handles
    )
    validate_palantir_step(step, target, platform=platform)
    if handles["request_digest"] != digest(
        job_payload(handles["installation"], bindings(target).version_id)
    ):
        raise ValueError("Marketplace receipt native request mismatch")
    if plan and handles["schema_digest"] != plan.details.get("schema_digest"):
        raise ValueError("Marketplace receipt schema differs from plan")
    if any(
        item.application_id != receipt.application_id or item.target_id != target.id
        for item in receipt.resources
    ):
        raise ValueError("Marketplace receipt ownership mismatch")
    if completed:
        for key in (CREATE, SUBMIT):
            raw = completed(key)
            if not raw:
                raise ValueError("Marketplace native checkpoint missing")
            saved = NativeStepReceipt.model_validate(raw)
            validate_palantir_step(saved, target, platform=platform)
            if (
                saved.resources != receipt.resources
                or saved.handles["installation"] != handles["installation"]
            ):
                raise ValueError("Marketplace checkpoint identity mismatch")
            if key == SUBMIT and saved.handles != step.handles:
                raise ValueError("Marketplace job checkpoint mismatch")
