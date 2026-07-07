"""Phase 5 (2026-07-07) portfolio count / exposure caps の検証。

_apply_portfolio_caps がデフォルトで no-op、締め付け config で trim すること、
既保有が allowance を減らすことを確認する。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.final_allocation import _apply_portfolio_caps  # noqa: E402

_NOOP_CAPS = {
    "max_total_positions": 70,
    "max_long_positions": 40,
    "max_short_positions": 30,
    "max_gross_exposure_pct": 1.0,
    "max_net_exposure_pct": 1.0,
}


def _df(n_long: int, n_short: int, pv: float = 1000.0) -> pd.DataFrame:
    rows = []
    for i in range(n_long):
        rows.append({"symbol": f"L{i}", "system": "system1", "side": "long", "position_value": pv})
    for i in range(n_short):
        rows.append({"symbol": f"S{i}", "system": "system2", "side": "short", "position_value": pv})
    return pd.DataFrame(rows)


def test_default_caps_are_noop():
    df = _df(5, 5)
    out, report = _apply_portfolio_caps(
        df, caps=_NOOP_CAPS, active_positions=None, symbol_system_map=None,
        long_systems=["system1"], short_systems=["system2"], equity=100000.0,
    )
    assert len(out) == 10
    assert report["applied"] is True
    assert report["trimmed"] == {}


def test_total_count_cap_trims_tail():
    caps = {**_NOOP_CAPS, "max_total_positions": 6}
    df = _df(5, 5)
    out, report = _apply_portfolio_caps(
        df, caps=caps, active_positions=None, symbol_system_map=None,
        long_systems=["system1"], short_systems=["system2"], equity=100000.0,
    )
    assert len(out) == 6
    assert report["kept"]["total"] == 6
    assert report["trimmed"].get("total", 0) == 4


def test_long_count_cap():
    caps = {**_NOOP_CAPS, "max_long_positions": 2}
    df = _df(5, 3)
    out, report = _apply_portfolio_caps(
        df, caps=caps, active_positions=None, symbol_system_map=None,
        long_systems=["system1"], short_systems=["system2"], equity=100000.0,
    )
    kept_long = (out["side"] == "long").sum()
    assert kept_long == 2
    assert report["trimmed"].get("long_count", 0) == 3


def test_gross_exposure_cap_trims():
    # equity 10k, gross cap 100% = $10k。$1000×N。long5+short5=10k ちょうど。
    # gross cap を 60% にすると $6k = 6 件で頭打ち。
    caps = {**_NOOP_CAPS, "max_gross_exposure_pct": 0.6}
    df = _df(5, 5, pv=1000.0)
    out, report = _apply_portfolio_caps(
        df, caps=caps, active_positions=None, symbol_system_map=None,
        long_systems=["system1"], short_systems=["system2"], equity=10000.0,
    )
    assert len(out) == 6  # $6000 分だけ通る
    assert report["trimmed"].get("gross_exposure", 0) == 4


def test_held_positions_reduce_long_allowance():
    """既に system1 (long) を 8 保有 → max_long 10 でも新規 long は 2 まで。"""
    class _Pos:
        def __init__(self, symbol):
            self.symbol = symbol
            self.qty = 10
            self.side = "long"

    caps = {**_NOOP_CAPS, "max_long_positions": 10}
    positions = [_Pos(f"H{i}") for i in range(8)]
    sym_map = {f"H{i}": "system1" for i in range(8)}
    df = _df(5, 0)
    out, report = _apply_portfolio_caps(
        df, caps=caps, active_positions=positions, symbol_system_map=sym_map,
        long_systems=["system1"], short_systems=["system2"], equity=100000.0,
    )
    assert report["held"]["long"] == 8
    assert report["allow"]["long"] == 2
    assert (out["side"] == "long").sum() == 2


def test_empty_df_returns_noop():
    out, report = _apply_portfolio_caps(
        pd.DataFrame(), caps=_NOOP_CAPS, active_positions=None, symbol_system_map=None,
        long_systems=["system1"], short_systems=["system2"], equity=100000.0,
    )
    assert out.empty
    assert report["applied"] is False
