"""`POST /api/chat`: the Phase 6 agent over Server-Sent Events.

The browser only ever sees text this module writes or validated answers: `progress`
events come from fixed templates (agent.router.Progress), the answer arrives once, after
the Phase 6 checks, together with its sources, and a rejected draft never leaves the
server. Event order: progress* -> exactly one of answer | withheld | error -> done.

The agent runs in a bounded thread pool; the worker owns its connections (a read-only
one for tools, a separate insert-only one for the log row) and closes them before it
ends. Events reach the response through an asyncio.Queue fed with call_soon_threadsafe,
so the event loop never blocks. If the client disconnects, the stream's `finally` sets
a flag the agent polls before every model and tool call. A retry is a new request.
"""

import asyncio
import json
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cache
from typing import Any

import anthropic
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from fantasy_rag.agent.budget import Budget, BudgetExceeded
from fantasy_rag.agent.llm import LLMClient, LLMUnavailable, price
from fantasy_rag.agent.router import (
    TOOL_LABELS,
    WITHHELD_MESSAGE,
    Cancelled,
    Progress,
    ToolCall,
    Turn,
    _failure_kind,
    _resolved_names,
    ask,
    my_leagues,
)
from fantasy_rag.analytics.queries import projection_week

WORKERS = 4  # shortcut: per-process pool and cap; one uvicorn worker is assumed
_pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="chat")
_slots = threading.BoundedSemaphore(WORKERS)

ERRORS = {
    "llm_unavailable": (
        "The answer service is unavailable right now. Stats, projections and lineups still work.",
        True,
    ),
    "budget_exceeded": ("The spending cap for answers has been reached.", False),
    "db_unavailable": ("The database is unavailable right now. Try again shortly.", True),
    "busy": ("Too many questions at once. Try again in a moment.", True),
    "internal": ("Something went wrong while answering. Try again.", True),
}


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    league_id: int | None = None


@dataclass
class ChatServices:
    """What a live chat needs; tests replace it (fake LLM, scratch engines)."""

    tools_engine: Engine
    log_engine: Engine | None
    llm: Callable[[], LLMClient]
    username: str


@cache
def process_budget(cap: float | None) -> Budget | None:
    return None if cap is None else Budget(cap)


def sse(seq: int, event: str, data: dict[str, Any]) -> bytes:
    return f"id: {seq}\nevent: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


def _cited(turn: Turn) -> list[str]:
    a = turn.answer
    ids = list(a.citations) + [i for c in a.facts + a.projections for i in c.source_ids]
    return list(dict.fromkeys(ids))


def _href(call: ToolCall) -> str | None:
    res, league = call.result or {}, call.args.get("league_id")
    rows = res.get("ranked") or res.get("players") or []
    ids = ",".join(p["id"] for p in (r.get("player") for r in rows) if p)
    scoring = call.args.get("scoring", "ppr")  # the tool's default when none was given
    if call.name == "compare_start_sit" and isinstance(league, int):
        return f"/league?league={league}&view=startsit&ids={ids}"
    if call.name in ("compare_start_sit", "compare_players") and league is None:
        return f"/compare?ids={ids}&scoring={scoring}"  # projections + stats, same scoring
    if call.name == "optimize_lineup" and isinstance(league, int):
        return f"/league?league={league}&roster={call.args.get('roster_id')}&view=lineup"
    if call.name == "rank_waivers" and isinstance(league, int):
        return f"/league?league={league}&view=waivers"
    if call.name == "get_usage" and ids and res.get("through_week"):
        # Each player's same last N games through the same week, at the usage section.
        window = (f"season={res['season']}&end_week={res['through_week']}"
                  f"&usage_games={res['last_n']}#usage")  # fmt: skip
        return f"/compare?ids={ids}&{window}" if "," in ids else f"/players/{ids}?{window}"
    player = res.get("player")
    if isinstance(player, dict) and player.get("id"):
        return f"/players/{player['id']}"
    return None


def sources(turn: Turn) -> list[dict[str, Any]]:
    """The answer's cited results only, ready to show (never raw tool output)."""
    calls = {c.result_id: c for c in turn.calls}
    stories = {
        h["result_id"]: h
        for c in turn.calls
        for h in (c.result or {}).get("results", [])
        if isinstance(h, dict) and "result_id" in h
    }
    out = []
    for i in _cited(turn):
        if i in stories:
            h = stories[i]
            out.append({"id": i, "kind": "news", "label": h.get("title"), "href": h.get("url"),
                        "published_at": h.get("published_at"), "source": h.get("source"),
                        "failed": False})  # fmt: skip
        elif i in calls:
            c = calls[i]
            if c.result is None:
                out.append({"id": i, "kind": "tool", "tool": c.name, "href": None, "failed": True,
                            "label": f"Lookup failed: {_failure_kind(c.error or '')}"})  # fmt: skip
                continue
            names = ", ".join(dict.fromkeys(_resolved_names(c.result)))
            label = TOOL_LABELS.get(c.name, "data").capitalize() + (f": {names}" if names else "")
            out.append({"id": i, "kind": "tool", "tool": c.name, "label": label,
                        "href": _href(c), "failed": False})  # fmt: skip
    return out


def _cost(usage: dict[str, list[int]]) -> float:
    return sum((i * price(m)[0] + o * price(m)[1]) / 1e6 for m, (i, o) in usage.items())


def _log(svc: ChatServices, row: dict[str, Any]) -> int | None:
    """One query_log row via the insert-only role; a failure never breaks the answer."""
    if svc.log_engine is None:
        return None
    try:
        with svc.log_engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO query_log (question, tools_called, tool_inputs, chunk_ids,"
                    " answer, latency_ms, status, answer_json, problems, first_problems,"
                    " league_id, error, progress_first_ms, answer_ms, usage, cost_usd, demo)"
                    " VALUES (:question, :tools_called, :tool_inputs, :chunk_ids, :answer,"
                    " :latency_ms, :status, :answer_json, :problems, :first_problems,"
                    " :league_id, :error, :progress_first_ms, :answer_ms, :usage, :cost_usd,"
                    " false)"
                ),
                row,
            )
            # INSERT ... RETURNING would need SELECT; currval needs only USAGE.
            qid: int = c.execute(text("SELECT currval('query_log_id_seq')")).scalar_one()
            return qid
    except Exception as e:  # logged to stderr, the user still gets the answer
        print(f"query_log write failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def run_chat(
    svc: ChatServices,
    req: ChatRequest,
    emit: Callable[[str, dict[str, Any]], None],
    cancelled: Callable[[], bool],
) -> None:
    """The worker: always emits exactly one terminal event (unless cancelled) and done."""
    start = time.monotonic()
    first: list[int] = []

    def ms() -> int:
        return int((time.monotonic() - start) * 1000)

    def progress(p: Progress) -> None:
        if not first:
            first.append(ms())
        data: dict[str, Any] = {
            "demo": False,
            "stage": p.stage,
            "message": p.message,
            "elapsed_ms": ms(),
        }
        if p.tool:
            data |= {"tool": p.tool, "ok": p.ok}
        emit("progress", data)

    def fail(code: str) -> None:
        msg, retry = ERRORS[code]
        emit("error", {"demo": False, "code": code, "message": msg, "retryable": retry})

    status, error, turn, answer_ms = "error", None, None, None
    # Kept outside the Turn so a failure after routing still logs what was done and spent.
    llm: LLMClient | None = None
    calls: list[ToolCall] = []
    try:
        with svc.tools_engine.connect() as conn:
            leagues = my_leagues(conn, svc.username)
            season, week = projection_week(conn)
            llm = svc.llm()
            turn = ask(conn, llm, req.question, leagues, req.league_id, progress, cancelled,
                       calls)  # fmt: skip
        answer_ms = ms()
        if turn.problems:
            status = "withheld"
            emit("withheld", {"demo": False, "message": WITHHELD_MESSAGE,
                              "reason": "verification_failed"})  # fmt: skip
        else:
            status = "repaired" if turn.first_problems else "ok"
            if turn.answer.answer_type == "clarification":
                status = "clarification"
            emit("answer", {"demo": False, "status": "repaired" if turn.first_problems else "ok",
                            "answer": turn.answer.model_dump(), "sources": sources(turn),
                            "as_of": {"season": season, "next_week": week}})  # fmt: skip
    except Cancelled:
        status = "cancelled"
    except BudgetExceeded as e:
        status, error = "budget", str(e)
        fail("budget_exceeded")
    except (LLMUnavailable, anthropic.APIError) as e:
        error = f"{type(e).__name__}: {e}"
        fail("llm_unavailable")
    except OperationalError as e:
        error = f"OperationalError: {str(e)[:300]}"
        fail("db_unavailable")
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:300]}"
        fail("internal")
    finally:
        # The client is built per request, so its tally is this request's usage.
        usage: dict[str, list[int]] = getattr(llm, "usage", {})
        unpriced: int = getattr(llm, "unpriced", 0)
        # A call whose usage is unknown makes the total unknown: never log it as a number.
        cost = round(_cost(usage), 6) if usage and not unpriced else None
        qid = _log(svc, {
            "question": req.question,
            "tools_called": json.dumps([c.name for c in calls]),
            "tool_inputs": json.dumps([c.args for c in calls], default=str),
            "chunk_ids": [int(i.split(":")[1]) for i in _cited(turn) if i.startswith("news:")]
            if turn and status not in ("withheld",) else [],
            "answer": turn.answer.answer if turn and status != "withheld" else None,
            "latency_ms": ms(),
            "status": status,
            "answer_json": turn.answer.model_dump_json() if turn and status != "withheld" else None,
            "problems": json.dumps(turn.problems if turn else []),
            "first_problems": json.dumps(turn.first_problems if turn else []),
            "league_id": req.league_id,
            "error": error,
            "progress_first_ms": first[0] if first else None,
            "answer_ms": answer_ms,
            "usage": json.dumps({"tokens": usage, "unpriced_calls": unpriced}),
            "cost_usd": cost,
        })  # fmt: skip
        emit("done", {"demo": False, "query_id": qid, "logged": qid is not None,
                      "progress_first_ms": first[0] if first else None,
                      "answer_ms": answer_ms, "cost_usd": cost})  # fmt: skip


async def stream_chat(svc: ChatServices, req: ChatRequest) -> AsyncIterator[bytes]:
    """Relay the worker's events; closing the stream (client gone) cancels the agent."""
    if not _slots.acquire(blocking=False):
        msg, retry = ERRORS["busy"]
        yield sse(1, "error", {"demo": False, "code": "busy", "message": msg, "retryable": retry})
        yield sse(2, "done", {"demo": False, "query_id": None, "logged": False,
                              "progress_first_ms": None, "answer_ms": None})  # fmt: skip
        return
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
    stop = threading.Event()

    def emit(event: str, data: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    def work() -> None:
        try:
            run_chat(svc, req, emit, stop.is_set)
        finally:
            _slots.release()
            loop.call_soon_threadsafe(queue.put_nowait, None)

    loop.run_in_executor(_pool, work)
    seq = 0
    try:
        while (item := await queue.get()) is not None:
            seq += 1
            yield sse(seq, *item)
    finally:
        stop.set()
