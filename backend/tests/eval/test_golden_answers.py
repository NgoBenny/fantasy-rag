"""Live check (PLAN Phase 6 "Done when") on the dev DB with the real router and writer:
route accuracy >= 90% on the 60 golden questions, every answer passes the grounding checks
(each claim cited, each number in the results it cites), out-of-scope and no-support
questions are recognised. Costs about $1-2 per run, so it has its own `llm` marker."""

import pytest

from fantasy_rag.agent.evaluate import run_golden
from fantasy_rag.agent.llm import AnthropicLLM
from fantasy_rag.config import Settings
from fantasy_rag.db import get_readonly_engine

pytestmark = [pytest.mark.llm, pytest.mark.db]


def test_golden_answers_meet_targets() -> None:
    with get_readonly_engine().connect() as conn:
        s = run_golden(conn, AnthropicLLM(), Settings().sleeper_username)["summary"]
    print("\n", s)
    r = s["revised"]  # route_overrides agreed after the first full run; original is reported too
    assert r["route_accuracy"] >= 0.9, r["route_misses"]
    assert s["withheld"] == []  # every answer passed the grounding checks
    assert s["answer_type_misses"] == []
