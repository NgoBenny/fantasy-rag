"""Question -> tool calls (router model) -> cited, structured answer (writer model).

The router can only call the typed tools in `agent.tools`; every input is validated
there. Tool results reach the writer wrapped as data, with `<` escaped so a retrieved
story can't close the wrapper. The answer is then checked in plain Python: unknown
citations and numbers that no tool result contains are reported, never hidden.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any

from anthropic.types import MessageParam, ToolParam, ToolResultBlockParam
from pydantic import BaseModel
from sqlalchemy import Connection, text

from fantasy_rag.agent.answer import Answer, problems
from fantasy_rag.agent.llm import LLMClient
from fantasy_rag.agent.prompts import ROUTER_SYSTEM, WRITER_SYSTEM
from fantasy_rag.agent.tools import TOOLS, run_tool
from fantasy_rag.analytics.queries import latest_week, league_roster

MAX_ROUNDS = 4


class MyLeague(BaseModel):
    league_id: int
    roster_id: int
    teams: int
    roster_positions: list[str]
    points_per_reception: float
    points_per_pass_td: float


@dataclass
class ToolCall:
    result_id: str
    name: str
    args: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class Turn:
    question: str
    calls: list[ToolCall]
    answer: Answer
    first_problems: list[str]  # checks the first draft failed
    problems: list[str]  # checks still failing after the repair; non-empty = withheld
    rejected: Answer | None
    hit_round_limit: bool
    latency_ms: int
    usage: dict[str, list[int]] = field(default_factory=dict)
    context: str = ""  # preserve the trusted context for later audits without live DB reads
    first_draft: Answer | None = None  # retain the failed draft even when repair succeeds

    @property
    def tools_called(self) -> list[str]:
        return [c.name for c in self.calls]


def my_leagues(conn: Connection, username: str) -> list[MyLeague]:
    """The user's roster in each imported league (matched on Sleeper display name)."""
    if not username:
        return []
    rows = conn.execute(
        text(
            """
            SELECT l.id, mine.roster_id, l.roster_positions, l.scoring_settings,
                   (SELECT count(*) FROM fantasy_rosters r
                    WHERE r.league_id = l.id AND r.week = mine.week) AS teams
            FROM fantasy_leagues l
            JOIN LATERAL (
                SELECT r.roster_id, r.week FROM fantasy_rosters r
                WHERE r.league_id = l.id AND lower(r.owner_name) = lower(:u)
                ORDER BY r.week DESC LIMIT 1
            ) mine ON true
            ORDER BY l.id
            """
        ),
        {"u": username},
    )
    return [
        MyLeague(
            league_id=r.id,
            roster_id=r.roster_id,
            teams=r.teams,
            roster_positions=r.roster_positions,
            points_per_reception=float(r.scoring_settings.get("rec", 0)),
            points_per_pass_td=float(r.scoring_settings.get("pass_td", 0)),
        )
        for r in rows
    ]


def context_block(conn: Connection, leagues: list[MyLeague], selected: int | None) -> str:
    season, week = latest_week(conn)
    lines = [
        f"Season {season}. Stats are complete through week {week}; the next games are"
        f" week {week + 1}.",
        "The user's leagues:" if leagues else "No leagues are known for this user.",
    ]
    lines += [f"- {lg.model_dump_json()}" for lg in leagues]
    if leagues:
        lines.append(f"Selected league: {selected}." if selected else "No league is selected.")
    mine = next((lg for lg in leagues if lg.league_id == selected), None)
    if mine is not None:  # full names let the router resolve "pierce" to the rostered player
        ids = league_roster(conn, mine.league_id, mine.roster_id)["player_ids"] or []
        rows = conn.execute(
            text(
                "SELECT full_name, position, team FROM players WHERE id = ANY(:ids) ORDER BY 2, 1"
            ),
            {"ids": ids},
        )
        lines.append(
            "Roster in the selected league: " + "; ".join(f"{n} ({p}, {t})" for n, p, t in rows)
        )
    return "<context>\n" + "\n".join(lines) + "\n</context>"


def tool_specs() -> list[ToolParam]:
    return [
        {"name": name, "description": desc, "input_schema": model.model_json_schema()}
        for name, (model, _, desc) in TOOLS.items()
    ]


def _as_data(value: Any) -> str:
    """JSON with `<` escaped: still valid JSON, but can't open or close a tag."""
    return json.dumps(value, default=str).replace("<", "\\u003c")


def route(
    conn: Connection, llm: LLMClient, question: str, context: str
) -> tuple[list[ToolCall], bool]:
    """Let the router call tools for up to MAX_ROUNDS rounds. Returns (calls, hit limit)."""
    messages: list[MessageParam] = [{"role": "user", "content": f"{context}\n\n{question}"}]
    tools = tool_specs()
    calls: list[ToolCall] = []
    for _ in range(MAX_ROUNDS):
        resp = llm.tool_turn(ROUTER_SYSTEM, messages, tools)
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses or resp.stop_reason != "tool_use":
            return calls, False
        results: list[ToolResultBlockParam] = []
        for b in uses:
            call = ToolCall(f"r{len(calls) + 1}", b.name, dict(b.input))
            try:
                call.result = run_tool(conn, b.name, call.args)
            except ValueError as e:  # bad input, unknown tool, ambiguous player, no data
                call.error = str(e)
            calls.append(call)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": call.error or _as_data(call.result),
                    "is_error": call.error is not None,
                }
            )
        messages += [
            {"role": "assistant", "content": resp.content},
            {"role": "user", "content": results},
        ]
    return calls, True


def _body(c: ToolCall) -> dict[str, Any]:
    return {"args": c.args, "error": c.error} if c.error else {"args": c.args, **(c.result or {})}


def writer_input(question: str, context: str, calls: list[ToolCall]) -> str:
    parts = [
        f'<result id="{c.result_id}" tool="{c.name}">{_as_data(_body(c))}</result>' for c in calls
    ]
    results = "\n".join(parts) or "(no tools were called)"
    return (
        f"{context}\n\n<question>{question}</question>\n\n"
        f"<tool_results>\n{results}\n</tool_results>"
    )


ECHOED_INPUTS = {"search_news": ("query", "since")}  # output fields that repeat the request


def sources(calls: list[ToolCall]) -> dict[str, str]:
    """Citable result IDs -> the evidence they hold: each call's returned data, plus each
    news story inside it. Request arguments, echoed inputs and error messages (which can
    quote the input) are not evidence, so a number the router typed can't support a claim."""
    out: dict[str, str] = {}
    for c in calls:
        if c.result is None:  # a failed call holds no evidence (see failed_ids)
            continue
        echoed = ECHOED_INPUTS.get(c.name, ())
        out[c.result_id] = _as_data({k: v for k, v in c.result.items() if k not in echoed})
        for r in (c.result or {}).get("results", []):
            if isinstance(r, dict) and "result_id" in r:  # a story carries its search's sync time
                out[r["result_id"]] = _as_data(
                    {**r, "last_news_sync": c.result.get("last_news_sync")}
                )
    return out


def failed_ids(calls: list[ToolCall]) -> set[str]:
    """Calls that errored: citable only by a clarification, never as evidence."""
    return {c.result_id for c in calls if c.result is None}


def _repair_input(content: str, answer: Answer, found: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in found)
    return (
        f"{content}\n\n<previous_answer>{_as_data(answer.model_dump())}</previous_answer>\n"
        f"<failed_checks>\n{listed}\n</failed_checks>\n\n"
        "Your previous answer failed these checks. Write it again so that every fact and"
        " projection cites the results it comes from, and every number appears in a result"
        " that the same claim cites. Drop a number rather than guess where it came from."
    )


def _withheld(found: list[str]) -> Answer:
    return Answer(
        answer="I couldn't match every number and source in my answer to the data, so I'm"
        " not showing it. Try asking about one player or one league at a time.",
        answer_type="no_data",
        season=None, week=None, scoring_format=None, data_as_of=None, model_version=None,
        facts=[], projections=[], citations=[],
        limitations=found,
    )  # fmt: skip


def ask(
    conn: Connection,
    llm: LLMClient,
    question: str,
    leagues: list[MyLeague] | None = None,
    selected: int | None = None,
) -> Turn:
    """Route, write, check. A failed check gets one repair attempt; if that fails too, the
    answer is withheld (kept in `rejected` for evaluation) and the user sees why."""
    start = time.monotonic()
    before = {m: list(v) for m, v in getattr(llm, "usage", {}).items()}
    context = context_block(conn, leagues or [], selected)
    calls, capped = route(conn, llm, question, context)
    content = writer_input(question, context, calls)
    srcs, failed = sources(calls), failed_ids(calls)
    answer = llm.structured(WRITER_SYSTEM, content, Answer)
    first = problems(answer, srcs, context, failed)
    first_draft = answer.model_copy(deep=True) if first else None
    found = first
    if first:
        answer = llm.structured(WRITER_SYSTEM, _repair_input(content, answer, first), Answer)
        found = problems(answer, srcs, context, failed)
    rejected = None
    if found:
        rejected, answer = answer, _withheld(found)
    if capped:
        answer.limitations.append(f"stopped after {MAX_ROUNDS} rounds of tool calls")
    return Turn(
        question=question,
        calls=calls,
        answer=answer,
        first_problems=first,
        problems=found,
        rejected=rejected,
        hit_round_limit=capped,
        latency_ms=int((time.monotonic() - start) * 1000),
        usage={
            m: [v[0] - before.get(m, [0, 0])[0], v[1] - before.get(m, [0, 0])[1]]
            for m, v in getattr(llm, "usage", {}).items()
        },
        context=context,
        first_draft=first_draft,
    )
