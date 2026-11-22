"""The local credential broker (design 5, settled call 14; FDE SDK `shifde_executor.credentials`,
Databricks tokens only; B-2689).

The deployment credential never enters SETH: the target stores only `credential_ref`, and the
executor's own file (`SHIFDE_EXECUTOR_CREDENTIALS_FILE`) maps it to
`{"tenant_id": "<organization id>", "host": "<https url>", "token": "<secret>",
"service_principal": "<workspace user name>"}`. A credentials file belongs to exactly one
organization (B-2737); a file whose entries name more than one is refused (issue #155). A token
is handed out only for a grant that names this job, executor and target, has not expired, whose
audience is the normalized host of the file's entry, and whose target belongs to the entry's
`tenant_id`. Every refusal is the same
`CredentialUnavailable`, which names no token, host or reference. On POSIX a file readable or
writable by group or others is refused; Windows has no such mode bits and is not checked.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from shifde.adapters.native.addresses import AddressRefused, normalize_databricks_address
from shifde.adapters.native.contracts import CredentialGrant, utcnow

POSIX = os.name == "posix"


class CredentialUnavailable(PermissionError):
    """The grant does not match this job, or no usable local credential exists (fixed text)."""

    def __init__(self) -> None:
        super().__init__("the deployment credential is unavailable for this job")


class CredentialFileRefused(ValueError):
    """The credentials file is unreadable, malformed or too open (fixed text per reason)."""


class CredentialEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: UUID
    host: str = Field(min_length=1)
    token: SecretStr
    service_principal: str = Field(min_length=1)


class CredentialBroker:
    def __init__(self, entries: Mapping[str, CredentialEntry]) -> None:
        self._entries = dict(entries)

    def __repr__(self) -> str:
        return f"CredentialBroker({len(self._entries)} credentials)"

    @property
    def tenant_id(self) -> UUID | None:
        """The file's one organization (`None` for a file with no entries)."""
        return next((e.tenant_id for e in self._entries.values()), None)

    @classmethod
    def from_file(cls, path: Path) -> CredentialBroker:
        try:
            too_open = POSIX and bool(os.stat(path).st_mode & 0o077)
            text = path.read_text(encoding="utf-8")
        except OSError:
            raise CredentialFileRefused("the credentials file cannot be read") from None
        if too_open:
            raise CredentialFileRefused(
                "the credentials file must not be readable or writable by group or others"
            )
        try:
            raw = json.loads(text)
        except ValueError:
            raise CredentialFileRefused("the credentials file is not valid JSON") from None
        if not isinstance(raw, dict):
            raise CredentialFileRefused("the credentials file must map references to entries")
        try:
            entries = {
                str(ref): CredentialEntry.model_validate(entry) for ref, entry in raw.items()
            }
        except ValidationError:
            # the validation error would echo the values, tokens included
            raise CredentialFileRefused("a credentials file entry is invalid") from None
        if len({e.tenant_id for e in entries.values()}) > 1:
            raise CredentialFileRefused("the credentials file must belong to one organization")
        return cls(entries)

    def entry_for(
        self,
        grant: CredentialGrant,
        *,
        job_id: UUID,
        executor_id: UUID,
        target_id: UUID,
        tenant_id: UUID,
    ) -> CredentialEntry:
        """The local entry for a grant that names this job, executor and target, is unexpired,
        whose audience is the entry's normalized host, and whose target's organization is the
        entry's `tenant_id`; else `CredentialUnavailable`."""
        if (
            grant.job_id != job_id
            or grant.executor_id != executor_id
            or grant.target_id != target_id
            or grant.expires_at <= utcnow()
        ):
            raise CredentialUnavailable
        entry = self._entries.get(grant.credential_ref)
        if entry is None or entry.tenant_id != tenant_id:
            raise CredentialUnavailable
        try:
            host = normalize_databricks_address(entry.host)
        except AddressRefused:
            raise CredentialUnavailable from None
        if host != grant.audience:
            raise CredentialUnavailable
        return entry

    def token_for(
        self,
        grant: CredentialGrant,
        *,
        job_id: UUID,
        executor_id: UUID,
        target_id: UUID,
        tenant_id: UUID,
    ) -> str:
        entry = self.entry_for(
            grant, job_id=job_id, executor_id=executor_id, target_id=target_id, tenant_id=tenant_id
        )
        return entry.token.get_secret_value()
