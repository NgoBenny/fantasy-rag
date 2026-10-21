"""Saved-answer audits never reroute questions or rerun tools."""

import copy
import json
from typing import Any

import pytest

from fantasy_rag.agent.grounding import audit_saved


def _report() -> dict[str, Any]:
    def row(qid: str, text: str, tool: str, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": qid,
            "question": "Is this a safe start?",
            "answer": {
                "answer": text,
                "answer_type": "start_sit",
                "season": None,
                "week": None,
                "scoring_format": None,
                "data_as_of": None,
                "model_version": None,
                "facts": [],
                "projections": [],
                "citations": ["r1"],
                "limitations": [],
            },
            "correct_if": ["all roster claims follow the complete lineup"],
            "calls": [
                {
                    "result_id": "r1",
                    "tool": tool,
                    "args": {"query": "Titans"},
                    "error": None,
                    "result": result,
                }
            ],
        }

    return {"rows": [
        row("a15", "Stevenson is the only RB with a projection in your starting lineup.",
            "optimize_lineup", {"recommended": [
                {"slot": "RB", "player": {"name": "Jahmyr Gibbs", "projected_points": 26.75}},
                {"slot": "RB", "player": {
                    "name": "Rhamondre Stevenson", "projected_points": 12.84,
                }},
            ]}),
        row("a09", "Loop hit a 64-yard field goal in the Titans game.", "search_news", {
            "query": "Titans", "results": [{
                "result_id": "news:7", "excerpt": "Loop hit a career-long 64-yard field goal.",
            }],
        }),
    ]}  # fmt: skip


class Reviewer:
    def __init__(self, fail: bool = False) -> None:
        self.seen: list[dict[str, Any]] = []
        self.fail = fail

    def structured(self, system: str, content: str, schema: type[Any]) -> Any:
        payload = json.loads(content.removeprefix("<audit_data>\n").removesuffix("\n</audit_data>"))
        self.seen.append(payload)
        if self.fail:
            raise RuntimeError("review unavailable")
        return schema.model_validate(
            {
                "score": 2,
                "unsupported_claims": [
                    {
                        "text": payload["answer"]["answer"],
                        "reason": "The cited source contradicts this or omits the asserted detail.",
                        "source_ids": ["r1"],
                    }
                ],
                "missed_requirements": [],
                "unverifiable_claims": [],
            }
        )

    def tool_turn(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an offline audit must not reroute")


def test_audit_checks_prose_against_complete_recorded_evidence() -> None:
    reviewer = Reviewer()
    saved = _report()
    original = copy.deepcopy(saved)
    got = audit_saved(saved, reviewer)
    assert saved == original
    assert got["summary"]["unsupported_answers"] == ["a15", "a09"]
    assert got["summary"]["mean_groundedness"] == 2
    # a15's false statement is only in prose; Gibbs must still reach the reviewer.
    assert "Jahmyr Gibbs" in reviewer.seen[0]["sources"]["r1"]
    assert "only RB" in reviewer.seen[0]["answer"]["answer"]
    # Neither an argument nor its echo can substantiate the invented Titans opponent.
    assert "Titans" not in reviewer.seen[1]["sources"]["r1"]
    assert got["summary"]["missing_context"] == ["a15", "a09"]


def test_missing_context_only_suspends_number_checks() -> None:
    # Review case: a14 quoted "12 teams, 0.5 PPR" from the context, which older reports
    # never saved. That can't be re-checked, but it isn't "passed" either; and checks the
    # saved tool results can still decide must keep failing.
    saved = _report()
    a14 = saved["rows"][0]
    a14["answer"]["answer"] = "Which league: league 1 (12 teams, 0.5 PPR) or league 4?"
    got = audit_saved(saved, Reviewer())
    assert got["rows"][0]["validation_problems"] == []
    assert got["rows"][0]["unverifiable"] and "12" in got["rows"][0]["unverifiable"][0]
    assert (
        "a15" in got["summary"]["unverifiable_answers"] and got["summary"]["fully_supported"] == 0
    )
    a14["answer"]["citations"] = ["news:404"]  # a citation the saved results don't hold
    assert got_problems(saved) == [
        "cites results that don't exist: news:404",
        "cites no tool result",
    ]
    a14["context"] = "<context>- league 1: 12 teams, 0.5 PPR; league 4</context>"
    a14["answer"]["citations"] = ["r1"]
    row = audit_saved(saved, Reviewer())["rows"][0]
    assert row["validation_problems"] == [] and row["unverifiable"] == []


def got_problems(saved: dict[str, Any]) -> list[str]:
    problems: list[str] = audit_saved(saved, Reviewer())["rows"][0]["validation_problems"]
    return problems


def test_invalid_saved_reports_are_rejected_before_any_model_call() -> None:
    reviewer = Reviewer()
    bad = _report()
    del bad["rows"][1]["calls"][0]["result"]
    with pytest.raises(ValueError, match="full tool results"):
        audit_saved(bad, reviewer)
    assert reviewer.seen == []
    with pytest.raises(ValueError, match="no saved questions"):
        audit_saved(_report(), reviewer, only={"absent"})
    assert reviewer.seen == []


def test_review_errors_are_visible_instead_of_counting_as_supported() -> None:
    got = audit_saved(_report(), Reviewer(fail=True))
    assert got["summary"]["review_errors"] == ["a15", "a09"]
    assert got["summary"]["mean_groundedness"] is None
    assert got["summary"]["fully_supported"] == 0
    assert all(r["error"] == "review unavailable" for r in got["rows"])


def test_audit_can_target_saved_failures_without_grading_other_rows() -> None:
    reviewer = Reviewer()
    got = audit_saved(_report(), reviewer, only={"a09"})
    assert got["summary"]["questions"] == 1
    assert [r["id"] for r in got["rows"]] == ["a09"]


def test_cli_audits_a_saved_report_without_rerunning_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fantasy_rag import cli

    called = []
    monkeypatch.setattr(
        cli, "_eval_grounding", lambda report, only: called.append((report, only)), raising=False
    )
    cli.main(["eval", "grounding", "--report", "saved.json", "--only", "a09,a15"])
    assert called == [("saved.json", "a09,a15")]
    with pytest.raises(SystemExit) as exc:
        cli.main(["eval", "grounding"])
    assert exc.value.code == 2
