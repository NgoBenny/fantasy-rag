"""The spending guard: counted reservations, no hidden retries, overrun lockout (no API)."""

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from fantasy_rag.agent.budget import Budget, BudgetExceeded
from fantasy_rag.agent.llm import ROUTER_MAX_TOKENS, ROUTER_MODEL, AnthropicLLM, price


class FakeMessages:
    def __init__(self, counted: int, used: tuple[int, int], fail: bool = False) -> None:
        self.counted, self.used, self.fail = counted, used, fail
        self.sent: list[dict[str, Any]] = []

    def count_tokens(self, **kw: Any) -> Any:
        assert "max_tokens" not in kw
        return SimpleNamespace(input_tokens=self.counted)

    def create(self, **kw: Any) -> Any:
        self.sent.append(kw)
        if self.fail:
            raise RuntimeError("connection reset")
        usage = SimpleNamespace(input_tokens=self.used[0], output_tokens=self.used[1])
        return SimpleNamespace(model=kw["model"], usage=usage, content=[], stop_reason="end_turn")


def llm(budget: Budget, msgs: FakeMessages) -> AnthropicLLM:
    out = AnthropicLLM(api_key="test-key", budget=budget)
    assert out.client.max_retries == 0  # no SDK retries under a cap
    out.client = SimpleNamespace(messages=msgs)  # type: ignore[assignment]
    return out


def allowance(counted: int) -> float:
    p_in, p_out = price(ROUTER_MODEL)
    return (counted * p_in + ROUTER_MAX_TOKENS * p_out) / 1e6


def turn(m: AnthropicLLM) -> Any:
    return m.tool_turn("system", [{"role": "user", "content": "hi"}], [])


def test_a_zero_cap_refuses_before_any_model_call() -> None:
    msgs = FakeMessages(counted=10, used=(10, 5))
    with pytest.raises(BudgetExceeded):
        turn(llm(Budget(0), msgs))
    assert msgs.sent == []


def test_the_allowance_uses_counted_input_not_the_text_length() -> None:
    # "hi" is two characters, but the counted request (system, tools) is far bigger.
    big = FakeMessages(counted=2_000_000, used=(10, 5))
    cap = allowance(10) * 50  # would fit any estimate from the text alone
    budget = Budget(cap)
    with pytest.raises(BudgetExceeded):
        turn(llm(budget, big))
    assert big.sent == [] and budget.spent == 0 and budget.reserved == 0


def test_settle_charges_the_real_cost_and_releases_the_rest() -> None:
    msgs = FakeMessages(counted=1000, used=(1000, 100))
    budget = Budget(1.0)
    turn(llm(budget, msgs))
    p_in, p_out = price(ROUTER_MODEL)
    assert budget.spent == pytest.approx((1000 * p_in + 100 * p_out) / 1e6)
    assert budget.reserved == 0
    assert msgs.sent[0]["max_tokens"] == ROUTER_MAX_TOKENS  # output is bounded


def test_a_failed_call_is_charged_its_full_allowance() -> None:
    msgs = FakeMessages(counted=1000, used=(0, 0), fail=True)
    budget = Budget(1.0)
    m = llm(budget, msgs)
    with pytest.raises(RuntimeError):
        turn(m)
    assert budget.spent == pytest.approx(allowance(1000)) and budget.reserved == 0
    assert m.unpriced == 1 and m.usage == {}  # its real usage is unknown, not zero


def test_an_overrun_is_charged_and_locks_the_budget() -> None:
    # Reported usage above the allowance (a counting mismatch): charged, then locked.
    msgs = FakeMessages(counted=100, used=(500_000, 100))
    budget = Budget(10.0)
    m = llm(budget, msgs)
    turn(m)
    assert budget.locked_reason and budget.spent > allowance(100)
    with pytest.raises(BudgetExceeded, match="locked"):
        turn(m)
    assert len(msgs.sent) == 1


def test_concurrent_reservations_cannot_both_fit() -> None:
    budget = Budget(1.0)
    gate = threading.Barrier(8)
    got: list[bool] = []

    def take() -> None:
        gate.wait()
        try:
            budget.reserve(0.6)
            got.append(True)
        except BudgetExceeded:
            got.append(False)

    threads = [threading.Thread(target=take) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got.count(True) == 1 and budget.reserved == pytest.approx(0.6)
