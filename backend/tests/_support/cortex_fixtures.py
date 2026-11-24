"""Reply shapes of Snowflake Cortex (SP6 design 6.1): example documents the in-process fake's
answers are compared with, key by key. The live run is the proof that Snowflake answers so."""

from __future__ import annotations

from typing import Any  # Any: JSON documents

CORTEX_COMPLETE_REPLY: dict[str, Any] = {
    "choices": [{"messages": "The North region has two customers."}],
    "created": 1767225600,
    "model": "mistral-large2",
    "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
}
"""`SNOWFLAKE.CORTEX.COMPLETE(model, messages, options)` with an options object: the one column
of the one row holds this JSON (the "options form" of the COMPLETE function reference)."""

CORTEX_CHAT_REPLY: dict[str, Any] = {
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "The North region has two customers."},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
}
"""`POST /api/v2/cortex/v1/chat/completions`: the OpenAI chat-completions reply (the part the
`openai_compatible` provider reads)."""


def key_structure(doc: Any) -> Any:  # Any: JSON
    """The keys of `doc`, nested, with every leaf replaced by its JSON type name."""
    if isinstance(doc, dict):
        return {k: key_structure(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [key_structure(v) for v in doc]
    return type(doc).__name__
