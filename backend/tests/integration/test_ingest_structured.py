"""End-to-end ingest on recorded fixtures (tests/fixtures/phase1); a re-run is a no-op."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from fantasy_rag.ingest import structured
from fantasy_rag.ingest.base import upsert

pytestmark = pytest.mark.db

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "phase1"
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
TABLES = ["sources", "players", "player_external_ids", "schedules", "player_week_stats",
          "snap_counts", "ff_opportunity", "depth_charts", "injury_history", "injury_snapshots",
          "trending", "team_week_defense"]  # fmt: skip


class FakeNfl:
    """Stands in for nflreadpy: every loader returns its recorded slice."""

    def _read(self, name: str) -> pl.DataFrame:
        return pl.read_parquet(FIX / f"{name}.parquet")

    def load_players(self) -> pl.DataFrame:
        return self._read("players")

    def load_ff_playerids(self) -> pl.DataFrame:
        return self._read("ff_playerids")

    def load_schedules(self, seasons: Any) -> pl.DataFrame:
        return self._read("schedules")

    def load_player_stats(self, seasons: Any) -> pl.DataFrame:
        return self._read("player_stats")

    def load_snap_counts(self, seasons: Any) -> pl.DataFrame:
        return self._read("snap_counts")

    def load_ff_opportunity(self, seasons: Any) -> pl.DataFrame:
        return self._read("ff_opportunity")

    def load_injuries(self, seasons: Any) -> pl.DataFrame:
        return self._read("injuries")

    def load_pbp(self, seasons: Any) -> pl.DataFrame:
        return self._read("pbp")

    def load_depth_charts(self, seasons: list[int]) -> pl.DataFrame:
        return self._read("depth_2024" if seasons[0] <= 2024 else "depth_2025plus")


class FakeSleeper:
    def __init__(self) -> None:
        self.data = json.loads((FIX / "sleeper_players.json").read_text(encoding="utf-8"))

    def players(self, cache_dir: Path) -> dict[str, Any]:
        return dict(self.data)

    def get(self, path: str, **params: Any) -> Any:
        ids = list(self.data)[:3]
        return [{"player_id": i, "count": 7} for i in ids] + [{"player_id": "DET", "count": 1}]


def _counts(url: str) -> dict[str, int]:
    engine = create_engine(url)
    with engine.connect() as conn:
        out = {t: int(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()) for t in TABLES}
    engine.dispose()
    return out


def test_ingest_fills_tables_and_rerun_changes_nothing(
    scratch_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    monkeypatch.setattr(structured, "nfl", FakeNfl())
    monkeypatch.setattr(structured, "update_config", lambda **_: None)
    engine = create_engine(scratch_db_url)
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)

    first = structured.run_structured(engine, FakeSleeper(), now, season=2026)
    assert first.ok, [r for r in first.results if r.error]
    counts = _counts(scratch_db_url)
    assert all(n > 0 for n in counts.values()), counts

    second = structured.run_structured(engine, FakeSleeper(), now, season=2026)
    assert second.ok
    assert {r.source: r.changed for r in second.results if r.changed} == {}
    assert _counts(scratch_db_url) == counts

    with engine.connect() as conn:
        statuses = conn.execute(text("SELECT DISTINCT status FROM sync_runs")).scalars().all()
        # stats never carry nflverse's precomputed fantasy points
        leaked = conn.execute(
            text("SELECT count(*) FROM player_week_stats WHERE stats ? 'fantasy_points_ppr'")
        ).scalar_one()
    engine.dispose()
    assert statuses == ["ok"] and leaked == 0


def test_upsert_updates_only_changed_rows(scratch_db_url: str) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    engine = create_engine(scratch_db_url)
    rows = structured.SOURCES
    with engine.begin() as conn:
        assert upsert(conn, "sources", rows, ["name"], None) == len(rows)
        assert upsert(conn, "sources", rows, ["name"], None) == 0
        edited = rows.with_columns(
            pl.when(pl.col("name") == "sleeper")
            .then(pl.lit("changed"))
            .otherwise(pl.col("license"))
            .alias("license")
        )
        assert upsert(conn, "sources", edited, ["name"], None) == 1
        with pytest.raises(ValueError, match="unsafe"):
            upsert(conn, "sources; drop table players", rows, ["name"], None)
    engine.dispose()


def _migrated(url: str) -> Any:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    return create_engine(url)


def test_conflicted_id_loses_its_old_mapping(scratch_db_url: str) -> None:
    engine = _migrated(scratch_db_url)
    with engine.begin() as conn:
        run_id = conn.execute(
            text("INSERT INTO sync_runs (source, status) VALUES ('t', 'ok') RETURNING id")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO players (id, full_name, search_name, sync_run_id)"
                " VALUES ('00-1', 'A', 'a', :r), ('00-2', 'B', 'b', :r)"
            ),
            {"r": run_id},
        )
        # Import 1: pfr X belongs to 00-1 only.
        conn.execute(
            text(
                "INSERT INTO player_external_ids (provider, external_id, player_id, sync_run_id)"
                " VALUES ('pfr', 'X', '00-1', :r), ('pfr', 'Y', '00-2', :r)"
            ),
            {"r": run_id},
        )
    # Import 2: X is now claimed by both players.
    players_raw = pl.DataFrame({"gsis_id": ["00-1", "00-2"], "pfr_id": ["X", "X"]})
    good, conflicts = structured.nv.external_ids(
        players_raw,
        pl.DataFrame({"gsis_id": []}),
        structured.pl.DataFrame(
            schema={"provider": pl.String, "external_id": pl.String, "player_id": pl.String}
        ),
        {"00-1", "00-2"},
    )
    with engine.begin() as conn:
        assert structured.drop_conflicted(conn, conflicts) == 1
        left = conn.execute(text("SELECT external_id FROM player_external_ids")).scalars().all()
    engine.dispose()
    assert left == ["Y"]


def test_first_practice_only_report_is_kept(scratch_db_url: str) -> None:
    engine = _migrated(scratch_db_url)
    with engine.begin() as conn:
        run_id = conn.execute(
            text("INSERT INTO sync_runs (source, status) VALUES ('t', 'ok') RETURNING id")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO players (id, full_name, search_name, sync_run_id)"
                " VALUES ('00-1', 'A', 'a', :r), ('00-2', 'B', 'b', :r)"
            ),
            {"r": run_id},
        )
        run = structured.Run(run_id, conn)
        df = pl.DataFrame(
            {
                "player_id": ["00-1", "00-2"],
                "status": [None, None],
                "body_part": ["Knee", None],
                "practice": ["Did Not Participate In Practice", None],
                "season": [2026, 2026],
                "week": [7, 7],
            }
        )
        assert structured._snapshot(run, "nflverse", df) == 1  # 00-2 has no info at all
        assert structured._snapshot(run, "nflverse", df) == 0  # re-run: nothing new
    engine.dispose()


class EditedPbp(FakeNfl):
    """FakeNfl whose play-by-play can be corrected between runs, or fail to load."""

    def __init__(self) -> None:
        self.pbp: pl.DataFrame | None = self._read("pbp")

    def load_pbp(self, seasons: Any) -> pl.DataFrame:
        if self.pbp is None:
            raise ConnectionError("nflverse unreachable")
        return self.pbp


def _with_forced_fumble(pbp: pl.DataFrame, player_id: str | None) -> pl.DataFrame:
    """Credit (or un-credit) a forced fumble on the game's first kickoff."""
    first = pbp.with_row_index("_i").filter(pl.col("play_type") == "kickoff")["_i"][0]
    return pbp.with_columns(
        pl.when(pl.int_range(pl.len()) == first)
        .then(pl.lit(player_id, pl.String))
        .otherwise(pl.col("forced_fumble_player_1_player_id"))
        .alias("forced_fumble_player_1_player_id")
    )


def test_corrected_play_by_play_removes_stale_rows(
    scratch_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", scratch_db_url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    nfl = EditedPbp()
    monkeypatch.setattr(structured, "nfl", nfl)
    monkeypatch.setattr(structured, "update_config", lambda **_: None)
    engine = create_engine(scratch_db_url)
    now = datetime(2026, 10, 20, 12, tzinfo=UTC)
    original = nfl._read("pbp")
    a, b = nfl._read("players")["gsis_id"][:2].to_list()

    def state() -> tuple[list[tuple[str, Any]], list[int]]:
        with engine.connect() as conn:
            st = conn.execute(
                text("SELECT player_id, stats->'st_ff' FROM player_week_special_teams")
            ).all()
            weeks = conn.execute(text("SELECT DISTINCT week FROM team_week_defense")).scalars()
            return [tuple(r) for r in st], sorted(weeks)

    def run() -> tuple[list[tuple[str, Any]], list[int]]:
        assert structured.run_structured(engine, FakeSleeper(), now, season=2026).ok
        return state()

    nfl.pbp = _with_forced_fumble(original, a)
    assert run() == ([(a, 1)], [6])
    nfl.pbp = _with_forced_fumble(original, b)  # stat correction: credit moves
    assert run() == ([(b, 1)], [6])
    nfl.pbp = _with_forced_fumble(original, None)  # credit removed
    assert run() == ([], [6])
    nfl.pbp = _with_forced_fumble(original, a).with_columns(pl.lit(7).alias("week"))
    assert run() == ([(a, 1)], [7])  # game moved weeks: old week-6 rows are gone
    nfl.pbp = None  # a failed load deletes nothing
    report = structured.run_structured(engine, FakeSleeper(), now, season=2026)
    failed = {r.source for r in report.results if r.error}
    assert failed == {"nflverse.pbp_team_defense", "nflverse.pbp_special_teams"}
    assert state() == ([(a, 1)], [7])
    engine.dispose()
