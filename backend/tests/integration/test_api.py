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
    assert names == ["Justin Jefferson"]  # the only rostered player
