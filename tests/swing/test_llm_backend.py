from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from src.swing.llm_backend import AnthropicStructuredBackend, estimate_cost


class _EchoSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class _FakeRaw:
    def __init__(self, usage: dict):
        self.usage_metadata = usage


class _FakeStructuredRunnable:
    def __init__(self, response: dict):
        self._response = response

    def invoke(self, messages):
        return self._response


class _FakeChatAnthropic:
    def __init__(self, model: str, api_key: str):
        self.model = model
        self.api_key = api_key
        self.response: dict | None = None

    def with_structured_output(self, schema, include_raw=True):
        assert include_raw is True
        assert self.response is not None
        return _FakeStructuredRunnable(self.response)


def _install_fake_anthropic(monkeypatch, response: dict):
    created: list[_FakeChatAnthropic] = []

    def factory(model, api_key):
        client = _FakeChatAnthropic(model, api_key)
        client.response = response
        created.append(client)
        return client

    monkeypatch.setattr("langchain_anthropic.ChatAnthropic", factory)
    return created


def test_complete_parses_result_and_records_cost(monkeypatch, database):
    response = {
        "raw": _FakeRaw({"input_tokens": 1000, "output_tokens": 200}),
        "parsed": _EchoSchema(value="ok"),
        "parsing_error": None,
    }
    _install_fake_anthropic(monkeypatch, response)
    backend = AnthropicStructuredBackend(database, api_key="test-key")

    result = backend.complete(
        role="technical",
        model_name="claude-sonnet-5",
        payload={"candidate": {"candidate_id": "cand-1"}},
        schema=_EchoSchema,
    )

    assert result.value == "ok"
    rows = database.rows("SELECT * FROM model_costs")
    assert len(rows) == 1
    assert rows[0]["role"] == "technical"
    assert rows[0]["model_name"] == "claude-sonnet-5"
    assert rows[0]["candidate_id"] == "cand-1"
    assert rows[0]["prompt_tokens"] == 1000
    assert rows[0]["completion_tokens"] == 200
    assert rows[0]["cost"] == estimate_cost("claude-sonnet-5", 1000, 200)
    assert rows[0]["cost"] > 0


def test_complete_raises_on_parsing_error_and_records_no_cost(monkeypatch, database):
    response = {"raw": _FakeRaw({"input_tokens": 10, "output_tokens": 5}), "parsed": None, "parsing_error": "bad json"}
    _install_fake_anthropic(monkeypatch, response)
    backend = AnthropicStructuredBackend(database, api_key="test-key")

    with pytest.raises(ValueError):
        backend.complete(role="bear", model_name="claude-opus-5", payload={"candidate": {}}, schema=_EchoSchema)

    assert database.rows("SELECT * FROM model_costs") == []


def test_missing_api_key_raises_without_network_call(monkeypatch, database):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    backend = AnthropicStructuredBackend(database, api_key=None)
    with pytest.raises(RuntimeError):
        backend.complete(role="technical", model_name="claude-sonnet-5", payload={}, schema=_EchoSchema)


def test_estimate_cost_matches_pricing_family():
    opus_cost = estimate_cost("claude-opus-5-20260101", 1_000_000, 0)
    sonnet_cost = estimate_cost("claude-sonnet-5-20260101", 1_000_000, 0)
    assert opus_cost > sonnet_cost > 0
