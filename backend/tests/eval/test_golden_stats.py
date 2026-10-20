"""Live check: the 20 golden stats questions return numbers matching direct queries.

Reads the ingested dev DB (run `fantasy-rag ingest structured` and `league import` first).
Tools run through the read-only engine; checks run independent SQL.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import text

from fantasy_rag.agent.tools import run_tool
from fantasy_rag.db import get_engine, get_readonly_engine

pytestmark = [pytest.mark.live, pytest.mark.db]

GOLDEN = yaml.safe_load((Path(__file__).parent / "golden.yaml").read_text(encoding="utf-8"))


def _pts(rec: float) -> str:
    """Standard scoring written straight over nflverse columns (independent of scoring.py)."""
    terms = {
        "passing_yards": 0.04, "passing_tds": 4, "passing_interceptions": -2,
        "passing_2pt_conversions": 2, "rushing_yards": 0.1, "rushing_tds": 6,
        "rushing_2pt_conversions": 2, "receptions": rec, "receiving_yards": 0.1,
        "receiving_tds": 6, "receiving_2pt_conversions": 2, "fumbles_lost_total": -2,
        "fumble_recovery_tds": 6,
        "special_teams_tds": 6,
    }  # fmt: skip
    return (
        "round(("
        + " + ".join(f"coalesce((stats->>'{k}')::numeric, 0) * {w}" for k, w in terms.items())
        + ")::numeric, 2)"
    )


def _get(obj: Any, path: str) -> Any:
    for part in path.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj.get(part)
    return obj


@pytest.fixture(scope="module")
def latest() -> int:
    with get_engine().connect() as conn:
        week: int = conn.execute(
            text(
                "SELECT max(week) FROM player_week_stats"
                " WHERE season = 2026 AND season_type = 'REG'"
            )
        ).scalar_one()
    return week


@pytest.mark.parametrize("item", GOLDEN["stats"], ids=[i["id"] for i in GOLDEN["stats"]])
def test_golden_stats_question(item: dict[str, Any], latest: int) -> None:
    with get_readonly_engine().connect() as conn:
        result = run_tool(conn, item["tool"], item["args"])
    with get_engine().connect() as conn:
        for check in item["checks"]:
            got = _get(result, check["path"])
            if "equals" in check:
                assert got == check["equals"], (item["question"], check["path"])
                continue
            sql = (
                check["sql"]
                .replace("{LATEST}", str(latest))
                .replace("{PTS_PPR}", _pts(1.0))
                .replace("{PTS_HALF}", _pts(0.5))
            )
            expected = conn.execute(text(sql)).scalar_one()
            assert expected is not None, f"{item['id']}: direct query found no data"
            assert abs(float(got) - float(expected)) <= check.get("tolerance", 0.01), (
                f"{item['id']} {item['question']}: tool {check['path']}={got}, direct={expected}"
            )
