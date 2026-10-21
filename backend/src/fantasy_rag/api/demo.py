"""DEMO_MODE: labelled sample data from `data/demo/demo.json`. No database, no API key.

Every response says `demo: true`. Chat replays recorded Phase 6 answers (progress, then
the validated answer with its sources) for the listed sample questions; any other
question gets an honest "the demo answers only these questions" reply, never a
generated one.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from functools import cache
from typing import Any

from fastapi import HTTPException

from fantasy_rag.api.chat import sse
from fantasy_rag.config import REPO_ROOT

DEMO_FILE = REPO_ROOT / "data" / "demo" / "demo.json"
MAX_REPLAY_S = 3.0  # recorded timing, squeezed into at most this long


@cache
def data() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(DEMO_FILE.read_text(encoding="utf-8"))
    return loaded


def get(section: str, *keys: str) -> Any:
    found = data().get(section)
    for key in keys:
        found = (found or {}).get(key)
    if found is None:
        raise HTTPException(404, f"not in the demo data ({'/'.join([section, *keys])})")
    return found


def stats(
    player_id: str, scoring: str, season: int | None, last_n: int | None, league_id: int | None
) -> dict[str, Any]:
    """Saved full-season stats in the requested preset; other windows are refused."""
    if season is not None or last_n is not None or league_id is not None:
        raise HTTPException(422, "the demo has full-season stats in PPR, half or std only")
    found: dict[str, Any] = get("player", scoring, player_id)
    return found


def _norm(q: str) -> str:
    return " ".join(q.lower().replace("?", " ").split())


def find(question: str) -> dict[str, Any] | None:
    chats: list[dict[str, Any]] = data()["chat"]
    return next((c for c in chats if _norm(c["question"]) == _norm(question)), None)


def fallback() -> list[tuple[str, dict[str, Any]]]:
    asked = [c["question"] for c in data()["chat"] if not c.get("scenario")]
    text = "The demo answers only these sample questions: " + "; ".join(asked) + "."
    answer = {"answer": text, "answer_type": "out_of_scope", "season": None, "week": None,
              "scoring_format": None, "data_as_of": None, "model_version": None, "facts": [],
              "projections": [], "citations": [], "limitations": ["Demo mode: sample data only."]}  # fmt: skip # noqa: E501
    return [
        ("answer", {"status": "ok", "answer": answer, "sources": [], "as_of": None}),
        (
            "done",
            {"query_id": None, "logged": False, "progress_first_ms": None, "answer_ms": 0},
        ),  # fmt: skip
    ]


async def stream(question: str) -> AsyncIterator[bytes]:
    found = find(question)
    events = (
        [(e["event"], e["data"], e.get("at_ms", 0)) for e in found["events"]]
        if found
        else [(ev, d, 0) for ev, d in fallback()]
    )
    total = max((at for _, _, at in events), default=0) or 1
    scale = min(1.0, MAX_REPLAY_S * 1000 / total)
    clock = 0.0
    for seq, (event, payload, at) in enumerate(events, start=1):
        await asyncio.sleep(max(0.0, at * scale - clock) / 1000)
        clock = max(clock, at * scale)
        yield sse(seq, event, {**payload, "demo": True})
