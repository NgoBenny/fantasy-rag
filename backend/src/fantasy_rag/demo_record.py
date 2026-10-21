"""`fantasy-rag demo record`: build data/demo/demo.json for DEMO_MODE.

Free by default. Real NFL players' stats, projections and news come from the dev
database; the league is a synthetic "Demo League" (no Sleeper IDs, owners or names from
real leagues). Chat answers are reused from a saved Phase 6 evaluation report: the
validated answer and its cited sources exactly as produced, with progress lines rebuilt
by the same templates the live API uses (their timing is approximate). Questions with
no saved answer can be recorded live only with `--record-question` and `--budget-usd` (paid); they
are kept in data/demo/recorded_chats.json and included in every rebuild.
"""

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import Connection, text

from fantasy_rag.agent import tools
from fantasy_rag.agent.answer import Answer
from fantasy_rag.agent.llm import LLMClient
from fantasy_rag.agent.router import (
    WITHHELD_MESSAGE,
    Progress,
    ToolCall,
    Turn,
    ask,
    tool_progress,
)
from fantasy_rag.analytics import queries as q
from fantasy_rag.analytics.optimizer import Candidate, optimize
from fantasy_rag.analytics.scoring import ScoringRules
from fantasy_rag.api.chat import sources
from fantasy_rag.config import REPO_ROOT

OUT = REPO_ROOT / "data" / "demo" / "demo.json"
RECORDED = REPO_ROOT / "data" / "demo" / "recorded_chats.json"  # paid live recordings
LABEL = "Sample data from week 7, 2026, not live"
DEMO_LEAGUE = 900
DEMO_SCORING = "Demo League rules (half PPR)"
# Half PPR plus a rule we can't compute yet (a short-reception bonus), so the demo shows
# the same "projections leave out" warning a real league with such a rule gets.
DEMO_RULES = ScoringRules.from_sleeper({**q.PRESETS["half"], "rec_0_4": 0.5})
SAVED = {"s05": None, "n03": None, "a08": None, "a13": None, "o02": None}  # league-free
PLAYERS = [  # (name, position) of real players shown in the demo
    ("Josh Allen", "QB"), ("Bijan Robinson", "RB"), ("Jaylen Warren", "RB"),
    ("Bucky Irving", "RB"), ("Ja'Marr Chase", "WR"), ("A.J. Brown", "WR"),
    ("Puka Nacua", "WR"), ("Justin Jefferson", "WR"), ("Trey McBride", "TE"),
    ("Jahmyr Gibbs", "RB"), ("Brock Purdy", "QB"), ("Tyler Loop", "K"), ("CeeDee Lamb", "WR"),
]  # fmt: skip
ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K"] + ["BN"] * 5
DEMO_ROSTER = [
    "Josh Allen",
    "Brock Purdy",
    "Bijan Robinson",
    "Jaylen Warren",
    "Bucky Irving",
    "Ja'Marr Chase",
    "A.J. Brown",
    "Puka Nacua",
    "Trey McBride",
    "Tyler Loop",
]


def _events_from_turn(turn: Turn, latency_ms: int) -> list[dict[str, Any]]:
    """Progress lines from the live templates, then the recorded validated answer."""
    steps = [Progress("context", "Reading the season and your league context")]
    steps += [tool_progress(c) for c in turn.calls]
    steps += [Progress("writing", "Writing the answer"),
              Progress("checking", "Checking every number and source")]  # fmt: skip
    if turn.first_problems:
        steps += [Progress("repairing", "Fixing a failed check"),
                  Progress("checking", "Checking every number and source")]  # fmt: skip
    gap = max(150, latency_ms // (len(steps) + 1))
    out = []
    out = [_progress_event(p, gap * i) for i, p in enumerate(steps)]
    return out + _answer_events(turn, gap * len(steps), 0)


def _progress_event(p: Progress, at: int) -> dict[str, Any]:
    data: dict[str, Any] = {"stage": p.stage, "message": p.message, "elapsed_ms": at}
    if p.tool:
        data |= {"tool": p.tool, "ok": p.ok}
    return {"event": "progress", "at_ms": at, "data": data}


def _answer_events(turn: Turn, end: int, first: int | None) -> list[dict[str, Any]]:
    status = "repaired" if turn.first_problems else "ok"
    return [
        {"event": "answer", "at_ms": end, "data": {
            "status": status, "answer": turn.answer.model_dump(), "sources": sources(turn),
            "as_of": {"season": 2026, "next_week": 7}}},
        {"event": "done", "at_ms": end, "data": {
            "query_id": None, "logged": False, "progress_first_ms": first, "answer_ms": end}},
    ]  # fmt: skip


def record_question(conn: Connection, llm: LLMClient, question: str) -> dict[str, Any]:
    """Ask one league-free question live (paid) and keep it only if it passed every check.
    Progress lines and timing are the real ones from this run."""
    start, events = time.monotonic(), []

    def on_progress(p: Progress) -> None:
        events.append(_progress_event(p, int((time.monotonic() - start) * 1000)))

    turn = ask(conn, llm, question, [], None, on_progress)  # no leagues: nothing personal
    if turn.problems:
        raise ValueError(f"the answer was withheld ({turn.problems[0]}); nothing saved")
    end = int((time.monotonic() - start) * 1000)
    when = datetime.now(UTC).date().isoformat()
    return {"question": question, "recorded_from": f"live run {when}",
            "events": events + _answer_events(turn, end, events[0]["at_ms"])}  # fmt: skip


def load_recorded(path: Path = RECORDED) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = (
        json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    )
    return loaded


def save_recorded(chat: dict[str, Any], path: Path = RECORDED) -> None:
    """Keep live recordings apart from demo.json, so free rebuilds never drop them."""
    kept = [c for c in load_recorded(path) if c["question"] != chat["question"]]
    body = json.dumps([*kept, chat], indent=1, sort_keys=True) + "\n"
    path.write_text(body, encoding="utf-8", newline="\n")


def _from_report(path: Path, ids: list[str]) -> list[dict[str, Any]]:
    rows = {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))["rows"]}
    chats = []
    for qid in ids:
        r = rows[qid]
        if r["problems"]:
            raise ValueError(f"{qid} was withheld in that run; pick another")
        calls = [ToolCall(c["result_id"], c["tool"], c["args"], c["result"], c["error"])
                 for c in r["calls"]]  # fmt: skip
        turn = Turn(r["question"], calls, Answer.model_validate(r["answer"]),
                    r["first_draft_problems"], [], None, False, r["latency_ms"])  # fmt: skip
        chats.append({"question": r["question"], "recorded_from": f"{path.name}#{qid}",
                      "events": _events_from_turn(turn, r["latency_ms"])})  # fmt: skip
    return chats


def _scenarios() -> list[dict[str, Any]]:
    """Synthetic, labelled paths the browser tests need; not real answers."""
    progress = {"event": "progress", "at_ms": 0, "data": {
        "stage": "context", "message": "Reading the season and your league context",
        "elapsed_ms": 0}}  # fmt: skip
    done = {"event": "done", "at_ms": 400, "data": {
        "query_id": None, "logged": False, "progress_first_ms": 0, "answer_ms": None}}  # fmt: skip
    return [
        {"question": "Demo scenario: an answer that fails verification", "scenario": True,
         "events": [progress, {"event": "withheld", "at_ms": 400, "data": {
             "message": WITHHELD_MESSAGE, "reason": "verification_failed"}}, done]},
        {"question": "Demo scenario: the answer service is down", "scenario": True,
         "events": [progress, {"event": "error", "at_ms": 400, "data": {
             "code": "llm_unavailable", "retryable": True,
             "message": "The answer service is unavailable right now. Stats, projections and"
                        " lineups still work."}}, done]},
    ]  # fmt: skip


def _refs(conn: Connection) -> dict[str, q.PlayerRef]:
    return {name: q.resolve_player(conn, name, pos) for name, pos in PLAYERS}


def build(conn: Connection, report: Path, extra_chats: list[dict[str, Any]]) -> dict[str, Any]:
    """`extra_chats`: live recordings (load_recorded()), placed after the saved answers."""
    refs = _refs(conn)
    season, week = q.latest_week(conn)
    presets = ("ppr", "half", "std")
    # Per player and preset, so the demo can serve any scoring toggle and rank any
    # 2-4 demo players with the live ranking code (tools.rank_projections).
    projection: dict[str, dict[str, Any]] = {
        s: {p["player"]["id"]: p for p in tools.rank_start_sit(
            conn, list(refs.values()), tools.ScoringChoice(scoring=s))["ranked"]}
        for s in presets
    }  # fmt: skip
    # The unsupported rule is left out of the points (as live), so they equal half PPR.
    projection[f"league:{DEMO_LEAGUE}"] = proj = {
        pid: {**p, "scoring": DEMO_SCORING,
              "unscored_rules": DEMO_RULES.unscored_for(p["player"]["position"] or "")}
        for pid, p in projection["half"].items()
    }  # fmt: skip
    roster = [refs[n] for n in DEMO_ROSTER]
    ranked = list(proj.values())
    # As the live lineup tool does: a player on bye or ruled out is never started.
    playable = [r for r in roster if (proj[r.id]["availability"] or {}).get("playable", True)]
    lineup = optimize(ROSTER_POSITIONS, [
        Candidate(r.id, r.position or "", proj[r.id]["projected_points"] or 0.0) for r in playable
    ])  # fmt: skip
    names = {r.id: r for r in roster}

    def slot_player(pid: str | None) -> dict[str, Any] | None:
        if pid is None:
            return None
        return {**names[pid].model_dump(), "projected_points": proj[pid]["projected_points"],
                "availability": proj[pid]["availability"], "policy": proj[pid]["policy"],
                "range_80": proj[pid]["range_80"]}  # fmt: skip

    starters = {pid for _, pid, _ in lineup.slots if pid}
    now = datetime.now(UTC)
    sample_as_of = {"status": "sample", "note": "Sample data: availability is illustrative.",
                    "reasons": [], "missing_weeks": [], "applied": 0}  # fmt: skip
    waiver_refs = [refs["Justin Jefferson"], refs["Jahmyr Gibbs"], refs["CeeDee Lamb"]]
    waiver_proj = tools.rank_projections([proj[r.id] for r in waiver_refs])["ranked"]
    data = {
        "label": LABEL,
        "recorded_at": now.isoformat(),
        "status": {
            "label": LABEL,
            "season": season,
            "latest_week": week,
            "next_week": week + 1,
            "sources": {
                k: {"last_ok": (now - timedelta(hours=h)).isoformat(), "age_hours": h}
                for k, h in [
                    ("stats", 6.0),
                    ("injuries", 6.0),
                    ("news", 1.0),
                    ("rosters", 6.0),
                    ("transactions", 1.0),
                ]
            },
        },  # fmt: skip
        "freshness": [{"source": "sample data", "last_ok": now.isoformat(), "age_hours": 0.0}],
        "players": [{**r.model_dump(), "active": True} for r in refs.values()],
        "player": {
            s: {
                r.id: tools._player_block(conn, r, tools.StatsWindow(season=season, scoring=s))
                for r in refs.values()
            }
            for s in presets
        },  # fmt: skip
        "projection": projection,
        "trending": {"add": tools.get_trending(conn, tools.GetTrending(kind="add", limit=10))},
        "leagues": [
            {
                "id": DEMO_LEAGUE,
                "name": "Demo League (sample data)",
                "season": season,
                "roster_positions": ROSTER_POSITIONS,
                "rosters": [{"roster_id": 1, "owner_name": "Demo team"}],
            }
        ],  # fmt: skip
        "lineup": {
            f"{DEMO_LEAGUE}:1": {
                "league_id": DEMO_LEAGUE,
                "roster_id": 1,
                "week": week + 1,
                "scoring": DEMO_SCORING,
                "label": LABEL,
                "rosters_as_of": {"week": week + 1, "synced_at": now.isoformat()},
                "recommended": [
                    {"slot": s, "player": slot_player(pid), "current": slot_player(pid)}
                    for s, pid, _ in lineup.slots
                ],
                "bench": [slot_player(r.id) for r in roster if r.id not in starters],
                "changes": {"start": [], "sit": []},
                "recommended_total": round(lineup.total, 2),
                "current_total": round(lineup.total, 2),
                "gain": 0.0,
                "unhandled_slots": lineup.unhandled_slots,
                "no_projection": [],
                "unscored_rules": sorted({u for r in roster for u in proj[r.id]["unscored_rules"]}),
                "unavailable": [
                    {"name": r.name, **proj[r.id]["availability"]}
                    for r in roster
                    if r not in playable
                ],
                "model_version": ranked[0]["model_version"],
            }
        },  # fmt: skip
        "waivers": {
            f"{DEMO_LEAGUE}:ALL": {
                "league_id": DEMO_LEAGUE,
                "scoring": DEMO_SCORING,
                "week": week + 1,
                "label": LABEL,
                "rosters_as_of": {"week": week + 1, "synced_at": now.isoformat()},
                "availability_as_of": sample_as_of,
                "rostered_players_excluded": len(DEMO_ROSTER),
                "candidates": [
                    {
                        **p["player"],
                        "projected_points": p["projected_points"],
                        "games_used": p["games_used"],
                        "availability": p["availability"],
                        "policy": p["policy"],
                        "range_80": p["range_80"],
                        "unscored_rules": p["unscored_rules"],
                    }
                    for p in waiver_proj
                ],
                "model_version": ranked[0]["model_version"],
            }
        },  # fmt: skip
        "model_runs": [
            dict(r._mapping)
            for r in conn.execute(
                text(
                    "SELECT model_version, training_window, features, metrics, created_at"
                    " FROM model_runs ORDER BY created_at DESC"
                )
            )
        ],  # fmt: skip
        "chat": _from_report(report, list(SAVED)) + extra_chats + _scenarios(),
    }
    encoded: dict[str, Any] = jsonable_encoder(data)
    return encoded


def write(data: dict[str, Any], out: Path = OUT) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, indent=1, sort_keys=True) + "\n"
    out.write_text(body, encoding="utf-8", newline="\n")
    return out
