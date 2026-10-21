"""Prediction ranges from earlier out-of-sample errors (split conformal on final points).

Residuals r = actual - predicted, sorted r(1) <= ... <= r(n), one-based, no interpolation:
lower = prediction + r(floor((n+1)*0.1)), upper = prediction + r(ceil((n+1)*0.9)).
That is nominally 80% under exchangeability; a changing season and weekly retraining
break that assumption, so coverage is measured, not promised.
"""

from math import ceil, floor

MIN_RESIDUALS = 50
LOW, HIGH = 0.1, 0.9


def offsets(residuals: list[float]) -> tuple[float, float] | None:
    """(lower, upper) offsets to add to a prediction, or None without enough history."""
    n = len(residuals)
    lo, hi = floor((n + 1) * LOW), ceil((n + 1) * HIGH)
    if n < MIN_RESIDUALS or lo < 1 or hi > n:
        return None
    r = sorted(residuals)
    return r[lo - 1], r[hi - 1]
