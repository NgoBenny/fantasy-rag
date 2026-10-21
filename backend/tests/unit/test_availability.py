"""Roster snapshot + Sleeper transactions -> current ownership, and how sure we are."""

from datetime import UTC, datetime, timedelta

import pytest

from fantasy_rag.analytics.availability import SKEW, Fetch, Tx, parse_tx, reconcile

B = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)  # when the roster snapshot was requested
NOW = B + timedelta(hours=1)
WEEK = 7


def tx(
    tid: str,
    minutes: float,
    adds=None,
    drops=None,
    status="complete",
    type_="free_agent",
    week=WEEK,
):  # type: ignore[no-untyped-def]  # noqa: E501
    return Tx(tid, type_, status, week, B + timedelta(minutes=minutes), adds or {}, drops or {})


def fresh(*weeks: int) -> list[Fetch]:
    return [Fetch(w, NOW - timedelta(minutes=5), True) for w in weeks or (WEEK - 1, WEEK)]


def run(snapshot, txs, fetches=None, boundary=B):  # type: ignore[no-untyped-def]
    return reconcile(
        snapshot, WEEK, boundary, WEEK, txs, fresh() if fetches is None else fetches, NOW
    )


SNAP = {"p1": 1, "p2": 2}


def test_same_day_add_drop_and_re_add() -> None:
    assert run(SNAP, [tx("a", 10, adds={"p3": 1})]).owner == {"p1": 1, "p2": 2, "p3": 1}
    assert run(SNAP, [tx("d", 10, drops={"p1": 1})]).owner == {"p2": 2}
    add_then_drop = [tx("a", 10, adds={"p3": 1}), tx("d", 20, drops={"p3": 1})]
    assert "p3" not in run(SNAP, add_then_drop).owner
    drop_then_readd = [tx("d", 10, drops={"p1": 1}), tx("a", 20, adds={"p1": 2})]
    assert run(SNAP, drop_then_readd).owner["p1"] == 2


def test_a_trade_with_the_same_player_in_drops_and_adds_ends_on_the_receiving_roster() -> None:
    trade = tx("t", 10, adds={"p1": 2, "p2": 1}, drops={"p1": 1, "p2": 2}, type_="trade")
    got = run(SNAP, [trade])
    assert got.owner == {"p1": 2, "p2": 1} and got.as_of["status"] == "current"


def test_chopped_frees_a_whole_roster_and_failed_ones_change_nothing() -> None:
    chop = tx("c", 10, adds=None, drops={"p1": 1, "p9": 1}, type_="chopped")
    assert run({"p1": 1, "p9": 1, "p2": 2}, [chop]).owner == {"p2": 2}
    failed = tx("f", 10, adds={"p3": 1}, drops={"p1": 1}, status="failed", type_="waiver")
    got = run(SNAP, [failed])
    assert got.owner == SNAP and got.as_of["applied"] == 0


def test_snapshot_boundary_overlap() -> None:
    before_overlap = tx("old", -(SKEW.total_seconds() / 60) - 1, drops={"p1": 1})
    assert run(SNAP, [before_overlap]).owner == SNAP  # already in the snapshot: ignored
    # Inside the overlap and already reflected by the snapshot: replaying changes nothing.
    in_overlap = tx("o", -5, adds={"p1": 1})
    assert run(SNAP, [in_overlap]).owner == SNAP
    # Completed between the request and the snapshot write: replayed, and it matters.
    between = tx("b", 0.5, drops={"p2": 2})
    assert run(SNAP, [between]).owner == {"p1": 1}


def test_week_rollover_and_backfilled_outage() -> None:
    txs = [tx("w6", 10, adds={"p3": 1}, week=6), tx("w7", 20, adds={"p4": 2}, week=7)]
    got = reconcile(SNAP, 5, B, WEEK, txs, fresh(4, 5, 6, 7), NOW)
    assert {"p3", "p4"} <= got.rostered and got.as_of["status"] == "current"
    # An outage over two weeks: weeks 5 and 6 never fetched successfully -> not current.
    gap = [Fetch(4, NOW, True), Fetch(5, NOW, False), Fetch(7, NOW, True)]
    stale = reconcile(SNAP, 5, B, WEEK, txs, gap, NOW)
    assert stale.as_of["status"] == "stale" and stale.as_of["missing_weeks"] == [5, 6]
    assert stale.as_of["note"].startswith("availability as of")


def test_old_or_failed_fetches_are_stale() -> None:
    old = [Fetch(w, NOW - timedelta(hours=3), True) for w in (6, 7)]
    assert run(SNAP, [], old).as_of["status"] == "stale"
    one_failed = [Fetch(6, NOW, True), Fetch(7, NOW, False)]
    got = run(SNAP, [tx("a", 10, adds={"p3": 1})], one_failed)
    assert got.as_of["status"] == "stale" and "p3" in got.owner  # corrections still applied
    before_snapshot = [Fetch(w, B - timedelta(minutes=1), True) for w in (6, 7)]
    assert run(SNAP, [], before_snapshot).as_of["missing_weeks"] == [6, 7]


def test_uncertain_when_an_assumption_cannot_be_checked() -> None:
    no_boundary = run(SNAP, [tx("a", 10, adds={"p3": 1})], boundary=None)
    assert no_boundary.as_of["status"] == "uncertain" and no_boundary.owner == SNAP
    future = tx("f", (NOW - B).total_seconds() / 60 + 30, adds={"p3": 1})
    assert run(SNAP, [future]).as_of["status"] == "uncertain"
    odd = tx("x", 10, adds={"p3": 1}, type_="commissioner")
    got = run(SNAP, [odd])
    assert got.as_of["status"] == "uncertain" and "commissioner" in got.as_of["reasons"][0]


def test_parse_normalizes_only_null_and_rejects_malformed() -> None:
    raw = {"transaction_id": "1", "type": "chopped", "status": "complete", "leg": 6,
           "status_updated": 1791961814588, "adds": None, "drops": {"4034": 3}}  # fmt: skip
    t = parse_tx(raw)
    assert t.adds == {} and t.drops == {"4034": 3} and t.week == 6
    assert t.status_updated.year == 2026
    for bad in ([], "x", {"4034": "3"}, {"4034": True}):
        with pytest.raises(ValueError, match="malformed"):
            parse_tx({**raw, "adds": bad})
    with pytest.raises(ValueError, match="malformed"):
        parse_tx({k: v for k, v in raw.items() if k != "leg"})


def test_an_unmapped_player_movement_since_the_snapshot_is_uncertain() -> None:
    # A completed add of a Sleeper ID we can't map: we can't know he left the free pool.
    unmapped = Tx("u", "free_agent", "complete", WEEK, B + timedelta(minutes=10), {}, {},
                  frozenset({"999"}))  # fmt: skip
    got = run(SNAP, [unmapped])
    assert got.as_of["status"] == "uncertain" and "999" in got.as_of["reasons"][0]
    # Before the replay window it's already reflected in the snapshot; failed ones do nothing.
    old = Tx("o", "free_agent", "complete", WEEK, B - SKEW * 2, {}, {}, frozenset({"999"}))
    failed = Tx("f", "waiver", "failed", WEEK, B + timedelta(minutes=10), {}, {},
                frozenset({"999"}))  # fmt: skip
    assert run(SNAP, [old, failed]).as_of["status"] == "current"
    # A team defense is known to be outside the ranked (QB/RB/WR/TE/K) population.
    defense = Tx("d", "waiver", "complete", WEEK, B + timedelta(minutes=10), {}, {},
                 frozenset({"MIN"}))  # fmt: skip
    got = reconcile(SNAP, WEEK, B, WEEK, [defense], fresh(), NOW, frozenset({"MIN"}))
    assert got.as_of["status"] == "current"
