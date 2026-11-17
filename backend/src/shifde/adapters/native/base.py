"""The native adapter protocol, its journal, artifact store and contexts (FDE SDK `shifde_core`).

Uncertain native writes require explicit reconciliation: a step whose intent was recorded but whose
receipt never was raises `NeedsAttention` instead of being written again.
"""

from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol  # Any: JSON step inputs and receipts
from uuid import UUID, uuid4

from shifde.adapters.native.contracts import (
    ApplyReceipt,
    ApplyRequest,
    BuildArtifact,
    BuildRequest,
    CapabilityReport,
    DeploymentPlan,
    EnvironmentSnapshot,
    InspectRequest,
    JournalEntry,
    NativeObservation,
    ObserveRequest,
    PlanRequest,
    ReconcileRequest,
    ReconciliationResult,
    ResourceBinding,
    ScaffoldRequest,
    SourceArchive,
    Target,
    ValidateRequest,
    ValidationReport,
    VerificationReport,
    VerifyRequest,
    digest,
    relative_path,
    utcnow,
)

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_FILES = 500


class NeedsAttention(RuntimeError):
    pass


# --------------------------------------------------------------------------- journal
class Journal(Protocol):
    tenant_id: UUID
    operation_id: UUID

    def intent(self, key: str, inputs: dict[str, Any]) -> bool: ...
    def complete(self, key: str, receipt: dict[str, Any]) -> None: ...
    def completed(self, key: str) -> dict[str, Any] | None: ...
    def has_intent(self, key: str) -> bool: ...


class InMemoryJournal:
    """Step journal with the SDK `StepJournal` semantics, held in memory."""

    def __init__(self, tenant_id: UUID, operation_id: UUID) -> None:
        self.tenant_id, self.operation_id = tenant_id, operation_id
        self.steps: dict[str, JournalEntry] = {}

    def intent(self, key: str, inputs: dict[str, Any]) -> bool:
        previous = self.steps.get(key)
        fingerprint = digest(inputs)
        if previous:
            if previous.input_digest != fingerprint:
                raise ValueError("step inputs changed")
            if previous.state != "complete":
                raise NeedsAttention(f"step {key} has an uncertain native outcome")
            return False
        self.steps[key] = JournalEntry(step_key=key, input_digest=fingerprint, state="intent")
        return True

    def complete(self, key: str, receipt: dict[str, Any]) -> None:
        entry = self.steps[key]
        if entry.state == "complete" and entry.receipt != receipt:
            raise ValueError("conflicting step receipt")
        self.steps[key] = entry.model_copy(update={"state": "complete", "receipt": receipt})

    def completed(self, key: str) -> dict[str, Any] | None:
        entry = self.steps.get(key)
        return entry.receipt if entry and entry.state == "complete" else None

    def has_intent(self, key: str) -> bool:
        return key in self.steps


# --------------------------------------------------------------------------- artifacts
class ArtifactStore(Protocol):
    def put(self, tenant_id: UUID, data: bytes) -> SourceArchive: ...
    def read(self, reference: SourceArchive, tenant_id: UUID) -> bytes: ...


def read_archive(data: bytes) -> dict[str, bytes]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds the size limit")
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError("expanded archive exceeds the limit")
        seen: set[str] = set()
        for info in entries:
            # ZipInfo normalizes backslashes on Windows. Validate the original name first.
            name = info.orig_filename.rstrip("/")
            relative_path(name)
            if name == ".":
                raise ValueError("archive entry must name a file or directory")
            if name.casefold() in seen:
                raise ValueError("duplicate archive entry")
            seen.add(name.casefold())
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("archive symlinks are forbidden")
            if info.is_dir():
                continue
            files[name] = archive.read(info)
    return files


def create_archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            relative_path(name)
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    result = output.getvalue()
    read_archive(result)
    return result


class InMemoryArtifactStore:
    """Content-verified artifacts held in memory, keyed by tenant and archive id."""

    def __init__(self) -> None:
        self.archives: dict[tuple[UUID, UUID], bytes] = {}

    def put(self, tenant_id: UUID, data: bytes) -> SourceArchive:
        read_archive(data)
        reference = SourceArchive(
            tenant_id=tenant_id, digest=hashlib.sha256(data).hexdigest(), size=len(data)
        )
        self.archives[(tenant_id, reference.id)] = data
        return reference

    def read(self, reference: SourceArchive, tenant_id: UUID) -> bytes:
        if reference.tenant_id != tenant_id:
            raise PermissionError("artifact belongs to another tenant")
        data = self.archives.get((tenant_id, reference.id))
        if data is None:
            raise FileNotFoundError("artifact not found")
        if hashlib.sha256(data).hexdigest() != reference.digest or len(data) != reference.size:
            raise ValueError("artifact digest or size mismatch")
        read_archive(data)
        return data


# --------------------------------------------------------------------------- contexts
@dataclass(frozen=True, kw_only=True)
class BuildContext:
    tenant_id: UUID
    artifacts: ArtifactStore
    toolchain_digest: str
    operation_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, kw_only=True)
class ReadContext:
    tenant_id: UUID
    target: Target
    owned_resources: tuple[ResourceBinding, ...] = ()
    operation_id: UUID = field(default_factory=uuid4)
    deadline: datetime = field(default_factory=lambda: utcnow() + timedelta(hours=1))

    def __post_init__(self) -> None:
        if self.target.tenant_id != self.tenant_id:
            raise PermissionError("target belongs to another tenant")

    def check_deadline(self) -> None:
        if utcnow() >= self.deadline:
            raise TimeoutError("operation deadline exceeded")


@dataclass(frozen=True, kw_only=True)
class DeployContext(ReadContext):
    artifacts: ArtifactStore
    journal: Journal
    toolchain_digest: str
    write_guard: Callable[[], None] | None = None

    def check_deadline(self) -> None:
        super().check_deadline()
        if self.write_guard:
            self.write_guard()

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            self.journal.tenant_id != self.tenant_id
            or self.journal.operation_id != self.operation_id
        ):
            raise PermissionError("journal context mismatch")


# --------------------------------------------------------------------------- adapter
class NativeAdapter(Protocol):
    """The ten operations every native delivery adapter implements."""

    def capabilities(self, ctx: ReadContext) -> CapabilityReport: ...
    def inspect(self, request: InspectRequest, ctx: ReadContext) -> EnvironmentSnapshot: ...
    def scaffold(self, request: ScaffoldRequest, ctx: BuildContext) -> SourceArchive: ...
    def validate(self, request: ValidateRequest, ctx: BuildContext) -> ValidationReport: ...
    def build(self, request: BuildRequest, ctx: BuildContext) -> BuildArtifact: ...
    def plan(self, request: PlanRequest, ctx: ReadContext) -> DeploymentPlan: ...
    def apply(self, request: ApplyRequest, ctx: DeployContext) -> ApplyReceipt: ...
    def observe(self, request: ObserveRequest, ctx: ReadContext) -> NativeObservation: ...
    def reconcile(self, request: ReconcileRequest, ctx: ReadContext) -> ReconciliationResult: ...
    def verify(self, request: VerifyRequest, ctx: ReadContext) -> VerificationReport: ...
