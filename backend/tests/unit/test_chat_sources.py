"""Chat sources: every cited, successful tool result links to a page showing that result."""

from typing import Any

from fantasy_rag.agent.answer import Answer
from fantasy_rag.agent.router import ToolCall, Turn
from fantasy_rag.api.chat import sources


def turn_citing(call: ToolCall) -> Turn:
    a = Answer(answer="x", answer_type="start_sit", season=None, week=None, scoring_format=None,
               data_as_of=None, model_version=None, facts=[], projections=[],
               citations=[call.result_id], limitations=[])  # fmt: skip
    return Turn("q", [call], a, [], [], None, False, 0)


def ranked(*ids: str) -> dict[str, Any]:
    return {"ranked": [{"player": {"id": i, "name": i}} for i in ids]}


def test_a_league_free_start_sit_links_to_its_projections_with_selection_and_scoring() -> None:
    call = ToolCall("r1", "compare_start_sit", {"players": [], "scoring": "half"},
                    ranked("00-2", "00-1"))  # fmt: skip
    [src] = sources(turn_citing(call))
    assert src["href"] == "/compare?ids=00-2,00-1&scoring=half" and not src["failed"]
    no_scoring = ToolCall("r1", "compare_start_sit", {"players": []}, ranked("00-2", "00-1"))
    assert sources(turn_citing(no_scoring))[0]["href"].endswith("&scoring=ppr")  # tool default
    league = ToolCall("r1", "compare_start_sit", {"league_id": 3}, ranked("00-2", "00-1"))
    assert sources(turn_citing(league))[0]["href"] == "/league?league=3&view=startsit&ids=00-2,00-1"


def test_a_league_free_stats_comparison_links_to_compare() -> None:
    players = {"players": [{"player": {"id": "00-1"}}, {"player": {"id": "00-3"}}]}
    call = ToolCall("r1", "compare_players", {"scoring": "std"}, players)
    assert sources(turn_citing(call))[0]["href"] == "/compare?ids=00-1,00-3&scoring=std"
