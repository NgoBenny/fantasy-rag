import polars as pl

from fantasy_rag.verify_sources import (
    max_of,
    null_rate,
    render,
    summarize_sleeper_players,
    top_values,
)


def test_null_rate_counts_nulls_and_blanks_and_handles_missing_column() -> None:
    df = pl.DataFrame({"id": ["00-0000001", None, "  ", "00-0000002"]})
    assert null_rate(df, "id") == 0.5
    assert null_rate(df, "date_modified") is None


def test_max_and_top_values() -> None:
    df = pl.DataFrame({"week": [1, 6, 3], "status": ["Out", "Out", "Questionable"]})
    assert max_of(df, "week") == 6
    assert max_of(df, "missing") is None
    assert top_values(df, "status") == {"Out": 2, "Questionable": 1}


def test_sleeper_summary_links_ids_and_flags_gaps() -> None:
    players = {
        "1": {
            "player_id": "1",
            "active": True,
            "team": "DET",
            "position": "WR",
            "gsis_id": " 00-0000001",
            "injury_status": "Questionable",
        },
        "2": {
            "player_id": "2",
            "active": True,
            "team": "KC",
            "position": "RB",
            "gsis_id": "00-9999999",
        },  # disagrees with crosswalk
        "3": {"player_id": "3", "active": True, "team": None, "position": "QB"},  # no team
        "4": {"player_id": "4", "active": True, "team": "BUF", "position": "DL"},  # not skill
    }
    crosswalk = pl.DataFrame({"sleeper_id": ["1", "2"], "gsis_id": ["00-0000001", "00-0000002"]})
    stat_gsis = {"00-0000001", "00-0000002", "00-0000003"}

    facts, gaps = summarize_sleeper_players(players, crosswalk, stat_gsis)

    assert facts["active_skill_players_on_a_team (QB/RB/WR/TE/K)"] == 2
    assert facts["sleeper_gsis_vs_crosswalk_gsis_disagreements"] == "1 of 2"
    assert facts["stat_players_linked_via_either"] == "2 of 3 (66.7%)"
    assert facts["injury_status_values"] == {"Questionable": 1}
    assert any("1 skill players" in g for g in gaps)
    assert any("practice_participation" in g for g in gaps)


def test_render_lists_gaps_facts_and_columns() -> None:
    md = render(
        [("load_injuries([2026])", {"rows": 3, "has_date_modified": False, "columns": ["week"]})],
        ["injuries 2026: `date_modified` column is absent."],
        {"Season checked": 2026},
    )
    assert "- injuries 2026: `date_modified` column is absent." in md
    assert "| has_date_modified | False |" in md
    assert "<details><summary>1 columns</summary>" in md
    assert render([], [], {}).count("- None found.") == 1
