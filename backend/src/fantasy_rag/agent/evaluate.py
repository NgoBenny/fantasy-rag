"""`fantasy-rag eval answers`: the Phase 6 golden set through the real router and writer.

60 questions (20 stats, 20 news, 15 mixed, 5 out-of-scope) are scored on routing, and the
four no-support news questions on the answer only (it must not confirm the rumour).

- tool selection: the expected tools were called (`a|b` = either)
- route: tool selection, plus the critical arguments (stats: the same player, weeks and
  scoring as the golden call, compared on the tools' resolved output; mixed: the selected
  league and the user's roster) and no unexpected tool errors
- grounding: every answer must pass `answer.problems`; a failing answer gets one repair,
  and `withheld` lists those that still failed (the user would see no answer)

Scores are reported under the original rubric and under the revised one
(`route_overrides` in golden_answers.yaml). Every row keeps the full answer, the rejected
draft and each tool call with its result, so answers can be audited without rerunning.
"""

import json
import statistics
from typing import Any

import yaml
from sqlalchemy import Connection

from fantasy_rag.agent.llm import AnthropicLLM
from fantasy_rag.agent.router import MyLeague, ToolCall, Turn, ask, my_leagues
from fantasy_rag.agent.tools import TOOLS, run_tool
from fantasy_rag.config import REPO_ROOT

EVAL = REPO_ROOT / "backend" / "tests" / "eval"
HELD_APART = {"no_support", "fresh"}  # not in the 60-question route score


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((EVAL / name).read_text(encoding="utf-8"))
    return data


def _alts(tools: list[str]) -> list[list[str]]:
    return [t.split("|") for t in tools]


def golden_questions() -> list[dict[str, Any]]:
    """Each: id, set, question, tools, context, expect_type, golden_call, override."""
    mine: dict[str, Any] = {"leagues": "mine", "selected": None}
    out = [
        {"id": s["id"], "set": "stats", "question": s["question"], "tools": [[s["tool"]]],
         "context": mine, "expect_type": None, "golden_call": (s["tool"], s["args"])}
        for s in _load("golden.yaml")["stats"]
    ] + [
        {"id": n["id"], "set": "news", "question": n["question"], "tools": [["search_news"]],
         "context": mine, "expect_type": None}
        for n in _load("golden_news.yaml")["news"]
    ]  # fmt: skip
    answers = _load("golden_answers.yaml")
    for m in answers["mixed"]:
        ambiguous = m["category"].startswith("ambiguous")
        out.append({**m, "set": "mixed", "tools": _alts(m["tools"]),
                    "expect_type": "clarification" if ambiguous else None})  # fmt: skip
    fresh = _load("golden_fresh.yaml")
    for f in fresh["fresh"]:  # held out: scored apart from the 60
        out.append({**f, "set": "fresh", "tools": _alts(f["tools"]),
                    "expect_type": f.get("expect_type")})  # fmt: skip
    for o in answers["out_of_scope"]:
        out.append({**o, "set": "out_of_scope", "tools": [], "context": mine,
                    "expect_type": "out_of_scope"})  # fmt: skip
    for h in _load("golden_news_heldout.yaml")["heldout"]:
        if h["expect"] == "phase6":
            out.append({"id": h["id"], "set": "no_support", "question": h["question"],
                        "tools": None, "context": mine, "expect_type": "no_data"})  # fmt: skip
    overrides = (answers.get("route_overrides") or {}) | (fresh.get("route_overrides") or {})
    for q in out:
        q["override"] = overrides.get(q["id"])
    return out


def _identity(result: dict[str, Any] | None) -> list[str]:
    """What a stats call resolved to: player IDs, season, weeks, scoring, team, position."""
    if result is None:
        return []
    players = result.get("players")
    blocks = (
        players if isinstance(players, list) and players and "player" in players[0] else [result]
    )  # noqa: E501
    keys = ("season", "weeks", "scoring", "team", "position", "kind")
    return sorted(
        json.dumps({"player": (b.get("player") or {}).get("id"), **{k: b.get(k) for k in keys}},
                   sort_keys=True, default=str)
        for b in blocks
    )  # fmt: skip


def route_problems(
    q: dict[str, Any],
    turn: Turn,
    tools: list[list[str]],
    golden_identity: list[str] | None,
    mine: dict[int, int],
) -> tuple[bool, list[str]]:
    """(tools selected, everything wrong with the route)."""
    called = turn.tools_called
    selected = all(any(t in called for t in alts) for alts in tools) if tools else not called
    out = [] if selected else [f"expected {['|'.join(a) for a in tools] or 'no tools'}"]
    if not q.get("category", "").startswith("ambiguous"):
        out += [f"{c.name} failed: {c.error}" for c in turn.calls if c.error]
    if golden_identity is not None and selected:
        tool = q["golden_call"][0]
        if not any(_identity(c.result) == golden_identity for c in turn.calls if c.name == tool):
            out.append(f"{tool} not called for the golden player/weeks/scoring")
    out += [f"unknown tool {c.name}" for c in turn.calls if c.name not in TOOLS]
    known = [c for c in turn.calls if c.name in TOOLS]
    sel = q["context"].get("selected")
    for c in known if sel else []:
        if "league_id" in TOOLS[c.name][0].model_fields and c.args.get("league_id") != sel:
            out.append(f"{c.name} used league {c.args.get('league_id')}, not {sel}")
        if c.name == "optimize_lineup" and c.args.get("roster_id") != mine.get(sel):
            out.append(f"optimize_lineup used roster {c.args.get('roster_id')}")
    for req in q.get("require") or []:
        out += _requirement_problems(req, known)
    return selected, out


def _resolved_names(result: dict[str, Any] | None) -> set[str]:
    """Players a call actually resolved to (or, for a lineup, the roster it read)."""
    if not result:
        return set()
    found: list[Any] = [result.get("player")]
    found += [r.get("player") for r in result.get("ranked", [])]
    found += [p.get("player", p) for p in result.get("players") or [] if isinstance(p, dict)]
    found += [s.get("player") or s.get("current") for s in result.get("recommended", [])]
    found += result.get("bench", [])
    return {p["name"].lower() for p in found if isinstance(p, dict) and p.get("name")}


def _requirement_problems(req: dict[str, Any], calls: list[ToolCall]) -> list[str]:
    """A question-specific constraint: some call of `tool` (a|b) with this `position`,
    resolving to every name in `players` and using every league in `leagues` (a|b)."""
    alts = req["tool"].split("|")
    matching = [
        c for c in calls
        if c.name in alts and ("position" not in req or c.args.get("position") == req["position"])
    ]  # fmt: skip
    label = req["tool"] + (f" position={req['position']}" if "position" in req else "")
    if not matching:
        return [f"no {label} call"]
    out = []
    names = set().union(*(_resolved_names(c.result) for c in matching))
    out += [f"{label} never resolved {p}" for p in req.get("players", []) if p.lower() not in names]
    used = {c.args.get("league_id") for c in matching}
    for lg in req.get("leagues", []):
        if not any(int(x) in used for x in str(lg).split("|")):
            out.append(f"{label} never used league {lg}")
    return out


def grade(
    q: dict[str, Any], turn: Turn, golden_identity: list[str] | None, mine: dict[int, int]
) -> dict[str, Any]:
    a = turn.answer
    row: dict[str, Any] = {"id": q["id"], "set": q["set"], "question": q["question"]}
    if q["tools"] is not None:
        rubrics = {"original": q["tools"]}
        ov = q["override"] or {}
        rubrics["revised"] = _alts(ov["tools"]) if "tools" in ov else q["tools"]
        for name, tools in rubrics.items():
            # an override may also replace the question's intent checks (revised only)
            qr = {**q, "require": ov["require"]} if name == "revised" and "require" in ov else q
            selected, wrong = route_problems(qr, turn, tools, golden_identity, mine)
            if name == "revised" and ov.get("also_accept") == a.answer_type and not turn.calls:
                selected, wrong = True, []
            row[f"{name}_tools_ok"], row[f"{name}_route_ok"] = selected, not wrong
            row[f"{name}_route_problems"] = wrong
        row["override"] = q["override"]
    row |= {
        "expect_type": q["expect_type"],
        "type_ok": None if q["expect_type"] is None else (
            a.answer_type in ("no_data", "out_of_scope") if q["expect_type"] == "no_data"
            else a.answer_type == q["expect_type"]),
        "answer": a.model_dump(),
        "context": turn.context,
        "correct_if": q.get("correct_if"),
        "fail_if": q.get("fail_if"),
        "first_draft_problems": turn.first_problems,
        "first_draft": turn.first_draft.model_dump() if turn.first_draft else None,
        "problems": turn.problems,
        "rejected": turn.rejected.model_dump() if turn.rejected else None,
        "calls": [{"result_id": c.result_id, "tool": c.name, "args": c.args, "error": c.error,
                   "result": c.result} for c in turn.calls],
        "hit_round_limit": turn.hit_round_limit,
        "latency_ms": turn.latency_ms,
        "usage": turn.usage,
    }  # fmt: skip
    return row


def _context(q: dict[str, Any], leagues: list[MyLeague]) -> tuple[list[MyLeague], int | None]:
    ctx = q["context"]
    return (leagues if ctx.get("leagues") == "mine" else []), ctx.get("selected")


def _rate(rows: list[dict[str, Any]], key: str) -> float | None:
    return round(sum(r[key] for r in rows) / len(rows), 3) if rows else None


def run_golden(
    conn: Connection, llm: AnthropicLLM, username: str, only: set[str] | None = None
) -> dict[str, Any]:
    leagues = my_leagues(conn, username)
    mine = {lg.league_id: lg.roster_id for lg in leagues}
    rows = []
    for q in golden_questions():
        if only and q["id"] not in only and q["set"] not in only:
            continue
        golden = None
        if "golden_call" in q:
            golden = _identity(run_tool(conn, *q["golden_call"]))  # direct call, no LLM
        scoped, selected = _context(q, leagues)
        turn = ask(conn, llm, q["question"], scoped, selected)
        rows.append(grade(q, turn, golden, mine))
    summary = _summary(rows) | {"tokens": llm.usage, "cost_usd": round(llm.cost_usd(), 4)}
    return {"summary": summary, "rows": rows}


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    routed = [r for r in rows if "original_route_ok" in r and r["set"] not in HELD_APART]
    fresh = [r for r in rows if r["set"] == "fresh"]
    typed = [r for r in rows if r["type_ok"] is not None]
    lat = sorted(r["latency_ms"] for r in rows)
    out: dict[str, Any] = {"questions": len(rows), "routed_questions": len(routed)}
    for rubric in ("original", "revised"):
        out[rubric] = {
            "tool_selection_accuracy": _rate(routed, f"{rubric}_tools_ok"),
            "route_accuracy": _rate(routed, f"{rubric}_route_ok"),
            "route_misses": {r["id"]: r[f"{rubric}_route_problems"] for r in routed
                             if not r[f"{rubric}_route_ok"]},
        }  # fmt: skip
    if fresh:  # held-out questions: never part of the 60-question rubric
        out["fresh"] = {
            rubric: {
                "route_accuracy": _rate(fresh, f"{rubric}_route_ok"),
                "route_misses": {r["id"]: r[f"{rubric}_route_problems"] for r in fresh
                                 if not r[f"{rubric}_route_ok"]},
            }
            for rubric in ("original", "revised")
        }  # fmt: skip
    return out | {
        "repaired": [r["id"] for r in rows if r["first_draft_problems"] and not r["problems"]],
        "withheld": [r["id"] for r in rows if r["problems"]],
        "answer_type_misses": [r["id"] for r in typed if not r["type_ok"]],
        "latency_ms_p50": statistics.median(lat) if lat else None,
        "latency_ms_p95": lat[int(0.95 * (len(lat) - 1))] if lat else None,
    }


def rescore(conn: Connection, report: dict[str, Any], username: str) -> dict[str, Any]:
    """Grade a saved run's calls and answers again under the current rubric (no LLM)."""
    from fantasy_rag.agent.answer import Answer

    mine = {lg.league_id: lg.roster_id for lg in my_leagues(conn, username)}
    questions = {q["id"]: q for q in golden_questions()}
    rows = []
    for old in report["rows"]:
        q = questions[old["id"]]
        calls = [ToolCall(c["result_id"], c["tool"], c["args"], c["result"], c["error"])
                 for c in old["calls"]]  # fmt: skip
        rejected = Answer.model_validate(old["rejected"]) if old["rejected"] else None
        first_draft = Answer.model_validate(old["first_draft"]) if old.get("first_draft") else None
        turn = Turn(q["question"], calls, Answer.model_validate(old["answer"]),
                    old["first_draft_problems"], old["problems"], rejected,
                    old["hit_round_limit"], old["latency_ms"], old.get("usage", {}),
                    context=old.get("context", ""), first_draft=first_draft)  # fmt: skip
        golden = _identity(run_tool(conn, *q["golden_call"])) if "golden_call" in q else None
        rows.append(grade(q, turn, golden, mine))
    return {"summary": _summary(rows) | {"rescored_from": report["summary"]}, "rows": rows}
