"""Who is rostered right now: the latest roster snapshot corrected by Sleeper transactions.

Conditional on assumptions Sleeper does not document; where one can't be established the
result says `uncertain` instead of claiming to be current:
  A1. `/rosters` reflects every transaction completed before its response (not proven
      to be an atomic snapshot).
  A2. `status_updated` is Sleeper server time within SKEW of our clock (unverified).
  A3. A completed transaction's adds/drops are its whole effect; commissioner edits that
      bypass transactions are invisible to us.
So the answer is "as best the source allows", never a guarantee.

Algorithm: start from the snapshot (boundary B = when its /rosters request started),
replay every *completed* transaction updated at or after B - SKEW in (time, id) order.
Each transaction is atomic: a player in its adds ends on that roster even if he is also
in its drops (a trade); a player only in its drops ends free. Replaying a transaction
the snapshot already reflects changes nothing, so the overlap is safe.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

SKEW = timedelta(minutes=15)
MAX_FETCH_AGE = timedelta(hours=2)
KNOWN_TYPES = {"free_agent", "waiver", "trade", "chopped"}  # seen live, Oct 21 2026
Status = Literal["current", "stale", "uncertain"]


@dataclass(frozen=True)
class Tx:
    transaction_id: str
    type: str
    status: str
    week: int
    status_updated: datetime
    adds: dict[str, int]  # player ID -> roster_id
    drops: dict[str, int]


@dataclass(frozen=True)
class Fetch:
    week: int
    fetched_at: datetime
    ok: bool


@dataclass
class Availability:
    owner: dict[str, int]  # player ID -> roster_id, after corrections
    as_of: dict[str, Any] = field(default_factory=dict)

    @property
    def rostered(self) -> set[str]:
        return set(self.owner)


def _mapping(value: Any, name: str) -> dict[str, int]:
    """Only null becomes empty; anything else that isn't {player: roster_id} is rejected."""
    if value is None:
        return {}
    if not isinstance(value, Mapping) or not all(
        isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
        for k, v in value.items()
    ):
        raise ValueError(f"malformed {name}: expected {{player_id: roster_id}}")
    return dict(value)


def parse_tx(raw: Mapping[str, Any]) -> Tx:
    """One Sleeper transaction record -> Tx (Sleeper player IDs). Raises on malformed input."""
    try:
        updated = int(raw["status_updated"])
        return Tx(
            transaction_id=str(raw["transaction_id"]),
            type=str(raw["type"]),
            status=str(raw["status"]),
            week=int(raw["leg"]),
            status_updated=datetime.fromtimestamp(updated / 1000, tz=UTC),
            adds=_mapping(raw.get("adds"), "adds"),
            drops=_mapping(raw.get("drops"), "drops"),
        )
    except (KeyError, TypeError) as e:
        raise ValueError(f"malformed transaction: {e!r}") from e


def reconcile(
    snapshot: Mapping[str, int],
    snapshot_week: int,
    boundary: datetime | None,
    current_week: int,
    txs: Iterable[Tx],
    fetches: Iterable[Fetch],
    now: datetime,
) -> Availability:
    owner = dict(snapshot)
    reasons: list[str] = []
    status: Status = "current"
    applied = 0
    unknown_types: set[str] = set()
    if boundary is None:
        # rosters imported before the fetch log existed: no boundary to replay from
        return Availability(owner, _as_of("uncertain", None, [], None, 0, [
            "the roster snapshot has no recorded fetch time"
        ]))  # fmt: skip
    since = boundary - SKEW
    replay = sorted(
        (t for t in txs if t.status == "complete" and t.status_updated >= since),
        key=lambda t: (t.status_updated, t.transaction_id),
    )
    for t in replay:
        if t.status_updated > now + SKEW:
            status = "uncertain"
            reasons.append(f"a transaction is dated after now + {SKEW} (clock skew?)")
        if t.type not in KNOWN_TYPES:
            unknown_types.add(t.type)
        for pid in t.drops.keys() - t.adds.keys():
            owner.pop(pid, None)
        owner.update(t.adds)
        applied += 1
    if unknown_types:
        status = "uncertain"
        reasons.append(f"unfamiliar transaction types: {', '.join(sorted(unknown_types))}")

    weeks = range(max(1, snapshot_week - 1), current_week + 1)
    latest = {f.week: f for f in fetches}
    missing = [w for w in weeks if not ((f := latest.get(w)) and f.ok and f.fetched_at > boundary)]
    newest = max((f.fetched_at for f in latest.values() if f.ok), default=None)
    if missing or newest is None or now - newest >= MAX_FETCH_AGE:
        if status == "current":
            status = "stale"
        if missing:
            reasons.append(f"weeks without a successful fetch since the roster snapshot: {missing}")
        if newest is None or now - newest >= MAX_FETCH_AGE:
            reasons.append(f"no successful transaction fetch in the last {MAX_FETCH_AGE}")
    return Availability(owner, _as_of(status, boundary, missing, newest, applied, reasons))


def _as_of(
    status: Status,
    boundary: datetime | None,
    missing: list[int],
    newest: datetime | None,
    applied: int,
    reasons: list[str],
) -> dict[str, Any]:
    note = None
    if status != "current":
        when = boundary.isoformat() if boundary else "the last roster import"
        note = f"availability as of {when}; adds/drops since then may be missing or unconfirmed"
    return {
        "status": status,
        "roster_fetched_at": boundary,
        "transactions_fetched_at": newest,
        "applied": applied,
        "missing_weeks": missing,
        "reasons": reasons,
        "note": note,
    }
