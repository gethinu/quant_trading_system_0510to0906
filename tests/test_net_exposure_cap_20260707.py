"""net exposure cap を 0.5 に締めた際の挙動 (2026-07-07 user 有効化)。

- config デフォルトが 0.5 になっていること。
- 片側集中 (net > 0.5×equity) で trim されること。
- long/short balanced (net <= 0.5×equity) は no-op であること。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.final_allocation import _apply_portfolio_caps  # noqa: E402

_CAPS_NET_HALF = {
    "max_total_positions": 70,
    "max_long_positions": 40,
    "max_short_positions": 30,
    "max_gross_exposure_pct": 1.0,
    "max_net_exposure_pct": 0.5,
}


def _df(n_long: int, n_short: int, pv: float = 1000.0) -> pd.DataFrame:
    rows = []
    for i in range(n_long):
        rows.append({"symbol": f"L{i}", "system": "system1", "side": "long", "position_value": pv})
    for i in range(n_short):
        rows.append({"symbol": f"S{i}", "system": "system2", "side": "short", "position_value": pv})
    return pd.DataFrame(rows)


def test_config_default_net_cap_is_half():
    from config.settings import get_settings

    assert get_settings().risk.portfolio.max_net_exposure_pct == 0.5


def test_one_sided_long_trimmed_at_net_cap():
    # equity 10k, net cap 0.5 = $5000。$1000×8 の long のみ → net が $5000 超で trim。
    df = _df(8, 0, pv=1000.0)
    out, report = _apply_portfolio_caps(
        df, caps=_CAPS_NET_HALF, active_positions=None, symbol_system_map=None,
        long_systems=["system1"], short_systems=["system2"], equity=10000.0,
    )
    assert len(out) == 5  # $5000 分 (5 件) まで
    assert report["trimmed"].get("net_exposure", 0) == 3


def test_balanced_book_is_noop_under_net_cap():
    # long4 + short4 = 各 $4000。net = |4000-4000| = 0 <= $5000 → 全通過。
    df = _df(4, 4, pv=1000.0)
    out, report = _apply_portfolio_caps(
        df, caps=_CAPS_NET_HALF, active_positions=None, symbol_system_map=None,
        long_systems=["system1"], short_systems=["system2"], equity=10000.0,
    )
    assert len(out) == 8
    assert report["trimmed"] == {}
