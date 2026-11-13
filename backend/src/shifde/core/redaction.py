"""Shared secret redaction (SECURITY_MODEL section 5).

Three layers: (1) a registry of exact secret values that were resolved/created in this process,
scrubbed wherever they would be rendered; (2) pattern rules (bearer tokens, `shf_` API keys, JWTs,
PEM blocks, cloud key shapes, connection-string passwords); (3) a field-name denylist.
Used by the log processor, problem+json rendering and audit `details`.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Any  # Any: recursive walk over arbitrary JSON-like payloads

REDACTED = "[REDACTED]"
_MAX_REGISTERED = 2048
_MIN_LEN = 6

_registry: OrderedDict[str, None] = OrderedDict()
_lock = threading.Lock()

# Field names that are always masked, except identifier / timestamp style names (`key_id`, ...).
_SENSITIVE_KEY = re.compile(r"pass(word)?|secret|token|authorization|cookie|csrf|api[_-]?key", re.I)
_SAFE_KEY_SUFFIX = re.compile(r"(_id|_ids|_at|_count|_set)$", re.I)

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bshf_(?:dev|test|prod)_[A-Za-z0-9]{8}_[A-Za-z0-9]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"),
    re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
)
_CONN_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^:/\s@]+:)([^@\s]+)(@)", re.I)


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key)) and not _SAFE_KEY_SUFFIX.search(key)


def register_secret(value: str | bytes | None) -> None:
    """Remember a plaintext so any later rendering of it is masked (bounded, process-local)."""
    if value is None:
        return
    text = value.decode("utf-8", "ignore") if isinstance(value, bytes) else value
    if len(text) < _MIN_LEN:
        return
    with _lock:
        _registry[text] = None
        _registry.move_to_end(text)
        while len(_registry) > _MAX_REGISTERED:
            _registry.popitem(last=False)


def scrub_text(text: str) -> str:
    with _lock:
        known = list(_registry)
    for value in known:
        if value in text:
            text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _CONN_PASSWORD.sub(rf"\1{REDACTED}\3", text)


def scrub(value: Any) -> Any:  # Any: recursive walk over arbitrary JSON-like payloads
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and is_sensitive_key(k) else scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    return value
