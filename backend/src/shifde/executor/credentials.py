"""The local credential broker (design 5, settled call 14; FDE SDK `shifde_executor.credentials`;
B-2689; SP6 design 6.4).

The deployment credential never enters SETH: the target stores only `credential_ref`, and the
executor's own file (`SHIFDE_EXECUTOR_CREDENTIALS_FILE`) maps it to an entry of the target's
platform: Databricks `{"tenant_id": "<organization id>", "host": "<https url>", "token":
"<secret>", "service_principal": "<workspace user name>"}` (`"platform": "databricks"` may be
given), or Snowflake `{"tenant_id", "host", "platform": "snowflake", "user", "role",
"private_key_file"}` (an unencrypted PKCS#8 PEM, read on every use and never kept), Fabric
`{"tenant_id", "host", "platform": "fabric", "entra_tenant_id", "client_id",
"client_secret_file"}`, Palantir `{"tenant_id", "host", "platform": "palantir", "client_id",
"client_secret_file"}` or Docker `{"tenant_id", "host", "platform": "docker"}` (SP8-1). A secret
file holds one value (surrounding whitespace stripped), read on every use and never kept; a
missing, unreadable or empty one is refused. A credentials file belongs to exactly one
organization (B-2737); a file whose entries name more than one is
refused (issue #155). A credential is handed out only for a grant that names this job, executor
and target, has not expired, whose audience is the entry's host normalized by its platform's
normalizer, for an entry of the target's platform whose `tenant_id` is the target's organization.
The installation's extra Palantir host suffixes (`palantir_host_suffixes`) count only when a
Palantir host is normalized. Every refusal is the same `CredentialUnavailable`, which names no
token, key, path, host, user or reference. On POSIX a credentials file, a Snowflake key file or a
secret file readable or writable by group or others is refused; Windows has no such mode bits and
is not checked.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal  # Any: one raw JSON entry
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from shifde.adapters.native.addresses import (
    AddressRefused,
    normalize_address,
    snowflake_claim_identity,
)
from shifde.adapters.native.contracts import CredentialGrant, utcnow
from shifde.adapters.native.palantir.clients import FoundryCredentials
from shifde.adapters.native.snowflake.clients import SnowflakeKeyPair

POSIX = os.name == "posix"
GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

PlatformKind = Literal["databricks", "snowflake", "fabric", "palantir", "docker"]


class CredentialUnavailable(PermissionError):
    """The grant does not match this job, or no usable local credential exists (fixed text)."""

    def __init__(self) -> None:
        super().__init__("the deployment credential is unavailable for this job")


class CredentialFileRefused(ValueError):
    """The credentials file is unreadable, malformed or too open (fixed text per reason)."""


class CredentialEntry(BaseModel):
    """A Databricks entry (an entry without `platform` is one)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: UUID
    host: str = Field(min_length=1)
    platform: Literal["databricks"] = "databricks"
    token: SecretStr
    service_principal: str = Field(min_length=1)


class SnowflakeEntry(BaseModel):
    """A Snowflake key-pair entry; no `repr` shows the key file's path."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: UUID
    host: str = Field(min_length=1)
    platform: Literal["snowflake"]
    user: str = Field(min_length=1)
    role: str = Field(min_length=1)
    private_key_file: str = Field(min_length=1, repr=False)


class FabricEntry(BaseModel):
    """A Fabric Entra client-credentials entry; no `repr` shows the secret file's path."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: UUID
    host: str = Field(min_length=1)
    platform: Literal["fabric"]
    entra_tenant_id: str = Field(pattern=GUID)
    client_id: str = Field(pattern=GUID)
    client_secret_file: str = Field(min_length=1, repr=False)


class PalantirEntry(BaseModel):
    """A Palantir Foundry OAuth2 client-credentials entry; no `repr` shows the secret file's
    path."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: UUID
    host: str = Field(min_length=1)
    platform: Literal["palantir"]
    client_id: str = Field(min_length=1)
    client_secret_file: str = Field(min_length=1, repr=False)


class DockerEntry(BaseModel):
    """A Docker daemon entry (`host` is `docker://<daemon id>`); it carries no secret."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: UUID
    host: str = Field(min_length=1)
    platform: Literal["docker"]


Entry = CredentialEntry | SnowflakeEntry | FabricEntry | PalantirEntry | DockerEntry
ENTRY_MODELS: dict[str, type[Entry]] = {
    "snowflake": SnowflakeEntry,
    "fabric": FabricEntry,
    "palantir": PalantirEntry,
    "docker": DockerEntry,
}


@dataclass(frozen=True)
class FabricCredentials:
    """A Fabric entry's Entra tenant and client ids and its secret; `repr` hides the secret."""

    entra_tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)


def _entry(raw: Any) -> Entry:  # Any: one raw JSON entry
    platform = raw.get("platform") if isinstance(raw, dict) else None
    model = ENTRY_MODELS.get(platform) if isinstance(platform, str) else None
    return (model or CredentialEntry).model_validate(raw)


def _secret(name: str) -> str:
    """The secret in file `name`, read now, surrounding whitespace stripped; a missing,
    unreadable or empty file, or on POSIX one open to group or others, is
    `CredentialUnavailable`."""
    path = Path(name)
    try:
        too_open = POSIX and bool(os.stat(path).st_mode & 0o077)
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise CredentialUnavailable from None
    if too_open or not value:
        raise CredentialUnavailable
    return value


def snowflake_account(host: str) -> str:
    """The account of a Snowflake host: the organization-account name of its claim identity
    (`snowflake_claim_identity`, B-3942), so the key pair names the account the target claims;
    `SnowflakeAddressRefused` for a host that does not reduce to one (an account-locator form)."""
    return snowflake_claim_identity(host).removeprefix("https://").split(".")[0]


class CredentialBroker:
    def __init__(
        self, entries: Mapping[str, Entry], *, palantir_host_suffixes: Iterable[str] = ()
    ) -> None:
        self._entries = dict(entries)
        self.palantir_host_suffixes = tuple(palantir_host_suffixes)

    def __repr__(self) -> str:
        return f"CredentialBroker({len(self._entries)} credentials)"

    @property
    def tenant_id(self) -> UUID | None:
        """The file's one organization (`None` for a file with no entries)."""
        return next((e.tenant_id for e in self._entries.values()), None)

    @classmethod
    def from_file(
        cls, path: Path, *, palantir_host_suffixes: Iterable[str] = ()
    ) -> CredentialBroker:
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
            entries = {str(ref): _entry(entry) for ref, entry in raw.items()}
        except ValidationError:
            # the validation error would echo the values, tokens included
            raise CredentialFileRefused("a credentials file entry is invalid") from None
        if len({e.tenant_id for e in entries.values()}) > 1:
            raise CredentialFileRefused("the credentials file must belong to one organization")
        return cls(entries, palantir_host_suffixes=palantir_host_suffixes)

    def _checked(
        self,
        grant: CredentialGrant,
        platform: PlatformKind,
        *,
        job_id: UUID,
        executor_id: UUID,
        target_id: UUID,
        tenant_id: UUID,
    ) -> Entry:
        """The local entry for a grant that names this job, executor and target, is unexpired,
        whose entry is of `platform` and of the target's organization (`tenant_id`), and whose
        audience is the entry's host normalized by that platform's normalizer; else
        `CredentialUnavailable`."""
        if (
            grant.job_id != job_id
            or grant.executor_id != executor_id
            or grant.target_id != target_id
            or grant.expires_at <= utcnow()
        ):
            raise CredentialUnavailable
        entry = self._entries.get(grant.credential_ref)
        if entry is None or entry.tenant_id != tenant_id or entry.platform != platform:
            raise CredentialUnavailable
        suffixes = self.palantir_host_suffixes if platform == "palantir" else ()
        try:
            host = normalize_address(platform, entry.host, suffixes)
        except AddressRefused:
            raise CredentialUnavailable from None
        if host != grant.audience:
            raise CredentialUnavailable
        return entry

    def entry_for(
        self,
        grant: CredentialGrant,
        *,
        job_id: UUID,
        executor_id: UUID,
        target_id: UUID,
        tenant_id: UUID,
    ) -> CredentialEntry:
        """The grant's Databricks entry (the checks of `_checked`), else `CredentialUnavailable`."""
        entry = self._checked(
            grant,
            "databricks",
            job_id=job_id,
            executor_id=executor_id,
            target_id=target_id,
            tenant_id=tenant_id,
        )
        if not isinstance(entry, CredentialEntry):
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

    def snowflake_entry_for(
        self,
        grant: CredentialGrant,
        *,
        job_id: UUID,
        executor_id: UUID,
        target_id: UUID,
        tenant_id: UUID,
    ) -> SnowflakeEntry:
        """The grant's Snowflake entry (the checks of `_checked`), else `CredentialUnavailable`."""
        entry = self._checked(
            grant,
            "snowflake",
            job_id=job_id,
            executor_id=executor_id,
            target_id=target_id,
            tenant_id=tenant_id,
        )
        if not isinstance(entry, SnowflakeEntry):
            raise CredentialUnavailable
        return entry

    def fabric_entry_for(
        self,
        grant: CredentialGrant,
        *,
        job_id: UUID,
        executor_id: UUID,
        target_id: UUID,
        tenant_id: UUID,
    ) -> FabricEntry:
        """The grant's Fabric entry (the checks of `_checked`), else `CredentialUnavailable`."""
        entry = self._checked(
            grant,
            "fabric",
            job_id=job_id,
            executor_id=executor_id,
            target_id=target_id,
            tenant_id=tenant_id,
        )
        if not isinstance(entry, FabricEntry):
            raise CredentialUnavailable
        return entry

    def fabric_credentials_for(
        self,
        grant: CredentialGrant,
        *,
        job_id: UUID,
        executor_id: UUID,
        target_id: UUID,
        tenant_id: UUID,
    ) -> FabricCredentials:
        """The grant's Fabric Entra tenant and client ids and its secret, the secret file read
        now (`_secret`), else `CredentialUnavailable`."""
        entry = self.fabric_entry_for(
            grant, job_id=job_id, executor_id=executor_id, target_id=target_id, tenant_id=tenant_id
        )
        return FabricCredentials(
            entra_tenant_id=entry.entra_tenant_id,
            client_id=entry.client_id,
            client_secret=_secret(entry.client_secret_file),
        )

    def palantir_entry_for(
        self,
        grant: CredentialGrant,
        *,
        job_id: UUID,
        executor_id: UUID,
        target_id: UUID,
        tenant_id: UUID,
    ) -> PalantirEntry:
        """The grant's Palantir entry (the checks of `_checked`), else `CredentialUnavailable`."""
        entry = self._checked(
            grant,
            "palantir",
            job_id=job_id,
            executor_id=executor_id,
            target_id=target_id,
            tenant_id=tenant_id,
        )
        if not isinstance(entry, PalantirEntry):
            raise CredentialUnavailable
        return entry

    def foundry_credentials_for(
        self,
        grant: CredentialGrant,
        *,
        job_id: UUID,
        executor_id: UUID,
        target_id: UUID,
        tenant_id: UUID,
    ) -> FoundryCredentials:
        """The grant's Foundry client credentials, the secret file read now (`_secret`), else
        `CredentialUnavailable`."""
        entry = self.palantir_entry_for(
            grant, job_id=job_id, executor_id=executor_id, target_id=target_id, tenant_id=tenant_id
        )
        return FoundryCredentials(
            client_id=entry.client_id, client_secret=_secret(entry.client_secret_file)
        )

    def docker_entry_for(
        self,
        grant: CredentialGrant,
        *,
        job_id: UUID,
        executor_id: UUID,
        target_id: UUID,
        tenant_id: UUID,
    ) -> DockerEntry:
        """The grant's Docker entry (the checks of `_checked`), else `CredentialUnavailable`."""
        entry = self._checked(
            grant,
            "docker",
            job_id=job_id,
            executor_id=executor_id,
            target_id=target_id,
            tenant_id=tenant_id,
        )
        if not isinstance(entry, DockerEntry):
            raise CredentialUnavailable
        return entry

    def key_pair_for(
        self,
        grant: CredentialGrant,
        *,
        job_id: UUID,
        executor_id: UUID,
        target_id: UUID,
        tenant_id: UUID,
    ) -> SnowflakeKeyPair:
        """The grant's Snowflake key pair, its key file read now (the account is the claim
        identity's organization-account name of the audience, the entry's normalized host; the
        organization is the file's one organization, checked equal to the target's). An audience
        that does not reduce to a claim identity, a missing or unreadable key file, or on POSIX
        one open to group or others, is `CredentialUnavailable`."""
        entry = self.snowflake_entry_for(
            grant, job_id=job_id, executor_id=executor_id, target_id=target_id, tenant_id=tenant_id
        )
        try:
            account = snowflake_account(grant.audience)
        except AddressRefused:
            raise CredentialUnavailable from None
        path = Path(entry.private_key_file)
        try:
            too_open = POSIX and bool(os.stat(path).st_mode & 0o077)
            pem = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise CredentialUnavailable from None
        if too_open:
            raise CredentialUnavailable
        return SnowflakeKeyPair(
            tenant_id=entry.tenant_id,
            host=grant.audience,
            account=account,
            user=entry.user,
            role=entry.role,
            private_key_pem=pem,
        )
