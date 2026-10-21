"""Which Sleeper source wins for the live week's players and starters (no network)."""

from fantasy_rag.ingest.league import live_week_entries

ROSTER = {"roster_id": 1, "players": ["11", "22", "33", "44"], "starters": ["11", "22"]}


def test_a_lineup_change_in_the_matchup_wins_over_lagging_roster_starters() -> None:
    # Seen 2026-10-21: /rosters still held an old lineup; the matchup had the new one.
    matchup = {"roster_id": 1, "matchup_id": 3, "players": ["11", "22", "33", "44"],
               "starters": ["11", "33"]}  # fmt: skip
    [got] = live_week_entries([matchup], [ROSTER])
    assert got["starters"] == ["11", "33"] and got["matchup_id"] == 3


def test_roster_players_win_when_the_matchup_omits_them() -> None:
    matchup = {"roster_id": 1, "matchup_id": 3, "players": [], "starters": ["11", "33"]}
    [got] = live_week_entries([matchup], [ROSTER])
    assert got["players"] == ROSTER["players"] and got["starters"] == ["11", "33"]


def test_roster_starters_when_the_matchup_has_none_or_names_a_dropped_player() -> None:
    empty = {"roster_id": 1, "players": [], "starters": ["0", "0"]}
    assert live_week_entries([empty], [ROSTER])[0]["starters"] == ["11", "22"]
    stale = {"roster_id": 1, "players": ["11", "99"], "starters": ["11", "99"]}  # 99 dropped
    assert live_week_entries([stale], [ROSTER])[0]["starters"] == ["11", "22"]
    assert live_week_entries([], [ROSTER])[0]["starters"] == ["11", "22"]  # no matchup yet
