"""Replay a published signal/order artifact through capital-derived slot limits.

This is deliberately read-only: it reads a YAML configuration and two JSON
artifacts, then prints a report.  It neither imports the order publisher nor
loads environment files.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import yaml

# Allow ``python tools/replay_capital_slots.py`` from any working directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.final_allocation import derive_capital_weighted_slots

SYSTEMS = tuple(f"system{number}" for number in range(1, 8))
LONG_SYSTEMS = {"system1", "system3", "system4", "system5"}


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _artifact_signals(payload: dict[str, Any], system: str) -> list[dict[str, Any]]:
    raw = payload.get("systems", {}).get(f"sys{system.removeprefix('system')}", {})
    signals = list(raw.get("signals", [])) if isinstance(raw, dict) else []
    return sorted(
        (item for item in signals if isinstance(item, dict)),
        key=lambda item: (
            _as_float(item.get("rank"), float("inf")),
            str(item.get("symbol", "")),
        ),
    )


def build_replay(
    *,
    config: dict[str, Any],
    signals_payload: dict[str, Any],
    orders_payload: dict[str, Any],
) -> dict[str, Any]:
    """Calculate the artifact's post-export selection under the new slot limits."""
    risk = config.get("risk", {})
    portfolio = risk.get("portfolio", {})
    ui = config.get("ui", {})
    strategies = config.get("strategies", {})
    long_allocations = dict(ui.get("long_allocations", {}))
    short_allocations = dict(ui.get("short_allocations", {}))
    default_max_pct = _as_float(risk.get("max_pct"), 0.10)
    max_pct = {
        system: _as_float(strategies.get(system, {}).get("max_pct"), default_max_pct)
        for system in SYSTEMS
    }
    equity = _as_float(orders_payload.get("account_equity_usd"), 0.0)
    if equity <= 0:
        raise ValueError("paper_orders artifact lacks a positive account_equity_usd")

    derivation = derive_capital_weighted_slots(
        long_allocations=long_allocations,
        short_allocations=short_allocations,
        max_pct_by_system=max_pct,
        equity=equity,
        long_ratio=_as_float(ui.get("default_long_ratio"), 0.5),
        gross_exposure_pct=_as_float(portfolio.get("max_gross_exposure_pct"), 1.0),
        gross_budget_factor=_as_float(
            risk.get("slots_from_capital_gross_budget_factor"), 1.0
        ),
        min_slots=int(risk.get("slots_from_capital_min_slots", 1)),
        max_long_positions=int(portfolio.get("max_long_positions", 40)),
        max_short_positions=int(portfolio.get("max_short_positions", 30)),
        max_total_positions=int(portfolio.get("max_total_positions", 70)),
        max_net_exposure_pct=_as_float(portfolio.get("max_net_exposure_pct"), 0.5),
    )

    orders = [
        item for item in orders_payload.get("orders", []) if isinstance(item, dict)
    ]
    rows: list[dict[str, Any]] = []
    for system in SYSTEMS:
        before = _artifact_signals(signals_payload, system)
        after = before[: derivation.slots.get(system, 0)]
        selected_symbols = {str(item.get("symbol", "")) for item in after}
        before_orders = [item for item in orders if item.get("system") == system]
        after_orders = [
            item
            for item in before_orders
            if str(item.get("symbol", "")) in selected_symbols
        ]
        rows.append(
            {
                "system": system,
                "side": "long" if system in LONG_SYSTEMS else "short",
                "capital_weight": (
                    long_allocations.get(system, 0.0)
                    if system in LONG_SYSTEMS
                    else short_allocations.get(system, 0.0)
                ),
                "budget_usd": derivation.system_budgets.get(system, 0.0),
                "per_position_notional_usd": derivation.per_position_notional.get(
                    system, 0.0
                ),
                "raw_slots": derivation.raw_slots.get(system, 0.0),
                "slots": derivation.slots.get(system, 0),
                "signals_before": len(before),
                "signals_after": len(after),
                "signal_delta": len(after) - len(before),
                "selected_symbols": [item.get("symbol") for item in after],
                "paper_order_rows_before": len(before_orders),
                "paper_order_rows_after": len(after_orders),
                "paper_notional_usd_before": round(
                    sum(
                        _as_float(item.get("notional_usd"), 0.0)
                        for item in before_orders
                    ),
                    2,
                ),
                "paper_notional_usd_after": round(
                    sum(
                        _as_float(item.get("notional_usd"), 0.0)
                        for item in after_orders
                    ),
                    2,
                ),
                "submitted_before": sum(
                    bool(item.get("order_id")) for item in before_orders
                ),
                "submitted_after": sum(
                    bool(item.get("order_id")) for item in after_orders
                ),
                "skipped_before": sum(
                    not bool(item.get("order_id")) for item in before_orders
                ),
                "skipped_after": sum(
                    not bool(item.get("order_id")) for item in after_orders
                ),
            }
        )
    return {
        "date": signals_payload.get("date"),
        "source_signals_run_id": orders_payload.get("source_signals_run_id"),
        "equity_usd": equity,
        "equity_source": orders_payload.get("equity_source"),
        "slots": derivation.slots,
        "slot_totals": {
            "long": sum(derivation.slots.get(system, 0) for system in LONG_SYSTEMS),
            "short": sum(
                derivation.slots.get(system, 0)
                for system in set(SYSTEMS) - LONG_SYSTEMS
            ),
            "total": sum(derivation.slots.values()),
        },
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/config.yaml"))
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--paper-orders", type=Path, required=True)
    args = parser.parse_args()
    with args.config.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    with args.signals.open(encoding="utf-8") as handle:
        signals = json.load(handle)
    with args.paper_orders.open(encoding="utf-8") as handle:
        orders = json.load(handle)
    print(
        json.dumps(
            build_replay(config=config, signals_payload=signals, orders_payload=orders),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
