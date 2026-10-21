"""A spending guard shared by every model call in one process (or one test session).

Before each call the caller reserves the call's worst case (counted input tokens plus
`max_tokens` of output, at list prices); the reservation fails, and the call never
starts, if spent + reserved would pass the cap. Reservations are taken under one lock,
so concurrent requests can't both slip under the cap. After the call the reservation
is settled at its real cost. A strong guard on our spending, not a guarantee about the
provider's bill: if a call ever reports more usage than it reserved (a counting
mismatch), the overrun is charged and the budget locks, refusing every later call.
"""

import threading
from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Reservation:
    id: int
    amount: float


class Budget:
    def __init__(self, cap_usd: float) -> None:
        if cap_usd < 0:
            raise ValueError("cap_usd must be >= 0")
        self.cap = cap_usd
        self.spent = 0.0
        self._held: dict[int, float] = {}
        self._next = 0
        self.locked_reason: str | None = None
        self._lock = threading.Lock()

    @property
    def reserved(self) -> float:
        return sum(self._held.values())

    def reserve(self, amount: float) -> Reservation:
        with self._lock:
            if self.locked_reason:
                raise BudgetExceeded(f"budget locked: {self.locked_reason}")
            if self.spent + self.reserved + amount > self.cap:
                raise BudgetExceeded(
                    f"a call needing up to ${amount:.4f} would pass the ${self.cap:.2f} cap"
                    f" (spent ${self.spent:.4f}, reserved ${self.reserved:.4f})"
                )
            self._next += 1
            self._held[self._next] = amount
            return Reservation(self._next, amount)

    def settle(self, r: Reservation, actual: float | None) -> None:
        """Charge the real cost (None = unknown, e.g. the call failed: charge it all)."""
        with self._lock:
            held = self._held.pop(r.id)
            cost = held if actual is None else actual
            self.spent += cost
            if cost > held:
                self.locked_reason = f"a call cost ${cost:.4f}, more than its ${held:.4f} allowance"
