"""Domain error model; rendered as RFC 9457 problem+json by the API layer."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for errors with a stable machine-readable `code` and HTTP status."""

    status = 500
    code = "internal_error"
    title = "Internal Server Error"

    def __init__(self, detail: str | None = None, *, errors: list[dict[str, Any]] | None = None):
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.errors = errors


class Unauthenticated(AppError):
    status, code, title = 401, "unauthenticated", "Unauthorized"


class InvalidCredentials(AppError):
    status, code, title = 401, "invalid_credentials", "Unauthorized"


class UnsupportedAuthScheme(AppError):
    status, code, title = 401, "unsupported_auth_scheme", "Unauthorized"


class CredentialRevoked(AppError):
    status, code, title = 401, "credential_revoked", "Unauthorized"


class CredentialExpired(AppError):
    status, code, title = 401, "credential_expired", "Unauthorized"


class Forbidden(AppError):
    status, code, title = 403, "forbidden", "Forbidden"


class CsrfFailed(AppError):
    status, code, title = 403, "csrf_failed", "Forbidden"


class AuthDisabled(AppError):
    status, code, title = 403, "auth_disabled", "Forbidden"


class NotFound(AppError):
    status, code, title = 404, "not_found", "Not Found"


class Conflict(AppError):
    status, code, title = 409, "conflict", "Conflict"


class IssuerConflict(AppError):
    status, code, title = 409, "issuer_conflict", "Conflict"


class ScopeArchived(AppError):
    status, code, title = 409, "scope_archived", "Conflict"


class ValidationFailed(AppError):
    status, code, title = 422, "validation_error", "Unprocessable Entity"


class IdempotencyKeyReserved(AppError):
    """A client's `Idempotency-Key` must not start with the platform's own run key prefix (#373)."""

    status, code, title = 422, "idempotency_key_reserved", "Unprocessable Entity"


class RateLimited(AppError):
    status, code, title = 429, "rate_limited", "Too Many Requests"

    def __init__(
        self,
        detail: str | None = None,
        *,
        errors: list[dict[str, Any]] | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(detail, errors=errors)
        self.retry_after = retry_after  # seconds; rendered as `Retry-After` when set


# ---------------------------------------------------------------- P3: integrations
class AdapterNotImplemented(AppError):
    status, code, title = 422, "adapter_not_implemented", "Unprocessable Entity"


class EgressDenied(AppError):
    status, code, title = 422, "egress_denied", "Unprocessable Entity"


class ConnectionDisabled(AppError):
    status, code, title = 409, "connection_disabled", "Conflict"


class SecretScopeMismatch(AppError):
    """A connection's secret must have exactly the connection's scope (#156 rule 4.4)."""

    status, code, title = 422, "secret_scope_mismatch", "Unprocessable Entity"


class EgressPolicyMismatch(AppError):
    """A connection's egress policy must belong to the connection's client."""

    status, code, title = 422, "egress_policy_mismatch", "Unprocessable Entity"


class VendorNotVisible(AppError):
    """A connection's or model provider's vendor must be global or the client's own."""

    status, code, title = 422, "vendor_not_visible", "Unprocessable Entity"


class ConnectionTestBusy(AppError):
    """No active test job could be queued or read back (ARCH M2): retryable."""

    status, code, title = 409, "connection_test_busy", "Conflict"


class ConnectionDiscoveryBusy(AppError):
    """No active discovery job could be queued or read back (ARCH M2): retryable."""

    status, code, title = 409, "connection_discovery_busy", "Conflict"


class ProviderJobBusy(AppError):
    """No active provider health check or discovery job could be queued or read back (ARCH M2):
    retryable."""

    status, code, title = 409, "provider_job_busy", "Conflict"


class ModelUnavailable(AppError):
    status, code, title = 409, "model_unavailable", "Conflict"


class ModelNotAllowed(AppError):
    status, code, title = 403, "model_not_allowed", "Forbidden"


class BudgetExceeded(AppError):
    status, code, title = 403, "budget_denied", "Forbidden"


class ModelRateLimited(AppError):
    status, code, title = 429, "model_rate_limited", "Too Many Requests"


class UseModelsEndpoint(AppError):
    status, code, title = 422, "use_model_invoke_endpoint", "Unprocessable Entity"


class UpstreamError(AppError):
    status, code, title = 502, "upstream_error", "Bad Gateway"


# ---------------------------------------------------------------- P4: build artifacts
class PublishPending(AppError):
    status, code, title = 409, "publish_pending", "Conflict"


class InvalidTransition(AppError):
    status, code, title = 409, "invalid_transition", "Conflict"


class SideEffectsNotAcknowledged(AppError):
    status, code, title = 422, "side_effects_not_acknowledged", "Unprocessable Entity"


# ---------------------------------------------------------------- P5: execution harness
class SeparationOfDuties(Forbidden):
    code = "separation_of_duties"


class IdempotencyConflict(Conflict):
    code = "idempotency_conflict"


class AlreadyDecided(Conflict):
    code = "already_decided"


class SchedulingNotEnabled(AppError):
    """Pipeline schedules are an extension point in the MVP (OD-8): the API answers 501 until the
    deployment sets SHIFDE_FEATURE_PIPELINE_SCHEDULES=true."""

    status, code, title = 501, "scheduling_not_enabled", "Not Implemented"
