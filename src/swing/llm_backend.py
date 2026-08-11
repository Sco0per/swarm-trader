"""Claude-backed implementation of the AgentPipeline's StructuredModelBackend.

Uses langchain_anthropic (already a project dependency, see pyproject.toml)
with structured/schema-constrained output so every analyst/red-team/PM call
returns a validated pydantic object or raises — never free text that could
silently drift from the expected schema.

Every successful call also records its token usage and estimated dollar cost
to ``model_costs`` so GROSS-vs-NET reporting (src/swing/reporting.py) has
real data instead of permanently-empty rows.
"""

from __future__ import annotations

import os
from typing import Any, TypeVar

from pydantic import BaseModel

from .database import SwingDatabase

SchemaT = TypeVar("SchemaT", bound=BaseModel)


# Approximate list pricing in USD per million tokens, matched by substring
# against the configured model name (case-insensitive). VERIFY AGAINST
# CURRENT ANTHROPIC PRICING before trusting NET-of-AI-cost figures -- these
# are a reasonable default, not a guarantee, and model pricing changes over
# time. Override any entry by editing PRICE_PER_MILLION_TOKENS below.
PRICE_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    # substring: (input $/M tokens, output $/M tokens)
    "opus": (15.00, 75.00),
    "sonnet": (3.00, 15.00),
    "haiku": (0.80, 4.00),
    "fable": (3.00, 15.00),
}
DEFAULT_PRICE_PER_MILLION_TOKENS = (3.00, 15.00)


def estimate_cost(model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    lowered = model_name.lower()
    input_price, output_price = next(
        (prices for key, prices in PRICE_PER_MILLION_TOKENS.items() if key in lowered),
        DEFAULT_PRICE_PER_MILLION_TOKENS,
    )
    return round((prompt_tokens / 1_000_000) * input_price + (completion_tokens / 1_000_000) * output_price, 6)


class AnthropicStructuredBackend:
    """Provider-neutral adapter contract from src.swing.agents.StructuredModelBackend."""

    def __init__(self, database: SwingDatabase | None = None, *, api_key: str | None = None):
        self.database = database
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._clients: dict[str, Any] = {}

    def _client(self, model_name: str):
        if model_name not in self._clients:
            from langchain_anthropic import ChatAnthropic

            if not self._api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set; cannot call Claude")
            self._clients[model_name] = ChatAnthropic(model=model_name, api_key=self._api_key)
        return self._clients[model_name]

    def complete(self, *, role: str, model_name: str, payload: dict[str, Any], schema: type[SchemaT]) -> SchemaT:
        client = self._client(model_name)
        structured = client.with_structured_output(schema, include_raw=True)
        instruction = "You are a disciplined swing-trading analyst. Use only the supplied JSON data; " "never invent prices, dates, or facts not present in the payload. " f"Role: {role}. Respond only with the requested structured schema."
        response = structured.invoke(
            [
                {"role": "system", "content": instruction},
                {"role": "user", "content": _to_prompt(payload)},
            ]
        )
        parsing_error = response.get("parsing_error") if isinstance(response, dict) else None
        if parsing_error:
            raise ValueError(f"Structured output parsing failed for role={role}: {parsing_error}")
        parsed = response["parsed"] if isinstance(response, dict) else response
        if parsed is None:
            raise ValueError(f"No structured output returned for role={role}")
        self._record_cost(role, model_name, payload, response)
        return parsed

    def _record_cost(self, role: str, model_name: str, payload: dict[str, Any], response: Any) -> None:
        if self.database is None:
            return
        raw = response.get("raw") if isinstance(response, dict) else None
        usage = getattr(raw, "usage_metadata", None) or {}
        prompt_tokens = usage.get("input_tokens")
        completion_tokens = usage.get("output_tokens")
        if prompt_tokens is None or completion_tokens is None:
            return
        candidate_id = None
        candidate_payload = payload.get("candidate")
        if isinstance(candidate_payload, dict):
            candidate_id = candidate_payload.get("candidate_id")
        cost = estimate_cost(model_name, int(prompt_tokens), int(completion_tokens))
        self.database.record_model_cost(
            role=role,
            model_name=model_name,
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            cost=cost,
            candidate_id=candidate_id,
        )


def _to_prompt(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, default=str, sort_keys=True, indent=2)
