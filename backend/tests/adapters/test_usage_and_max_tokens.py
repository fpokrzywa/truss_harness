"""R3 cost, adapters part (findings Item 9, Item 11, ARCH m13): the call is capped by the
`max_tokens` the caller asked for, and the usage a provider omits is reported as omitted, never as
zero tokens; what the provider does return, cached tokens included, is parsed."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from shifde.adapters.base import (
    AdapterFailure,
    ChatMessage,
    ChatRequest,
    ConnectionConfig,
    ErrorClass,
    Usage,
)
from shifde.adapters.model_providers import AnthropicProvider, OpenAICompatibleProvider

MSGS = (ChatMessage("user", "hi"),)


def _conn(adapter_key: str) -> ConnectionConfig:
    return ConnectionConfig(None, "c", adapter_key, {"base_url": "https://p.example/v1"})


def _stub(adapter: Any, doc: dict[str, Any], sent: list[dict[str, Any]]) -> None:
    def _request(
        ctx: Any, conn: Any, method: str, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        assert body is not None
        sent.append(body)
        return httpx.Response(200, json=doc)

    adapter._request = _request


OPENAI_DOC: dict[str, Any] = {"choices": [{"message": {"content": "ok"}}]}
ANTHROPIC_DOC: dict[str, Any] = {"content": [{"type": "text", "text": "ok"}]}


def _openai(usage: Any, sent: list[dict[str, Any]]) -> OpenAICompatibleProvider:
    adapter = OpenAICompatibleProvider()
    doc = dict(OPENAI_DOC) if usage is None else {**OPENAI_DOC, "usage": usage}
    _stub(adapter, doc, sent)
    return adapter


def _anthropic(usage: Any, sent: list[dict[str, Any]]) -> AnthropicProvider:
    adapter = AnthropicProvider()
    doc = dict(ANTHROPIC_DOC) if usage is None else {**ANTHROPIC_DOC, "usage": usage}
    _stub(adapter, doc, sent)
    return adapter


# ------------------------------------------------------------------ Item 9: max_tokens
def test_max_tokens_is_sent_to_both_providers_as_asked() -> None:
    sent: list[dict[str, Any]] = []
    _openai({}, sent).generate(None, _conn("openai_compatible"), "m", ChatRequest(MSGS, 77))  # type: ignore[arg-type]
    _anthropic({}, sent).generate(None, _conn("anthropic"), "m", ChatRequest(MSGS, 77))  # type: ignore[arg-type]
    assert [p["max_tokens"] for p in sent] == [77, 77]


@pytest.mark.parametrize("bad", [0, -5])
@pytest.mark.parametrize("kind", ["openai_compatible", "anthropic"])
def test_a_zero_or_negative_max_tokens_is_refused_not_replaced_by_a_default(
    kind: str, bad: int
) -> None:
    sent: list[dict[str, Any]] = []
    adapter = _openai({}, sent) if kind == "openai_compatible" else _anthropic({}, sent)
    with pytest.raises(AdapterFailure) as info:
        adapter.generate(None, _conn(kind), "m", ChatRequest(MSGS, bad))  # type: ignore[arg-type]
    assert info.value.error_class is ErrorClass.CONFIG
    assert sent == []  # nothing reached the provider


@pytest.mark.parametrize("bad", ["100", 1.5, True, [1]])
def test_the_invoke_session_refuses_a_max_tokens_that_is_not_a_positive_integer(bad: Any) -> None:
    sent: list[dict[str, Any]] = []
    session = _openai({}, sent).open(None, _conn("openai_compatible"))  # type: ignore[arg-type]
    request = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": bad}
    with pytest.raises(AdapterFailure) as info:
        session.invoke(request)  # type: ignore[attr-defined]
    assert info.value.error_class is ErrorClass.CONFIG and sent == []


def test_the_invoke_session_forwards_max_tokens_and_reports_usage_with_gaps() -> None:
    sent: list[dict[str, Any]] = []
    session = _openai({"prompt_tokens": 9}, sent).open(None, _conn("openai_compatible"))  # type: ignore[arg-type]
    request = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 33}
    result = session.invoke(request)  # type: ignore[attr-defined]
    assert sent[0]["max_tokens"] == 33
    assert result.result["usage"]["input_tokens"] == 9
    assert result.result["usage"]["output_tokens"] is None


# ------------------------------------------------------------------ Item 11: omitted usage
@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": None, "completion_tokens": "x"}])
def test_omitted_usage_is_not_zero_tokens_openai(usage: Any) -> None:
    resp = _openai(usage, []).generate(None, _conn("openai_compatible"), "m", ChatRequest(MSGS))  # type: ignore[arg-type]
    assert resp.usage == Usage(None, None, None)
    assert not resp.usage.reported


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": None, "output_tokens": -3}])
def test_omitted_usage_is_not_zero_tokens_anthropic(usage: Any) -> None:
    resp = _anthropic(usage, []).generate(None, _conn("anthropic"), "m", ChatRequest(MSGS))  # type: ignore[arg-type]
    assert resp.usage == Usage(None, None, None)
    assert not resp.usage.reported


def test_a_real_zero_is_kept_and_partial_usage_is_parsed() -> None:
    zero = _openai({"prompt_tokens": 4, "completion_tokens": 0}, []).generate(
        None,  # type: ignore[arg-type]
        _conn("openai_compatible"),
        "m",
        ChatRequest(MSGS),
    )
    assert zero.usage == Usage(4, 0, None) and zero.usage.reported
    partial = _anthropic({"output_tokens": 12}, []).generate(
        None,  # type: ignore[arg-type]
        _conn("anthropic"),
        "m",
        ChatRequest(MSGS),
    )
    assert partial.usage == Usage(None, 12, None) and not partial.usage.reported


# ------------------------------------------------------------------ ARCH m13: cached tokens
def test_cached_input_tokens_are_parsed() -> None:
    oai = _openai(
        {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 64},
        },
        [],
    ).generate(None, _conn("openai_compatible"), "m", ChatRequest(MSGS))  # type: ignore[arg-type]
    assert oai.usage == Usage(100, 5, 64)
    ant = _anthropic(
        {"input_tokens": 7, "output_tokens": 5, "cache_read_input_tokens": 1000}, []
    ).generate(None, _conn("anthropic"), "m", ChatRequest(MSGS))  # type: ignore[arg-type]
    assert ant.usage == Usage(7, 5, 1000)
