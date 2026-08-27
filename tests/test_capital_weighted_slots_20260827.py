"""Capital-derived system slot limits stay opt-in and auditable."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import textwrap

import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

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


# ---------------------------------------------------------------------------
# P1: the ON path must actually bite, on every route the production code takes.
# The pre-existing ON tests all monkeypatch ``_load_capital_slot_policy``, so
# they would still pass if the real config wiring were dead — the same blind
# spot that let the coid-map regression through.  The tests below close it.
# ---------------------------------------------------------------------------


class _SizingStrategy(_Strategy):
    """A strategy that can actually size a position.

    ``_allocate_by_capital`` returns nothing when ``calculate_position_size``
    is absent, so capital-mode tests need a real one to observe the slot cap.
    """

    def calculate_position_size(
        self,
        capital: float,
        entry_price: float,
        stop_price: float,
        *,
        risk_pct: float,
        max_pct: float,
    ) -> int:
        risk_per_share = abs(float(entry_price) - float(stop_price))
        by_risk = (
            int((float(capital) * risk_pct) // risk_per_share)
            if risk_per_share > 0
            else 0
        )
        by_cap = (
            int((float(capital) * max_pct) // float(entry_price))
            if float(entry_price) > 0
            else 0
        )
        return max(0, min(by_risk, by_cap))


class _Position:
    """Minimal stand-in for an Alpaca position object."""

    def __init__(self, symbol: str, qty: float = 1.0) -> None:
        self.symbol = symbol
        self.qty = qty
        self.side = "long" if qty >= 0 else "short"


def _on_policy(**kwargs: object) -> CapitalSlotPolicy:
    params: dict[str, object] = {
        "enabled": True,
        "gross_budget_factor": 1.0,
        "min_slots": 1,
    }
    params.update(kwargs)
    return CapitalSlotPolicy(**params)  # type: ignore[arg-type]


CONFIG_YAML_TEMPLATE = """
risk:
  risk_pct: 0.02
  max_positions: 10
  max_pct: 0.10
  slots_from_capital: {flag}
  slots_from_capital_gross_budget_factor: 1.0
  slots_from_capital_min_slots: 1
  portfolio:
    max_total_positions: 70
    max_long_positions: 40
    max_short_positions: 30
    max_gross_exposure_pct: 1.0
    max_net_exposure_pct: 0.5
ui:
  default_capital: 100000
  default_long_ratio: 0.5
  long_allocations:
    system1: 0.25
    system3: 0.25
    system4: 0.25
    system5: 0.25
  short_allocations:
    system2: 0.40
    system6: 0.40
    system7: 0.20
"""


def _write_config(tmp_path: Path, *, flag: bool) -> Path:
    path = tmp_path / f"config_{'on' if flag else 'off'}.yaml"
    path.write_text(
        textwrap.dedent(CONFIG_YAML_TEMPLATE).format(flag="true" if flag else "false"),
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def real_settings(monkeypatch):
    """Load settings from a real YAML file, with the lru_cache reset both ways."""
    from config.settings import get_settings

    def _load(path: Path):
        monkeypatch.setenv("APP_CONFIG", str(path))
        monkeypatch.delenv("SLOTS_FROM_CAPITAL", raising=False)
        get_settings.cache_clear()
        return get_settings()

    try:
        yield _load
    finally:
        get_settings.cache_clear()


def test_capital_mode_on_is_not_a_no_op(monkeypatch) -> None:
    """Capital mode must honour the derived slots, not its own ``max_positions``.

    ``_allocate_by_capital`` rebuilds a per-system ceiling from
    ``strategy.config['max_positions']``.  Before the ``slot_limits`` wiring the
    flag was silently inert on this route: every system still got 10.
    """
    per_system = {name: _candidates(name, 10) for name in (*LONG, *SHORT)}
    strategies = {name: _SizingStrategy(MAX_PCT[name]) for name in per_system}
    monkeypatch.setattr(allocation, "_load_portfolio_caps", lambda: dict(CAPS))

    monkeypatch.setattr(
        allocation,
        "_load_capital_slot_policy",
        lambda: CapitalSlotPolicy(enabled=False),
    )
    off_df, _ = finalize_allocation(
        per_system,
        strategies=strategies,
        long_allocations=LONG,
        short_allocations=SHORT,
        capital_long=50_000.0,
        capital_short=50_000.0,
        slot_capital_equity=100_000.0,
        include_trade_management=False,
    )

    monkeypatch.setattr(allocation, "_load_capital_slot_policy", _on_policy)
    on_df, on_summary = finalize_allocation(
        per_system,
        strategies=strategies,
        long_allocations=LONG,
        short_allocations=SHORT,
        capital_long=50_000.0,
        capital_short=50_000.0,
        slot_capital_equity=100_000.0,
        include_trade_management=False,
    )

    assert on_summary.mode == "capital"
    expected = {
        "system1": 1,
        "system2": 2,
        "system3": 1,
        "system4": 1,
        "system5": 1,
        "system6": 2,
        "system7": 1,
    }
    assert on_summary.system_diagnostics["capital_slots"]["slots"] == expected
    on_counts = on_df["system"].value_counts().to_dict()
    for name, slots in expected.items():
        assert on_counts.get(name, 0) <= slots, (name, on_counts)
    # Not a no-op: OFF fills far more rows than the derived ceiling allows.
    assert len(on_df) < len(off_df)
    assert len(on_df) == sum(expected.values())


def test_real_config_on_wiring_fires_without_monkeypatching_the_policy(
    real_settings, tmp_path
) -> None:
    """``slots_from_capital: true`` in a real YAML must reach the allocator.

    Nothing here patches ``_load_capital_slot_policy`` or ``_load_portfolio_caps``:
    the only lever is the config file, exactly as production flips it.
    """
    settings_on = real_settings(_write_config(tmp_path, flag=True))
    assert settings_on.risk.slots_from_capital is True

    policy = allocation._load_capital_slot_policy()
    assert policy.enabled is True
    assert policy.gross_budget_factor == 1.0
    assert policy.min_slots == 1

    per_system = {name: _candidates(name, 10) for name in (*LONG, *SHORT)}
    strategies = {name: _Strategy(MAX_PCT[name]) for name in per_system}
    _, summary = finalize_allocation(
        per_system,
        strategies=strategies,
        long_allocations=LONG,
        short_allocations=SHORT,
        slot_capital_equity=100_000.0,
        include_trade_management=False,
    )
    diag = summary.system_diagnostics["capital_slots"]
    assert diag["enabled"] is True
    assert diag["slots"] == {
        "system1": 1,
        "system2": 2,
        "system3": 1,
        "system4": 1,
        "system5": 1,
        "system6": 2,
        "system7": 1,
    }
    assert summary.available_slots == diag["slots"]


def test_real_config_off_leaves_no_capital_slots_diagnostics(
    real_settings, tmp_path
) -> None:
    """The same wiring with the shipped default must stay inert."""
    settings_off = real_settings(_write_config(tmp_path, flag=False))
    assert settings_off.risk.slots_from_capital is False
    assert allocation._load_capital_slot_policy().enabled is False

    per_system = {name: _candidates(name, 10) for name in (*LONG, *SHORT)}
    strategies = {name: _Strategy(MAX_PCT[name]) for name in per_system}
    _, summary = finalize_allocation(
        per_system,
        strategies=strategies,
        long_allocations=LONG,
        short_allocations=SHORT,
        slot_capital_equity=100_000.0,
        include_trade_management=False,
    )
    assert "capital_slots" not in (summary.system_diagnostics or {})
    assert summary.available_slots == dict.fromkeys((*LONG, *SHORT), 10)


def test_env_override_can_enable_the_flag_over_a_false_yaml(
    real_settings, tmp_path, monkeypatch
) -> None:
    """``SLOTS_FROM_CAPITAL`` is the operational escape hatch (still default OFF)."""
    from config.settings import get_settings

    config_path = _write_config(tmp_path, flag=False)
    real_settings(config_path)
    monkeypatch.setenv("SLOTS_FROM_CAPITAL", "1")
    get_settings.cache_clear()
    assert get_settings().risk.slots_from_capital is True
    monkeypatch.setenv("SLOTS_FROM_CAPITAL", "0")
    get_settings.cache_clear()
    assert get_settings().risk.slots_from_capital is False


def test_on_with_holdings_subtracts_them_from_the_derived_slots(monkeypatch) -> None:
    """Slots are a standing ceiling: ``available = max(0, derived - held)``.

    Reproduces the 2026-08-26 shape, where system1 already held more names than
    its derived ceiling and therefore had no room for a new entry at all.
    """
    per_system = {name: _candidates(name, 10) for name in (*LONG, *SHORT)}
    strategies = {name: _Strategy(MAX_PCT[name]) for name in per_system}
    monkeypatch.setattr(allocation, "_load_capital_slot_policy", _on_policy)
    monkeypatch.setattr(allocation, "_load_portfolio_caps", lambda: dict(CAPS))

    held = {"system1": 7, "system2": 2, "system4": 9, "system5": 7}
    positions = [
        _Position(f"HELD_{name}_{i}")
        for name, count in held.items()
        for i in range(count)
    ]
    symbol_system_map = {
        f"HELD_{name}_{i}": name for name, count in held.items() for i in range(count)
    }

    final_df, summary = finalize_allocation(
        per_system,
        strategies=strategies,
        positions=positions,
        symbol_system_map=symbol_system_map,
        long_allocations=LONG,
        short_allocations=SHORT,
        slot_capital_equity=100_000.0,
        include_trade_management=False,
    )

    derived = summary.system_diagnostics["capital_slots"]["slots"]
    assert summary.active_positions == held
    for name, slots in derived.items():
        assert summary.available_slots[name] == max(0, slots - held.get(name, 0)), name
    # 1/2/1/1/1/2/1 derived, minus the holdings above -> only S3, S6, S7 are open.
    assert summary.available_slots == {
        "system1": 0,
        "system2": 0,
        "system3": 1,
        "system4": 0,
        "system5": 0,
        "system6": 2,
        "system7": 1,
    }
    assert len(final_df) == 4
    assert final_df["system"].value_counts().to_dict() == {
        "system6": 2,
        "system3": 1,
        "system7": 1,
    }


# ---------------------------------------------------------------------------
# Fail-safe hardening: every failure mode must degrade to legacy, never to 0.
# ---------------------------------------------------------------------------


def test_zero_budget_factor_is_rejected_by_the_schema() -> None:
    """F=0 zeroes every slot; it must not be a legal configuration."""
    from pydantic import ValidationError

    from config.schemas import RiskModel

    RiskModel(slots_from_capital_gross_budget_factor=1.0)
    RiskModel(slots_from_capital_gross_budget_factor=0.5)
    with pytest.raises(ValidationError):
        RiskModel(slots_from_capital_gross_budget_factor=0.0)
    with pytest.raises(ValidationError):
        RiskModel(slots_from_capital_gross_budget_factor=1.5)


def test_policy_loader_clamps_a_non_positive_factor(monkeypatch, caplog) -> None:
    """A factor that escaped the schema still must not reach the derivation."""

    class _Risk:
        slots_from_capital = True
        slots_from_capital_gross_budget_factor = 0.0
        slots_from_capital_min_slots = 1

    class _Settings:
        risk = _Risk()

    monkeypatch.setattr(
        "config.settings.get_settings", lambda *a, **k: _Settings(), raising=True
    )
    with caplog.at_level(logging.WARNING, logger=allocation.logger.name):
        policy = allocation._load_capital_slot_policy()
    assert policy.gross_budget_factor == 1.0
    assert "gross_budget_factor" in caplog.text


def test_policy_loader_warns_instead_of_silently_disabling(monkeypatch, caplog) -> None:
    def _boom(*_args, **_kwargs):
        raise RuntimeError("settings exploded")

    monkeypatch.setattr("config.settings.get_settings", _boom, raising=True)
    with caplog.at_level(logging.WARNING, logger=allocation.logger.name):
        policy = allocation._load_capital_slot_policy()
    assert policy.enabled is False
    assert "slot policy" in caplog.text


def test_non_finite_inputs_do_not_zero_every_slot() -> None:
    result = derive_capital_weighted_slots(
        long_allocations=LONG,
        short_allocations=SHORT,
        max_pct_by_system=MAX_PCT,
        equity=100_000.0,
        long_ratio=float("nan"),
        gross_exposure_pct=1.0,
        gross_budget_factor=float("nan"),
        min_slots=1,
        max_long_positions=40,
        max_short_positions=30,
        max_total_positions=70,
        max_net_exposure_pct=0.5,
    )
    assert all(math.isfinite(v) for v in result.raw_slots.values())
    assert sum(result.slots.values()) > 0


def test_derived_total_of_zero_falls_back_to_legacy(monkeypatch, caplog) -> None:
    """A derived total of 0 is indistinguishable from a full stop -> legacy."""
    per_system = {name: _candidates(name, 4) for name in (*LONG, *SHORT)}
    strategies = {name: _Strategy(MAX_PCT[name]) for name in per_system}
    monkeypatch.setattr(
        allocation, "_load_capital_slot_policy", lambda: _on_policy(min_slots=0)
    )
    monkeypatch.setattr(allocation, "_load_portfolio_caps", lambda: dict(CAPS))
    # Every system's weight is 0 -> every budget is 0 -> every slot would be 0.
    zero = {name: 0.0 for name in LONG}
    zero_short = {name: 0.0 for name in SHORT}
    monkeypatch.setattr(allocation, "_normalize_allocations", lambda w, d: dict(w or d))

    with caplog.at_level(logging.ERROR, logger=allocation.logger.name):
        _, summary = finalize_allocation(
            per_system,
            strategies=strategies,
            long_allocations=zero,
            short_allocations=zero_short,
            slot_capital_equity=100_000.0,
            include_trade_management=False,
        )
    assert "derived slot total is 0" in caplog.text
    assert "capital_slots" not in (summary.system_diagnostics or {})
    assert summary.available_slots == dict.fromkeys((*LONG, *SHORT), 10)


def test_derivation_failure_keeps_legacy_slots(monkeypatch, caplog) -> None:
    per_system = {name: _candidates(name, 4) for name in (*LONG, *SHORT)}
    strategies = {name: _Strategy(MAX_PCT[name]) for name in per_system}
    monkeypatch.setattr(allocation, "_load_capital_slot_policy", _on_policy)
    monkeypatch.setattr(allocation, "_load_portfolio_caps", lambda: dict(CAPS))

    def _boom(**_kwargs):
        raise RuntimeError("derivation exploded")

    monkeypatch.setattr(allocation, "derive_capital_weighted_slots", _boom)
    with caplog.at_level(logging.ERROR, logger=allocation.logger.name):
        _, summary = finalize_allocation(
            per_system,
            strategies=strategies,
            long_allocations=LONG,
            short_allocations=SHORT,
            slot_capital_equity=100_000.0,
            include_trade_management=False,
        )
    assert "slot derivation failed" in caplog.text
    assert "capital_slots" not in (summary.system_diagnostics or {})
    assert summary.available_slots == dict.fromkeys((*LONG, *SHORT), 10)


def test_system_without_a_capital_weight_keeps_its_legacy_slots(
    monkeypatch, caplog
) -> None:
    """System8 is deliberately absent from ui.*_allocations; it must not hit 0."""
    per_system = {name: _candidates(name, 4) for name in (*LONG, *SHORT, "system8")}
    strategies = {
        name: _Strategy(MAX_PCT.get(name, 0.10)) for name in (*LONG, *SHORT, "system8")
    }
    monkeypatch.setattr(allocation, "_load_capital_slot_policy", _on_policy)
    monkeypatch.setattr(allocation, "_load_portfolio_caps", lambda: dict(CAPS))

    with caplog.at_level(logging.WARNING, logger=allocation.logger.name):
        _, summary = finalize_allocation(
            per_system,
            strategies=strategies,
            long_allocations=LONG,
            short_allocations=SHORT,
            slot_capital_equity=100_000.0,
            include_trade_management=False,
        )
    assert "system8" in caplog.text
    assert "no capital weight" in caplog.text
    assert summary.available_slots["system8"] == 10
    assert summary.available_slots["system1"] == 1


def test_equity_fallback_to_default_capital_is_logged(monkeypatch, caplog) -> None:
    per_system = {name: _candidates(name, 4) for name in (*LONG, *SHORT)}
    strategies = {name: _Strategy(MAX_PCT[name]) for name in per_system}
    monkeypatch.setattr(allocation, "_load_capital_slot_policy", _on_policy)
    monkeypatch.setattr(allocation, "_load_portfolio_caps", lambda: dict(CAPS))

    with caplog.at_level(logging.WARNING, logger=allocation.logger.name):
        _, summary = finalize_allocation(
            per_system,
            strategies=strategies,
            long_allocations=LONG,
            short_allocations=SHORT,
            slot_capital_equity=None,
            include_trade_management=False,
        )
    diag = summary.system_diagnostics["capital_slots"]
    assert diag["equity_source"] == "finalize_default_capital"
    assert "account equity unavailable" in caplog.text


def test_slots_are_equity_independent() -> None:
    """E cancels out of B_i / N_i; only the audit $ figures move."""
    kwargs = {
        "long_allocations": LONG,
        "short_allocations": SHORT,
        "max_pct_by_system": MAX_PCT,
        "long_ratio": 0.5,
        "gross_exposure_pct": 1.0,
        "gross_budget_factor": 1.0,
        "min_slots": 1,
        "max_long_positions": 40,
        "max_short_positions": 30,
        "max_total_positions": 70,
        "max_net_exposure_pct": 0.5,
    }
    small = derive_capital_weighted_slots(equity=10_000.0, **kwargs)
    large = derive_capital_weighted_slots(equity=10_000_000.0, **kwargs)
    assert small.slots == large.slots
    assert small.raw_slots == large.raw_slots
    assert small.system_budgets != large.system_budgets
