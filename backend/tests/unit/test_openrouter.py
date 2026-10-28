"""OpenRouter client: format translation and failure handling, with no network."""

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from fantasy_rag.agent.llm import LLMUnavailable, OpenRouterLLM, price, to_openai

FREE = "vendor/model:free"


def client(*replies: httpx.Response, seen: list[dict[str, Any]] | None = None) -> OpenRouterLLM:
    queue = list(replies)

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return queue.pop(0)

    return OpenRouterLLM(
        "test-key", FREE, FREE, httpx.Client(transport=httpx.MockTransport(handle))
    )


def ok(message: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "g1",
            "choices": [{"message": message}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        },
    )


def test_history_translates_tool_calls_and_results() -> None:
    out = to_openai("sys", [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "c1", "name": "get_trending", "input": {"limit": 3}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "bad", "is_error": True},
        ]},
    ])  # fmt: skip
    assert out[0] == {"role": "system", "content": "sys"}
    assert out[2]["tool_calls"][0]["function"] == {
        "name": "get_trending",
        "arguments": '{"limit": 3}',
    }
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "Error: bad"}


def test_tool_turn_returns_tool_use_blocks() -> None:
    seen: list[dict[str, Any]] = []
    llm = client(ok({"content": None, "tool_calls": [
        {"id": "c9", "function": {"name": "get_trending", "arguments": '{"limit": 2}'}},
        {"function": {"name": "search_news", "arguments": "not json"}},
    ]}), seen=seen)  # fmt: skip
    tools = [{"name": "get_trending", "description": "d", "input_schema": {"type": "object"}}]
    msg = llm.tool_turn("sys", [{"role": "user", "content": "q"}], tools)  # type: ignore[list-item]
    assert msg.stop_reason == "tool_use"
    a, b = msg.content
    assert (a.type, a.id, a.name, a.input) == ("tool_use", "c9", "get_trending", {"limit": 2})
    assert (b.id, b.input) == ("call_1", {})  # unparseable arguments: the tool check reports it
    assert seen[0]["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert llm.usage == {FREE: [7, 3]} and llm.cost_usd() == 0.0 and price(FREE) == (0.0, 0.0)


class Ans(BaseModel):
    answer: str
    n: int


def test_structured_strips_wrapping_and_retries_once() -> None:
    seen: list[dict[str, Any]] = []
    llm = client(
        ok({"content": '{"answer": "x"}'}),
        ok({"content": '```json\n{"answer": "y", "n": 2}\n```'}),
        seen=seen,
    )
    assert llm.structured("sys", "q", Ans) == Ans(answer="y", n=2)
    assert "does not match the schema" in seen[1]["messages"][-1]["content"]

    bad = client(ok({"content": "no"}), ok({"content": "still no"}))
    with pytest.raises(RuntimeError):
        bad.structured("sys", "q", Ans)


@pytest.mark.parametrize("reply", [
    httpx.Response(429, json={"error": {"message": "Rate limit exceeded: free-models-per-day"}}),
    httpx.Response(200, json={"error": {"message": "Provider returned error", "code": 502}}),
    httpx.Response(200, json={"choices": []}),
])  # fmt: skip
def test_rate_limits_and_upstream_errors_are_unavailable(reply: httpx.Response) -> None:
    with pytest.raises(LLMUnavailable):
        client(reply).tool_turn("sys", [{"role": "user", "content": "q"}], [])


def test_only_free_models_are_allowed() -> None:
    with pytest.raises(ValueError, match="only free"):
        OpenRouterLLM("k", "vendor/paid-model", FREE)
