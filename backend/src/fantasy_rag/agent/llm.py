"""The two LLM calls the agent makes, behind a protocol so tests use a fake."""

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

import anthropic
from anthropic.types import Message, MessageParam, ToolParam
from pydantic import BaseModel

from fantasy_rag.agent.budget import Budget
from fantasy_rag.config import Settings

ROUTER_MODEL = "claude-haiku-5-5"
WRITER_MODEL = "claude-sonnet-5-5"
PRICE_PER_MTOK = {ROUTER_MODEL: (0.10, 0.50), WRITER_MODEL: (2.00, 10.00)}  # input, output
# Output bounds keep each call's worst case small; Phase 6 answers averaged ~400 tokens.
ROUTER_MAX_TOKENS = 2048
WRITER_MAX_TOKENS = 3000

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """No API key is configured, so no model call can be made."""


class LLMClient(Protocol):
    def tool_turn(
        self, system: str, messages: list[MessageParam], tools: list[ToolParam]
    ) -> Message:
        """One routing step: the model may answer with tool_use blocks."""
        ...

    def structured(self, system: str, content: str, schema: type[T]) -> T:
        """One answer, parsed into `schema`."""
        ...


def price(model: str) -> tuple[float, float]:
    found = next((p for k, p in PRICE_PER_MTOK.items() if model.startswith(k)), None)
    if found is None:
        raise ValueError(f"no price for model {model!r}")
    return found


class AnthropicLLM:
    """Haiku routes, Sonnet writes. `usage` accumulates [input, output] tokens per model,
    keyed by the model that actually served each response.

    With a `budget`, every call is first counted (`messages.count_tokens` with the same
    request) and reserves counted input + `max_tokens` output before it starts, and the
    SDK's automatic retries are off, so no attempt runs outside a reservation.
    """

    def __init__(self, api_key: str | None = None, budget: Budget | None = None) -> None:
        key = api_key or Settings().anthropic_api_key.get_secret_value()
        if not key:
            raise LLMUnavailable("set ANTHROPIC_API_KEY in .env")
        self.budget = budget
        self.client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=0 if budget else 2)
        self.usage: dict[str, list[int]] = {}

    def _count(self, model: str, usage: Any) -> None:
        tally = self.usage.setdefault(model, [0, 0])
        tally[0] += usage.input_tokens
        tally[1] += usage.output_tokens

    def _guarded(self, request: dict[str, Any], send: Callable[..., Any]) -> Any:
        """Run `send(**request)` inside a budget reservation (or directly, uncapped)."""
        if self.budget is None:
            resp = send(**request)
            self._count(resp.model, resp.usage)
            return resp
        counted = {k: v for k, v in request.items() if k != "max_tokens"}
        tokens = self.client.messages.count_tokens(**counted).input_tokens
        p_in, p_out = price(request["model"])
        allowance = (tokens * p_in + request["max_tokens"] * p_out) / 1e6
        held = self.budget.reserve(allowance)
        try:
            resp = send(**request)
        except BaseException:
            self.budget.settle(held, None)  # tokens may have been billed: charge it all
            raise
        p_in, p_out = price(resp.model)
        self.budget.settle(
            held, (resp.usage.input_tokens * p_in + resp.usage.output_tokens * p_out) / 1e6
        )
        self._count(resp.model, resp.usage)
        return resp

    def tool_turn(
        self, system: str, messages: list[MessageParam], tools: list[ToolParam]
    ) -> Message:
        request = {"model": ROUTER_MODEL, "max_tokens": ROUTER_MAX_TOKENS, "system": system,
                   "messages": messages, "tools": tools, "output_config": {"effort": "low"}}  # fmt: skip # noqa: E501
        resp: Message = self._guarded(request, self.client.messages.create)
        return resp

    def structured(self, system: str, content: str, schema: type[T]) -> T:
        # No refusal fallback: evaluation must measure the chosen model only.
        request = {"model": WRITER_MODEL, "max_tokens": WRITER_MAX_TOKENS, "system": system,
                   "messages": [{"role": "user", "content": content}], "output_format": schema,
                   "output_config": {"effort": "medium"}}  # fmt: skip
        resp = self._guarded(request, self.client.messages.parse)
        if resp.parsed_output is None:
            raise RuntimeError(f"no parsed answer (stop_reason={resp.stop_reason})")
        parsed: T = resp.parsed_output
        return parsed

    def cost_usd(self) -> float:
        return sum((i * price(m)[0] + o * price(m)[1]) / 1e6 for m, (i, o) in self.usage.items())
