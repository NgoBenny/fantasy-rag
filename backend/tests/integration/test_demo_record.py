"""`demo record --record-question`: one live answer, kept only if it passed every check.

Uses the scripted fake model from test_chat, so nothing here spends money."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from test_chat import FakeLLM, _msg, answer

from fantasy_rag.agent.answer import Claim
from fantasy_rag.demo_record import load_recorded, record_question, save_recorded

pytestmark = pytest.mark.db
PAIR = ("compare_start_sit", {"players": [{"name": "Zero Guy"}, {"name": "Negative Guy"}],
                              "scoring": "half"})  # fmt: skip


def test_a_recorded_answer_streams_real_progress_then_its_linked_sources(
    db: str, tmp_path: Path
) -> None:
    good = answer("Start Zero Guy over Negative Guy.", kind="start_sit", cites=["r1"])
    with create_engine(db).connect() as conn:
        chat = record_question(conn, FakeLLM([_msg(PAIR)], [good]), "zero guy or negative guy?")
    kinds = [e["event"] for e in chat["events"]]
    assert kinds[0] == "progress" and kinds[-2:] == ["answer", "done"]
    assert set(kinds[:-2]) == {"progress"}
    assert "Looked up projections for Zero Guy, Negative Guy" in str(chat["events"])
    [src] = chat["events"][-2]["data"]["sources"]
    assert src["href"] == "/compare?ids=00-6,00-7&scoring=half"
    done = chat["events"][-1]["data"]
    assert done["progress_first_ms"] is not None and done["answer_ms"] >= done["progress_first_ms"]

    path = tmp_path / "recorded.json"
    save_recorded(chat, path)
    save_recorded(chat, path)  # re-recording a question replaces it
    assert load_recorded(path) == [chat]


def test_a_withheld_answer_is_never_saved(db: str) -> None:
    bad: Any = answer("Zero Guy scores 99.", Claim(text="99 points", source_ids=["r1"]),
                      kind="start_sit", cites=["r1"])  # fmt: skip
    llm = FakeLLM([_msg(PAIR)], [bad, bad.model_copy(deep=True)])
    with create_engine(db).connect() as conn, pytest.raises(ValueError, match="withheld"):
        record_question(conn, llm, "zero guy or negative guy?")
