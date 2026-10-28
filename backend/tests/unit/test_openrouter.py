"""OpenRouter client: format translation and failure handling, with no network."""

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from fantasy_rag.agent.llm import (
    INVALID_ARGS,
    Cancelled,
    LLMUnavailable,
    OpenRouterLLM,
    price,
    to_openai,
)
from fantasy_rag.agent.router import route

FREE = "vendor/model:free"


def client(*replies: httpx.Response, seen: list[dict[str, Any]] | None = None) -> OpenRouterLLM:
    queue = list(replies)

    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return queue.pop(0)

    llm = OpenRouterLLM("test-key", FREE, FREE, httpx.Client(transport=httpx.MockTransport(handle)))
    llm.sleep = lambda s: None
    return llm


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
    assert (b.id, b.input) == (
        "call_1",
        {INVALID_ARGS: "not json"},
    )  # unparseable arguments: the tool check reports it
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
    httpx.Response(403, json={"error": {"message": "only available on agentic harnesses"}}),
])  # fmt: skip
def test_client_errors_are_unavailable_without_retry(reply: httpx.Response) -> None:
    with pytest.raises(LLMUnavailable):
        client(reply).tool_turn("sys", [{"role": "user", "content": "q"}], [])


def test_upstream_overload_is_retried_then_reported() -> None:
    overloaded = httpx.Response(200, json={"error": {"message": "Service temporarily overloaded"}})
    q = [{"role": "user", "content": "q"}]
    assert (
        client(overloaded, httpx.Response(502), ok({"content": "hi"})).tool_turn("s", q, []).content
    )
    with pytest.raises(LLMUnavailable, match="overloaded"):
        client(
            httpx.Response(502), httpx.Response(200, json={"choices": []}), overloaded
        ).tool_turn("s", q, [])


def test_object_arguments_sent_as_json_text_are_decoded() -> None:
    args = json.dumps({
        "player": '{"name": "Josh Allen", "team": "BUF"}', "query": "{not json", "note": "{x}",
    })  # fmt: skip
    calls = [{"id": "c1", "function": {"name": "t", "arguments": args}}]
    schema = {"properties": {"player": {"$ref": "#/$defs/P"}, "note": {"type": "string"}}}
    tools = [{"name": "t", "description": "d", "input_schema": schema}]
    msg = client(ok({"content": None, "tool_calls": calls})).tool_turn("s", [], tools)  # type: ignore[list-item]
    assert msg.content[0].input == {
        "player": {"name": "Josh Allen", "team": "BUF"},  # decoded
        "query": "{not json",  # not JSON: left as sent
        "note": "{x}",  # declared a string: never touched
    }


def test_only_free_models_are_allowed() -> None:
    with pytest.raises(ValueError, match="only free"):
        OpenRouterLLM("k", "vendor/paid-model", FREE)


def test_rate_limit_inside_a_200_body_is_not_retried() -> None:
    sent: list[dict[str, Any]] = []
    limited = httpx.Response(200, json={"error": {"code": 429, "message": "Rate limit exceeded"}})
    llm = client(limited, ok({"content": "unused"}), seen=sent)
    with pytest.raises(LLMUnavailable, match="429"):
        llm.tool_turn("s", [{"role": "user", "content": "q"}], [])
    assert len(sent) == 1


def test_disconnect_during_a_retry_wait_stops_further_requests() -> None:
    sent: list[dict[str, Any]] = []
    overloaded = httpx.Response(200, json={"error": {"code": 502, "message": "overloaded"}})
    llm = client(overloaded, ok({"content": "unused"}), seen=sent)
    gone: list[bool] = []
    llm.cancelled = lambda: bool(gone)
    llm.sleep = lambda s: gone.append(True)  # the client disconnects while we wait
    with pytest.raises(Cancelled):
        llm.tool_turn("s", [{"role": "user", "content": "q"}], [])
    assert len(sent) == 1


def test_disconnect_before_the_json_repair_retry_stops_it() -> None:
    sent: list[dict[str, Any]] = []
    llm = client(ok({"content": "not json"}), ok({"content": '{"answer": "y", "n": 1}'}), seen=sent)
    llm.cancelled = lambda: len(sent) >= 1
    with pytest.raises(Cancelled):
        llm.structured("s", "q", Ans)
    assert len(sent) == 1


@pytest.mark.parametrize("raw", ["not json", "[1, 2]"])
def test_malformed_tool_arguments_fail_the_call_instead_of_running_defaults(raw: str) -> None:
    calls = [{"id": "c1", "function": {"name": "get_trending", "arguments": raw}}]
    msg = client(ok({"content": None, "tool_calls": calls})).tool_turn(
        "s", [{"role": "user", "content": "q"}], []
    )
    assert msg.content[0].input == {INVALID_ARGS: raw}
    done = client(ok({"content": "no tools"}))
    got, _ = route(None, Replay(msg, done), "q", "ctx")  # type: ignore[arg-type]
    assert got[0].result is None and "not valid JSON" in (got[0].error or "")


class Replay:
    """First a canned tool turn, then whatever `rest` answers."""

    def __init__(self, first: Any, rest: OpenRouterLLM) -> None:
        self.first, self.rest = first, rest

    def tool_turn(self, system: str, messages: Any, tools: Any) -> Any:
        first, self.first = self.first, None
        return first or self.rest.tool_turn(system, messages, tools)

    def structured(self, system: str, content: str, schema: Any) -> Any:
        raise NotImplementedError
