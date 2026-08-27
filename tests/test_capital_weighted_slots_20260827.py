"""Capital-derived system slot limits stay opt-in and auditable."""

from __future__ import annotations

import json

import pandas as pd
from pandas.testing import assert_frame_equal

import core.final_allocation as allocation
from core.final_allocation import (
    CapitalSlotPolicy,
    derive_capital_weighted_slots,
    finalize_allocation,
    to_allocation_summary_dict,
)


class _Strategy:
    def __init__(self, max_pct: float) -> None:
        self.config = {
            "max_positions": 10,
            "risk_pct": 0.02,
            "max_pct": max_pct,
        }


def _candidates(system: str, count: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [f"{system.upper()}_{i:02d}" for i in range(count)],
            "score": list(range(count, 0, -1)),
            "entry_price": [100.0] * count,
            "stop_price": [95.0] * count,
        }
    )


LONG = {"system1": 0.25, "system3": 0.25, "system4": 0.25, "system5": 0.25}
SHORT = {"system2": 0.40, "system6": 0.40, "system7": 0.20}
MAX_PCT = {
    "system1": 0.10,
    "system2": 0.10,
    "system3": 0.10,
    "system4": 0.10,
    "system5": 0.10,
    "system6": 0.10,
    "system7": 0.20,
}
CAPS = {
    "max_total_positions": 70,
    "max_long_positions": 40,
    "max_short_positions": 30,
    "max_gross_exposure_pct": 1.0,
    "max_net_exposure_pct": 0.5,
}


def test_formula_uses_side_weight_and_fixed_system_max_pct() -> None:
    result = derive_capital_weighted_slots(
        long_allocations=LONG,
        short_allocations=SHORT,
        max_pct_by_system=MAX_PCT,
        equity=100_000.0,
        long_ratio=0.5,
        gross_exposure_pct=1.0,
        gross_budget_factor=1.0,
        min_slots=1,
        max_long_positions=40,
        max_short_positions=30,
        max_total_positions=70,
        max_net_exposure_pct=0.5,
    )

    assert result.system_budgets == {
        "system1": 12_500.0,
        "system3": 12_500.0,
        "system4": 12_500.0,
        "system5": 12_500.0,
        "system2": 20_000.0,
        "system6": 20_000.0,
        "system7": 10_000.0,
    }
    assert result.per_position_notional == {
        "system1": 10_000.0,
        "system2": 10_000.0,
        "system3": 10_000.0,
        "system4": 10_000.0,
        "system5": 10_000.0,
        "system6": 10_000.0,
        "system7": 20_000.0,
    }
    assert result.raw_slots == {
        "system1": 1.25,
        "system3": 1.25,
        "system4": 1.25,
        "system5": 1.25,
        "system2": 2.0,
        "system6": 2.0,
        "system7": 0.5,
    }
    # System7 is below one whole 20%-notional slot; the configured positive
    # system floor is an explicit hedge availability choice, not rounding.
    assert result.slots == {
        "system1": 1,
        "system3": 1,
        "system4": 1,
        "system5": 1,
        "system2": 2,
        "system6": 2,
        "system7": 1,
    }


def test_derived_slots_are_clamped_to_long_short_and_total_pools() -> None:
    low_notional = {name: 0.01 for name in MAX_PCT}
    result = derive_capital_weighted_slots(
        long_allocations=LONG,
        short_allocations=SHORT,
        max_pct_by_system=low_notional,
        equity=100_000.0,
        long_ratio=0.5,
        gross_exposure_pct=1.0,
        gross_budget_factor=1.0,
        min_slots=1,
        max_long_positions=40,
        max_short_positions=30,
        max_total_positions=70,
        max_net_exposure_pct=0.5,
    )
    assert sum(result.slots[name] for name in LONG) <= 40
    assert sum(result.slots[name] for name in SHORT) <= 30
    assert sum(result.slots.values()) <= 70


def test_planning_budget_obeys_gross_and_net_exposure_limits() -> None:
    result = derive_capital_weighted_slots(
        long_allocations=LONG,
        short_allocations=SHORT,
        max_pct_by_system=MAX_PCT,
        equity=100_000.0,
        # The requested 90/10 split would exceed the configured 50% net cap.
        long_ratio=0.90,
        gross_exposure_pct=1.0,
        gross_budget_factor=1.0,
        min_slots=1,
        max_long_positions=40,
        max_short_positions=30,
        max_total_positions=70,
        max_net_exposure_pct=0.5,
    )

    long_budget = sum(result.system_budgets[name] for name in LONG)
    short_budget = sum(result.system_budgets[name] for name in SHORT)
    assert long_budget + short_budget <= 100_000.0
    assert abs(long_budget - short_budget) <= 50_000.0
    assert long_budget == 75_000.0
    assert short_budget == 25_000.0


def test_flag_off_is_byte_identical_to_legacy_slot_allocation(monkeypatch) -> None:
    per_system = {
        "system1": _candidates("system1", 4),
        "system2": _candidates("system2", 4),
        "system3": _candidates("system3", 4),
    }
    strategies = {name: _Strategy(MAX_PCT[name]) for name in per_system}
    monkeypatch.setattr(
        allocation,
        "_load_capital_slot_policy",
        lambda: CapitalSlotPolicy(enabled=False),
    )
    before_df, before_summary = finalize_allocation(
        per_system,
        strategies=strategies,
        long_allocations={"system1": 0.5, "system3": 0.5},
        short_allocations={"system2": 1.0},
        include_trade_management=False,
    )
    after_df, after_summary = finalize_allocation(
        per_system,
        strategies=strategies,
        long_allocations={"system1": 0.5, "system3": 0.5},
        short_allocations={"system2": 1.0},
        slot_capital_equity=100_000.0,
        include_trade_management=False,
    )

    assert_frame_equal(before_df, after_df, check_dtype=True, check_like=False)
    assert (
        before_df.to_csv(index=False).encode() == after_df.to_csv(index=False).encode()
    )
    assert json.dumps(
        to_allocation_summary_dict(before_summary), sort_keys=True
    ) == json.dumps(to_allocation_summary_dict(after_summary), sort_keys=True)


def test_flag_on_replaces_uniform_ten_with_capital_weighted_slots(monkeypatch) -> None:
    per_system = {name: _candidates(name, 10) for name in (*LONG, *SHORT)}
    strategies = {name: _Strategy(MAX_PCT[name]) for name in per_system}
    monkeypatch.setattr(
        allocation,
        "_load_capital_slot_policy",
        lambda: CapitalSlotPolicy(enabled=True, gross_budget_factor=1.0, min_slots=1),
    )
    monkeypatch.setattr(allocation, "_load_portfolio_caps", lambda: dict(CAPS))

    final_df, summary = finalize_allocation(
        per_system,
        strategies=strategies,
        long_allocations=LONG,
        short_allocations=SHORT,
        slot_capital_equity=100_000.0,
        include_trade_management=False,
    )

    assert summary.final_counts == {
        "system1": 1,
        "system2": 2,
        "system3": 1,
        "system4": 1,
        "system5": 1,
        "system6": 2,
        "system7": 1,
    }
    assert len(final_df) == 9
    diag = summary.system_diagnostics["capital_slots"]
    assert diag["slots"] == summary.final_counts
    assert diag["equity_source"] == "account_start_equity"
