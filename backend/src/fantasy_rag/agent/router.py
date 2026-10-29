"""Question -> tool calls (router model) -> cited, structured answer (writer model).

The router can only call the typed tools in `agent.tools`; every input is validated
there. Tool results reach the writer wrapped as data, with `<` escaped so a retrieved
story can't close the wrapper. The answer is then checked in plain Python: unknown
citations and numbers that no tool result contains are reported, never hidden.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from anthropic.types import MessageParam, ToolParam, ToolResultBlockParam
from pydantic import BaseModel
from sqlalchemy import Connection, text

from fantasy_rag.agent.answer import Answer, problems
from fantasy_rag.agent.llm import INVALID_ARGS, Cancelled, LLMClient
from fantasy_rag.agent.prompts import ROUTER_SYSTEM, WRITER_SYSTEM
from fantasy_rag.agent.tools import TOOLS, run_tool
from fantasy_rag.analytics.queries import (
    AmbiguousPlayer,
    latest_week,
    league_roster,
    projection_week,
    resolve_player,
)
from fantasy_rag.analytics.queries import _normalize as normalize_name

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
    _, target = projection_week(conn)
    lines = [
        f"Season {season}. Stats are available through week {week} (possibly partial)."
        f" Projection week: {target}. Started players cannot be moved into or out of"
        " their current lineup slots; optimizer totals are projections, not live scores.",
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


@dataclass(frozen=True)
class Progress:
    """A status line for the user. Written by this module from fixed templates and data
    the tools resolved, never from model text."""

    stage: Literal["context", "tool", "writing", "checking", "repairing"]
    message: str
    tool: str | None = None
    ok: bool | None = None


def _noop(_: Progress) -> None:
    return None


TOOL_LABELS = {
    "get_player_stats": "stats",
    "compare_players": "stats",
    "defense_vs_position": "points allowed",
    "get_injury_status": "injury status",
    "get_trending": "trending adds and drops",
    "project_player": "a projection",
    "compare_start_sit": "projections",
    "optimize_lineup": "your best lineup",
    "search_news": "recent news",
    "rank_waivers": "waiver options",
    "get_usage": "usage",
}


def _resolved_names(result: dict[str, Any]) -> list[str]:
    found = [result.get("player")]
    found += [r.get("player") for r in result.get("ranked", [])]
    found += [p.get("player", p) for p in result.get("players") or [] if isinstance(p, dict)]
    return [p["name"] for p in found if isinstance(p, dict) and isinstance(p.get("name"), str)]


def _failure_kind(error: str) -> str:
    """The class of a tool error, never its text (which can quote the model's input)."""
    if "matches no single player" in error:
        return "name matches several players" if "Candidates:" in error else "no such player"
    if error.startswith("unknown tool"):
        return "not an available tool"
    if "validation error" in error:
        return "invalid request"
    return "no data for that request"


def tool_progress(call: ToolCall) -> Progress:
    label = TOOL_LABELS.get(call.name, "data")
    if call.error is not None or call.result is None:
        what = "Player lookup" if "player" in (call.error or "") else f"Looking up {label}"
        return Progress("tool", f"{what} failed: {_failure_kind(call.error or '')}",
                        call.name, False)  # fmt: skip
    names = _resolved_names(call.result)
    who = f" for {', '.join(dict.fromkeys(names))}" if names else ""
    league = call.args.get("league_id")
    rules = f" (league {league} rules)" if isinstance(league, int) else ""
    return Progress("tool", f"Looked up {label}{who}{rules}", call.name, True)


# Surnames of active fantasy players that are also everyday question words ("would love
# to", "is he a lock"). shortcut: a hand list from the 2026 surnames; extend when a new
# player's surname reads as an ordinary word.
COMMON_WORDS = frozenset({
    "all", "bass", "black", "bond", "branch", "brooks", "brown", "cook", "cooks", "dart", "downs",
    "fields", "flowers", "folk", "gay", "golden", "gray", "hall", "helm", "hill", "homer", "horn",
    "knight", "lamb", "lane", "lemon", "likely", "little", "lock", "long", "love", "marks",
    "muse", "price", "reed", "rice", "rivers", "rush", "sample", "sharp", "smack", "steele",
    "strand", "strange", "strong", "swift", "white", "woods", "worthy", "young",
})  # fmt: skip


def _named_players(args: dict[str, Any]) -> list[dict[str, Any]]:
    many = args.get("players")
    found: list[Any] = [args.get("player"), *(many if isinstance(many, list) else [])]
    # The tools accept a bare name too (PlayerName._name_only), so check both forms.
    found = [{"name": p} if isinstance(p, str) else p for p in found]
    return [p for p in found if isinstance(p, dict) and isinstance(p.get("name"), str)]


def _question_words(question: str) -> set[str]:
    """Words normalized like players.search_name, possessives dropped ("Jefferson's")."""
    return set(normalize_name(re.sub(r"['\u2019]s\b", "", question)).split())


def _surname(name: str) -> str:
    parts = normalize_name(name).split()  # suffixes (Jr., III) are already gone
    return parts[-1] if parts else ""


def swapped_player(
    conn: Connection,
    question: str,
    args: dict[str, Any],
    covered: set[str] | None = None,
) -> str | None:
    """A looked-up player the question never names, while it does name another player of
    his position by surname ("is loop safe?" answered with the roster's other kicker).
    `covered`: surnames of players other calls already resolved successfully; a named
    player covered there (or in this same call) makes the roster player a fair comparison.

    Only a same-position, exact-surname match counts, and surnames that are everyday
    words are skipped, so ordinary questions don't trigger it.
    """
    words = _question_words(question)
    named = _named_players(args)
    in_call = {_surname(p["name"]) for p in named}  # all resolve, or the call fails
    surnames = words - COMMON_WORDS - in_call - (covered or set())
    if not surnames or not named:
        return None
    season, _ = latest_week(conn)
    for p in named:
        if _surname(p["name"]) in words:
            continue  # the question names him (a shared first name doesn't count)
        try:
            ref = resolve_player(conn, p["name"], p.get("position"), p.get("team"))
        except (AmbiguousPlayer, ValueError):
            continue
        other = conn.execute(
            text(
                "SELECT p.full_name, p.position, p.team FROM players p"
                " WHERE p.position = :pos AND p.id <> :id"
                " AND split_part(p.search_name, ' ', -1) = ANY(:words)"
                " AND EXISTS (SELECT 1 FROM player_week_stats s"
                "             WHERE s.player_id = p.id AND s.season = :season)"
                " ORDER BY p.full_name LIMIT 1"
            ),
            {"pos": ref.position, "id": ref.id, "words": sorted(surnames), "season": season},
        ).first()
        if other is not None:
            return (
                f"The question names {other.full_name} ({other.position}, {other.team}), not"
                f" {ref.name}; nothing was run. Call this tool again for {other.full_name}."
            )
    return None


def route(
    conn: Connection,
    llm: LLMClient,
    question: str,
    context: str,
    on_progress: Callable[[Progress], None] = _noop,
    cancelled: Callable[[], bool] = bool,
    calls: list[ToolCall] | None = None,
) -> tuple[list[ToolCall], bool]:
    """Let the router call tools for up to MAX_ROUNDS rounds. Returns (calls, hit limit).
    Each call is appended to `calls` when it finishes, so a caller keeps them if a later
    step fails."""
    messages: list[MessageParam] = [{"role": "user", "content": f"{context}\n\n{question}"}]
    tools = tool_specs()
    calls = [] if calls is None else calls
    for _ in range(MAX_ROUNDS):
        if cancelled():
            raise Cancelled
        resp = llm.tool_turn(ROUTER_SYSTEM, messages, tools)
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses or resp.stop_reason != "tool_use":
            return calls, False
        done: dict[str, ToolResultBlockParam] = {}

        def covered() -> set[str]:  # only lookups that succeeded count
            return {_surname(n) for c in calls if c.result for n in _resolved_names(c.result)}

        # A call that looks like a swap waits until this round's other calls ran: one of
        # them may resolve the named player, which makes it a fair comparison.
        first = [b for b in uses if INVALID_ARGS in b.input
                 or not swapped_player(conn, question, dict(b.input), covered())]  # fmt: skip
        for b in first + [b for b in uses if b not in first]:
            if cancelled():
                raise Cancelled
            call = ToolCall(f"r{len(calls) + 1}", b.name, dict(b.input))
            try:
                if INVALID_ARGS in call.args:
                    raise ValueError("tool arguments were not valid JSON; nothing was run")
                if swap := swapped_player(conn, question, call.args, covered()):
                    raise ValueError(swap)
                call.result = run_tool(conn, b.name, call.args)
            except ValueError as e:  # bad input, unknown tool, ambiguous player, no data
                call.error = str(e)
            calls.append(call)
            on_progress(tool_progress(call))
            done[b.id] = {
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": call.error or _as_data(call.result),
                "is_error": call.error is not None,
            }
        results = [done[b.id] for b in uses]
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


WITHHELD_MESSAGE = (
    "I couldn't match every number and source in my answer to the data, so I'm not showing"
    " it. Try asking about one player or one league at a time."
)


def _withheld() -> Answer:
    # The failed checks quote the rejected draft, so they stay in Turn.problems (and the
    # internal log); the user only learns that verification failed.
    return Answer(
        answer=WITHHELD_MESSAGE,
        answer_type="no_data",
        season=None, week=None, scoring_format=None, data_as_of=None, model_version=None,
        facts=[], projections=[], citations=[],
        limitations=["The answer could not be verified against the data."],
    )  # fmt: skip


def ask(
    conn: Connection,
    llm: LLMClient,
    question: str,
    leagues: list[MyLeague] | None = None,
    selected: int | None = None,
    on_progress: Callable[[Progress], None] = _noop,
    cancelled: Callable[[], bool] = bool,
    calls: list[ToolCall] | None = None,
) -> Turn:
    """Route, write, check. A failed check gets one repair attempt; if that fails too, the
    answer is withheld (kept in `rejected` for evaluation) and the user sees why.
    `cancelled` is polled before every model and tool call; it raises `Cancelled`."""
    start = time.monotonic()
    before = {m: list(v) for m, v in getattr(llm, "usage", {}).items()}
    on_progress(Progress("context", "Reading the season and your league context"))
    context = context_block(conn, leagues or [], selected)
    calls, capped = route(conn, llm, question, context, on_progress, cancelled, calls)
    content = writer_input(question, context, calls)
    srcs, failed = sources(calls), failed_ids(calls)
    if cancelled():
        raise Cancelled
    on_progress(Progress("writing", "Writing the answer"))
    answer = llm.structured(WRITER_SYSTEM, content, Answer)
    on_progress(Progress("checking", "Checking every number and source"))
    first = problems(answer, srcs, context, failed)
    first_draft = answer.model_copy(deep=True) if first else None
    found = first
    if first:
        if cancelled():
            raise Cancelled
        on_progress(Progress("repairing", "Fixing a failed check"))
        answer = llm.structured(WRITER_SYSTEM, _repair_input(content, answer, first), Answer)
        on_progress(Progress("checking", "Checking every number and source"))
        found = problems(answer, srcs, context, failed)
    rejected = None
    if found:
        rejected, answer = answer, _withheld()
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
