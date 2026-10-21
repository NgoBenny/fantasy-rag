"""The structured answer contract, and the checks an answer must pass before it is shown
(plain Python, no LLM): every claim cites a result from this turn, and every number in a
claim appears in the results that claim cites."""

import re
from typing import Literal

from pydantic import BaseModel, Field

AnswerType = Literal[
    "stats",
    "projection",
    "start_sit",
    "lineup",
    "waivers",
    "injury",
    "news",
    "clarification",
    "out_of_scope",
    "no_data",
]
# Asking back, declining and "no data" may rest on no result; every other answer type
# states facts or recommends something, so it must cite at least one result.
EVIDENCE_FREE_TYPES = {"clarification", "out_of_scope", "no_data"}


class Claim(BaseModel):
    text: str
    source_ids: list[str] = Field(description="result IDs this claim comes from")


class Answer(BaseModel):
    answer: str = Field(description="what the user reads, in plain sentences")
    answer_type: AnswerType
    season: int | None
    week: int | None
    scoring_format: str | None = Field(description="e.g. 'league 3 rules' or 'ppr'")
    data_as_of: str | None = Field(description="oldest sync or as-of time the answer relies on")
    model_version: str | None
    facts: list[Claim] = Field(description="observed stats, statuses and news")
    projections: list[Claim] = Field(description="model projections, kept apart from facts")
    citations: list[str] = Field(description="every result ID the answer relies on")
    limitations: list[str] = Field(description="missing, stale or unsupported data")


# A leading minus (ASCII or U+2212) is a sign unless it follows a digit or letter, so
# "-2.0" stays negative while "2026-10-20" and "6-10 weeks" are plain positive numbers.
_NUMBER = re.compile(r"(?<![\w.])[-−]?\d+(?:\.\d+)?")


def numbers(text: str) -> list[str]:
    return [n.replace("−", "-") for n in _NUMBER.findall(text)]


def _known(text: str) -> set[float]:
    """Numbers in a source, with the roundings an answer may show them as."""
    out: set[float] = set()
    for n in numbers(text):
        x = float(n)
        out |= {x, round(x, 2), round(x, 1), float(round(x))}
    return out


def problems(answer: Answer, sources: dict[str, str], context: str) -> list[str]:
    """Why `answer` can't be shown; empty means it passes.

    `sources` maps each result ID of this turn to its text. A claim's numbers must appear
    in the results it cites (or in the context, which is our own data); the answer text's
    numbers must appear in some result the answer cites. Numbers the user typed in the
    question are never evidence.
    """
    out: list[str] = []
    trusted = _known(context)

    def check(label: str, text: str, ids: list[str]) -> None:
        pool = trusted.union(*(_known(sources[i]) for i in ids if i in sources))
        bad = [n for n in numbers(text) if float(n) not in pool]
        if bad:
            out.append(f"{label}: {', '.join(bad)} not in {', '.join(ids) or 'any cited result'}")

    claims = [("fact", answer.facts), ("projection", answer.projections)]
    for kind, items in claims:
        for i, c in enumerate(items, start=1):
            if not c.source_ids:
                out.append(f"{kind} {i} cites no result")
            check(f"{kind} {i}", c.text, c.source_ids)
    cited = set(answer.citations).union(*(c.source_ids for _, items in claims for c in items))
    if unknown := sorted(cited - sources.keys()):
        out.append(f"cites results that don't exist: {', '.join(unknown)}")
    if not cited and answer.answer_type not in EVIDENCE_FREE_TYPES:
        out.append("cites no tool result")
    if answer.answer_type == "no_data" and re.match(
        r"^\s*(?:no|yes)\s*[.!?,:;\-–—](?:\s|$)", answer.answer, re.I
    ):
        out.append("no_data must describe missing evidence, not give a categorical yes/no")
    check("answer", answer.answer, sorted(cited))
    return out
