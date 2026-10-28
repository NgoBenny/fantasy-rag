"""The two LLM calls the agent makes, behind a protocol so tests use a fake.

Two providers: the Anthropic API (paid) and OpenRouter's free models (`LLM_PROVIDER`).
"""

import json
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

import anthropic
import httpx
from anthropic.types import Message, MessageParam, ToolParam
from pydantic import BaseModel, ValidationError

from fantasy_rag.agent.budget import Budget
from fantasy_rag.config import Settings

ROUTER_MODEL = "claude-haiku-5-5"
WRITER_MODEL = "claude-sonnet-5-5"
PRICE_PER_MTOK = {ROUTER_MODEL: (0.10, 0.50), WRITER_MODEL: (2.00, 10.00)}  # input, output
# Output bounds keep each call's worst case small; Phase 6 answers averaged ~400 tokens.
ROUTER_MAX_TOKENS = 2048
WRITER_MAX_TOKENS = 3000

T = TypeVar("T", bound=BaseModel)


class Cancelled(Exception):
    """The client went away; no further model or tool call is started."""


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
    if model.endswith(":free"):  # OpenRouter free variants (the only ones allowed there)
        return (0.0, 0.0)
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
    `unpriced` counts calls that failed after being sent: their usage (and cost) is unknown.
    """

    def __init__(self, api_key: str | None = None, budget: Budget | None = None) -> None:
        key = api_key or Settings().anthropic_api_key.get_secret_value()
        if not key:
            raise LLMUnavailable("set ANTHROPIC_API_KEY in .env")
        self.budget = budget
        self.client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=0 if budget else 2)
        self.usage: dict[str, list[int]] = {}
        self.unpriced = 0

    def _count(self, model: str, usage: Any) -> None:
        tally = self.usage.setdefault(model, [0, 0])
        tally[0] += usage.input_tokens
        tally[1] += usage.output_tokens

    def _guarded(self, request: dict[str, Any], send: Callable[..., Any]) -> Any:
        """Run `send(**request)` inside a budget reservation (or directly, uncapped)."""
        if self.budget is None:
            try:
                resp = send(**request)
            except BaseException:
                self.unpriced += 1
                raise
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
            self.unpriced += 1
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


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
USER_AGENT = "fantasy-rag/0.1 (+https://github.com/NgoBenny/fantasy-rag)"
RETRY_WAITS = (5.0, 15.0)  # seconds before retrying a temporary upstream failure
# A tool call whose arguments weren't JSON: the router fails it instead of running defaults.
INVALID_ARGS = "__invalid_arguments__"
JSON_ONLY = (
    "\n\nReply with only one JSON object (no prose, no code fences) that matches this"
    " JSON Schema:\n"
)


def _get(block: Any, key: str) -> Any:
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def to_openai(system: str, messages: list[MessageParam]) -> list[dict[str, Any]]:
    """Anthropic-format history (text, tool_use, tool_result blocks) -> chat completions."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
        elif m["role"] == "assistant":
            text = "".join(_get(b, "text") or "" for b in content if _get(b, "type") == "text")
            calls = [
                {
                    "id": _get(b, "id"),
                    "type": "function",
                    "function": {
                        "name": _get(b, "name"),
                        "arguments": json.dumps(_get(b, "input")),
                    },
                }
                for b in content
                if _get(b, "type") == "tool_use"
            ]
            turn: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                turn["tool_calls"] = calls
            out.append(turn)
        else:
            for b in content:
                if _get(b, "type") == "tool_result":
                    body = _get(b, "content")
                    text = body if isinstance(body, str) else json.dumps(body, default=str)
                    if _get(b, "is_error"):
                        text = f"Error: {text}"
                    out.append(
                        {"role": "tool", "tool_call_id": _get(b, "tool_use_id"), "content": text}
                    )
                elif _get(b, "type") == "text":
                    out.append({"role": "user", "content": _get(b, "text")})
    return out


def _json_object(text: str) -> str:
    """The outermost {...} in a reply (models sometimes wrap it in prose or fences)."""
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _decode_nested(args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Some free models send an object argument as JSON text ('{"name": ...}'); decode it
    when the tool's schema doesn't declare that argument a string."""
    props = schema.get("properties", {})
    out = dict(args)
    for key, value in args.items():
        if not (isinstance(value, str) and value.lstrip()[:1] in ("{", "[")):
            continue
        if props.get(key, {}).get("type") == "string":
            continue
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict | list):
            out[key] = decoded
    return out


class OpenRouterLLM:
    """OpenRouter's free models: one routes (tool calls), one writes (JSON answers).

    Only `:free` model IDs are accepted, so this client can never spend money. Free use is
    rate-limited by OpenRouter (per minute and per day); a 429 surfaces as LLMUnavailable.
    Free providers may log prompts: see the account's OpenRouter privacy settings.
    """

    def __init__(
        self,
        api_key: str | None = None,
        router_model: str | None = None,
        writer_model: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        s = Settings()
        key = api_key or s.openrouter_api_key.get_secret_value()
        if not key:
            raise LLMUnavailable("set OPENROUTER_API_KEY in .env")
        self.router_model = router_model or s.openrouter_router_model
        self.writer_model = writer_model or s.openrouter_writer_model
        for m in (self.router_model, self.writer_model):
            if not m.endswith(":free"):
                raise ValueError(f"only free OpenRouter models are allowed, got {m!r}")
        self.client = client or httpx.Client(timeout=180.0)
        self.headers = {
            "Authorization": f"Bearer {key}",
            "User-Agent": USER_AGENT,
            "X-Title": "fantasy-rag",
        }
        self.usage: dict[str, list[int]] = {}
        self.unpriced = 0  # free calls cost 0 even when they fail, so none are unpriced
        self.sleep = time.sleep  # tests replace it
        # Set by the chat worker: no request (first try or retry) starts after a disconnect.
        self.cancelled: Callable[[], bool] = bool

    def _chat(self, model: str, messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        for wait in (*RETRY_WAITS, None):
            if self.cancelled():  # checked before every request, retries included
                raise Cancelled
            try:
                r = self.client.post(
                    OPENROUTER_URL,
                    headers=self.headers,
                    json={"model": model, "messages": messages, **extra},
                )
                body: dict[str, Any] = r.json() if r.content else {}
            except (httpx.HTTPError, ValueError) as e:
                raise LLMUnavailable(f"OpenRouter request failed: {type(e).__name__}") from e
            # OpenRouter also reports upstream failures as a 200 with an "error" body.
            err = body.get("error")
            if r.status_code < 400 and not err and body.get("choices"):
                break
            msg = err.get("message") if isinstance(err, dict) else err
            # The error code can sit inside an HTTP 200 body; it decides, not the HTTP status.
            code = err.get("code") if isinstance(err, dict) else None
            # An error body without a code (seen live: "Upstream error ... overloaded") is upstream.
            code = (
                code if isinstance(code, int) else (r.status_code if r.status_code >= 400 else 502)
            )
            # Only upstream failures (5xx, or a reply with no choices) are temporary;
            # 4xx (rate limits, access) won't clear by waiting.
            temporary = code >= 500 or (code < 400 and not err)
            if wait is None or not temporary:
                raise LLMUnavailable(f"OpenRouter {code} ({model}): {msg or 'no choices'}")
            self.sleep(wait)
        u = body.get("usage") or {}
        tally = self.usage.setdefault(model, [0, 0])
        tally[0] += u.get("prompt_tokens", 0)
        tally[1] += u.get("completion_tokens", 0)
        return body

    def tool_turn(
        self, system: str, messages: list[MessageParam], tools: list[ToolParam]
    ) -> Message:
        specs = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]
        # Reasoning tokens count against max_tokens on these models, hence the headroom.
        body = self._chat(
            self.router_model,
            to_openai(system, messages),
            tools=specs,
            max_tokens=ROUTER_MAX_TOKENS * 4,
            reasoning={"effort": "low"},
        )
        msg = body["choices"][0]["message"]
        schemas: dict[str, Any] = {t["name"]: t["input_schema"] for t in tools}
        content: list[dict[str, Any]] = []
        if msg.get("content"):
            content.append({"type": "text", "text": msg["content"]})
        for i, c in enumerate(msg.get("tool_calls") or []):
            raw = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = None
            if not isinstance(args, dict):  # never run a tool on defaults it wasn't given
                args = {INVALID_ARGS: raw}
            else:
                args = _decode_nested(args, schemas.get(c["function"]["name"], {}))
            content.append(
                {
                    "type": "tool_use",
                    "id": c.get("id") or f"call_{i}",
                    "name": c["function"]["name"],
                    "input": args,
                }
            )
        uses = any(b["type"] == "tool_use" for b in content)
        return Message.model_validate(
            {
                "id": body.get("id") or "openrouter",
                "type": "message",
                "role": "assistant",
                "model": self.router_model,
                "content": content,
                "stop_reason": "tool_use" if uses else "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        )

    def structured(self, system: str, content: str, schema: type[T]) -> T:
        # Most free models lack native structured output, so the schema goes in the prompt.
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": system + JSON_ONLY + json.dumps(schema.model_json_schema()),
            },
            {"role": "user", "content": content},
        ]
        for attempt in range(2):  # one retry with the validation error
            body = self._chat(
                self.writer_model,
                messages,
                max_tokens=WRITER_MAX_TOKENS * 4,
                reasoning={"effort": "medium"},
            )
            reply = body["choices"][0]["message"].get("content") or ""
            try:
                return schema.model_validate_json(_json_object(reply))
            except ValidationError as e:
                if attempt:
                    raise RuntimeError(f"no parsed answer from {self.writer_model}") from e
                first = e.errors()[0]
                messages += [
                    {"role": "assistant", "content": reply},
                    {
                        "role": "user",
                        "content": "That reply does not match the schema"
                        f" ({e.error_count()} errors;"
                        f" first at {first['loc']}: {first['msg']}). Reply with only the corrected"
                        " JSON object.",
                    },
                ]
        raise AssertionError("unreachable")

    def cost_usd(self) -> float:
        return 0.0


ChatLLM = AnthropicLLM | OpenRouterLLM


def make_llm(budget: Budget | None = None) -> ChatLLM:
    """The configured provider. Free OpenRouter calls cost nothing, so no budget applies."""
    if Settings().llm_provider == "openrouter":
        return OpenRouterLLM()
    return AnthropicLLM(budget=budget)
