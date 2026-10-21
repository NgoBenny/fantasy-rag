"""The two LLM calls the agent makes, behind a protocol so tests use a fake."""

from typing import Any, Protocol, TypeVar

import anthropic
from anthropic.types import Message, MessageParam, ToolParam
from pydantic import BaseModel

from fantasy_rag.config import Settings

ROUTER_MODEL = "claude-haiku-5-5"
WRITER_MODEL = "claude-sonnet-5-5"
PRICE_PER_MTOK = {ROUTER_MODEL: (0.10, 0.50), WRITER_MODEL: (2.00, 10.00)}  # input, output

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    def tool_turn(
        self, system: str, messages: list[MessageParam], tools: list[ToolParam]
    ) -> Message:
        """One routing step: the model may answer with tool_use blocks."""
        ...

    def structured(self, system: str, content: str, schema: type[T]) -> T:
        """One answer, parsed into `schema`."""
        ...


class AnthropicLLM:
    """Haiku routes, Sonnet writes. `usage` accumulates [input, output] tokens per model,
    keyed by the model that actually served each response."""

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or Settings().anthropic_api_key.get_secret_value()
        if not key:
            raise RuntimeError("set ANTHROPIC_API_KEY in .env")
        self.client = anthropic.Anthropic(api_key=key, timeout=60.0)
        self.usage: dict[str, list[int]] = {}

    def _count(self, model: str, usage: Any) -> None:
        tally = self.usage.setdefault(model, [0, 0])
        tally[0] += usage.input_tokens
        tally[1] += usage.output_tokens

    def tool_turn(
        self, system: str, messages: list[MessageParam], tools: list[ToolParam]
    ) -> Message:
        resp = self.client.messages.create(
            model=ROUTER_MODEL,
            max_tokens=4096,
            system=system,
            messages=messages,
            tools=tools,
            output_config={"effort": "low"},
        )
        self._count(resp.model, resp.usage)
        return resp

    def structured(self, system: str, content: str, schema: type[T]) -> T:
        # No refusal fallback: evaluation must measure the chosen model only.
        resp = self.client.messages.parse(
            model=WRITER_MODEL,
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            output_config={"effort": "medium"},
        )
        self._count(resp.model, resp.usage)
        if resp.parsed_output is None:
            raise RuntimeError(f"no parsed answer (stop_reason={resp.stop_reason})")
        return resp.parsed_output

    def cost_usd(self) -> float:
        cost = 0.0
        for m, (i, o) in self.usage.items():
            price = next((p for k, p in PRICE_PER_MTOK.items() if m.startswith(k)), None)
            if price is None:
                raise ValueError(f"no price for model {m!r}")
            cost += (i * price[0] + o * price[1]) / 1e6
        return cost
