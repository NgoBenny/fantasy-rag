"""POST /api/chat over SSE, with a scripted fake LLM on the synthetic league database."""

import asyncio
import json
import re
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from anthropic.types import Message
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, text

from fantasy_rag.agent.answer import Answer, Claim
from fantasy_rag.agent.budget import BudgetExceeded
from fantasy_rag.agent.llm import LLMUnavailable
from fantasy_rag.api.chat import ChatServices
from fantasy_rag.api.main import app, chat_services, get_conn
from fantasy_rag.db import get_log_engine, get_readonly_engine

pytestmark = pytest.mark.db
MARKER = "ZZMODELTEXT"  # anything the fake model writes carries this
TEMPLATES = re.compile(
    r"^(Reading the season and your league context|Writing the answer|"
    r"Checking every number and source|Fixing a failed check|"
    r"Looked up [a-z ]+( for [A-Za-z .,'-]+)?( \(league \d+ rules\))?|"
    r"(Player lookup|Looking up [a-z ]+) failed: [a-z ]+)$"
)


def _msg(*uses: tuple[str, dict[str, Any]]) -> Message:
    content: list[dict[str, Any]] = [{"type": "text", "text": f"thinking {MARKER}"}]
    content += [{"type": "tool_use", "id": f"t{i}", "name": n, "input": a}
                for i, (n, a) in enumerate(uses)]  # fmt: skip
    return Message.model_validate({
        "id": "m", "type": "message", "role": "assistant", "model": "fake", "content": content,
        "stop_reason": "tool_use" if uses else "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })  # fmt: skip


def answer(text_: str, *claims: Claim, kind: str = "stats", cites: list[str]) -> Answer:
    return Answer(answer=text_, answer_type=kind, season=None, week=None,  # type: ignore[arg-type]
                  scoring_format=None, data_as_of=None, model_version=None, facts=list(claims),
                  projections=[], citations=cites, limitations=[])  # fmt: skip


class FakeLLM:
    def __init__(
        self, turns: list[Message], drafts: list[Answer], gate: threading.Event | None = None
    ) -> None:  # noqa: E501
        self.turns, self.drafts, self.gate = turns, drafts, gate
        self.calls = 0

    def tool_turn(self, system: str, messages: Any, tools: Any) -> Message:
        self.calls += 1
        if self.gate:
            self.gate.wait(10)
        return self.turns.pop(0) if self.turns else _msg()

    def structured(self, system: str, content: str, schema: Any) -> Any:
        self.calls += 1
        return self.drafts.pop(0)


@pytest.fixture
def engines(db: str) -> Iterator[tuple[Engine, Engine, Engine]]:
    tools_e, log_e, owner = get_readonly_engine(db), get_log_engine(db), create_engine(db)
    yield tools_e, log_e, owner
    for e in (tools_e, log_e, owner):
        e.dispose()


def client_for(
    engines: tuple[Engine, Engine, Engine], llm: Any, log: Engine | None = None
) -> TestClient:  # noqa: E501
    tools_e, log_e, _ = engines

    def conn() -> Iterator[Connection]:
        with tools_e.connect() as c:
            yield c

    app.dependency_overrides[get_conn] = conn
    app.dependency_overrides[chat_services] = lambda: ChatServices(
        tools_e, log if log is not None else log_e, llm, ""
    )
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


def events(raw: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in raw.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((fields["event"], json.loads(fields["data"])))
    return out


def ask(c: TestClient, question: str = "how many catches for jefferson?") -> list[tuple[str, Any]]:
    r = c.post("/api/chat", json={"question": question})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    return events(r.text)


def log_rows(engines: tuple[Engine, Engine, Engine]) -> list[Any]:
    with engines[2].connect() as c:
        return list(c.execute(text("SELECT * FROM query_log ORDER BY id")))


CLAIM = Claim(text="Justin Jefferson is a MIN receiver", source_ids=["r1"])
STATS = ("get_player_stats", {"player": {"name": "Justin Jefferson", "position": "WR"}})


def test_answer_arrives_once_with_its_sources_after_template_progress(engines: Any) -> None:
    good = answer(f"Justin Jefferson plays for MIN {MARKER}.",
                  CLAIM, cites=["r1"])  # fmt: skip
    got = ask(client_for(engines, lambda: FakeLLM([_msg(STATS)], [good])))
    kinds = [k for k, _ in got]
    assert kinds[-1] == "done" and kinds.count("answer") == 1 and "withheld" not in kinds
    assert set(kinds[: kinds.index("answer")]) == {"progress"}
    for _, p in got[: kinds.index("answer")]:
        assert TEMPLATES.match(p["message"]) and MARKER not in p["message"], p["message"]
    assert "Looked up stats for Justin Jefferson" in [
        p["message"] for k, p in got if k == "progress"
    ]
    ans = dict(got)["answer"]
    assert [s["id"] for s in ans["sources"]] == ["r1"] and ans["sources"][0][
        "href"
    ] == "/players/00-1"
    done = dict(got)["done"]
    assert done["logged"] and isinstance(done["query_id"], int) and done["progress_first_ms"] >= 0
    row = log_rows(engines)[-1]
    assert row.status == "ok" and row.tools_called == ["get_player_stats"]


def test_a_withheld_answer_never_reaches_the_client_or_the_log(engines: Any) -> None:
    draft = answer(f"Jefferson will score 99 {MARKER}", Claim(text="99 points", source_ids=["r1"]),
                   cites=["r1"])  # fmt: skip
    llm = FakeLLM([_msg(STATS)], [draft, draft.model_copy(deep=True)])
    got = ask(client_for(engines, lambda: llm))
    kinds = [k for k, _ in got]
    assert "withheld" in kinds and "answer" not in kinds and kinds[-1] == "done"
    stream = json.dumps([d for _, d in got])
    assert MARKER not in stream and "99" not in stream
    assert "Fixing a failed check" in stream  # the repair happened before withholding
    row = log_rows(engines)[-1]
    assert row.status == "withheld" and row.answer is None and row.answer_json is None
    assert MARKER not in json.dumps(row._asdict(), default=str)


def test_clarification_may_cite_the_failed_lookup_as_an_unlinked_source(engines: Any) -> None:
    lookup = ("get_injury_status", {"player": {"name": "Mike Williams"}})  # two fantasy players
    ask_back = answer("Which Mike Williams do you mean?", kind="clarification", cites=["r1"])
    got = ask(client_for(engines, lambda: FakeLLM([_msg(lookup)], [ask_back])))
    ans = dict(got)["answer"]
    assert ans["answer"]["answer_type"] == "clarification"
    assert ans["sources"] == [{"id": "r1", "kind": "tool", "tool": "get_injury_status",
                               "href": None, "failed": True,
                               "label": "Lookup failed: name matches several players"}]  # fmt: skip
    assert "Player lookup failed: name matches several players" in json.dumps(got)
    assert "Mike Williams" not in [p["message"] for k, p in got if k == "progress"][1]


def test_claude_unavailable_is_an_error_event_and_stats_still_work(engines: Any) -> None:
    def no_key() -> Any:
        raise LLMUnavailable("set ANTHROPIC_API_KEY in .env")

    c = client_for(engines, no_key)
    got = ask(c)
    err = dict(got)["error"]
    assert (
        err["code"] == "llm_unavailable" and err["retryable"] and [k for k, _ in got][-1] == "done"
    )
    assert c.get("/api/players/00-1").status_code == 200
    assert c.post("/api/leagues/1/lineup", json={"roster_id": 1}).status_code == 200
    assert log_rows(engines)[-1].status == "error"


def test_budget_exceeded_stops_before_any_model_call(engines: Any) -> None:
    class Broke(FakeLLM):
        def tool_turn(self, *a: Any) -> Message:
            raise BudgetExceeded("would pass the $0.00 cap")

    llm = Broke([], [])
    got = ask(client_for(engines, lambda: llm))
    assert dict(got)["error"]["code"] == "budget_exceeded" and llm.calls == 0
    assert log_rows(engines)[-1].status == "budget"


def test_a_logging_failure_still_delivers_the_answer(engines: Any) -> None:
    good = answer("Justin Jefferson plays for MIN.",
                  CLAIM, cites=["r1"])  # fmt: skip

    class LogDown:  # the insert-only log database is unreachable
        def begin(self) -> Any:
            raise ConnectionError("log database down")

    broken: Any = LogDown()
    got = ask(client_for(engines, lambda: FakeLLM([_msg(STATS)], [good]), log=broken))
    assert "answer" in dict(got)
    done = dict(got)["done"]
    assert done["query_id"] is None and done["logged"] is False and done["answer_ms"] is not None


def test_disconnect_cancels_further_model_calls(engines: Any) -> None:
    gate = threading.Event()
    llm = FakeLLM([_msg(STATS)], [], gate=gate)
    c = client_for(engines, lambda: llm)
    with c.stream("POST", "/api/chat", json={"question": "q"}) as r:
        first = next(r.iter_lines())  # the context progress line arrives first
        assert first.startswith("id: ")
    gate.set()  # the in-flight router call finishes; nothing new may start
    for _ in range(50):
        rows = log_rows(engines)
        if rows:
            break
        time.sleep(0.1)
    assert rows[-1].status == "cancelled" and llm.calls <= 1  # only an in-flight call


def test_the_event_loop_stays_free_while_a_chat_waits(engines: Any) -> None:
    gate = threading.Event()
    llm = FakeLLM([], [], gate=gate)
    client_for(engines, lambda: llm)  # installs the overrides

    async def scenario() -> float:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as h:
            chat = asyncio.create_task(h.post("/api/chat", json={"question": "q"}))
            await asyncio.sleep(0.3)  # the chat is now blocked inside the fake model
            t0 = time.monotonic()
            assert (await h.get("/api/health")).status_code == 200
            waited = time.monotonic() - t0
            gate.set()
            await chat
            return waited

    assert asyncio.run(scenario()) < 1.0


def test_no_connection_is_left_checked_out(engines: Any) -> None:
    good = answer("Justin Jefferson plays for MIN.",
                  CLAIM, cites=["r1"])  # fmt: skip

    def fresh() -> Any:
        return FakeLLM([_msg(STATS)], [good.model_copy(deep=True)])

    c = client_for(engines, fresh)
    for _ in range(10):
        ask(c)
    tools_e, log_e, _ = engines
    assert tools_e.pool.checkedout() == 0 and log_e.pool.checkedout() == 0  # type: ignore[attr-defined]


class Billed(FakeLLM):
    """Bills the router turn; the writer fails (before sending, or after: usage unknown)."""

    def __init__(self, writer_error: Exception, unpriced: bool) -> None:
        super().__init__([_msg(STATS)], [])
        self.usage: dict[str, list[int]] = {}
        self.unpriced, self.writer_error, self.sent = 0, writer_error, unpriced

    def tool_turn(self, *a: Any) -> Message:
        tally = self.usage.setdefault("claude-haiku-5-5", [0, 0])
        tally[0], tally[1] = tally[0] + 1000, tally[1] + 100  # two router turns: tool, then stop
        return super().tool_turn(*a)

    def structured(self, *a: Any) -> Any:
        self.calls += 1
        if self.sent:
            self.unpriced += 1  # the request went out; what it cost is unknown
        raise self.writer_error


def test_a_failure_after_routing_keeps_the_calls_and_usage_already_incurred(engines: Any) -> None:
    llm = Billed(BudgetExceeded("the writer's allowance would pass the cap"), unpriced=False)
    got = ask(client_for(engines, lambda: llm))
    assert dict(got)["error"]["code"] == "budget_exceeded"
    row = log_rows(engines)[-1]
    assert row.status == "budget" and row.tools_called == ["get_player_stats"]
    assert row.tool_inputs == [STATS[1]]
    assert row.usage == {"tokens": {"claude-haiku-5-5": [2000, 200]}, "unpriced_calls": 0}
    assert row.cost_usd is not None and float(row.cost_usd) > 0
    assert dict(got)["done"]["cost_usd"] == pytest.approx(float(row.cost_usd))


def test_a_call_with_unknown_usage_leaves_the_cost_unknown_not_zero(engines: Any) -> None:
    llm = Billed(LLMUnavailable("connection reset mid-request"), unpriced=True)
    got = ask(client_for(engines, lambda: llm))
    row = log_rows(engines)[-1]
    assert row.status == "error" and row.tools_called == ["get_player_stats"]
    assert row.usage == {"tokens": {"claude-haiku-5-5": [2000, 200]}, "unpriced_calls": 1}
    assert row.cost_usd is None and dict(got)["done"]["cost_usd"] is None


def test_disconnect_stops_openrouter_retries_in_the_chat_worker(
    engines: tuple[Engine, Engine, Engine],
) -> None:
    """The worker hands its cancel flag to the client, so a retry wait ends the request."""
    from fantasy_rag.agent.llm import OpenRouterLLM
    from fantasy_rag.api.chat import ChatRequest, run_chat

    sent: list[httpx.Request] = []

    def overloaded(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"error": {"code": 502, "message": "overloaded"}})

    llm = OpenRouterLLM(
        "k", "v/m:free", "v/m:free", httpx.Client(transport=httpx.MockTransport(overloaded))
    )
    gone: list[bool] = []
    llm.sleep = lambda s: gone.append(True)  # the browser disconnects during the first wait
    tools_e, _, _ = engines
    out: list[tuple[str, dict[str, Any]]] = []
    run_chat(ChatServices(tools_e, None, lambda: llm, ""), ChatRequest(question="q"),
             lambda e, d: out.append((e, d)), lambda: bool(gone))  # fmt: skip
    assert len(sent) == 1
    assert [e for e, _ in out] == ["progress", "done"]  # cancelled: no answer, no error
