"""DEMO_MODE: the API starts and answers with no .env, no database and no API key."""

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx
import pytest

from fantasy_rag.config import REPO_ROOT

DEMO = REPO_ROOT / "data" / "demo" / "demo.json"
BACKEND = REPO_ROOT / "backend"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def demo_api(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    port = _free_port()
    missing_env = tmp_path_factory.mktemp("noenv") / "absent.env"  # never created
    dropped = {"DATABASE_URL", "ANTHROPIC_API_KEY", "SLEEPER_USERNAME", "DB_PORT"}
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    env |= {"DEMO_MODE": "true", "FANTASY_RAG_ENV_FILE": str(missing_env),
            # a closed port: any database access would fail loudly
            "DATABASE_URL": "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"}  # fmt: skip
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "fantasy_rag.api.main:app", "--port", str(port)],
        cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )  # fmt: skip
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise RuntimeError(proc.stderr.read().decode() if proc.stderr else "no start")
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


def _events(text: str) -> list[tuple[str, dict]]:  # type: ignore[type-arg]
    out = []
    for block in text.strip().split("\n\n"):
        f = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((f["event"], json.loads(f["data"])))
    return out


def test_every_route_serves_labelled_sample_data(demo_api: str) -> None:
    get = lambda p, **kw: httpx.get(demo_api + p, params=kw, timeout=10)  # noqa: E731
    status = get("/api/status").json()
    assert status["demo"] is True and "not live" in status["label"]
    assert get("/api/health").json()["demo"] is True
    players = get("/api/players", q="").json()
    assert len(players) >= 10
    pid = players[0]["id"]
    assert get(f"/api/players/{pid}").status_code == 200
    assert get("/api/compare", ids=f"{pid},{players[1]['id']}").status_code == 200
    league = get("/api/leagues").json()[0]
    assert "sample" in league["name"].lower()
    lineup = httpx.post(f"{demo_api}/api/leagues/{league['id']}/lineup",
                        json={"roster_id": 1}, timeout=10).json()  # fmt: skip
    assert lineup["recommended"]
    waivers = get(f"/api/leagues/{league['id']}/waivers").json()
    assert waivers["availability_as_of"]["status"] == "sample"
    assert get("/api/start-sit", ids=f"{pid},{players[1]['id']}").json()["ranked"]
    assert get("/api/model-runs").status_code == 200
    assert get("/api/players/not-a-demo-player").status_code == 404


def test_chat_replays_recorded_answers_and_says_so_for_anything_else(demo_api: str) -> None:
    def chat(q: str) -> list[tuple[str, dict]]:  # type: ignore[type-arg]
        r = httpx.post(f"{demo_api}/api/chat", json={"question": q}, timeout=20)
        assert r.headers["content-type"].startswith("text/event-stream")
        return _events(r.text)

    recorded = chat("How many PPR points did Bijan Robinson score in week 3?")
    kinds = [k for k, _ in recorded]
    assert kinds[0] == "progress" and kinds.count("answer") == 1 and kinds[-1] == "done"
    assert all(d["demo"] is True for _, d in recorded)
    assert dict(recorded)["answer"]["sources"]
    unknown = dict(chat("who wins the super bowl?"))
    assert unknown["answer"]["answer"]["answer_type"] == "out_of_scope"
    assert "only these sample questions" in unknown["answer"]["answer"]["answer"]
    withheld = chat("Demo scenario: an answer that fails verification")
    assert "withheld" in dict(withheld) and "answer" not in dict(withheld)
    error = dict(chat("Demo scenario: the answer service is down"))["error"]
    assert error["code"] == "llm_unavailable" and error["retryable"] is True


def test_demo_fixtures_hold_no_personal_data_or_secrets() -> None:
    text = DEMO.read_text("utf-8")
    for needle in ("sk-ant", "creator", "consenter", "owner_id", "user_id", 'league_id": 1,'):
        assert needle not in text, needle
    username = os.environ.get("SLEEPER_USERNAME") or _env_value("SLEEPER_USERNAME")
    if username:
        assert username.lower() not in text.lower()


def _env_value(key: str) -> str | None:
    env = REPO_ROOT / ".env"
    if not env.exists():
        return None
    for line in env.read_text("utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip() or None
    return None


@pytest.mark.db
def test_demo_fixtures_hold_no_owner_names_or_league_ids() -> None:
    from sqlalchemy import text

    from fantasy_rag.db import get_readonly_engine

    try:
        with get_readonly_engine().connect() as c:
            sql = "SELECT DISTINCT owner_name FROM fantasy_rosters WHERE owner_name IS NOT NULL"
            owners = {r[0] for r in c.execute(text(sql))}
            ids = {r[0] for r in c.execute(text("SELECT external_league_id FROM fantasy_leagues"))}
            names = {r[0] for r in c.execute(text(
                "SELECT name FROM fantasy_leagues WHERE name IS NOT NULL"))}  # fmt: skip
    except Exception:
        pytest.skip("dev database not reachable")
    body = DEMO.read_text("utf-8").lower()
    leaked = [v for v in owners | ids | names if len(v) > 3 and v.lower() in body]
    assert leaked == [], f"{len(leaked)} personal values found in demo.json"


def test_demo_stats_follow_the_requested_scoring(demo_api: str) -> None:
    pid = next(p["id"] for p in httpx.get(f"{demo_api}/api/players", params={"q": "bijan"}).json())
    got = {
        s: httpx.get(f"{demo_api}/api/players/{pid}", params={"scoring": s}).json()
        for s in ("ppr", "half", "std")
    }
    assert [got[s]["scoring"] for s in got] == ["ppr", "half", "std"]
    assert got["ppr"]["total_points"] > got["half"]["total_points"] > got["std"]["total_points"]
    other = next(p["id"] for p in httpx.get(f"{demo_api}/api/players").json() if p["id"] != pid)
    std = httpx.get(f"{demo_api}/api/compare", params={"ids": f"{pid},{other}", "scoring": "std"})
    assert std.json()["players"][0] == got["std"]
    # Controls the demo has no data for are refused, never answered with another window.
    assert httpx.get(f"{demo_api}/api/players/{pid}", params={"last_n": 3}).status_code == 422


def test_demo_start_sit_ranks_any_demo_players_like_the_live_code(demo_api: str) -> None:
    from fantasy_rag.agent.tools import rank_projections

    data = json.loads(DEMO.read_text("utf-8"))
    by_name = {p["name"]: p["id"] for p in data["players"]}
    trio = [by_name[n] for n in ("Brock Purdy", "Josh Allen", "Trey McBride")]
    for params, key in [({"league_id": 900}, "league:900"), ({"scoring": "std"}, "std")]:
        got = httpx.get(f"{demo_api}/api/start-sit", params={"ids": ",".join(trio), **params})
        assert got.status_code == 200
        expected = rank_projections([data["projection"][key][i] for i in trio])
        assert got.json() == {**expected, "demo": True}
    bye = got.json()["ranked"][-1]  # Josh Allen is on bye: 0, ranked below real projections
    assert bye["player"]["name"] == "Josh Allen" and bye["projected_points"] == 0.0
    unknown = httpx.get(f"{demo_api}/api/start-sit", params={"ids": f"{trio[0]},00-x"})
    assert unknown.status_code == 404
