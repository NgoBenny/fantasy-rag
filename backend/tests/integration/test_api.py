"""HTTP API on the synthetic league database (tests/integration/conftest.py)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection

from fantasy_rag.api.main import app, get_conn
from fantasy_rag.db import get_readonly_engine

pytestmark = pytest.mark.db


@pytest.fixture
def client(db: str) -> Iterator[TestClient]:
    engine = get_readonly_engine(db)

    def conn() -> Iterator[Connection]:
        with engine.connect() as c:
            yield c

    app.dependency_overrides[get_conn] = conn
    yield TestClient(app)
    app.dependency_overrides.clear()
    engine.dispose()


def test_search_detail_and_compare(client: TestClient) -> None:
    assert client.get("/api/health").json()["latest_week"] == 2
    found = client.get("/api/players", params={"q": "jefferson"}).json()
    assert [p["id"] for p in found] == ["00-1"]  # the LB is not a fantasy position
    detail = client.get("/api/players/00-1").json()
    assert detail["totals"]["targets"] == 19 and detail["games"] == 2
    cmp = client.get("/api/compare", params={"ids": "00-1,00-5", "scoring": "half"}).json()
    assert [p["player"]["id"] for p in cmp["players"]] == ["00-1", "00-5"]


def test_errors_are_4xx_not_500(client: TestClient) -> None:
    assert client.get("/api/players/nobody").status_code == 404
    assert client.get("/api/compare", params={"ids": "00-1"}).status_code == 422
    assert client.get("/api/players", params={"limit": 1000}).status_code == 422
    r = client.post("/api/leagues/1/lineup", json={"roster_id": 99})
    assert r.status_code == 404


def test_lineup_endpoint_fills_slots_from_the_roster(client: TestClient) -> None:
    r = client.post("/api/leagues/1/lineup", json={"roster_id": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    names = [x["player"]["name"] for x in body["recommended"] if x["player"]]
    assert names == ["Justin Jefferson"]  # two open slots (WR, FLEX), one rostered WR
    # The slots shown add up to the total claimed.
    shown = sum(x["player"]["projected_points"] for x in body["recommended"] if x["player"])
    assert round(shown, 2) == body["recommended_total"] > 0
    assert "rec_0_4" in body["unscored_rules"]  # the league scores a rule we can't compute


def test_start_sit_endpoint_matches_the_tool_with_bye_injury_and_league_scoring(
    client: TestClient, db: str
) -> None:
    import json

    from fastapi.encoders import jsonable_encoder
    from sqlalchemy import create_engine, text

    from fantasy_rag.agent.tools import run_tool

    engine = create_engine(db)
    with engine.begin() as c:
        run = c.execute(text("SELECT max(id) FROM sync_runs")).scalar_one()
        # Week 3 (next week): MIN and NYJ play; NE (00-5's team) is on bye.
        c.execute(
            text(
                "INSERT INTO schedules (game_id, season, week, game_type, home_team, away_team,"
                " sync_run_id) VALUES ('g3', 2026, 3, 'REG', 'MIN', 'NYJ', :r)"
            ),
            {"r": run},
        )
        c.execute(
            text(
                "INSERT INTO injury_snapshots (player_id, status, source, sync_run_id) VALUES"
                " ('00-1', 'Questionable', 'sleeper', :r), ('00-3', 'Out', 'sleeper', :r)"
            ),
            {"r": run},
        )
    engine.dispose()
    got = client.get("/api/start-sit", params={"ids": "00-1,00-3,00-5", "league_id": 1}).json()
    by_id = {p["player"]["id"]: p for p in got["ranked"]}
    assert by_id["00-5"]["projected_points"] == 0.0  # healthy but on bye
    assert by_id["00-5"]["availability"]["status"] == "bye"
    assert by_id["00-3"]["projected_points"] == 0.0  # ruled out
    assert by_id["00-1"]["availability"]["status"] == "Questionable"  # flagged, not zeroed
    assert by_id["00-1"]["projected_points"] > 0 and got["higher_projection"] == "Justin Jefferson"
    assert by_id["00-1"]["scoring"] == "league 1 rules"
    std = client.get("/api/start-sit", params={"ids": "00-1,00-5", "scoring": "std"}).json()
    std_pts = {p["player"]["id"]: p["projected_points"] for p in std["ranked"]}
    assert std_pts["00-1"] < by_id["00-1"]["projected_points"]  # league gives 1/catch
    with get_readonly_engine(db).connect() as conn:
        tool = run_tool(conn, "compare_start_sit", {"league_id": 1, "players": [
            {"name": "Justin Jefferson"}, {"name": "Free Agent"}]})  # fmt: skip
    api = client.get("/api/start-sit", params={"ids": "00-1,00-5", "league_id": 1}).json()
    # Same data; only the timestamp style differs ("Z" in responses, "+00:00" from Python).
    exp = json.loads(json.dumps(jsonable_encoder(tool)).replace("+00:00", "Z"))
    assert api == exp  # one ranking code path (same JSON as the tool)
    assert client.get("/api/start-sit", params={"ids": "00-1"}).status_code == 422


def test_status_waivers_and_model_runs(client: TestClient) -> None:
    s = client.get("/api/status").json()
    assert s["demo"] is False and s["next_week"] == s["latest_week"] + 1 == 3
    assert set(s["sources"]) == {"stats", "injuries", "news", "rosters", "transactions"}
    w = client.get("/api/leagues/1/waivers", params={"position": "WR"}).json()
    assert w["availability_as_of"]["status"] == "uncertain"  # no roster fetch recorded
    assert "00-1" not in {c["id"] for c in w["candidates"]}  # rostered
    assert client.get("/api/model-runs").json() == []
