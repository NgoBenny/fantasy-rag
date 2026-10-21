"""The structured answer contract, and the checks an answer must pass before it is shown
(plain Python, no LLM): every claim cites a result from this turn, and every number in a
claim appears in the results that claim cites."""

import re
from collections.abc import Collection
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
# Thousands separators are part of one number: "1,000" is 1000, never 1 and 000.
_NUMBER = re.compile(r"(?<![\w.])[-−]?(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?")


def numbers(text: str) -> list[str]:
    return [n.replace("−", "-").replace(",", "") for n in _NUMBER.findall(text)]


def _known(text: str) -> set[float]:
    """Numbers in a source, with the roundings an answer may show them as."""
    out: set[float] = set()
    for n in numbers(text):
        x = float(n)
        out |= {x, round(x, 2), round(x, 1), float(round(x))}
        if len(n.partition(".")[2]) >= 4:  # timestamp seconds: "40.712784" may show as "40"
            out.add(float(int(x)))
    return out


def problems(
    answer: Answer, sources: dict[str, str], context: str, failed: Collection[str] = ()
) -> list[str]:
    """Why `answer` can't be shown; empty means it passes.

    `sources` maps each successful result ID of this turn to its text; `failed` lists the
    IDs of calls that errored, which hold no evidence (a clarification may still point at
    the error that made it ask). A claim's numbers must appear in the results it cites
    (or in the context, which is our own data); the numbers in the answer text, the
    limitations and the metadata must appear in some result the answer cites. Numbers
    the user typed in the question are never evidence.
    """
    out: list[str] = []
    trusted = _known(context)
    evidence_free = answer.answer_type in EVIDENCE_FREE_TYPES

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
            if not evidence_free and (bad := [s for s in c.source_ids if s in failed]):
                out.append(f"{kind} {i} cites a failed tool call: {', '.join(bad)}")
            check(f"{kind} {i}", c.text, c.source_ids)
    cited = set(answer.citations).union(*(c.source_ids for _, items in claims for c in items))
    if unknown := sorted(cited - sources.keys() - set(failed)):
        out.append(f"cites results that don't exist: {', '.join(unknown)}")
    if not (cited & sources.keys()) and not evidence_free:
        out.append("cites no tool result")
    if answer.answer_type == "no_data" and re.match(
        r"^\s*(?:no|yes)\s*[.!?,:;\-–—](?:\s|$)", answer.answer, re.I
    ):
        out.append("no_data must describe missing evidence, not give a categorical yes/no")
    shown = sorted(cited & sources.keys())
    check("answer", answer.answer, shown)
    check("limitations", " ".join(answer.limitations), shown)
    meta = (answer.season, answer.week, answer.data_as_of, answer.scoring_format)
    check("metadata", " ".join(str(m) for m in meta if m is not None), shown)
    return out
