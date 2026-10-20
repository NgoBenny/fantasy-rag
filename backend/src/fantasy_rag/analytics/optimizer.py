"""Lineup optimizer: the best legal lineup is a max-weight assignment of players to slots.

Each starting slot takes at most one eligible player and each player fills at most one
slot. Locked players keep their slot. Solved exactly with the Hungarian algorithm
(scipy's linear_sum_assignment); no LLM involved.
"""

from dataclasses import dataclass

import numpy as np
from scipy.optimize import linear_sum_assignment

ELIGIBLE: dict[str, set[str]] = {
    "QB": {"QB"},
    "RB": {"RB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "K": {"K"},
    "FLEX": {"RB", "WR", "TE"},
    "WRRB_FLEX": {"WR", "RB"},
    "REC_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}
BENCH = {"BN", "IR", "TAXI"}
FORBIDDEN = -1e9


@dataclass(frozen=True)
class Candidate:
    id: str
    position: str
    points: float


@dataclass
class Lineup:
    slots: list[tuple[str, str | None, float]]  # (slot, player id or None, projected points)
    total: float
    unhandled_slots: list[str]  # starting slots this optimizer can't fill (e.g. DEF, IDP)


TIE_BREAK = 1e-6  # far below 0.01-point projections, so it only breaks exact ties


def optimize(
    roster_positions: list[str],
    players: list[Candidate],
    locks: dict[str, int] | None = None,
    prefer: dict[str, int] | None = None,
) -> Lineup:
    """`locks` maps player id -> index into roster_positions that the player must keep.

    `prefer` (player id -> his current slot index) breaks ties toward the current lineup,
    so equal-value swaps (WR <-> FLEX) aren't recommended as moves.
    """
    locks, prefer = locks or {}, prefer or {}
    by_id = {p.id: p for p in players}
    starting = [(i, s) for i, s in enumerate(roster_positions) if s not in BENCH]
    unhandled = [s for _, s in starting if s not in ELIGIBLE]

    chosen: dict[int, Candidate] = {}
    for pid, idx in locks.items():
        p, slot = by_id.get(pid), roster_positions[idx] if 0 <= idx < len(roster_positions) else ""
        if p is None or p.position not in ELIGIBLE.get(slot, set()):
            raise ValueError(f"locked player {pid} can't fill slot {idx} ({slot or 'missing'})")
        chosen[idx] = p

    open_slots = [(i, s) for i, s in starting if s in ELIGIBLE and i not in chosen]
    pool = [p for p in players if p.id not in locks]
    if open_slots and pool:
        # One zero-point "leave it empty" column per slot: an empty slot (0) beats a
        # negative projection, and no slot is ever forced to take an ineligible player.
        value = np.array(
            [
                [
                    p.points + (TIE_BREAK if prefer.get(p.id) == i else 0.0)
                    if p.position in ELIGIBLE[s]
                    else FORBIDDEN
                    for p in pool
                ]
                + [0.0] * len(open_slots)
                for i, s in open_slots
            ]
        )
        rows, cols = linear_sum_assignment(value, maximize=True)
        for r, c in zip(rows, cols, strict=True):
            if c < len(pool) and value[r, c] > FORBIDDEN / 2:
                chosen[open_slots[r][0]] = pool[c]

    slots = [
        (s, chosen[i].id if i in chosen else None, chosen[i].points if i in chosen else 0.0)
        for i, s in starting
    ]
    return Lineup(slots, round(sum(pts for _, _, pts in slots), 2), unhandled)
