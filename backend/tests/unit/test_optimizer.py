import itertools
import random

import pytest

from fantasy_rag.analytics.optimizer import ELIGIBLE, Candidate, optimize

POSITIONS = ["QB", "RB", "WR", "TE", "K"]
SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "K", "DEF", "BN", "BN"]


def brute_force(slots: list[str], players: list[Candidate], locks: dict[str, int]) -> float:
    """Best total by trying every assignment of players (or nobody) to the starting slots."""
    starting = [(i, s) for i, s in enumerate(slots) if s in ELIGIBLE]
    best = float("-inf")
    choices = [*players, None]
    for combo in itertools.product(choices, repeat=len(starting)):
        used = [p.id for p in combo if p]
        if len(used) != len(set(used)):
            continue
        ok = all(
            p is None or p.position in ELIGIBLE[s]
            for (_, s), p in zip(starting, combo, strict=True)
        )
        ok &= all(
            any(
                i == idx and p is not None and p.id == pid
                for (i, _), p in zip(starting, combo, strict=True)
            )
            for pid, idx in locks.items()
        )
        if ok:
            best = max(best, sum(p.points for p in combo if p))
    return round(best, 2)


@pytest.mark.parametrize("seed", range(25))
def test_matches_brute_force_and_is_legal(seed: int) -> None:
    rng = random.Random(seed)
    slots = ["QB", "RB", "WR", "FLEX", "SUPER_FLEX", "BN"]
    players = [
        Candidate(f"p{i}", rng.choice(POSITIONS[:4]), round(rng.uniform(-2, 30), 1))
        for i in range(7)
    ]
    locks = {}
    if seed % 3 == 0:  # lock someone into a slot he's eligible for
        p = next(p for p in players if p.position in ELIGIBLE["SUPER_FLEX"])
        locks = {p.id: 4}
    lineup = optimize(slots, players, locks)
    assert lineup.total == brute_force(slots, players, locks)
    used = [pid for _, pid, _ in lineup.slots if pid]
    assert len(used) == len(set(used))  # nobody twice
    by_id = {p.id: p for p in players}
    assert all(pid is None or by_id[pid].position in ELIGIBLE[s] for s, pid, _ in lineup.slots)
    for pid, idx in locks.items():
        assert lineup.slots[idx][1] == pid  # locked player kept his slot


def test_bench_ignored_def_reported_and_bad_lock_refused() -> None:
    players = [Candidate("qb", "QB", 20), Candidate("k", "K", 8), Candidate("rb", "RB", 12)]
    lineup = optimize(SLOTS, players)
    assert lineup.unhandled_slots == ["DEF"]
    assert len(lineup.slots) == 10  # 12 slots minus 2 bench
    assert ("QB", "qb", 20) in lineup.slots and ("K", "k", 8) in lineup.slots
    with pytest.raises(ValueError, match="can't fill"):
        optimize(SLOTS, players, {"k": 0})  # a kicker can't be locked at QB


def test_ties_keep_players_in_their_current_slots() -> None:
    # Two WRs worth the same: either WR/FLEX order is optimal; keep the current one.
    players = [Candidate("a", "WR", 10.0), Candidate("b", "WR", 10.0), Candidate("c", "WR", 5.0)]
    slots = ["WR", "FLEX", "BN"]
    for prefer in ({"a": 0, "b": 1}, {"b": 0, "a": 1}):
        lineup = optimize(slots, players, prefer=prefer)
        assert [pid for _, pid, _ in lineup.slots] == sorted(prefer, key=prefer.__getitem__)
        assert lineup.total == 20.0
